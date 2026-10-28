"""Read-only Stud.IP JSON:API download, incremental OneDrive copy, PDF text extraction.

No deletion, enrolment or modification in Stud.IP. No Google Drive transport.
Only the Railway service runs scheduled material downloads.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(event, **fields):
    print(json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False), flush=True)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def safe_name(name):
    name = unicodedata.normalize("NFC", str(name or "Unbenannt"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if name in ("", ".", ".."): name = "Unbenannt"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    if len(name) > 160:
        suffix = PurePosixPath(name).suffix[:15]
        name = name[:130] + "_" + hashlib.sha256(name.encode()).hexdigest()[:10] + suffix
    return name


def norm(path):
    return unicodedata.normalize("NFC", path).casefold()


def related(obj, key):
    value = obj.get("relationships", {}).get(key, {}).get("links", {}).get("related")
    return value.get("href") if isinstance(value, dict) else value


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            new.remove_header("Authorization")
        return new


class Source:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path, binary=False):
        url = urllib.parse.urljoin(self.base + "/", path)
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise RuntimeError("Stud.IP-Link verweist auf fremden Host")
        for attempt in range(5):
            req = urllib.request.Request(url, headers={
                "Authorization": self.auth,
                "Accept": "*/*" if binary else "application/vnd.api+json",
                "User-Agent": "UniManagement-RailwaySync/1.0",
            })
            try:
                return self.opener.open(req, timeout=60)
            except urllib.error.HTTPError as exc:
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                    retry_note = " nach 5 Versuchen" if exc.code in (408, 429, 500, 502, 503, 504) else ""
                    raise RuntimeError(f"Stud.IP HTTP {exc.code}{retry_note}: {urllib.parse.urlsplit(url).path}") from None
                delay = min(60, int(exc.headers.get("Retry-After", "0")) if exc.headers.get("Retry-After", "0").isdigit() else 0)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 4: raise RuntimeError("Stud.IP Netzwerkfehler nach 5 Versuchen") from None
                delay = 0
            time.sleep(max(delay, 2 ** attempt))

    def get(self, path):
        with self.request(path) as response: return json.load(response)

    def pages(self, path):
        seen, result = set(), []
        while path:
            if path in seen: raise RuntimeError("Stud.IP Pagination wiederholt dieselbe Seite")
            seen.add(path)
            body = self.get(path)
            data = body.get("data", [])
            if not isinstance(data, list): raise RuntimeError("Stud.IP Liste fehlt")
            result.extend(data)
            path = body.get("links", {}).get("next")
            if isinstance(path, dict): path = path["href"]
        return result

    def download(self, ref, target):
        attrs = ref["attributes"]
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        try:
            with self.request(f"/jsonapi.php/v1/file-refs/{ref['id']}/content", binary=True) as response, partial.open("wb") as out:
                if "text/html" in response.headers.get("Content-Type", "") and attrs.get("mime-type") != "text/html":
                    raise RuntimeError("Stud.IP liefert eine HTML-Anmeldeseite statt der Datei")
                while chunk := response.read(1024 * 1024):
                    out.write(chunk); digest.update(chunk)
            expected = attrs.get("filesize")
            if expected is not None and partial.stat().st_size != int(expected):
                raise RuntimeError("Stud.IP Dateigroesse stimmt nicht mit Metadaten ueberein")
            if attrs.get("mime-type") == "application/pdf":
                with partial.open("rb") as inp:
                    if not inp.read(1024).lstrip().startswith(b"%PDF"):
                        raise RuntimeError("Stud.IP Inhalt ist kein PDF")
            partial.replace(target)
            return digest.hexdigest()
        finally:
            partial.unlink(missing_ok=True)


class Cloud:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = self.state_dir / "rclone.conf"
        seed = os.environ.get("ONEDRIVE_CONFIG_B64")
        marker = self.state_dir / "auth-seed.sha256"
        if seed:
            seed_hash = hashlib.sha256(seed.encode()).hexdigest()
            if not self.config.exists() or not marker.exists() or marker.read_text() != seed_hash:
                self.config.write_bytes(base64.b64decode(seed, validate=True))
                self.config.chmod(0o600)
                marker.write_text(seed_hash)
        if not self.config.exists(): raise RuntimeError("OneDrive-Freigabe fehlt")
        self.root = "uni:" + os.environ.get("ONEDRIVE_ROOT", "Desktop/Uni-Management").strip("/")
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def call(self, *args, binary=False):
        cmd = [os.environ.get("RCLONE_BIN", "rclone"), "--config", str(self.config),
               "--retries", "3", "--low-level-retries", "5", "--contimeout", "20s", "--timeout", "3m", *args]
        result = subprocess.run(cmd, capture_output=True, timeout=2400)
        if result.returncode:
            # Keep the cause, strip URLs and drop any auth-related output lines.
            lines = [line for line in result.stderr.decode(errors="replace").splitlines()
                     if not any(word in line.lower() for word in ("access_token", "refresh_token", "authorization", "client_secret"))]
            detail = re.sub(r"https?://\S+", "[URL]", " ".join(lines[-3:]))[-1200:]
            log("onedrive_command_failed", command=args[0], exit_code=result.returncode, detail=detail)
            raise RuntimeError(f"OneDrive {args[0]} fehlgeschlagen (Exit {result.returncode}): {detail}")
        return result.stdout if binary else result.stdout.decode()

    def target(self, relative=""):
        return self.root + ("/" + relative if relative else "")

    def inventory(self):
        # Require existing Uni folder; never create a substitute root.
        info = json.loads(self.call("lsjson", self.root, "--stat"))
        if not info.get("IsDir"): raise RuntimeError("Bestehender Uni-Management-Ordner fehlt")
        return json.loads(self.call("lsjson", self.target("01_StudIP/Automatisch"), "--recursive", "--files-only", "--no-mimetype", "--no-modtime"))

    def put_tree(self, staging):
        if not any(p.is_file() for p in Path(staging).rglob("*")): return
        # Each destination subtree must be disjoint from the archive subtree.
        # rclone rejects backup-dir nested inside a whole-Uni-folder destination.
        for child in sorted(Path(staging).iterdir()):
            if child.is_dir():
                self.call("copy", str(child), self.target(child.name),
                          "--transfers", "2", "--checkers", "4", "--checksum", "--no-traverse",
                          "--backup-dir", self.target("90_Archiv/StudIP-Versionen/" + self.run_id + "/" + child.name))
            else:
                # Only generated status files live at root. Originals are in 01_StudIP.
                self.call("copyto", str(child), self.target(child.name), "--checksum")

    def read(self, relative):
        return self.call("cat", self.target(relative), binary=True)


def folder_parts(folder_id, folders):
    result, seen = [], set()
    while folder_id:
        if folder_id in seen: raise RuntimeError("Stud.IP Ordnerzyklus")
        seen.add(folder_id)
        folder = folders.get(folder_id)
        if folder is None: raise RuntimeError("Stud.IP Elternordner fehlt")
        attrs = folder.get("attributes", {})
        if attrs.get("folder-type") == "RootFolder": break
        result.append(safe_name(attrs.get("name")))
        folder_id = folder.get("relationships", {}).get("parent", {}).get("data", {}).get("id")
    return list(reversed(result))


def file_version(ref):
    a = ref["attributes"]
    return {"changed": a.get("chdate"), "size": a.get("filesize"),
            "name": a.get("name"), "file_id": ref.get("relationships", {}).get("file", {}).get("data", {}).get("id")}


def markdown(pdf, output, original_relative, course):
    result = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
                            capture_output=True, timeout=120)
    output.parent.mkdir(parents=True, exist_ok=True)
    text = result.stdout.decode("utf-8", errors="replace").strip()
    status = "text_extracted" if result.returncode == 0 and text else "ocr_or_pdf_review_required"
    # Caller supplies the relative URL from Markdown to the exact original path.
    content = (f"# {pdf.name}\n\nKurs: {course}\n\nOriginal: [PDF](<{original_relative}>)\n\n"
               f"Extraktion: {now()} · Status: `{status}`\n\n")
    if text: content += text.replace("\x0c", "\n\n---\n\n") + "\n"
    else: content += "Kein extrahierbarer Text. Das Original-PDF erfordert OCR oder eine manuelle Prüfung.\n"
    output.write_text(content)
    return status


def run(args):
    os.umask(0o077)
    state_dir = Path(os.environ.get("DATA_DIR", "/data"))
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "run.lock").open("w") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("run_skipped_already_running"); return 0
        return _run_locked(args, state_dir)


def _run_locked(args, state_dir):
    if os.environ.get("SYNC_ENABLED", "false").lower() != "true":
        log("paused", reason="SYNC_ENABLED ist nicht true"); return 0
    cloud = Cloud(state_dir)
    source = Source(os.environ.get("STUDIP_BASE_URL", "https://elearning.uni-bremen.de"),
                    os.environ["STUDIP_USER"], os.environ["STUDIP_PASSWORD"])
    report = {"schema": 1, "started": now(), "mode": "probe" if args.probe else "sync",
              "status": "running", "courses_discovered": 0, "courses_checked": 0,
              "files_seen": 0, "downloaded": 0, "files_uploaded": 0, "adopted_by_metadata": 0,
              "unchanged": 0, "pdf_markdown_created": 0, "pdf_review_required": 0,
              "errors": [], "courses": [], "roundtrip_verified": [],
              "windows_scheduler_disabled": os.environ.get("WINDOWS_SCHEDULER_DISABLED", "false").lower() == "true"}
    state_path = state_dir / "manifest.json"
    manifest = json.loads(state_path.read_text()) if state_path.exists() else {"files": {}, "courses": {}}
    stage = state_dir / "staging"
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir()
    try:
        inventory = cloud.inventory()
        log("onedrive_existing_folder_verified", existing_files=len(inventory))
        existing = {norm(x["Path"]): x for x in inventory}
        if not state_path.exists():
            try:
                restored = json.loads(cloud.read(".automation/studip/manifest.json"))
                if isinstance(restored.get("files"), dict) and isinstance(restored.get("courses"), dict):
                    manifest = restored
                    log("manifest_recovered_from_onedrive", files=len(manifest["files"]))
            except Exception:
                log("first_run_without_remote_manifest")
        course_paths = {}
        basename_index = {}
        for item in inventory:
            p = PurePosixPath(item["Path"])
            if len(p.parts) < 2: continue
            match = re.search(r"\[([0-9a-f]{8})\]$", p.parts[0])
            if match: course_paths[match[1]] = p.parts[0]
            basename_index.setdefault((norm(p.parts[0]), norm(p.name)), []).append(item)
        me = source.get("/jsonapi.php/v1/users/me")["data"]
        courses = source.pages(related(me, "courses"))
        report["courses_discovered"] = len(courses)
        log("courses_discovered", count=len(courses))
        # New course IDs first; no --recent shortcut that drops tutorials or older exams.
        courses.sort(key=lambda c: (c["id"][:8] in course_paths, c["attributes"].get("title", "")))
        probe_done = False
        for course in courses:
            cid = course["id"]
            if args.course_id and cid != args.course_id: continue
            attrs = course["attributes"]
            title = " ".join(str(x) for x in (attrs.get("course-number"), attrs.get("title")) if x)
            directory = manifest["courses"].get(cid, {}).get("directory") or course_paths.get(cid[:8]) or safe_name(title) + f" [{cid[:8]}]"
            report_course = {"id": cid, "title": title, "directory": directory, "files": 0, "errors": 0}
            updates = {}
            try:
                refs = source.pages(related(course, "file-refs"))
                report["courses_checked"] += 1
                report_course["files"] = len(refs)
                folder_list = source.pages(related(course, "folders"))
                folders = {x["id"]: x for x in folder_list}
                occupied = {norm(row["relative"].removeprefix("01_StudIP/Automatisch/")): rid
                            for rid, row in manifest["files"].items()
                            if row["relative"].startswith("01_StudIP/Automatisch/" + directory + "/")}
                for ref in refs:
                    report["files_seen"] += 1
                    rid = ref["id"]; a = ref["attributes"]; version = file_version(ref)
                    prev = manifest["files"].get(rid)
                    previous_cloud_item = existing.get(norm(prev["relative"].removeprefix("01_StudIP/Automatisch/"))) if prev else None
                    if prev and prev.get("version") == version and previous_cloud_item and previous_cloud_item["Size"] == a.get("filesize") and not args.probe:
                        report["unchanged"] += 1; continue
                    if a.get("is-readable") is False or a.get("is-downloadable") is False:
                        report["errors"].append({"course": cid, "file": rid, "name": a.get("name"), "error": "Stud.IP verweigert Lese-/Downloadzugriff"})
                        report_course["errors"] += 1; continue
                    name = safe_name(a.get("name"))
                    candidates = [x for x in basename_index.get((norm(directory), norm(name)), []) if x["Size"] == a.get("filesize")]
                    parent = ref.get("relationships", {}).get("parent", {}).get("data", {}).get("id")
                    parts = folder_parts(parent, folders) if parent else []
                    suffix = "/".join([directory, *parts, name])
                    if prev:
                        relative = prev["relative"]
                    elif len(candidates) == 1:
                        relative = "01_StudIP/Automatisch/" + candidates[0]["Path"]
                    else:
                        if norm(suffix) in occupied and occupied[norm(suffix)] != rid:
                            p = PurePosixPath(name); name = p.stem + " [" + rid[:8] + "]" + p.suffix
                            suffix = "/".join([directory, *parts, name])
                        relative = "01_StudIP/Automatisch/" + suffix
                    key = norm(relative.removeprefix("01_StudIP/Automatisch/"))
                    if key in occupied and occupied[key] != rid:
                        p = PurePosixPath(relative)
                        relative = str(p.with_name(p.stem + " [" + rid[:8] + "]" + p.suffix))
                        key = norm(relative.removeprefix("01_StudIP/Automatisch/"))
                    occupied[key] = rid
                    cloud_item = existing.get(norm(relative.removeprefix("01_StudIP/Automatisch/")))
                    # Historical material is adopted by metadata only. New-semester files
                    # are always downloaded on the first run to establish a content baseline.
                    changed = a.get("chdate") or a.get("mkdate") or ""
                    baseline_cutoff = os.environ.get("BASELINE_BEFORE", "2026-10-05T00:00:00")
                    if not prev and cloud_item and cloud_item["Size"] == a.get("filesize") and changed and changed < baseline_cutoff and not args.probe:
                        updates[rid] = {"relative": relative, "version": version, "verification": "metadata_only"}
                        report["adopted_by_metadata"] += 1; continue
                    target = stage / relative
                    try:
                        sha = source.download(ref, target)
                        row = {"relative": relative, "version": version, "sha256": sha, "verification": "transferred", "transferred_at": now()}
                        if target.suffix.lower() == ".pdf":
                            md_relative = "04_Kurs-Markdown/Automatisch/" + directory + "/" + "/".join([*parts, target.stem + ".md"])
                            md_link = urllib.parse.quote(posixpath.relpath(relative, posixpath.dirname(md_relative)), safe="/")
                            extraction = markdown(target, stage / md_relative, md_link, title)
                            row["markdown"] = md_relative; row["extraction"] = extraction
                            report["pdf_markdown_created"] += 1
                            if extraction != "text_extracted": report["pdf_review_required"] += 1
                        updates[rid] = row
                        report["downloaded"] += 1
                        log("downloaded", course=title, file=a.get("name"), bytes=target.stat().st_size)
                        if args.probe: probe_done = True; break
                    except Exception as exc:
                        target.unlink(missing_ok=True)
                        report_course["errors"] += 1
                        message = str(exc)
                        report["errors"].append({"course": cid, "course_title": title, "directory": directory,
                            "file": rid, "name": a.get("name"), "relative": relative, "error": message,
                            "retry_on_next_run": bool(re.search(r"HTTP (?:408|429|5[0-9]{2})|Netzwerkfehler|Timeout", message, re.I))})
                cloud.put_tree(stage)
                report["files_uploaded"] += sum(bool(v.get("sha256")) for v in updates.values())
                # The first newly transferred file of each run is read back byte-for-byte.
                if not report["roundtrip_verified"]:
                    fresh = next((v for v in updates.values() if v.get("sha256")), None)
                    if fresh:
                        returned = cloud.read(fresh["relative"])
                        if hashlib.sha256(returned).hexdigest() != fresh["sha256"]:
                            raise RuntimeError("OneDrive-Ruecklesepruefung: SHA256 stimmt nicht")
                        report["roundtrip_verified"].append({"relative": fresh["relative"], "sha256": fresh["sha256"], "at": now()})
                        log("onedrive_roundtrip_verified", file=fresh["relative"])
                manifest["files"].update(updates)
                manifest["courses"][cid] = {"directory": directory, "title": title, "checked_at": now(), "files": len(refs)}
                save_json(state_path, manifest)
                shutil.rmtree(stage); stage.mkdir()
                log("course_complete", course=title, files=len(refs), errors=report_course["errors"])
            except Exception as exc:
                report_course["errors"] += 1
                message = str(exc)
                report["errors"].append({"course": cid, "title": title, "directory": directory, "error": message,
                    "retry_on_next_run": bool(re.search(r"HTTP (?:408|429|5[0-9]{2})|Netzwerkfehler|Timeout", message, re.I))})
                log("course_failed", course=title, error=str(exc))
                # Never carry an unconfirmed upload or Markdown into the next course.
                shutil.rmtree(stage); stage.mkdir()
            report["courses"].append(report_course)
            if args.probe and probe_done: break
        report["status"] = "partial_failure" if report["errors"] else "success"
        if args.probe and not report["roundtrip_verified"]:
            report["status"] = "failed"
            report["errors"].append({"error": "Kein erfolgreicher Datei-Ruecklesetest"})
    except Exception as exc:
        report["status"] = "failed"; report["errors"].append({"error": str(exc)})
        log("run_failed", error=str(exc))
    finally:
        if os.environ.get("RAG_ENABLED", "false").lower() == "true":
            try:
                from rag_index import publish
                report["rag"] = publish(cloud, state_dir)
                log("rag_published_and_read_back", **report["rag"])
            except Exception as exc:
                report["errors"].append({"error": "RAG-Index: " + str(exc)})
                report["status"] = "partial_failure"
                log("rag_failed", error=str(exc))
        report["finished"] = now()
        save_json(state_dir / "latest.json", report)
        # Status is stored outside the material folders and contains no secrets.
        status_stage = state_dir / "status-upload"
        if status_stage.exists(): shutil.rmtree(status_stage)
        save_json(status_stage / ".automation/studip/latest.json", report)
        save_json(status_stage / ".automation/studip/manifest.json", manifest)
        save_json(status_stage / ".automation/studip/courses.json", report["courses"])
        index = ["# Automatisch erfasste Stud.IP-Kurse", "", "Originale und neue PDF-Texte sind nach Kurs getrennt.", ""]
        for cid, course in sorted(manifest["courses"].items(), key=lambda pair: pair[1]["title"]):
            path = "../../01_StudIP/Automatisch/" + course["directory"]
            index.append(f"- [{course['title']}](<{urllib.parse.quote(path, safe='/')}>) · {course['files']} Dateieinträge · ID `{cid[:8]}`")
        index.append("\nStand (UTC): " + now() + "\n")
        index_path = status_stage / "04_Kurs-Markdown/Automatisch/00_Kursuebersicht.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("\n".join(index))
        text = ("# Stud.IP-Downloader auf Railway\n\n"
                f"Letzter Lauf (UTC): {report['started']} bis {report['finished']}\n\n"
                f"Status: **{report['status']}** · Modus: `{report['mode']}`\n\n"
                f"Kurse erkannt: {report['courses_discovered']}; geprüft: {report['courses_checked']}.\n\n"
                f"Dateien gesehen: {report['files_seen']}; heruntergeladen: {report['downloaded']}; in OneDrive übertragen: {report['files_uploaded']}; "
                f"unverändert: {report['unchanged']}; Altbestand nur nach Metadaten übernommen: {report['adopted_by_metadata']}.\n\n"
                f"Neue PDF-Markdown-Dateien: {report['pdf_markdown_created']}; OCR/PDF-Prüfung nötig: {report['pdf_review_required']}.\n\n"
                f"Fehler: {len(report['errors'])}. Einzelheiten: `.automation/studip/latest.json`.\n\n"
                "Originale: `01_StudIP/Automatisch`. Neue PDF-Texte: `04_Kurs-Markdown/Automatisch`.\n\n"
                "Quelle ist der Dateibereich aller belegten Kurse. Keine automatische Kurseintragung. "
                "Courseware, externe Links und Videos außerhalb des Dateibereichs sind nicht eingeschlossen.\n\n"
                "Mac-Abruf deaktiviert. Abschaltung des alten Windows-Abrufs und Empfang auf Windows separat prüfen.\n")
        status_stage.mkdir(parents=True, exist_ok=True)
        (status_stage / "StudIP-Downloader-Status.md").write_text(text)
        try:
            cloud.put_tree(status_stage)
            remote_status = json.loads(cloud.read(".automation/studip/latest.json"))
            if remote_status["started"] != report["started"]: raise RuntimeError("Veralteter OneDrive-Laufstatus")
            log("status_uploaded_and_read_back", status=report["status"], downloaded=report["downloaded"], errors=len(report["errors"]))
        except Exception as exc:
            report["status"] = "failed"
            report["errors"].append({"error": "Statusuebertragung: " + str(exc)})
            save_json(state_dir / "latest.json", report)
            log("status_upload_failed", error=str(exc))
        shutil.rmtree(status_stage, ignore_errors=True)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--course-id")
    args = parser.parse_args()
    if os.environ.get("RUN_MODE") == "probe": args.probe = True
    if not args.course_id: args.course_id = os.environ.get("COURSE_ID") or None
    try: sys.exit(run(args))
    except Exception as exc:
        log("fatal", error=str(exc)); sys.exit(1)
