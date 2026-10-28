#!/usr/bin/env python3
"""Check the deployed MCP with private local credentials; never print source text or tokens."""
import argparse
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--access-file',type=Path,default=Path.home()/'Library/Application Support/UniRAG/remote-access.json')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--oauth-test',action='store_true')
    args=parser.parse_args()
    access=json.loads(args.access_file.read_text());base=access['url']
    if not base.startswith('https://'):
        raise RuntimeError('Remote checks require HTTPS')
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'endpoint':base+'/mcp'}
    with httpx.Client(timeout=60) as client:
        def rpc(name,arguments=None,token=None,method='tools/call'):
            response=client.post(base+'/mcp',headers={'Authorization':'Bearer '+(token or access['service_token']),
                'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':method,
                'params':{'name':name,'arguments':arguments or {}} if method=='tools/call' else {}})
            response.raise_for_status();data=response.json()
            if 'error' in data or data.get('result',{}).get('isError'):
                raise RuntimeError('MCP returned a protocol/tool error')
            return data['result']
        result['health_ok']=client.get(base+'/health').status_code==200
        result['oauth_discovery_ok']=client.get(base+'/.well-known/oauth-authorization-server').status_code==200
        anonymous=client.post(base+'/mcp',headers={'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        result['anonymous_rejected']=anonymous.status_code==401
        result['tools']=[x['name'] for x in rpc('',method='tools/list')['tools']]
        status=rpc('uni_status')['structuredContent']
        result['index']={k:v for k,v in status.items() if k!='sync'}
        if not status['documents']:
            raise RuntimeError('Remote index is not populated yet')
        found=rpc('uni_search',{'question':'Instrumentvariablen','course':'Ökonometrie Fortgeschrittene','limit':3})['structuredContent']
        if not found['sources']:
            raise RuntimeError('No original course evidence retrieved')
        hit=found['sources'][0]
        page=rpc('uni_read_page',{'document_id':hit['document_id'],'page':hit['page'] or 0})['structuredContent']
        if not page['text'] or hit['quote'][:100] not in page['text']:
            raise RuntimeError('Source page does not match the search result')
        result['real_course_query']={'course':hit['course'],'document_id':hit['document_id'],'source':hit['source'],
            'page':hit['page'],'complete_page':page['complete'],'page_readback_matched':True,
            'text_sha256':hashlib.sha256(page['text'].encode()).hexdigest()}
        search=rpc('search',{'query':'Instrumentvariablen'})['structuredContent']
        fetched=rpc('fetch',{'id':search['results'][0]['id']})['structuredContent']
        result['search_fetch_ok']=bool(fetched['text']) and fetched['id']==search['results'][0]['id']
        result['private_citation_viewer_rejects_anonymous_text']=page['text'][:100] not in client.get(base+'/source/'+hit['document_id']+'/'+str(hit['page'] or 0)).text
        if args.oauth_test:
            reg=client.post(base+'/register',json={'client_name':'Codex connection verification',
                'redirect_uris':['https://client.example/callback'],'token_endpoint_auth_method':'client_secret_post',
                'grant_types':['authorization_code','refresh_token'],'scope':'uni:read offline_access'})
            reg.raise_for_status();info=reg.json()
            verifier=secrets.token_urlsafe(64)
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            auth=client.get(base+'/authorize',params={'client_id':info['client_id'],'response_type':'code',
                'redirect_uri':'https://client.example/callback','scope':'uni:read offline_access','state':'verification',
                'code_challenge_method':'S256','code_challenge':challenge,'resource':base+'/mcp'})
            consent=client.get(auth.headers['location'])
            ticket=re.search('name="id" value="([^"]+)"',consent.text).group(1)
            csrf=re.search('name="csrf" value="([^"]+)"',consent.text).group(1)
            approved=client.post(base+'/consent',headers={'Origin':base},data={'id':ticket,'csrf':csrf,'owner_secret':access['owner_secret']})
            code=parse_qs(urlparse(approved.headers['location']).query)['code'][0]
            credentials={'client_id':info['client_id'],'client_secret':info['client_secret']}
            token=client.post(base+'/token',data={**credentials,'grant_type':'authorization_code','code':code,
                'code_verifier':verifier,'redirect_uri':'https://client.example/callback','resource':base+'/mcp'})
            token.raise_for_status();tokens=token.json()
            via_oauth=rpc('uni_status',token=tokens['access_token'])['structuredContent']
            result['oauth_authorization_and_authenticated_tool_ok']=via_oauth['documents']>0
            rotated=client.post(base+'/token',data={**credentials,'grant_type':'refresh_token',
                'refresh_token':tokens['refresh_token'],'scope':'uni:read offline_access'})
            rotated.raise_for_status();new=rotated.json()
            result['oauth_refresh_ok']=bool(new['access_token'])
            for grant in (tokens,new):
                for key,hint in [('access_token','access_token'),('refresh_token','refresh_token')]:
                    revoked=client.post(base+'/revoke',data={**credentials,'token':grant[key],'token_type_hint':hint})
                    revoked.raise_for_status()
            result['oauth_test_tokens_revoked']=True
        result['chatgpt_account_connected']=False
        result['claude_account_connected']=False
        result['actual_llm_answer_tested']=False
        result['note']='Real HTTPS MCP retrieval and optional OAuth verified; personal app accounts and model answer remain separate checks.'
    if args.output:
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
