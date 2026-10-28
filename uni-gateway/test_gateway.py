import base64
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "remote-rag"))
from server import create_server
from server import scope_check


OWNER = "gateway-test-owner"
SERVICE = "gateway-test-service"
DOC_ID = hashlib.sha256(b"doc").hexdigest()


class BrowserConsentForm(HTMLParser):
    """Collect successful HTML inputs, including only form-associated scopes."""

    def __init__(self, page):
        super().__init__()
        self.inside = False
        self.fields = {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = attrs.get("action") == "/consent"
        if tag != "input" or not self.inside or "disabled" in attrs or not attrs.get("name"):
            return
        if attrs.get("type") in {"checkbox", "radio"} and "checked" not in attrs:
            return
        self.fields.setdefault(attrs["name"], []).append(attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


class FakeBackend:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def call(self, tool, arguments=None):
        arguments = arguments or {}
        self.calls.append((tool, arguments))
        if tool == "uni_search":
            return {"sources": [{"document_id": DOC_ID, "page": 2, "course": "Statistik",
                                "source": "01_StudIP/statistik.pdf", "text": "Klausurhinweis"}]}
        if tool == "mail_search":
            return {"messages": [{"id": "inbox:7:10", "subject": "Abgabe", "from": "Tutor",
                                  "date": "2026-10-20"}]}
        if tool == "fetch":
            return {"id": arguments.get("id"), "text": "Quelle"}
        if tool == "uni_status":
            return {"source_index_at": "2026-01-01T00:00:00+00:00", "pending_documents": 0,
                    "sync": {"state": "ok", "finished": datetime.now(timezone.utc).isoformat()}}
        if tool == "mail_status":
            return {"last_poll": {"at": datetime.now(timezone.utc).isoformat(), "status": "ok"}}
        if tool == "uni_downloader_status":
            return {"finished": datetime.now(timezone.utc).isoformat(), "status": "success"}
        if tool == "uni_study_plan":
            return {"path": "Stundenplan_WiSe_2026-27.xlsx", "read_only": True}
        if tool == "uni_calendar_candidates":
            return {"source_document": "Stundenplan_WiSe_2026-27.xlsx", "source_hash": DOC_ID,
                    "semester": "WiSe 2026/27", "schedule_state": "draft", "candidates": [], "conflicts": []}
        if tool == "mail_briefing":
            return {"items": []}
        if tool == "pilot_obligations":
            return {"items": []}
        raise AssertionError(f"unexpected backend tool: {tool}")


class FakeCalendar:
    def status(self):
        return {"state": "needs_setup", "configured": False}

    def events(self, starts, ends):
        return []


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rag = FakeBackend("rag")
        self.mail = FakeBackend("mail")
        env = patch.dict(os.environ, {
            "OWNER_SECRET_SHA256": hashlib.sha256(OWNER.encode()).hexdigest(),
            "SERVICE_TOKEN_SHA256": hashlib.sha256(SERVICE.encode()).hexdigest(),
        })
        env.start(); self.addCleanup(env.stop)
        self.app = create_server(self.temp.name, "https://uni.example",
                                 {"rag": self.rag, "mail": self.mail}, FakeCalendar(), start_worker=False)
        self.client = TestClient(self.app, base_url="https://uni.example")
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)

    def rpc(self, method, params=None):
        return self.client.post("/mcp", headers={
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Authorization": "Bearer " + SERVICE,
        }, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_single_search_and_fetch_route_keep_source_types_distinct(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        names = [item["name"] for item in tools]
        self.assertEqual(names.count("search"), 1)
        self.assertEqual(names.count("fetch"), 1)
        self.assertIn("system_status", names)
        self.assertIn("calendar_events", names)
        self.assertIn("calendar_sync_preview", names)
        self.assertIn("uni_study_plan", names)
        send_tool = next(item for item in tools if item["name"] == "mail_send")
        self.assertEqual(send_tool["securitySchemes"], [{"type": "oauth2", "scopes": ["uni:write", "mail:send"]}])
        preview_tool = next(item for item in tools if item["name"] == "mail_preview_send")
        self.assertEqual(preview_tool["securitySchemes"], [{"type": "oauth2", "scopes": ["uni:write"]}])
        self.assertIn("pilot_briefing", names)

        result = self.rpc("tools/call", {"name": "search", "arguments": {"query": "Klausur"}}).json()["result"]["structuredContent"]
        ids = {item["kind"]: item["id"] for item in result["results"]}
        self.assertEqual(ids["course"], f"doc:{DOC_ID}:2")
        self.assertEqual(ids["mail"], "mail:inbox:7:10")
        self.rpc("tools/call", {"name": "fetch", "arguments": {"id": ids["course"]}})
        self.rpc("tools/call", {"name": "fetch", "arguments": {"id": ids["mail"]}})
        self.assertIn(("fetch", {"id": f"{DOC_ID}:2"}), self.rag.calls)
        self.assertIn(("fetch", {"id": "inbox:7:10"}), self.mail.calls)

    def test_search_starts_both_backends_before_waiting_for_either(self):
        both_started = asyncio.Event()
        started = set()

        async def call(backend, tool, arguments=None):
            started.add(backend)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            return {"sources": []} if backend == "rag" else {"messages": []}

        self.rag.call = lambda tool, arguments=None: call("rag", tool, arguments)
        self.mail.call = lambda tool, arguments=None: call("mail", tool, arguments)
        result = self.rpc("tools/call", {"name": "search", "arguments": {"query": "parallel"}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(started, {"rag", "mail"})

    def test_oversized_mcp_request_is_rejected_instead_of_truncated(self):
        response = self.client.post("/mcp", headers={
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Authorization": "Bearer " + SERVICE,
        }, content=b"{" + b"x" * (2 * 1024 * 1024))
        self.assertEqual(response.status_code, 413)

    def test_system_status_uses_latest_successful_cloud_sync_for_index_freshness(self):
        result=self.rpc("tools/call",{"name":"system_status","arguments":{}}).json()["result"]["structuredContent"]
        self.assertEqual(result["course_index"]["state"],"ok")
        self.assertLess(result["course_index"]["age_seconds"],60)
        self.assertEqual(result["course_index"]["data"]["source_index_at"],"2026-01-01T00:00:00+00:00")

    def test_read_and_write_scopes_are_distinct(self):
        class Token:
            def __init__(self, scopes): self.scopes = scopes
        class Context:
            def __init__(self, scopes): self.token = Token(scopes)
        self.assertTrue(scope_check("uni:read")(Context(["uni:read"])))
        self.assertFalse(scope_check("uni:write")(Context(["uni:read"])))
        self.assertTrue(scope_check("uni:write")(Context(["uni:read", "uni:write"])))
        self.assertFalse(scope_check("mail:send")(Context(["uni:read", "uni:write"])))
        self.assertTrue(scope_check("mail:send")(Context(["mail:send"])))

    def test_oauth_client_registration_supports_explicit_scope_upgrade(self):
        # Omitted DCR scope must allow later explicit consent, without granting
        # access before the owner submits the actual browser form.
        registered = self.client.post("/register", json={
            "client_name": "Synthetic default registration",
            "redirect_uris": ["https://client.example/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "client_secret_post",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        client = registered.json()
        scopes = {"uni:read", "uni:write", "mail:send", "offline_access"}
        self.assertEqual(set(client["scope"].split()), scopes)
        verifier = "d" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorize = self.client.get("/authorize", params={
            "client_id": client["client_id"], "redirect_uri": "https://client.example/callback",
            "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
            "scope": "uni:read uni:write mail:send offline_access", "resource": "https://uni.example/mcp",
        }, follow_redirects=False)
        self.assertEqual(authorize.status_code, 302, authorize.text)
        consent = self.client.get(authorize.headers["location"])
        fields = BrowserConsentForm(consent.text).fields
        self.assertEqual(set(fields.get("scopes", [])), scopes,
                         "Every granted scope must belong to the submitted HTML form")
        fields["owner_secret"] = OWNER
        approved = self.client.post("/consent", data=fields,
            headers={"Origin": "https://uni.example"}, follow_redirects=False)
        self.assertEqual(approved.status_code, 303, approved.text)
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = self.client.post("/token", data={
            "grant_type": "authorization_code", "client_id": client["client_id"],
            "client_secret": client["client_secret"], "code": code, "code_verifier": verifier,
            "redirect_uri": "https://client.example/callback", "resource": "https://uni.example/mcp",
        })
        self.assertEqual(token.status_code, 200, token.text)
        self.assertEqual(set(token.json()["scope"].split()), scopes)
        refreshed = self.client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client["client_id"],
            "client_secret": client["client_secret"], "refresh_token": token.json()["refresh_token"],
        })
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(set(refreshed.json()["scope"].split()), scopes)

    def test_oauth_consent_can_grant_read_without_write_or_send_and_tool_requests_upgrade(self):
        metadata = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertIn("uni:write", metadata["scopes_supported"])
        auth_metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertIn("S256", auth_metadata["code_challenge_methods_supported"])
        self.assertEqual(self.rpc_without_token().headers["www-authenticate"],
                         'Bearer resource_metadata="https://uni.example/.well-known/oauth-protected-resource/mcp"')

        registered = self.client.post("/register", json={
            "client_name": "Synthetic ChatGPT", "redirect_uris": ["https://client.example/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "uni:read uni:write mail:send offline_access",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        client = registered.json()
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = self.client.get("/authorize", params={
            "client_id": client["client_id"], "redirect_uri": "https://client.example/callback",
            "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "scope": "uni:read uni:write mail:send offline_access",
            "resource": "https://uni.example/mcp",
        }, follow_redirects=False)
        consent = self.client.get(response.headers["location"])
        self.assertIn("Uni-Mail nach frischer Vorschau", consent.text)
        ticket = re.search('name="id" value="([^"]+)"', consent.text).group(1)
        csrf = re.search('name="csrf" value="([^"]+)"', consent.text).group(1)
        approved = self.client.post("/consent", data={"id": ticket, "csrf": csrf, "owner_secret": OWNER,
            # ChatGPT's custom-MCP flow serializes scopes as one OAuth string.
            "scopes": "uni:read offline_access"}, headers={"Origin": "https://uni.example"}, follow_redirects=False)
        self.assertEqual(approved.status_code, 303, approved.text)
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = self.client.post("/token", data={"grant_type": "authorization_code", "client_id": client["client_id"],
            "client_secret": client["client_secret"], "code": code, "code_verifier": verifier,
            "redirect_uri": "https://client.example/callback", "resource": "https://uni.example/mcp"})
        self.assertEqual(token.status_code, 200, token.text)
        self.assertEqual(set(token.json()["scope"].split()), {"uni:read", "offline_access"})
        result = self.client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "Authorization": "Bearer " + token.json()["access_token"]},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "mail_send", "arguments": {"draft_id": "a"*64, "confirmation_token": "x"*40}}})
        call = result.json()["result"]
        self.assertTrue(call["isError"])
        self.assertIn("uni:write mail:send", call["_meta"]["mcp/www_authenticate"])
        self.assertNotIn(("mail_send", {"draft_id": "a"*64, "confirmation_token": "x"*40}), self.mail.calls)
        preview_result = self.client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "Authorization": "Bearer " + token.json()["access_token"]},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "mail_preview_send", "arguments": {"draft_id": "a"*64}}}).json()["result"]
        self.assertTrue(preview_result["isError"])
        self.assertIn('scope="uni:write"', preview_result["_meta"]["mcp/www_authenticate"])
        self.assertNotIn(("mail_preview_send", {"draft_id": "a"*64}), self.mail.calls)

    def rpc_without_token(self):
        return self.client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})


if __name__ == "__main__":
    unittest.main()
