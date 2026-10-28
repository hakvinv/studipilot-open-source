"""University ActiveSync HTTPS submission; no browser automation or third-party relay."""
import hashlib

import certifi
import httpx

ENDPOINT='https://webmail.uni-bremen.de/Microsoft-Server-ActiveSync'
VERSION='12.1'
FOLDER_REQUEST=bytes.fromhex('03016a00000756520330000101')
# FolderHierarchy:FolderSync / Status=1, inline UTF-8, empty string table.
FOLDER_SUCCESS=bytes.fromhex('03016a000007564c03310001')


class HTTPSMailError(RuntimeError):pass


class UniversityHTTPS:
    def __init__(self,config):
        self.config=config
        self.device_id='UNI'+hashlib.sha256(config['email'].encode()).hexdigest()[:24].upper()

    def client(self):
        # Never follow redirects carrying Basic authentication. Endpoint is fixed, not mail supplied.
        return httpx.Client(verify=certifi.where(),timeout=30,follow_redirects=False,
                            auth=(self.config['username'],self.config['password']),
                            headers={'MS-ASProtocolVersion':VERSION,'User-Agent':'UniPilot/1.0'})

    def params(self,command):
        return {'Cmd':command,'User':self.config['username'],'DeviceId':self.device_id,'DeviceType':'UniPilot'}

    def check(self):
        """Authenticate and read folder hierarchy only. Never call SendMail during a check."""
        try:
            with self.client() as c:
                options=c.options(ENDPOINT)
                if options.status_code!=200 or VERSION not in options.headers.get('MS-ASProtocolVersions','').split(',') or 'SendMail' not in options.headers.get('MS-ASProtocolCommands','').split(','):
                    raise HTTPSMailError('HTTPS-Mailprotokoll oder Versandbefehl nicht verfuegbar.')
                response=c.post(ENDPOINT,params=self.params('FolderSync'),content=FOLDER_REQUEST,
                                headers={'Content-Type':'application/vnd.ms-sync.wbxml'})
                # Reject unknown encodings/statuses instead of interpreting a login page as success.
                if response.status_code!=200 or response.headers.get('content-type','').split(';')[0].strip()!='application/vnd.ms-sync.wbxml' or not response.content.startswith(FOLDER_SUCCESS):
                    raise HTTPSMailError('HTTPS-Mailanmeldung nicht bestaetigt.')
            return {'login_ok':True,'transport':'activesync_https','protocol':VERSION,
                    'send_command_advertised':True,'send_tested':False,
                    'check':'authenticated_folder_read_only'}
        except Exception:
            # Do not expose auth headers, account URLs, response bodies or credentials in errors.
            raise HTTPSMailError('Uni-HTTPS-Mailzugang nicht bestaetigt.') from None

    def send(self,raw):
        """One submission, no retries. Caller owns preview, single-use approval and sent copy."""
        try:
            with self.client() as c:
                response=c.post(ENDPOINT,params={**self.params('SendMail'),'SaveInSent':'F'},
                                headers={'Content-Type':'message/rfc822'},content=raw)
                # ActiveSync 12.1 success is HTTP 200 with an empty body. Nonempty results fail closed.
                if response.status_code!=200 or response.content:
                    raise HTTPSMailError('Uni-Mailserver hat den Versand nicht bestaetigt.')
        except Exception:
            raise HTTPSMailError('HTTPS-Versand nicht bestaetigt. Nicht erneut senden; Gesendet-Ordner pruefen.') from None
