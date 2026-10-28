import hashlib
import io
import json
import os
import tempfile
import unittest
import subprocess
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock,patch

from starlette.testclient import TestClient
from core import Mailbox,Service,MailError,message_parts,quoted
from server import create_server
from https_mail import UniversityHTTPS, HTTPSMailError, FOLDER_SUCCESS

CONFIG={'email':'student@example.test','username':'student','password':'synthetic'}


def original():
    msg=EmailMessage();msg['From']='Teacher <teacher@example.test>';msg['To']=CONFIG['email'];msg['Subject']='Seminarfrage';msg['Message-ID']='<original@example.test>';msg.set_content('Bitte sende eine Rueckmeldung bis 23.10.2026.')
    item=message_parts(msg.as_bytes());item.update(id='inbox:7:10',folder='inbox',flags=[]);return item


class FakeMailbox:
    def __init__(self):self.items={'inbox:7:10':original()};self.append_count=0;self.send_count=0
    def read(self,key):return dict(self.items[key])
    def append_message(self,raw,mid,folder='drafts'):
        self.append_count+=1;key=f'{folder}:7:{20+self.append_count}'
        self.items[key]={**message_parts(raw),'id':key,'folder':folder,'flags':[]};return key
    def send(self,raw,to,port=465):self.send_count+=1
    def search(self,*args,**kw):return {'uidvalidity':'7','ids':['inbox:7:10'],'total_matches':1}
    def smtp_check(self):return True


class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=Service(Path(self.tmp.name),CONFIG);self.fake=FakeMailbox();self.service.mailbox=self.fake
        self.service.meta('smtp_check',{'login_ok':True,'port':465})

    def test_html_does_not_load_remote_content_and_attachment_metadata(self):
        msg=EmailMessage();msg['Subject']='HTML';msg.set_content('<p>Termin Freitag</p><script>malicious</script><img src="https://example.test/track">',subtype='html')
        msg.add_attachment(b'synthetic',maintype='application',subtype='pdf',filename='test.pdf')
        raw=msg.as_bytes();p=message_parts(raw,len(raw)+10)
        self.assertIn('Termin Freitag',p['text']);self.assertNotIn('malicious',p['text']);self.assertNotIn('track',p['text']);self.assertTrue(p['truncated']);self.assertEqual(p['attachments'][0]['name'],'test.pdf')

    def test_supported_text_attachment_is_extracted_into_private_mail_payload(self):
        msg=EmailMessage();msg['Subject']='Abgabe';msg.set_content('Bitte pruefen.')
        msg.add_attachment('Abgabe am 2027-01-12.\nProspect Paper einreichen.',subtype='plain',filename='seminar.txt')
        item=message_parts(msg.as_bytes())
        self.assertEqual(item['attachment_text_version'],2)
        self.assertEqual(item['attachments'][0]['content_status'],'extracted')
        self.assertIn('2027-01-12',item['attachments'][0]['text'])

    def test_scanned_pdf_attachment_is_ocrd_on_the_mail_service(self):
        msg=EmailMessage();msg['Subject']='Scan';msg.set_content('Bitte pruefen.')
        msg.add_attachment(b'%PDF-synthetic',maintype='application',subtype='pdf',filename='deadline.pdf')
        def command(args,**kwargs):
            if args[0]=='pdftotext':
                if 'attachment-ocr.pdf' in args[-2]:
                    return subprocess.CompletedProcess(args,0,b'Abgabe bis 2027-01-12.',b'')
                return subprocess.CompletedProcess(args,0,b'',b'')
            if args[0]=='ocrmypdf':
                Path(args[-1]).write_bytes(b'%PDF-ocr')
                return subprocess.CompletedProcess(args,0,b'',b'')
            raise AssertionError(args[0])
        with patch('core.subprocess.run',side_effect=command):
            item=message_parts(msg.as_bytes())
        attachment=item['attachments'][0]
        self.assertEqual(attachment['content_status'],'extracted')
        self.assertTrue(attachment['ocr_used'])
        self.assertIn('2027-01-12',attachment['text'])

    def test_read_uses_peek_and_rejects_stale_uid(self):
        box=Mailbox(CONFIG);m=MagicMock();m.__enter__.return_value=m;m.select.return_value=('OK',[b'1']);m.response.return_value=('UIDVALIDITY',[b'7'])
        raw=EmailMessage();raw.set_content('Content remains unread')
        m.uid.return_value=('OK',[(b'1 (FLAGS () RFC822.SIZE 1)',raw.as_bytes()),b')'])
        with patch.object(box,'connect',return_value=m):
            p=box.read('inbox:7:10');self.assertIn('Content',p['text']);self.assertIn('BODY.PEEK',m.uid.call_args.args[2]);m.select.assert_called_with('INBOX',readonly=True)
            with self.assertRaises(MailError):box.read('inbox:8:10')

    def test_uid_range_does_not_reimport_last_message_and_quoting(self):
        box=Mailbox(CONFIG);m=MagicMock();m.__enter__.return_value=m;m.select.return_value=('OK',[b'1']);m.response.return_value=('UIDVALIDITY',[b'7']);m.uid.return_value=('OK',[b'10'])
        with patch.object(box,'connect',return_value=m):self.assertEqual(box.search(after=10)['ids'],[])
        self.assertEqual(quoted('a"b'),'"a\\"b"')
        with self.assertRaises(MailError):quoted('x\r\nUID SEARCH ALL')

    def test_reply_idempotence_thread_and_readback(self):
        p=self.service.create_reply('inbox:7:10','Ich pruefe die Unterlagen.');again=self.service.create_reply('inbox:7:10','Ich pruefe die Unterlagen.')
        self.assertEqual(p['draft_id'],again['draft_id']);self.assertEqual(self.fake.append_count,1);self.assertEqual(self.fake.send_count,0)
        with self.service.db() as db:r=db.execute('SELECT raw FROM drafts').fetchone()
        self.assertIn(b'In-Reply-To: <original@example.test>',r['raw'])
        self.assertEqual(p['recipient'],'teacher@example.test')

    def test_reply_rejects_truncated_source_and_ambiguous_recipient(self):
        self.fake.items['inbox:7:10']['truncated']=True
        with self.assertRaises(MailError):self.service.create_reply('inbox:7:10','Antwort')
        self.fake.items['inbox:7:10']['truncated']=False;self.fake.items['inbox:7:10']['reply_to']='a@example.test,b@example.test'
        with self.assertRaises(MailError):self.service.create_reply('inbox:7:10','Antwort')
        self.assertEqual(self.fake.append_count,0)

    def test_send_needs_preview_single_use_and_sent_copy(self):
        draft=self.service.create_reply('inbox:7:10','Antwort');key=draft['draft_id']
        with self.assertRaises(MailError):self.service.send(key,'invalid-token')
        preview=self.service.preview_send(key);result=self.service.send(key,preview['confirmation_token'])
        self.assertTrue(result['smtp_accepted']);self.assertTrue(result['sent_copy_mail_id'].startswith('sent:'));self.assertEqual(self.fake.send_count,1)
        with self.assertRaises(MailError):self.service.send(key,preview['confirmation_token'])
        self.assertEqual(self.fake.send_count,1)

    def test_send_rejects_changed_draft_after_preview(self):
        draft=self.service.create_reply('inbox:7:10','Antwort');preview=self.service.preview_send(draft['draft_id'])
        self.fake.items[draft['mail_id']]['text']='Extern geaendert'
        with self.assertRaises(MailError):self.service.send(draft['draft_id'],preview['confirmation_token'])
        self.assertEqual(self.fake.send_count,0)

    def test_unavailable_smtp_does_not_consume_or_send_draft(self):
        draft=self.service.create_reply('inbox:7:10','Antwort');self.service.meta('smtp_check',{'login_ok':False})
        with self.assertRaises(MailError):self.service.preview_send(draft['draft_id'])
        with self.assertRaises(MailError):self.service.send(draft['draft_id'],'unused-token')
        with self.service.db() as db:self.assertEqual(db.execute('SELECT state FROM drafts').fetchone()[0],'prepared')
        self.assertEqual(self.fake.send_count,0)

    def test_ambiguous_smtp_result_blocks_retry(self):
        draft=self.service.create_reply('inbox:7:10','Antwort');preview=self.service.preview_send(draft['draft_id'])
        with patch.object(self.fake,'send',side_effect=MailError('connection lost')):
            with self.assertRaises(MailError):self.service.send(draft['draft_id'],preview['confirmation_token'])
        with self.assertRaises(MailError):self.service.preview_send(draft['draft_id'])

    def test_background_worker_only_prepares_proposals(self):
        result={'summary':'Nachfrage','priority':'normal','category':'Lehre','needs_reply':True,'deadlines':['23.10.2026'],'suggested_reply':'Antwortvorschlag'}
        def analyze(item):
            with self.service.db() as db:db.execute('UPDATE messages SET analysis=? WHERE id=?',(json.dumps(result),item['id']))
        with patch.object(self.service,'analyze',side_effect=analyze):self.service.poll()
        self.assertEqual(self.fake.send_count,0);self.assertEqual(self.fake.append_count,0);self.assertEqual(self.service.status()['pending_analysis'],0);self.assertEqual(len(self.service.briefing()),1)

    def test_api_uses_structured_untrusted_input_without_storage(self):
        self.service.read('inbox:7:10')
        analysis={'summary':'Nachfrage','priority':'normal','category':'Lehre','needs_reply':True,'deadlines':[{'date_text':'23.10.2026','task':'Rueckmeldung','evidence_quote':'Bitte sende eine Rueckmeldung bis 23.10.2026.'},{'date_text':'24.10.2026','task':'Erfunden','evidence_quote':'Erfundener Text 24.10.2026'}],'suggested_reply':'Danke.'}
        response=MagicMock(status_code=200);response.json.return_value={'output':[{'content':[{'type':'output_text','text':json.dumps(analysis)}]}]}
        with patch.dict(os.environ,OPENAI_API_KEY='synthetic'),patch('core.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value=response
            result=self.service.analyze(original());self.assertEqual(result['priority'],'normal');self.assertEqual(len(result['deadlines']),1)
            data=client.return_value.__enter__.return_value.post.call_args.kwargs['json'];self.assertFalse(data['store']);self.assertTrue(data['text']['format']['strict']);self.assertNotIn('tools',data)

    def test_new_mail_is_idempotent_and_not_a_reply(self):
        draft=self.service.create_message('teacher@example.test','Neue Frage','Kurze Frage.')
        again=self.service.create_message('teacher@example.test','Neue Frage','Kurze Frage.')
        self.assertEqual(draft['draft_id'],again['draft_id']);self.assertEqual(self.fake.append_count,1)
        with self.service.db() as db:raw=db.execute('SELECT raw FROM drafts').fetchone()['raw']
        self.assertNotIn(b'In-Reply-To:',raw);self.assertEqual(self.fake.send_count,0)
        for address in ['a@example.test,b@example.test','a@example.test\r\nBcc: b@example.test','A <a@example.test>']:
            with self.assertRaises(MailError):self.service.create_message(address,'Betreff','Text')
        self.assertEqual(self.fake.append_count,1)

    def test_https_probe_never_sends_and_checks_authenticated_body(self):
        import httpx
        calls=[]
        def handle(request):
            calls.append(request)
            if request.method=='OPTIONS':return httpx.Response(200,headers={'MS-ASProtocolVersions':'12.1,14.1','MS-ASProtocolCommands':'FolderSync,SendMail'})
            self.assertEqual(request.url.params['Cmd'],'FolderSync')
            return httpx.Response(200,headers={'Content-Type':'application/vnd.ms-sync.wbxml'},content=FOLDER_SUCCESS+b'\x01')
        box=UniversityHTTPS(CONFIG)
        with patch.object(box,'client',side_effect=lambda:httpx.Client(transport=httpx.MockTransport(handle),auth=('student','synthetic'),follow_redirects=False)):
            result=box.check()
        self.assertTrue(result['login_ok']);self.assertFalse(result['send_tested']);self.assertEqual(len(calls),2)
        self.assertTrue(all(r.url.host=='webmail.uni-bremen.de' for r in calls))

    def test_https_probe_rejects_login_page_and_failed_auth_without_secret_error(self):
        import httpx
        for response in [httpx.Response(401,content=b'synthetic'),httpx.Response(200,headers={'MS-ASProtocolVersions':'12.1','MS-ASProtocolCommands':'SendMail'},content=b'')]:
            box=UniversityHTTPS(CONFIG)
            def handle(request):
                if request.method=='OPTIONS':return response
                return httpx.Response(200,headers={'Content-Type':'text/html'},content=b'login synthetic')
            with patch.object(box,'client',side_effect=lambda:httpx.Client(transport=httpx.MockTransport(handle))):
                with self.assertRaises(HTTPSMailError) as error:box.check()
                self.assertNotIn('synthetic',str(error.exception))

    def test_https_submission_is_once_and_rejects_redirect_or_nonempty_success(self):
        import httpx
        for status,content,success in [(200,b'',True),(200,b'error',False),(302,b'',False),(500,b'',False)]:
            calls=[];box=UniversityHTTPS(CONFIG)
            def handle(request):
                calls.append(request);self.assertEqual(request.url.params['Cmd'],'SendMail');self.assertEqual(request.url.params['SaveInSent'],'F')
                self.assertEqual(request.headers['Content-Type'],'message/rfc822');self.assertEqual(request.content,b'synthetic MIME')
                return httpx.Response(status,content=content,headers={'Location':'https://other.example.test/'})
            with patch.object(box,'client',side_effect=lambda:httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False)):
                if success:box.send(b'synthetic MIME')
                else:
                    with self.assertRaises(HTTPSMailError):box.send(b'synthetic MIME')
            self.assertEqual(len(calls),1)

    def test_https_gate_is_separate_from_old_smtp_and_preserves_approval(self):
        self.service.send_transport='activesync_https'
        self.assertFalse(self.service.status()['send_available'])
        draft=self.service.create_message('teacher@example.test','Neue Frage','Frage')
        with self.assertRaises(MailError):self.service.preview_send(draft['draft_id'])
        self.service.meta('https_check',{'login_ok':True,'transport':'activesync_https','send_tested':False})
        preview=self.service.preview_send(draft['draft_id'])
        with self.assertRaises(MailError):self.service.send(draft['draft_id'],'invalid')
        result=self.service.send(draft['draft_id'],preview['confirmation_token'])
        self.assertTrue(result['server_accepted']);self.assertFalse(result['smtp_accepted']);self.assertEqual(result['transport'],'activesync_https')
        self.assertEqual(self.fake.send_count,1)

    def test_mcp_access_scope_and_write_annotations(self):
        with patch.dict(os.environ,OWNER_SECRET_SHA256=hashlib.sha256(b'owner').hexdigest(),SERVICE_TOKEN_SHA256=hashlib.sha256(b'client').hexdigest()):
            app=create_server(Path(self.tmp.name)/'server','https://mail.example',False,CONFIG)
            with TestClient(app,base_url='https://mail.example') as c:
                req={'jsonrpc':'2.0','id':1,'method':'tools/list'};headers={'Accept':'application/json, text/event-stream'}
                self.assertEqual(c.post('/mcp',json=req,headers=headers).status_code,401)
                headers['Authorization']='Bearer client';r=c.post('/mcp',json=req,headers=headers)
                tools={x['name']:x for x in r.json()['result']['tools']}
                self.assertTrue(tools['mail_read']['annotations']['readOnlyHint']);self.assertFalse(tools['mail_send']['annotations']['readOnlyHint']);self.assertTrue(tools['mail_send']['annotations']['destructiveHint'])
                self.assertTrue(tools['pilot_obligations']['annotations']['readOnlyHint'])
                self.assertFalse(tools['pilot_obligation_complete']['annotations']['readOnlyHint'])
                self.assertTrue(tools['pilot_obligation_complete']['annotations']['idempotentHint'])
                register=c.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'pilot_obligation_register','arguments':{'records':[{'id':'fixture-task','course':'Fixture','title':'Fixture task','due':None}]}}},headers=headers)
                self.assertEqual(register.status_code,200);self.assertNotIn('error',register.json())
                complete=c.post('/mcp',json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'pilot_obligation_complete','arguments':{'task_id':'fixture-task','source':'user_reported','note':'Fixture'}}},headers=headers)
                self.assertEqual(complete.status_code,200);self.assertNotIn('error',complete.json())
                self.assertEqual(app.state.pilot.list('open')['count'],0)
                self.assertEqual(app.state.pilot.list('completed')['count'],1)
                meta=c.get('/.well-known/oauth-authorization-server').json();self.assertIn('mail:manage',meta['scopes_supported']);self.assertNotIn('uni:read',meta['scopes_supported'])
                reg=c.post('/register',json={'redirect_uris':['https://client.example/cb'],'scope':'uni:read'});self.assertEqual(reg.status_code,400)


if __name__=='__main__':unittest.main()
