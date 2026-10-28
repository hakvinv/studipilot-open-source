import argparse
import hashlib
import json
import os
import copy
import posixpath
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import sync


ROOT = {"id": "root", "attributes": {"folder-type": "RootFolder"}}
COURSE = {"id": "a" * 32, "attributes": {"course-number": "07-TEST-01", "title": "Übung"},
          "relationships": {"file-refs": {"links": {"related": "/refs"}},
                            "folders": {"links": {"related": "/folders"}}}}
REF = {"id": "b" * 32, "attributes": {"name": "Aufgabe.txt", "filesize": 4,
       "chdate": "2026-10-14T10:00:00+02:00", "is-readable": True, "is-downloadable": True},
       "relationships": {"parent": {"data": {"id": "root"}}, "file": {"data": {"id": "c" * 32}}}}


class FakeSource:
    downloads = 0
    def __init__(self, *args): pass
    def get(self, path):
        return {"data": {"relationships": {"courses": {"links": {"related": "/courses"}}}}}
    def pages(self, path): return {"/courses": [COURSE], "/refs": [REF], "/folders": [ROOT]}[path]
    def download(self, ref, target):
        FakeSource.downloads += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test")
        return hashlib.sha256(b"test").hexdigest()


class FakeCloud:
    remote = {}
    fail_material = False
    def __init__(self, state_dir): pass
    def inventory(self):
        prefix = "01_StudIP/Automatisch/"
        return [{"Path": p[len(prefix):], "Size": len(b)} for p, b in self.remote.items() if p.startswith(prefix)]
    def read(self, relative):
        if relative not in self.remote: raise RuntimeError("missing")
        return self.remote[relative]
    def put_tree(self, staging):
        files = list(Path(staging).rglob("*"))
        if self.fail_material and any(p.name == "Aufgabe.txt" for p in files):
            raise RuntimeError("test upload failed")
        for p in files:
            if p.is_file(): self.remote[p.relative_to(staging).as_posix()] = p.read_bytes()


class SyncTests(unittest.TestCase):
    def setUp(self):
        FakeCloud.remote = {}; FakeCloud.fail_material = False; FakeSource.downloads = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def execute(self, probe=False):
        env = {"SYNC_ENABLED": "true", "DATA_DIR": self.tmp.name, "STUDIP_USER": "test", "STUDIP_PASSWORD": "secret"}
        with patch.dict(os.environ, env), patch.object(sync, "Cloud", FakeCloud), patch.object(sync, "Source", FakeSource):
            return sync.run(argparse.Namespace(probe=probe, course_id=None))

    def test_real_transfer_then_incremental_skip(self):
        self.assertEqual(self.execute(), 0)
        self.assertEqual(FakeSource.downloads, 1)
        report = json.loads(FakeCloud.remote[".automation/studip/latest.json"])
        self.assertEqual(report["files_uploaded"], 1)
        self.assertEqual(len(report["roundtrip_verified"]), 1)
        self.assertEqual(self.execute(), 0)
        self.assertEqual(FakeSource.downloads, 1)
        report = json.loads(FakeCloud.remote[".automation/studip/latest.json"])
        self.assertEqual(report["unchanged"], 1)

    def test_run_releases_process_lock(self):
        self.assertEqual(self.execute(), 0)
        with Path(self.tmp.name, "run.lock").open("w") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_failed_upload_never_commits_file(self):
        FakeCloud.fail_material = True
        self.assertEqual(self.execute(), 1)
        manifest = json.loads(FakeCloud.remote[".automation/studip/manifest.json"])
        self.assertEqual(manifest["files"], {})
        self.assertEqual(self.execute(), 1)
        self.assertEqual(FakeSource.downloads, 2)

    def test_missing_cloud_original_is_downloaded_again(self):
        self.assertEqual(self.execute(), 0)
        key = next(p for p in FakeCloud.remote if p.endswith("Aufgabe.txt"))
        del FakeCloud.remote[key]
        self.assertEqual(self.execute(), 0)
        self.assertEqual(FakeSource.downloads, 2)

    def test_manifest_recovers_after_local_state_loss(self):
        self.assertEqual(self.execute(), 0)
        Path(self.tmp.name, "manifest.json").unlink()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(FakeSource.downloads, 1)

    def test_wrong_cloud_size_is_repaired(self):
        self.assertEqual(self.execute(), 0)
        key = next(p for p in FakeCloud.remote if p.endswith("Aufgabe.txt"))
        FakeCloud.remote[key] = b"truncated"
        self.assertEqual(self.execute(), 0)
        self.assertEqual(FakeSource.downloads, 2)

    def test_paths_and_auth_boundaries(self):
        self.assertEqual(sync.safe_name("../Übung: 1?"), ".._Übung_ 1_")
        self.assertEqual(sync.safe_name("CON.txt"), "_CON.txt")
        self.assertEqual(sync.folder_parts("root", {"root": ROOT}), [])
        with self.assertRaises(RuntimeError): sync.folder_parts("missing", {"root": ROOT})
        source = sync.Source("https://example.org", "test", "secret")
        with self.assertRaises(RuntimeError): source.request("https://other.example/private")

    def test_pagination_collects_all_pages(self):
        source = sync.Source("https://example.org", "test", "secret")
        with patch.object(source, "get", side_effect=[{"data": [1, 2], "links": {"next": "/p2"}}, {"data": [3]}]):
            self.assertEqual(source.pages("/p1"), [1, 2, 3])
        with patch.object(source, "get", return_value={"data": [1], "links": {"next": "/p1"}}):
            with self.assertRaises(RuntimeError): source.pages("/p1")

    def test_same_named_references_do_not_overwrite_each_other(self):
        second = copy.deepcopy(REF); second["id"] = "d" * 32
        original = FakeSource.pages
        def pages(instance, path):
            return [REF, second] if path == "/refs" else original(instance, path)
        with patch.object(FakeSource, "pages", pages): self.assertEqual(self.execute(), 0)
        manifest = json.loads(FakeCloud.remote[".automation/studip/manifest.json"])
        paths = [row["relative"] for row in manifest["files"].values()]
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(path in FakeCloud.remote for path in paths))

    def test_course_index_resolves_to_uploaded_course_directory(self):
        self.assertEqual(self.execute(), 0)
        md_path = "04_Kurs-Markdown/Automatisch/00_Kursuebersicht.md"
        text = FakeCloud.remote[md_path].decode()
        import re
        link = re.search(r"\]\(<([^>]+)>\)", text).group(1)
        target = posixpath.normpath(posixpath.join(posixpath.dirname(md_path), urllib.parse.unquote(link)))
        self.assertTrue(any(key.startswith(target + "/") for key in FakeCloud.remote))

    def test_archive_never_overlaps_material_destination(self):
        cloud = sync.Cloud.__new__(sync.Cloud)
        cloud.root = "uni:Desktop/Uni-Management"; cloud.run_id = "test"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "01_StudIP/Automatisch/Test/a.pdf"); p.parent.mkdir(parents=True); p.write_bytes(b"test")
            Path(d, "StudIP-Downloader-Status.md").write_text("test")
            with patch.object(cloud, "call") as call:
                cloud.put_tree(d)
                for args, _ in call.call_args_list:
                    if args[0] == "copy":
                        destination = args[2]
                        archive = args[args.index("--backup-dir") + 1]
                        self.assertFalse(archive.startswith(destination + "/"))
                        self.assertFalse(destination.startswith(archive + "/"))
                    else:
                        self.assertEqual(args[0], "copyto")


if __name__ == "__main__": unittest.main()
