#!/usr/bin/env python3
"""Retrieve bounded university source context; never generate an answer or start services."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(os.environ.get('UNI_RAG_STATE', str(Path.home()/'Library/Application Support/UniRAG')))
BASE = 'http://127.0.0.1:8767'
STOP = set('der die das den dem des ein eine einer eines einem einen und oder ist sind ich du mir mich was wie warum welche welcher welches wird werden bitte erklar erklare erklaer erklaere zu zum zur im in am an auf fur fuer mit von aus als es er sie the a an of to is are explain what how berechnen berechne erklaren erklare bitte'.split())


def fold(value):
    return unicodedata.normalize('NFKD', value.casefold().replace('ß','ss')).encode('ascii','ignore').decode()


def connect():
    path=STATE/'index.sqlite'
    if not path.is_file():raise RuntimeError('Uni-Index fehlt auf diesem Mac. Keine Ersatzablage anlegen; bestehenden OneDrive-/Railway-Index pruefen.')
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
    return c


def http(path,payload=None):
    request=urllib.request.Request(BASE+path)
    if payload is not None:
        request=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Origin':BASE})
    with urllib.request.urlopen(request,timeout=40 if payload is not None else 3) as response:return json.load(response)


def local_status():
    with connect() as c:
        data={'documents':c.execute('SELECT count(*) FROM docs').fetchone()[0],
              'chunks':c.execute('SELECT count(*) FROM passages').fetchone()[0],
              'courses':[r[0] for r in c.execute('SELECT DISTINCT course FROM docs ORDER BY course')]}
    p=STATE/'corpus/catalogue.json'
    if p.exists():data['corpus']=json.loads(p.read_text()).get('status',{})
    data['retrieval']='fulltext';return data


def summary(data,mode,warnings):
    corpus=data.get('corpus',{})
    return {'retrieval':mode,'documents':data.get('documents'),'chunks':data.get('chunks'),
            'source_index_at':corpus.get('updated_at'),'pending_documents':corpus.get('pending'),
            'review_required':corpus.get('review_required'),'coverage_complete':corpus.get('complete',False),
            'warnings':warnings}


def matching(courses,query):
    if not query:return []
    words=fold(query).split()
    result=[c for c in courses if all(w in fold(c) for w in words)]
    if not result:raise RuntimeError('Kein passender Kurs im aktuellen Index. Mit --courses die vorhandenen Kurse anzeigen; Restbestand ist nicht vollstaendig indexiert.')
    if len(result)>4:raise RuntimeError('Kursfilter passt auf mehr als vier Kurse. Filter genauer angeben.')
    return result


def lexical(question,courses):
    words=list(dict.fromkeys(w for w in re.findall(r'[a-z0-9]+',fold(question)) if w not in STOP and len(w)>1))[:20]
    synonyms={'standardfehler':['standard error','standard errors'],'se':['standardfehler','standard error'],
              'abgabe':['deadline','submission'],'abgaben':['abgabe','deadline','submission'],
              'frist':['deadline','submission'],'vortrag':['presentation'],'okonometrie':['econometrics']}
    words=list(dict.fromkeys(words+[x for w in words for x in synonyms.get(w,[])]))
    if not words:return []
    match=' OR '.join('"'+w+'"'+('*' if len(w)>=4 else '') for w in words)
    condition=' AND docs.course IN ('+','.join('?' for _ in courses)+')' if courses else ''
    with connect() as c:
        return [dict(r) for r in c.execute('SELECT passages.doc_id,page,text,source,course,bm25(passages) AS score FROM passages JOIN docs ON docs.id=passages.doc_id WHERE passages MATCH ?'+condition+' ORDER BY score LIMIT 60',[match,*courses])]


def bounded(rows,limit,chars,per_document=2):
    out=[];seen=set();perdoc={}
    for row in rows:
        signature=hashlib.sha256(row['text'].encode()).hexdigest()
        if signature in seen or perdoc.get(row['doc_id'],0)>=per_document:continue
        seen.add(signature);perdoc[row['doc_id']]=perdoc.get(row['doc_id'],0)+1
        source=row['source'];page=row.get('page')
        label={'.pdf':'PDF-Seite','.pptx':'Folie','.docx':'Textabschnitt'}.get(Path(source).suffix.lower(),'Text')
        out.append({'citation':'U'+str(len(out)+1),'document_id':row['doc_id'],'course':row['course'],
                    'source':source,'location':label+(' '+str(page) if page is not None else ''), 'page':page,
                    'source_status':row.get('source_status','indexed'),
                    'quote':row['text'][:chars],'truncated':len(row['text'])>chars,
                    'original_url':BASE+'/api/source?id='+urllib.parse.quote(row['doc_id'])+('#page='+str(page) if page else ''),
                    'download_url':BASE+'/api/source?id='+urllib.parse.quote(row['doc_id'])+'&download=1'})
        if len(out)>=limit:break
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query',nargs='?');parser.add_argument('--course',default='')
    parser.add_argument('--status',action='store_true');parser.add_argument('--courses',action='store_true')
    parser.add_argument('--offline',action='store_true',help='Read local SQLite snapshot without HTTP, embeddings or model calls')
    parser.add_argument('--limit',type=int,default=6);parser.add_argument('--chars',type=int,default=1500)
    parser.add_argument('--document',help='Read a returned document ID from the local index');parser.add_argument('--page',type=int)
    args=parser.parse_args()
    if not 1<=args.limit<=10 or not 200<=args.chars<=3000:parser.error('--limit: 1..10; --chars: 200..3000')
    warnings=[];live=False
    if not args.offline:
        try:data=http('/api/status');live=True
        except (OSError,ValueError):data=local_status();warnings.append('Lokaler Dienst nicht erreichbar; lesbarer lokaler Index verwendet.')
    else:data=local_status()
    mode=data.get('retrieval','fulltext') if live else 'fulltext'
    result=summary(data,mode,warnings)
    if args.status or args.courses:
        result['courses']=data.get('courses',[]);print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.document:
        with connect() as c:
            sql='SELECT doc_id,page,text,source,course FROM passages JOIN docs ON docs.id=passages.doc_id WHERE doc_id=?'
            params=[args.document]
            if args.page is not None:sql+=' AND page=?';params.append(args.page)
            rows=[dict(r) for r in c.execute(sql+' ORDER BY passages.rowid LIMIT 20',params)]
            page_total=c.execute('SELECT count(*) FROM ('+sql+')',params).fetchone()[0]
        result['retrieval']='document-page'
    else:
        if not args.query or len(args.query)>1000:parser.error('Suchfrage mit 1..1000 Zeichen angeben, oder --status/--courses verwenden.')
        courses=matching(data.get('courses',[]),args.course);result['matched_courses']=courses
        if live and mode=='hybrid':
            try:
                groups=[http('/api/query',{'question':args.query,'course':course,'generate':False})['sources'] for course in (courses or [''])]
                rows=[group[i] for i in range(max(map(len,groups),default=0)) for group in groups if i<len(group)]
            except (OSError,ValueError,KeyError):
                rows=lexical(args.query,courses);result['retrieval']='fulltext';warnings.append('Dienst/API-Abfrage fehlgeschlagen; lokale Volltextsuche verwendet.')
        else:rows=lexical(args.query,courses)
    result['sources']=bounded(rows,args.limit,args.chars,args.limit if args.document else 2)
    if args.document and args.page is not None:
        result['page_context_complete']=len(result['sources'])==page_total and not any(r['truncated'] for r in result['sources'])
    if not result['sources']:warnings.append('Keine passenden Belege im aktuellen Index; das belegt nicht, dass im gesamten Kurs nichts vorliegt.')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(1)
