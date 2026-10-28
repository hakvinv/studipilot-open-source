#!/usr/bin/env python3
"""Gather source-backed private briefing input. No model, mail or calendar writes."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi
import httpx

HERE=Path(__file__).resolve().parent
STATE=Path.home()/'Library/Application Support/UniPilot'
TZ=ZoneInfo('Europe/Berlin')
SIGNALS=re.compile(r'abgab|frist|deadline|submit|submission|delivery of|prospect paper|vortrag|korreferat|review.bogen|sprechstunde|raum|room|agenda|kursplan|seminarplan|organisator|bis zum|anmeld|faellt aus|fällt aus|verlegt|schedule|cancelled',re.I)


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def load(path,default):
    return json.loads(path.read_text()) if path.exists() else default


def remote(kind,method,args=None):
    folder={'mail':'UniMail','rag':'UniRAG'}[kind]
    access=load(Path.home()/'Library/Application Support'/folder/'remote-access.json',{})
    expected=os.environ.get({'mail':'UNI_MAIL_MCP_ORIGIN','rag':'UNI_RAG_MCP_ORIGIN'}[kind], '').rstrip('/')
    if not expected.startswith('https://'):raise ValueError('Configure an HTTPS MCP origin')
    if access.get('url','').rstrip('/')!=expected:raise ValueError('Unexpected private MCP origin')
    with httpx.Client(timeout=45,verify=certifi.where(),follow_redirects=False) as client:
        res=client.post(expected+'/mcp',headers={'Authorization':'Bearer '+access['service_token'],
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
            'method':'tools/call','params':{'name':method,'arguments':args or {}}})
        res.raise_for_status();body=res.json()
    if 'error' in body or body.get('result',{}).get('isError'):raise ValueError('MCP tool failed')
    result=body['result']
    if 'structuredContent' in result:return result['structuredContent']
    return json.loads(next(x['text'] for x in result['content'] if x['type']=='text'))


def calendar(now):
    start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    result=subprocess.run(['osascript','-l','JavaScript',str(HERE/'calendar.js'),start.isoformat(),
        (start+timedelta(days=15)).isoformat()],capture_output=True,text=True,timeout=20)
    if result.returncode:raise ValueError('EventKit calendar read failed')
    data=json.loads(result.stdout)
    if data.get('ok'):data['overlaps']=overlaps(data['events'])
    return data


def overlaps(events):
    # Half-open intervals; all-day deadlines are not occupancy conflicts.
    timed=sorted((e for e in events if not e.get('all_day') and e.get('status')!=3),key=lambda e:datetime.fromisoformat(e['start']))
    conflicts=[]
    for i,a in enumerate(timed):
        for b in timed[i+1:]:
            if datetime.fromisoformat(b['start'])>=datetime.fromisoformat(a['end']):break
            conflicts.append({'first':a['id'],'second':b['id'],'date':datetime.fromisoformat(a['start']).astimezone(TZ).date().isoformat()})
    return conflicts


def join_segments(segments):
    text=''
    for segment in segments:
        overlap=0
        for size in range(min(len(text),len(segment),1600),0,-1):
            if text.endswith(segment[:size]):overlap=size;break
        text+=segment[overlap:]
    return text


def read_index(path,course_ids,previous,reviewed=None):
    if not path.is_file():raise FileNotFoundError('Local course index missing')
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        docs=[dict(x) for x in db.execute('SELECT id,source,course,sha FROM docs ORDER BY course,source')]
        chosen=[d for d in docs if any('['+cid+']' in d['course'] for cid in course_ids)]
        old={d['id']:d['sha'] for d in previous.get('documents',[])}
        changed=[d for d in chosen if old.get(d['id'])!=d['sha']]
        review_queue=[d for d in chosen if (reviewed or {}).get(d['id'])!=d['sha']]
        oldpages=previous.get('candidate_page_hashes',{})
        pages=[];pagehashes={}
        for d in chosen:
            groups={}
            for row in db.execute('SELECT page,text FROM passages WHERE doc_id=? ORDER BY rowid',(d['id'],)):
                groups.setdefault(row['page'] or 0,[]).append(row['text'])
            for page,segments in groups.items():
                text=join_segments(segments)
                if not SIGNALS.search(text):continue
                key=d['id']+':'+str(page);digest=hashlib.sha256(text.encode()).hexdigest();pagehashes[key]=digest
                pages.append({'document_id':d['id'],'source':d['source'],'course':d['course'],'sha256':d['sha'],
                    'page':page,'text':text[:24000],'complete':len(text)<=24000,
                    'new_or_changed':oldpages.get(key)!=digest})
        removed=[d for d in previous.get('documents',[]) if d['id'] not in {x['id'] for x in chosen}]
    return {'documents':chosen,'changed_documents':changed,'removed_documents':removed,
        'unreviewed_documents':review_queue,
        'missing_course_ids':[cid for cid in course_ids if not any('['+cid+']' in d['course'] for d in chosen)],
        'candidate_pages':pages,'candidate_page_hashes':pagehashes,
        'note':'Heuristische Organisationskandidaten, keine vollstaendige Extraktion und keine bestaetigten Fristen. Neue Dokumente auch ausserhalb dieser Treffer pruefen.'}


def normalize(text):return ' '.join(text.split())


def freshness(data,now):
    def age(stamp):
        if not stamp:return None
        try:return round((now-datetime.fromisoformat(stamp)).total_seconds()/60,1)
        except (ValueError,TypeError):return None
    mail=data.get('mail',{}).get('status',{}).get('last_poll',{})
    rag=data.get('rag_status',{})
    local=data.get('local_index_status',{})
    minutes=age(mail.get('at'))
    return {'mail_poll_age_minutes':minutes,
        'mail_poll_current':minutes is not None and -1<=minutes<=15 and mail.get('status')=='ok',
        'rag_sync_age_minutes':age(rag.get('sync',{}).get('finished')),
        'source_index_age_minutes':age(rag.get('source_index_at')),
        'local_matches_remote_source_index':bool(local.get('updated_at')) and local.get('updated_at')==rag.get('source_index_at'),
        'index_coverage_complete':rag.get('coverage_complete',False),
        'pending_documents':rag.get('pending_documents'),
        'ocr_or_review_required':rag.get('review_required')}


def validate_obligations(items,index,now):
    docs={d['id']:d for d in index['documents']}
    pages={(p['document_id'],p['page']):p for p in index['candidate_pages']}
    output=[]
    for item in items:
        item=dict(item);reasons=[]
        if not item.get('evidence'):reasons.append('Beleg fehlt')
        for ev in item.get('evidence',[]):
            doc=docs.get(ev['document_id']);page=pages.get((ev['document_id'],ev['page']))
            if not doc or doc['sha']!=ev['sha256']:reasons.append('Quelle fehlt oder geaendert')
            if not page or not page['complete'] or not normalize(ev['quote']) or normalize(ev['quote']) not in normalize(page['text']):
                reasons.append('Woertlicher Seitenbeleg nicht bestaetigt')
        item['evidence_valid']=not reasons;item['review_reasons']=sorted(set(reasons))
        done=item.get('completion',{}).get('evidence')
        item['completion_verified']=bool(done)
        due=item.get('due')
        if due:
            day=datetime.fromisoformat(due).date();left=(day-now.date()).days
            item['days_left']=left
            item['overdue_unconfirmed']=left<0 and not done
            if item.get('lead_days') is not None:
                item['suggested_start']=(day-timedelta(days=item['lead_days'])).isoformat()
                item['start_is_planning_not_official']=True
        output.append(item)
    return output


def gather(state=STATE,now=None):
    now=now or datetime.now(TZ)
    config=load(state/'config.json',{})
    if not config.get('active_course_ids'):raise ValueError('Private active semester course configuration missing')
    previous=load(state/'latest.json',{})
    tasks={'mail':lambda:remote('mail','mail_briefing',{'limit':50}),
        'rag_status':lambda:remote('rag','uni_status'),
        'calendar':lambda:calendar(now),
        'course_index':lambda:read_index(Path.home()/'Library/Application Support/UniRAG/index.sqlite',config['active_course_ids'],previous.get('course_index',{}),load(state/'reviewed-documents.json',{}))}
    data={'checked_at':now.isoformat(),'timezone':'Europe/Berlin','configuration':config,'errors':{}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(fn):name for name,fn in tasks.items()}
        for future in concurrent.futures.as_completed(jobs):
            name=jobs[future]
            try:data[name]=future.result()
            except Exception as exc:data['errors'][name]=type(exc).__name__ # Never log private upstream errors.
    cat=Path.home()/'Library/Application Support/UniRAG/corpus/catalogue.json'
    data['local_index_status']=load(cat,{}).get('status',{})
    data['source_checks']=freshness(data,now)
    if data.get('course_index'):
        data['obligations']=validate_obligations(load(state/'obligations.json',[]),data['course_index'],now)
    data['limits']=['Kalender ist vorhandener iCloud-Bestand; kein Live-Abgleich gegen Stud.IP-Terminseite.',
        'Mailbox-Cache anfangs sieben Tage; aeltere Mails separat suchen. Anhanginhalte fehlen.',
        'Fristenkandidaten sind heuristisch. Gesamtindex/OCR-Restbestand beachten.',
        'Das Briefing laeuft lokal in Codex; kein ungefragter ChatGPT-Push und kein Tonauftrag.',
        'Excel bleibt verbindliche Studienplanung und wird von diesem Leselauf nicht geaendert.']
    state.mkdir(parents=True,exist_ok=True,mode=0o700);state.chmod(0o700)
    atomic_json(state/'latest.json',data)
    atomic_json(state/'runs'/(now.strftime('%Y-%m-%dT%H-%M-%S')+'.json'),data)
    return data


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',type=Path,default=STATE)
    args=parser.parse_args();data=gather(args.state)
    print(json.dumps({'report':str(args.state/'latest.json'),'checked_at':data['checked_at'],
        'calendar_events':len(data.get('calendar',{}).get('events',[])),
        'calendar_ok':data.get('calendar',{}).get('ok',False),
        'mail_ok':'mail' in data,'rag_ok':'rag_status' in data,'errors':data['errors'],
        'candidate_pages':len(data.get('course_index',{}).get('candidate_pages',[])),
        'obligations':len(data.get('obligations',[]))},ensure_ascii=False))
