import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name);self.override=patch.object(app,'STATE',self.state);self.override.start();self.addCleanup(self.override.stop)
        self.cache=self.state/'corpus';(self.cache/'documents').mkdir(parents=True)
    def fixture(self,text='Der Standardfehler misst die Streuung des Schaetzers.',course='Einführung in die Ökonometrie',page=8):
        source='01_StudIP/Test/Vorlesung.pdf';key=hashlib.sha256(source.encode()).hexdigest()
        record={'id':key,'source':source,'course':course,'sha256':hashlib.sha256(text.encode()).hexdigest(),'chunks':[{'text':text,'page':page}]}
        (self.cache/'documents'/(key+'.json')).write_text(json.dumps(record))
        (self.cache/'catalogue.json').write_text(json.dumps({'files':{source:{'id':key,'status':'indexed'}},'status':{'pending':0}}))
        return key
    def test_unicode_search_filter_citations_and_update(self):
        self.fixture();app.ingest(self.cache)
        hit=app.retrieve('Was bedeutet der Standardfehler in der Ökonometrie?')
        self.assertEqual(hit[0]['page'],8);self.assertEqual(hit[0]['citation'],'Q1')
        self.assertEqual(app.retrieve('Standardfehler',course='Makro'),[])
        self.fixture(text='Das Solow-Modell beschreibt Kapitalakkumulation.');app.ingest(self.cache)
        self.assertEqual(app.retrieve('Standardfehler'),[])
        self.assertTrue(app.retrieve('Solow'))
    def test_removal_deactivates_stale_source(self):
        self.fixture();app.ingest(self.cache)
        (self.cache/'catalogue.json').write_text('{"files":{},"status":{}}');app.ingest(self.cache)
        self.assertEqual(app.retrieve('Standardfehler'),[])
    def test_empty_query_and_missing_evidence(self):
        self.assertEqual(app.retrieve('und oder'),[])
        self.assertIn('keinen Beleg',app.answer('Test',[]))
    def test_actual_pdf_page_extraction(self):
        spec=importlib.util.spec_from_file_location('rag_index',Path(__file__).parents[1]/'railway-studip-sync/rag_index.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        pdf=Path(__file__).parents[1]/'tests/fixtures/course-sample.pdf'
        pages=module.extract(pdf)
        self.assertGreater(len(pages),5);self.assertEqual(pages[0][0],1)
        self.assertTrue(any('konometr' in t.lower() for _,t in pages))

if __name__=='__main__':unittest.main()

class HttpTests(unittest.TestCase):
    def test_cross_site_requests_are_rejected(self):
        import threading,urllib.request,urllib.error
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        port=server.server_port
        with patch.object(app,'PORT',port):
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{port}/api/query',data=b'{"question":"test"}',headers={'Content-Type':'application/json','Origin':'https://external.example'})
                with self.assertRaises(urllib.error.HTTPError) as result:urllib.request.urlopen(req,timeout=3)
                self.assertEqual(result.exception.code,403)
            finally:server.shutdown();server.server_close();thread.join()
