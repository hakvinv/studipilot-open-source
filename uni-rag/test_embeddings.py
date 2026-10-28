import io,json,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
import numpy as np
import app,embedding_api as api


def vector(i):
 v=np.zeros(api.DIMENSIONS,dtype=np.float32);v[i]=1;return v

class EmbeddingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.state=Path(self.tmp.name);self.override=patch.object(app,'STATE',self.state);self.override.start();self.addCleanup(self.override.stop)
 def test_incremental_cache_and_semantic_match_without_shared_keyword(self):
  with app.db() as c:
   c.execute('INSERT INTO docs VALUES(?,?,?,?)',('a','01_StudIP/Inflation.pdf','Makro','hash'))
   c.execute('INSERT INTO passages VALUES(?,?,?,?)',('a',3,'Inflation erhoeht die Lebenshaltungskosten.','inflation erhoeht die lebenshaltungskosten'))
   c.commit()
   def embed(texts,key):return [vector(0) for _ in texts]
   with patch.object(api,'request',side_effect=embed) as mock:
    result=api.populate(c,'test-key');self.assertEqual(result['pending'],0)
    self.assertEqual(mock.call_count,1)
    api.populate(c,'test-key');self.assertEqual(mock.call_count,1)
    hit=api.semantic(c,'Warum wird der Einkauf teurer?','Makro','test-key')
    self.assertEqual(len(hit),1);self.assertAlmostEqual(hit[0][1],1)
    api.semantic(c,'Warum wird der Einkauf teurer?','Makro','test-key');self.assertEqual(mock.call_count,2)
    self.assertEqual(api.semantic(c,'Warum wird der Einkauf teurer?','Statistik','test-key'),[])
   (self.state/'embedding-private.json').write_text('{"api_key":"test-key"}')
   with patch.object(api,'request',side_effect=embed):
    result=app.retrieve('Warum wird der Einkauf teurer?',course='Makro')
    self.assertEqual(result[0]['page'],3)
    self.assertIn('Inflation',result[0]['text'])
 def test_missing_key_never_calls_api(self):
  with app.db() as c,patch.object(api,'request') as mock:
   self.assertEqual(api.populate(c,None)['status'],'missing_key');mock.assert_not_called()
 def test_api_response_order_dimension_and_normalization(self):
  payload={'data':[{'index':1,'embedding':(vector(1)*2).tolist()},{'index':0,'embedding':(vector(0)*2).tolist()}]}
  with patch.object(api.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(payload).encode())) as mock:
   vectors=api.request(['first','second'],'secret-test')
   self.assertEqual(vectors[0][0],1);self.assertEqual(vectors[1][1],1)
   req=mock.call_args.args[0];self.assertEqual(req.full_url,'https://api.openai.com/v1/embeddings')
   body=json.loads(req.data);self.assertEqual(body['dimensions'],512);self.assertEqual(body['model'],'text-embedding-3-small')
 def test_bad_response_is_rejected(self):
  with patch.object(api.urllib.request,'urlopen',return_value=io.BytesIO(b'{"data":[]}')):
   with self.assertRaises(api.EmbeddingError):api.request(['text'],'test')
 def test_rate_limit_honors_retry_after_and_recovers(self):
  error=urllib.error.HTTPError(api.ENDPOINT,429,'limited',{'Retry-After':'12'},io.BytesIO(b'{"error":{"code":"rate_limit_exceeded","message":"private-body"}}'))
  result=io.BytesIO(json.dumps({'data':[{'index':0,'embedding':vector(0).tolist()}]}).encode())
  with patch.object(api.urllib.request,'urlopen',side_effect=[error,result]),patch.object(api.time,'sleep') as sleep,patch.object(api.random,'uniform',return_value=0.5):
   self.assertEqual(api.request(['text'],'test')[0][0],1)
   sleep.assert_called_once_with(12.5)
 def test_quota_error_is_not_retried_or_exposed(self):
  error=urllib.error.HTTPError(api.ENDPOINT,429,'limited',{},io.BytesIO(b'{"error":{"code":"insufficient_quota","message":"private-body"}}'))
  with patch.object(api.urllib.request,'urlopen',side_effect=error) as request,patch.object(api.time,'sleep') as sleep:
   with self.assertRaises(api.EmbeddingError) as raised:api.request(['text'],'test')
   self.assertEqual(raised.exception.api_code,'insufficient_quota');self.assertEqual(request.call_count,1);sleep.assert_not_called()
   self.assertNotIn('private-body',str(raised.exception))
 def test_long_server_delay_defers_without_early_retry(self):
  error=urllib.error.HTTPError(api.ENDPOINT,429,'limited',{'Retry-After':'400'},io.BytesIO(b'{}'))
  with patch.object(api.urllib.request,'urlopen',side_effect=error),patch.object(api.time,'sleep') as sleep:
   with self.assertRaises(api.EmbeddingError) as raised:api.request(['text'],'test')
   self.assertEqual(raised.exception.retry_after,400);sleep.assert_not_called()
  self.assertIsNone(api.retry_after({'Retry-After':'nan'}))

if __name__=='__main__':unittest.main()
