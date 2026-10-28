"""Single-owner OAuth storage and explicit consent; protocol handling stays in FastMCP."""
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, TokenError, RegistrationError, AuthorizeError
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerOAuth(OAuthProvider):
    def __init__(self, state, base_url, owner_hash, service_hash, *, scope="uni:read", name="Uni-Kontext", description="lesenden Zugriff auf deine indexierten Uni-Unterlagen", consent_label="Lesenden Zugriff erlauben", additional_scopes=None):
        hashes=[part.strip() for part in (service_hash or '').split(',') if part.strip()]
        if len(owner_hash) != 64 or any(len(value) != 64 for value in hashes):
            raise ValueError('Private SHA-256 credentials are malformed')
        self.scope,self.name,self.description,self.consent_label=scope,name,description,consent_label
        self.permitted_scopes=list(dict.fromkeys([scope, *(additional_scopes or [])]))
        self.service_hashes=hashes
        self.gateway_service_hash=os.environ.get('GATEWAY_SERVICE_TOKEN_SHA256','').strip()
        self.gateway_service_scopes=os.environ.get('GATEWAY_SERVICE_SCOPES',scope).split()
        if self.gateway_service_hash and len(self.gateway_service_hash)!=64:
            raise ValueError('Gateway service token hash is malformed')
        super().__init__(base_url=base_url, required_scopes=[self.scope],
                         client_registration_options=ClientRegistrationOptions(
                             enabled=True, valid_scopes=[*self.permitted_scopes,'offline_access'],
                             # Register new OAuth clients for every supported capability;
                             # users still grant each write scope separately at consent.
                             default_scopes=[*self.permitted_scopes,'offline_access']),
                         revocation_options=RevocationOptions(enabled=True))
        self.state = state
        self.owner_hash, self.service_hash = owner_hash, service_hash
        self.url = base_url.rstrip('/')
        state.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS oauth(kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY(kind,key))')
            db.execute('CREATE TABLE IF NOT EXISTS failures(key TEXT PRIMARY KEY, count INTEGER, expires REAL)')
        (state/'oauth.sqlite').chmod(0o600)

    def db(self):
        return sqlite3.connect(self.state/'oauth.sqlite', timeout=10)

    def put(self, kind, key, value, expires):
        with self.db() as db:
            db.execute('DELETE FROM oauth WHERE expires < ?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO oauth VALUES(?,?,?,?)',
                       (kind, digest(key), json.dumps(value), expires))

    def get(self, kind, key, consume=False):
        with self.db() as db:
            if consume:
                row = db.execute('DELETE FROM oauth WHERE kind=? AND key=? AND expires>? RETURNING value',
                                 (kind, digest(key), time.time())).fetchone()
            else:
                row = db.execute('SELECT value FROM oauth WHERE kind=? AND key=? AND expires>?',
                                 (kind, digest(key), time.time())).fetchone()
        return json.loads(row[0]) if row else None

    async def get_client(self, client_id):
        data = self.get('client', client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        redirects = client_info.redirect_uris or []
        if any(u.username or u.password or u.fragment for u in redirects):
            raise RegistrationError('invalid_redirect_uri','Redirect must not contain credentials or a fragment')
        # HTTPS clients and local MCP applications; arbitrary schemes are rejected.
        if any(u.scheme != 'https' and not (u.scheme == 'http' and u.host in {'localhost', '127.0.0.1', '::1'}) for u in redirects):
            raise RegistrationError('invalid_redirect_uri','Redirect must use HTTPS or a loopback address')
        if set((client_info.scope or self.scope).split()) - {*self.permitted_scopes,'offline_access'}:
            raise RegistrationError('invalid_client_metadata','Unsupported OAuth scope')
        self.put('client', client_info.client_id, client_info.model_dump(mode='json'), time.time()+365*86400)

    async def authorize(self, client, params):
        if set(params.scopes or [self.scope]) - {*self.permitted_scopes,'offline_access'}:
            raise AuthorizeError('invalid_scope','Unsupported OAuth scope')
        if params.resource and params.resource.rstrip('/') != self.url+'/mcp':
            raise AuthorizeError('invalid_request','Wrong resource')
        ticket = secrets.token_urlsafe(32)
        self.put('pending', ticket, {'client_id':client.client_id, 'params':params.model_dump(mode='json')}, time.time()+600)
        return self.url+'/consent?'+urlencode({'id':ticket})

    async def consent(self, request):
        headers = {'Cache-Control':'no-store', 'Referrer-Policy':'same-origin',
                   'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}
        if request.method == 'GET':
            ticket = request.query_params.get('id', '')
            pending = self.get('pending', ticket)
            if not pending:
                return HTMLResponse('Anfrage abgelaufen.', status_code=400, headers=headers)
            client = await self.get_client(pending['client_id'])
            if not client:
                return HTMLResponse('Client fehlt.', status_code=400, headers=headers)
            csrf = secrets.token_urlsafe(32)
            pending['csrf'] = digest(csrf)
            self.put('pending', ticket, pending, time.time()+600)
            label = html.escape(client.client_name or client.client_id)
            target = html.escape(pending['params']['redirect_uri'])
            scope_labels = {
                'uni:read': 'Kursunterlagen, Uni-Mail, Aufgaben, Status und Kalender lesen',
                'uni:write': 'Aufgaben aktualisieren, Mailentwuerfe speichern und belegte Uni-Termine im Uni-Kalender aktualisieren',
                'mail:send': 'Uni-Mail nach frischer Vorschau mit deinem ausdruecklichen Einzelauftrag versenden',
                'offline_access': 'Verbindung fuer kuenftige Zugriffe erneuern',
            }
            requested = pending['params'].get('scopes') or [self.scope]
            permission_list = ''.join(
                f'<li><label><input type="checkbox" name="scopes" value="{html.escape(scope)}" '
                f'{"checked disabled" if scope == self.scope else "checked"}> '
                f'{html.escape(scope_labels.get(scope, scope))}</label>'
                f'{f"<input type=\"hidden\" name=\"scopes\" value=\"{html.escape(scope)}\">" if scope == self.scope else ""}</li>'
                for scope in requested)
            callback = urlsplit(pending['params']['redirect_uri'])
            # Chrome also applies form-action to the final OAuth redirect.
            headers['Content-Security-Policy'] = (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "
                + callback.scheme+'://'+callback.netloc+"; frame-ancestors 'none'")
            page = f'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{html.escape(self.name)} verbinden</title>
            <style>body{{font:18px system-ui;max-width:580px;margin:8vh auto;padding:24px;line-height:1.6}}input,button{{font:inherit;padding:12px;box-sizing:border-box;width:100%;margin:8px 0}}small{{overflow-wrap:anywhere}}</style>
            <h1>{html.escape(self.name)} verbinden</h1><form method="post" action="/consent">
            <p><b>{label}</b> erhaelt angeforderte Berechtigungen:</p><ul>{permission_list}</ul>
            <p><small>Rückkehr zu: {target}</small></p>
            <input type="hidden" name="id" value="{html.escape(ticket)}"><input type="hidden" name="csrf" value="{csrf}">
            <label>Persönlicher Verbindungsschlüssel<input type="password" name="owner_secret" autocomplete="off" required maxlength="200"></label>
            <button>{html.escape(self.consent_label)}</button></form></html>'''
            response = HTMLResponse(page, headers=headers)
            response.set_cookie('uni_consent', csrf, secure=self.url.startswith('https:'), httponly=True,
                                samesite='lax', max_age=600, path='/consent')
            return response
        if request.headers.get('origin') != self.url:
            return HTMLResponse('Falscher Ursprung.', status_code=403, headers=headers)
        # A browser submits one field per selected scope, plus id, csrf and
        # owner_secret. Full gateway consent therefore needs seven fields.
        form = await request.form(max_fields=16, max_part_size=4096)
        ticket, csrf = str(form.get('id','')), str(form.get('csrf',''))
        pending = self.get('pending', ticket)
        if (not pending or not csrf or not hmac.compare_digest(pending.get('csrf',''),digest(csrf))
                or not hmac.compare_digest(request.cookies.get('uni_consent',''),csrf)):
            return HTMLResponse('Ungueltige Anfrage.', status_code=403, headers=headers)
        # Global gate as well as per-IP gate; no proxy-header trust is required.
        with self.db() as db:
            db.execute('DELETE FROM failures WHERE expires < ?', (time.time(),))
            row = db.execute('SELECT count FROM failures WHERE key=?',('owner',)).fetchone()
            if row and row[0] >= 30:
                return HTMLResponse('Anmeldung voruebergehend gesperrt.', status_code=429, headers=headers)
            if not hmac.compare_digest(digest(str(form.get('owner_secret',''))),self.owner_hash):
                db.execute('INSERT INTO failures VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1',
                           ('owner',time.time()+600))
                return HTMLResponse('Falscher Verbindungsschluessel.', status_code=403, headers=headers)
        pending = self.get('pending', ticket, consume=True)
        if not pending:
            return HTMLResponse('Anfrage bereits verwendet.', status_code=400, headers=headers)
        params = AuthorizationParams.model_validate(pending['params'])
        requested_scopes=set(params.scopes or [self.scope])
        # Some MCP clients submit repeated scope fields while others serialize
        # the complete OAuth scope list into one space-delimited form field.
        # Accept both encodings, then intersect with the exact requested set.
        posted_scopes = {scope for value in form.getlist('scopes') for scope in str(value).split()}
        selected_scopes = posted_scopes & requested_scopes
        if self.scope not in selected_scopes:
            return HTMLResponse('Lesender Uni-Zugriff ist erforderlich.', status_code=400, headers=headers)
        code = secrets.token_urlsafe(32)
        entry = AuthorizationCode(code='', client_id=pending['client_id'], scopes=sorted(selected_scopes),
                                  expires_at=time.time()+120, code_challenge=params.code_challenge,
                                  redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                  resource=self.url+'/mcp', subject='owner')
        self.put('code',code,entry.model_dump(mode='json'),entry.expires_at)
        query = {'code':code}
        if params.state is not None:
            query['state'] = params.state
        target = str(params.redirect_uri)
        callback = urlsplit(target)
        headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "
            + callback.scheme+'://'+callback.netloc+"; frame-ancestors 'none'")
        response = RedirectResponse(target+('&' if '?' in target else '?')+urlencode(query), status_code=303, headers=headers)
        response.delete_cookie('uni_consent', path='/consent')
        return response

    async def load_authorization_code(self, client, authorization_code):
        data = self.get('code',authorization_code)
        if not data or data['client_id'] != client.client_id:
            return None
        return AuthorizationCode.model_validate(dict(data,code=authorization_code))

    def issue(self, client_id, scopes):
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        now = int(time.time())
        a = AccessToken(token='',client_id=client_id,scopes=scopes,expires_at=now+3600,resource=self.url+'/mcp',subject='owner')
        r = RefreshToken(token='',client_id=client_id,scopes=scopes,expires_at=now+30*86400,resource=self.url+'/mcp',subject='owner')
        self.put('access',access,a.model_dump(mode='json'),a.expires_at)
        self.put('refresh',refresh,r.model_dump(mode='json'),r.expires_at)
        return OAuthToken(access_token=access,refresh_token=refresh,expires_in=3600,scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.get('code',authorization_code.code,consume=True)
        if not data or data['client_id'] != client.client_id:
            raise TokenError('invalid_grant','Code expired or already used')
        return self.issue(client.client_id,data['scopes'])

    async def load_refresh_token(self, client, refresh_token):
        data = self.get('refresh',refresh_token)
        if not data or data['client_id'] != client.client_id:
            return None
        return RefreshToken.model_validate(dict(data,token=refresh_token))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if set(scopes) - set(refresh_token.scopes):
            raise TokenError('invalid_scope')
        data = self.get('refresh',refresh_token.token,consume=True)
        if not data or data['client_id'] != client.client_id:
            raise TokenError('invalid_grant')
        return self.issue(client.client_id,scopes)

    async def load_access_token(self, token):
        token_hash=digest(token)
        if any(hmac.compare_digest(token_hash,value) for value in self.service_hashes):
            return AccessToken(token=token,client_id='private-owner-client',scopes=[self.scope],resource=self.url+'/mcp',subject='owner')
        if self.gateway_service_hash and hmac.compare_digest(token_hash,self.gateway_service_hash):
            return AccessToken(token=token,client_id='private-gateway',scopes=self.gateway_service_scopes,resource=self.url+'/mcp',subject='owner')
        data = self.get('access',token)
        return AccessToken.model_validate(dict(data,token=token)) if data else None

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        self.get('access' if isinstance(token,AccessToken) else 'refresh',token.token,consume=True)

    def get_routes(self, mcp_path=None):
        return [*super().get_routes(mcp_path),Route('/consent',self.consent,methods=['GET','POST'])]
