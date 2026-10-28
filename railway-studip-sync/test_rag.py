import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess
import rag_index

class Cloud:
    def __init__(self): self.root='test:';self.files={};self.uploads=0;self.fail=False
    def call(self,*args):
        return json.dumps([{'Path':p,'Size':len(b),'ModTime':'2026-10-19T00:00:00Z'} for p,b in self.files.items() if not p.startswith('.automation')])
    def read(self,path): return self.files[path]
    def put_tree(self,stage):
        if self.fail: raise RuntimeError('upload failed')
        for p in Path(stage).rglob('*'):
            if p.is_file(): self.files[p.relative_to(stage).as_posix()]=p.read_bytes();self.uploads+=1

class RagTests(unittest.TestCase):
    def test_incremental_change_and_removal(self):
        cloud=Cloud();name='01_StudIP/Automatisch/Test [12345678]/Lektion.md';cloud.files[name]=('Standardfehler messen die Streuung eines Schaetzers. '*8).encode()
        with tempfile.TemporaryDirectory() as d:
            first=rag_index.publish(cloud,d)
            self.assertEqual(first['indexed_documents'],1);self.assertEqual(first['pending'],0)
            self.assertEqual(rag_index.publish(cloud,d)['indexed_this_run'],0)
            cloud.files[name]+=b' Aktualisierung'
            self.assertEqual(rag_index.publish(cloud,d)['indexed_this_run'],1)
            del cloud.files[name]
            self.assertEqual(rag_index.publish(cloud,d)['indexed_documents'],0)
    def test_budget_backfill(self):
        cloud=Cloud();cloud.files={f'01_StudIP/Kurs/{n}.txt':('Unterlagen und Quellen '*10).encode() for n in range(3)}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rag_index.publish(cloud,d,max_files=1)['pending'],2)
            self.assertEqual(rag_index.publish(cloud,d,max_files=2)['pending'],0)
    def test_failed_upload_does_not_commit(self):
        cloud=Cloud();cloud.files['01_StudIP/A.txt']=b'Text '*50;cloud.fail=True
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError): rag_index.publish(cloud,d)
            self.assertFalse(Path(d,'rag/manifest.json').exists())
            cloud.fail=False;self.assertEqual(rag_index.publish(cloud,d)['indexed_this_run'],1)
    def test_pages_and_empty_scan(self):
        parts=rag_index.chunks([(1,'Aussage '*700),(2,'Seite zwei '*50),(3,'')])
        self.assertEqual(set(p['page'] for p in parts),{1,2})
        self.assertTrue(all(len(p['text'])<=1600 for p in parts))
        self.assertEqual(rag_index.chunks([(1,'')]),[])
    def test_scanned_pdf_uses_ocr_and_preserves_page_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'scan.pdf';source.write_bytes(b'%PDF-test')
            calls=[CompletedProcess([],0,b'\f',b''),CompletedProcess([],0,b'',b''),
                   CompletedProcess([],0,b'OCR erkannte eine belegte Abgabefrist.\f',b'')]
            with patch.object(rag_index.subprocess,'run',side_effect=calls) as run:
                pages,used=rag_index.extract_pdf(source)
            self.assertTrue(used)
            self.assertIn('Abgabefrist',pages[0][1])
            self.assertEqual(run.call_count,3)
            self.assertFalse((Path(directory)/'scan-ocr.pdf').exists())
    def test_scope(self):
        self.assertFalse(rag_index.eligible('90_Archiv/secret.pdf'))
        self.assertFalse(rag_index.eligible('Stundenplan_WiSe_2026-27.xlsx'))
        self.assertTrue(rag_index.eligible('03_Modulhandbuch/Handbuch.pdf'))

if __name__=='__main__': unittest.main()
