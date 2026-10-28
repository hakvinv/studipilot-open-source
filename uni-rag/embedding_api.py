"""OpenAI embeddings, incremental content cache; no secrets in logs or cloud files."""
import hashlib
import json
import math
import random
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from email.utils import parsedate_to_datetime
import numpy as np
import certifi

MODEL='text-embedding-3-small'
DIMENSIONS=512
ENDPOINT='https://api.openai.com/v1/embeddings'
TLS_CONTEXT=ssl.create_default_context(cafile=certifi.where())

class EmbeddingError(RuntimeError):
    def __init__(self,message,*,http_status=None,api_code=None,retry_after=None):
        super().__init__(message)
        self.http_status=http_status;self.api_code=api_code;self.retry_after=retry_after


def retry_after(headers):
    value=headers.get('Retry-After') if headers else None
    if value is None:return None
    try:delay=float(value)
    except (TypeError,ValueError):
        try:delay=parsedate_to_datetime(value).timestamp()-time.time()
        except (TypeError,ValueError,OverflowError):return None
    return max(0,delay) if math.isfinite(delay) else None


def key_for(text):
    return hashlib.sha256((MODEL+'\0'+str(DIMENSIONS)+'\0'+text).encode()).hexdigest()


def config(state):
    path=Path(state)/'embedding-private.json'
    return json.loads(path.read_text()) if path.exists() else {}


def request(texts, api_key):
    if not texts:return []
    payload=json.dumps({'model':MODEL,'dimensions':DIMENSIONS,'input':texts,'encoding_format':'float'}).encode()
    waited=0
    for attempt in range(6):
        req=urllib.request.Request(ENDPOINT,data=payload,headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=90,context=TLS_CONTEXT) as response:body=json.load(response)
            break
        except urllib.error.HTTPError as exc:
            # Whitelist machine codes; never expose an upstream body or private input.
            try:code=json.loads(exc.read(8192)).get('error',{}).get('code')
            except (ValueError,AttributeError):code=None
            allowed={'rate_limit_exceeded','slow_down','insufficient_quota','billing_hard_limit_reached','server_is_overloaded'}
            code=code if code in allowed else None
            hint=retry_after(exc.headers)
            delay=(hint if hint is not None else min(60,10*2**attempt))+random.uniform(0,1)
            retry=exc.code in (429,500,502,503,504) and code not in {'insufficient_quota','billing_hard_limit_reached'}
            if retry and attempt<5 and waited+delay<=180:
                time.sleep(delay);waited+=delay;continue
            raise EmbeddingError(f'Embedding-API: HTTP {exc.code}. API-Schluessel, Projektbudget und API-Zugriff pruefen.',
                                 http_status=exc.code,api_code=code,retry_after=hint) from None
        except OSError:
            delay=min(30,2**attempt)
            if attempt<5 and waited+delay<=180:time.sleep(delay);waited+=delay;continue
            raise EmbeddingError('Embedding-API ist nicht erreichbar.') from None
    rows=sorted(body.get('data',[]),key=lambda x:x['index'])
    if len(rows)!=len(texts) or [x['index'] for x in rows]!=list(range(len(texts))):
        raise EmbeddingError('Embedding-API liefert unvollstaendige oder falsch zugeordnete Vektoren.')
    result=[]
    for row in rows:
        vector=np.asarray(row['embedding'],dtype=np.float32)
        if vector.shape!=(DIMENSIONS,) or not np.isfinite(vector).all():raise EmbeddingError('Embedding-Format stimmt nicht.')
        norm=float(np.linalg.norm(vector))
        if norm<1e-10:raise EmbeddingError('Embedding-API liefert Nullvektor.')
        result.append(vector/norm)
    return result


def schema(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS embedding_cache(key TEXT PRIMARY KEY, model TEXT, dimensions INTEGER, vector BLOB)')
    connection.execute('CREATE TABLE IF NOT EXISTS passage_vectors(passage_rowid INTEGER PRIMARY KEY, content_key TEXT)')
    connection.execute('CREATE INDEX IF NOT EXISTS passage_vectors_key ON passage_vectors(content_key)')


def cached(connection,texts,api_key):
    schema(connection)
    keys=[key_for(t) for t in texts];known={}
    for key in set(keys):
        row=connection.execute('SELECT vector FROM embedding_cache WHERE key=?',(key,)).fetchone()
        if row:known[key]=np.frombuffer(row[0],dtype=np.float32)
    missing={key:text for key,text in zip(keys,texts) if key not in known}
    pairs=list(missing.items())
    for start in range(0,len(pairs),64):
        batch=pairs[start:start+64]
        vectors=request([text for _,text in batch],api_key)
        for (key,_),vector in zip(batch,vectors):
            connection.execute('INSERT OR REPLACE INTO embedding_cache VALUES(?,?,?,?)',(key,MODEL,DIMENSIONS,vector.tobytes()))
            known[key]=vector
        connection.commit()
    return [known[k] for k in keys]


def populate(connection,api_key,progress=None):
    schema(connection)
    rows=connection.execute('SELECT rowid,text FROM passages').fetchall()
    unique={}
    for row in rows:
        key=key_for(row['text']);unique[key]=row['text']
        connection.execute('INSERT OR REPLACE INTO passage_vectors VALUES(?,?)',(row['rowid'],key))
    connection.execute('DELETE FROM passage_vectors WHERE passage_rowid NOT IN (SELECT rowid FROM passages)')
    connection.commit()
    missing=[text for key,text in unique.items() if connection.execute('SELECT 1 FROM embedding_cache WHERE key=?',(key,)).fetchone() is None]
    status={'model':MODEL,'dimensions':DIMENSIONS,'unique_chunks':len(unique),'pending':len(missing), 'status':'ready' if not missing else 'building'}
    if not api_key:status['status']='missing_key';return status
    for start in range(0,len(missing),64):
        batch=missing[start:start+64];cached(connection,batch,api_key)
        status['pending']=len(missing)-start-len(batch)
        if progress:progress(dict(status))
    status['status']='ready';return status


def semantic(connection,question,course,api_key,count=60):
    schema(connection)
    rows=connection.execute('''SELECT passages.rowid,embedding_cache.vector FROM passages
      JOIN passage_vectors ON passage_vectors.passage_rowid=passages.rowid
      JOIN embedding_cache ON embedding_cache.key=passage_vectors.content_key
      JOIN docs ON docs.id=passages.doc_id WHERE (?='' OR docs.course=?)''',(course,course)).fetchall()
    if not rows:return []
    query=cached(connection,[question],api_key)[0]
    matrix=np.stack([np.frombuffer(r['vector'],dtype=np.float32) for r in rows])
    scores=matrix@query
    indices=np.argsort(scores)[::-1][:count]
    return [(rows[int(i)]['rowid'],float(scores[int(i)])) for i in indices if scores[int(i)]>=0.20]
