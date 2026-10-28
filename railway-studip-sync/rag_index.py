"""Incremental, page-aware corpus publisher; invoked ONLY by the existing sync job."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

VERSION = 2
EXTENSIONS = {".pdf", ".md", ".txt", ".docx", ".pptx"}
PREFIXES = ("01_StudIP/", "02_Pruefungen/", "03_Modulhandbuch/", "04_Kurs-Markdown/")


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.replace(path)


def extract_pdf(path):
    def text_pages(source):
        result = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"],
                                capture_output=True, timeout=180)
        if result.returncode:
            raise ValueError("PDF-Textextraktion fehlgeschlagen")
        pages = result.stdout.decode("utf-8", errors="replace").split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        return [(i + 1, text.strip()) for i, text in enumerate(pages)]

    pages = text_pages(path)
    readable = sum(len(text) >= 25 for _, text in pages)
    if pages and readable == len(pages):
        return pages, False

    ocr_path = path.with_name(path.stem + "-ocr.pdf")
    try:
        try:
            result = subprocess.run(
                ["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew", "--language", "deu+eng",
                 "--optimize", "0", str(path), str(ocr_path)],
                capture_output=True, timeout=900)
        except FileNotFoundError:
            raise ValueError("OCR-Werkzeug fehlt auf Railway") from None
        if result.returncode:
            raise ValueError(f"OCR fehlgeschlagen (Exit {result.returncode})")
        pages = text_pages(ocr_path)
        if not any(text.strip() for _, text in pages):
            raise ValueError("OCR lieferte keinen lesbaren Text")
        return pages, True
    finally:
        ocr_path.unlink(missing_ok=True)


def extract(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)[0]
    if suffix in {".docx", ".pptx"}:
        with zipfile.ZipFile(path) as z:
            names = ["word/document.xml"] if suffix == ".docx" else sorted(
                [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide[0-9]+.xml", n)],
                key=lambda n: int(re.search(r"slide([0-9]+)", n).group(1)))
            return [(i + 1, " ".join(t.text or "" for t in ET.fromstring(z.read(n)).iter() if t.tag.endswith("}t")))
                    for i, n in enumerate(names)]
    return [(None, path.read_text(encoding="utf-8-sig", errors="replace"))]


def chunks(pages, limit=1600, overlap=240):
    result = []
    for page, text in pages:
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) < 30: continue
        start = 0
        while start < len(text):
            end = min(start + limit, len(text))
            if end < len(text):
                cut = max(text.rfind("\n", start + limit // 2, end), text.rfind(". ", start + limit // 2, end))
                if cut > start: end = cut + 1
            result.append({"page": page, "text": text[start:end].strip()})
            if end == len(text): break
            start = max(start + 1, end - overlap)
    return result


def eligible(relative):
    p = PurePosixPath(relative)
    return relative.startswith(PREFIXES) and p.suffix.lower() in EXTENSIONS and not p.name.startswith("~$")


def course_name(relative):
    parts = PurePosixPath(relative).parts
    if len(parts) > 2 and parts[1] == "Automatisch": return parts[2]
    return parts[1] if len(parts) > 2 else parts[0]


def publish(cloud, state_dir, max_files=None):
    state_dir = Path(state_dir) / "rag"
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = state_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema": VERSION, "files": {}}
    listing = json.loads(cloud.call("lsjson", cloud.root, "--recursive", "--files-only", "--no-mimetype",
                                    "--exclude", "/90_Archiv/**", "--exclude", "/.automation/**"))
    files = {x["Path"]: x for x in listing if eligible(x["Path"])}
    # Prefer original PDFs over generated Markdown copies of the very same PDF.
    for relative in list(files):
        if relative.startswith("04_Kurs-Markdown/Automatisch/"):
            original = "01_StudIP/Automatisch/" + relative.removeprefix("04_Kurs-Markdown/Automatisch/")
            if str(PurePosixPath(original).with_suffix(".pdf")) in files: del files[relative]
    changed = []
    for relative, item in files.items():
        version = {"size": item["Size"], "modified": item.get("ModTime"), "extractor": VERSION}
        previous = manifest["files"].get(relative, {})
        if previous.get("version") != version or previous.get("status") == "error":
            changed.append((relative, item, version))
    # Newest materials first; every unchanged run advances the historical backfill.
    changed.sort(key=lambda x: (x[1].get("ModTime", ""), x[0]), reverse=True)
    budget = max_files if max_files is not None else int(os.environ.get("RAG_BACKFILL_FILES", "120"))
    stage = state_dir / "publish"
    import shutil
    shutil.rmtree(stage, ignore_errors=True)
    indexed_now, review_now, errors = 0, 0, []
    print(json.dumps({"event": "rag_index_started", "at": stamp(), "pending": len(changed), "budget": budget}), flush=True)
    def index_one(entry):
        relative, item, version = entry
        key = hashlib.sha256(relative.encode()).hexdigest()
        content_hash = None
        try:
            if item["Size"] > 100 * 1024 * 1024: raise ValueError("Datei ueber 100 MB; separat pruefen")
            data = cloud.read(relative)
            if len(data) != item["Size"]: raise ValueError("Cloud-Dateigroesse waehrend Abruf geaendert; naechster Lauf wiederholt")
            content_hash = hashlib.sha256(data).hexdigest()
            with tempfile.TemporaryDirectory(dir=state_dir) as temp:
                path = Path(temp) / ("source" + PurePosixPath(relative).suffix.lower())
                path.write_bytes(data)
                pages, ocr_used = extract_pdf(path) if path.suffix.lower() == ".pdf" else (extract(path), False)
                parts = chunks(pages)
            record = {"schema": VERSION, "id": key, "source": relative, "course": course_name(relative),
                      "sha256": content_hash, "indexed_at": stamp(),
                      "ocr_used": ocr_used, "chunks": parts}
            atomic(stage / ".automation/rag/documents" / (key + ".json"), record)
            return relative, {"id": key, "version": version, "chunks": len(parts),
                "status": "indexed" if parts else "ocr_or_review_required", "ocr_used": ocr_used,
                "indexed_at": record["indexed_at"]}, None
        except Exception as exc:
            return relative, {"id": key, "version": version, "sha256": content_hash,
                              "status": "error", "error": str(exc), "retry_on_next_run": True}, str(exc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for processed, (relative, row, error) in enumerate(pool.map(index_one, changed[:budget]), 1):
            manifest["files"][relative] = row
            if error: errors.append({"source": relative, "error": error})
            else:
                indexed_now += row["status"] == "indexed"
                review_now += row["status"] == "ocr_or_review_required"
            if processed % 10 == 0:
                print(json.dumps({"event": "rag_index_progress", "at": stamp(), "processed": processed}), flush=True)
    # Removed/renamed sources are deactivated in the catalogue, never deleted in OneDrive.
    active = {p: dict(row) for p, row in manifest["files"].items() if p in files}
    for p, row in active.items():
        current = {"size": files[p]["Size"], "modified": files[p].get("ModTime"), "extractor": VERSION}
        if row.get("version") != current:
            row["status"] = "pending"
    pending = sum(1 for p, item in files.items() if p not in active or active[p].get("version") !=
                  {"size": item["Size"], "modified": item.get("ModTime"), "extractor": VERSION} or active[p].get("status") == "error")
    status = {"schema": VERSION, "updated_at": stamp(), "eligible_files": len(files),
              "indexed_documents": sum(x.get("status") == "indexed" for x in active.values()),
              "chunks": sum(x.get("chunks", 0) for x in active.values()),
              "review_required": sum(x.get("status") == "ocr_or_review_required" for x in active.values()),
              "pending": pending, "indexed_this_run": indexed_now, "review_this_run": review_now,
              "errors": errors, "complete": pending == 0,
              "scope": "PDF, Markdown, TXT, DOCX, PPTX in 01_StudIP, 02_Pruefungen, 03_Modulhandbuch, 04_Kurs-Markdown; keine Archive, kein Excel; scanned PDFs use OCR on Railway"}
    catalogue = {"schema": VERSION, "status": status, "files": active}
    atomic(stage / ".automation/rag/catalogue.json", catalogue)
    cloud.put_tree(stage)
    remote = json.loads(cloud.read(".automation/rag/catalogue.json"))
    if remote["status"]["updated_at"] != status["updated_at"]: raise ValueError("RAG-Katalog-Ruecklesetest fehlgeschlagen")
    atomic(manifest_path, manifest)
    atomic(state_dir / "latest.json", status)
    shutil.rmtree(stage, ignore_errors=True)
    return status
