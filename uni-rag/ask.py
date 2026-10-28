"""Command-line client for Codex or a terminal. Uses the same local RAG UI service."""
import argparse,json,urllib.request
parser=argparse.ArgumentParser();parser.add_argument('question');parser.add_argument('--course',default='');parser.add_argument('--sources-only',action='store_true');args=parser.parse_args()
request=urllib.request.Request('http://127.0.0.1:8767/api/query',data=json.dumps({'question':args.question,'course':args.course,'generate':not args.sources_only}).encode(),headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:8767'})
with urllib.request.urlopen(request,timeout=300) as response:print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
