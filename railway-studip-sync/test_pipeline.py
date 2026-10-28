"""Exercise the real sync hook, publisher and local retrieval together."""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sync
from test_sync import FakeSource,FakeCloud,REF

class IndexCloud(FakeCloud):
    root='test:'
    def call(self,*args):
        return json.dumps([{'Path':p,'Size':len(b),'ModTime':'2026-10-19T12:00:00Z'} for p,b in self.remote.items()])

class PipelineTests(unittest.TestCase):
    def test_new_download_reaches_search_and_update_replaces_old_text(self):
        FakeCloud.remote={};FakeCloud.fail_material=False
        sys.path.insert(0,str(Path(__file__).parents[1]/'uni-rag'))
        spec=importlib.util.spec_from_file_location('local_rag_app',Path(__file__).parents[1]/'uni-rag/app.py')
        app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
        with tempfile.TemporaryDirectory() as d:
            env={'SYNC_ENABLED':'true','RAG_ENABLED':'true','DATA_DIR':d,'STUDIP_USER':'test','STUDIP_PASSWORD':'secret'}
            content=[b'Standardfehler messen die Streuung des Schaetzers.']
            def download(self,ref,target):
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content[0]);return hashlib.sha256(content[0]).hexdigest()
            def run():
                with patch.dict(os.environ,env),patch.object(sync,'Cloud',IndexCloud),patch.object(sync,'Source',FakeSource),patch.object(FakeSource,'download',download):
                    self.assertEqual(sync.run(argparse.Namespace(probe=False,course_id=None)),0)
                cache=Path(d)/'local/corpus';cache.mkdir(parents=True,exist_ok=True)
                for p,b in FakeCloud.remote.items():
                    if p.startswith('.automation/rag/'):
                        target=cache/p.removeprefix('.automation/rag/');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b)
                app.STATE=Path(d)/'local';app.ingest(cache)
            run();self.assertTrue(app.retrieve('Standardfehler'))
            content[0]=b'Das Solow-Modell beschreibt die Kapitalakkumulation.'
            with patch.dict(REF['attributes'],{'chdate':'2026-10-20T00:00:00Z'}):run()
            self.assertFalse(app.retrieve('Standardfehler'));self.assertTrue(app.retrieve('Solow'))

if __name__=='__main__':unittest.main()
