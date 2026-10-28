"""Portable offline checks for bounded, read-only course retrieval."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1]/'skills/uni-kontext/scripts/uni_context.py'

class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        with sqlite3.connect(self.state/'index.sqlite') as db:
            db.execute('CREATE TABLE docs(id TEXT PRIMARY KEY, source TEXT, course TEXT, sha TEXT)')
            db.execute('CREATE VIRTUAL TABLE passages USING fts5(doc_id UNINDEXED, page UNINDEXED, text UNINDEXED, terms)')
            db.execute('INSERT INTO docs VALUES(?,?,?,?)', ('a','01_StudIP/Test.pdf','Ökonometrie Fortgeschrittene','fixture'))
            text = 'Instrumentvariablen beheben Endogenitaet unter geeigneten Annahmen. ' * 12
            db.execute('INSERT INTO passages VALUES(?,?,?,?)', ('a',2,text,text))
        (self.state/'corpus').mkdir()
        (self.state/'corpus/catalogue.json').write_text(json.dumps({'status':{'pending':3,'review_required':1,'complete':False}}))
    def command(self, *args):
        env = dict(os.environ, UNI_RAG_STATE=str(self.state))
        return subprocess.run([sys.executable,str(SCRIPT),'--offline',*args], env=env, capture_output=True,text=True,timeout=10)
    def test_offline_filter_source_page_limits_and_unchanged_database(self):
        before = hashlib.sha256((self.state/'index.sqlite').read_bytes()).hexdigest()
        result = self.command('Instrumentvariablen','--course','Okonometrie Fortgeschrittene','--chars','200')
        self.assertEqual(result.returncode,0,result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['retrieval'],'fulltext')
        self.assertEqual(data['pending_documents'],3)
        self.assertFalse(data['coverage_complete'])
        self.assertEqual(data['sources'][0]['page'],2)
        self.assertEqual(len(data['sources'][0]['quote']),200)
        self.assertTrue(data['sources'][0]['truncated'])
        self.assertEqual(before,hashlib.sha256((self.state/'index.sqlite').read_bytes()).hexdigest())
    def test_connection_rejects_writes(self):
        spec = importlib.util.spec_from_file_location('uni_context_test',SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.STATE = self.state
        with module.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM docs')
    def test_missing_course_and_no_result_are_explicit(self):
        result = self.command('Instrumentvariablen','--course','Unbekannter Kurs')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Kein passender Kurs',result.stderr)
        result = self.command('zzzzzzabcdef')
        self.assertEqual(result.returncode,0)
        data = json.loads(result.stdout)
        self.assertEqual(data['sources'],[])
        self.assertTrue(data['warnings'])

if __name__ == '__main__':
    unittest.main()
