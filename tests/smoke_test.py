"""End-to-end smoke test.

Starts the real relay as a subprocess, points it at a FAKE Microsoft (token + Graph) running in this
process, then drives it over HTTP exactly the way Claude would. Nothing real is contacted.

Run from the project folder:   python3 tests/smoke_test.py
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-secret-" + "x" * 40
PROTO = "2025-06-18"

# ------------------------------------------------------------------ fake Microsoft
GRAPH_CALLS: list[dict] = []


class FakeMicrosoft(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        if self.path.endswith("/token"):
            form = dict(p.split("=", 1) for p in raw.decode().split("&"))
            if form.get("client_secret") == "bad-secret":
                return self._reply(401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret.\r\nTrace ID: x"})
            return self._reply(200, {"access_token": "fake-token", "expires_in": 3600})
        if self.path.endswith("/sendMail"):
            GRAPH_CALLS.append({"path": self.path, "auth": self.headers.get("Authorization"), "json": json.loads(raw)})
            if "denied%40helpaccess.ca" in self.path or "denied@helpaccess.ca" in self.path:
                return self._reply(403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
            return self._reply(202)
        self._reply(404, {})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


fake_port = free_port()
fake = ThreadingHTTPServer(("127.0.0.1", fake_port), FakeMicrosoft)
threading.Thread(target=fake.serve_forever, daemon=True).start()

# ------------------------------------------------------------------ relay launcher


def start_relay(extra_env: dict) -> tuple[subprocess.Popen, int, str]:
    port = free_port()
    data = tempfile.mkdtemp(prefix="relay-data-")
    env = {
        **os.environ,
        "PORT": str(port),
        "DATA_DIR": data,
        "MCP_PATH_SECRET": SECRET,
        "ALLOWED_MAILBOXES": "jcarter@helpaccess.ca,vhale@helpaccess.ca,denied@helpaccess.ca",
        "INTERNAL_ONLY_MAILBOXES": "vhale@helpaccess.ca",
        "TENANT_ID": "t",
        "CLIENT_ID": "c",
        "CLIENT_SECRET": "s",
        "TOKEN_URL": f"http://127.0.0.1:{fake_port}/token",
        "GRAPH_BASE": f"http://127.0.0.1:{fake_port}/v1.0",
        "PYTHONPATH": str(ROOT),
        **extra_env,
    }
    p = subprocess.Popen([sys.executable, "-m", "app.server"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            if requests.get(f"http://127.0.0.1:{port}/healthz", timeout=1).text == "ok":
                return p, port, data
        except requests.RequestException:
            time.sleep(0.1)
        if p.poll() is not None:
            break
    out = p.stdout.read() if p.stdout else ""
    raise SystemExit(f"relay did not start:\n{out}")


class Client:
    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}/{SECRET}/mcp"
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        r = requests.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": PROTO},
            timeout=30,
        )
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:])
        return r.json()

    def call(self, name, args=None):
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        if "error" in res:  # protocol-level error
            return True, res["error"].get("message", str(res["error"]))
        r = res["result"]
        text = "".join(c.get("text", "") for c in r.get("content", []))
        if r.get("isError"):
            return True, text
        payload = r.get("structuredContent")
        if payload is None:
            payload = json.loads(text) if text else None
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        return False, payload


# ------------------------------------------------------------------ checks
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = ""):
    results.append((ok, label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if (detail and not ok) else ""))


PDF = b"%PDF-1.4\n" + (b"0123456789abcdef\n" * 460) + b"%%EOF\n"
PDF_B64 = base64.b64encode(PDF).decode()

relay, port, data_dir = start_relay({})
try:
    c = Client(port)
    init = c.rpc("initialize", {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
    check("result" in init, "initialize works", str(init)[:200])

    tools = c.rpc("tools/list")
    names = sorted(t["name"] for t in tools["result"]["tools"])
    check(names == ["delete_staged_attachment", "list_staged_attachments", "relay_status", "send_mail", "stage_attachment"], "exposes the 5 tools", str(names))

    check(requests.get(f"http://127.0.0.1:{port}/not-the-secret/mcp", timeout=5).status_code == 404 or
          requests.post(f"http://127.0.0.1:{port}/wrong/mcp", json={}, timeout=5).status_code == 404, "wrong path is rejected")

    err, st = c.call("relay_status")
    check(not err and st.get("microsoft_login") == "ok", "relay_status: Microsoft login ok", str(st))

    err, a = c.call("stage_attachment", {"filename": "Supplier_Report.pdf", "content_base64": PDF_B64, "expected_size_bytes": len(PDF)})
    check(not err and a and len(a["attachment_id"]) == 32 and a["size_bytes"] == len(PDF), f"stage a {len(PDF)}-byte PDF", str(a))
    att_id = a["attachment_id"] if not err else ""

    err, m = c.call("stage_attachment", {"filename": "x.pdf", "content_base64": PDF_B64, "expected_size_bytes": len(PDF) - 5})
    check(err and "truncated" in m, "truncated upload is detected", str(m))
    err, m = c.call("stage_attachment", {"filename": "run.exe", "content_base64": PDF_B64})
    check(err and "not allowed" in m, ".exe is refused", str(m))
    err, m = c.call("stage_attachment", {"filename": "fake.pdf", "content_base64": base64.b64encode(b"hello world").decode()})
    check(err and "bad header" in m, "non-PDF bytes named .pdf are refused", str(m))
    err, m = c.call("stage_attachment", {"filename": "x.pdf", "content_base64": PDF_B64[:100] + "\n" + PDF_B64[100:]})
    check(err and "base64" in m, "base64 with a line break is refused", str(m))
    err, m = c.call("stage_attachment", {"filename": "../../etc/passwd.pdf", "content_base64": PDF_B64})
    check(not err and m["filename"] == "passwd.pdf", "path in filename is stripped", str(m))
    c.call("delete_staged_attachment", {"attachment_id": m["attachment_id"]} if not err else {"attachment_id": "0" * 32})

    err, lst = c.call("list_staged_attachments")
    check(not err and [x["attachment_id"] for x in lst] == [att_id], "list shows exactly the staged file", str(lst))

    err, m = c.call("send_mail", {"mailbox": "mteskie@helpaccess.ca", "to": ["a@b.com"], "subject": "x", "body": "<p>x</p>"})
    check(err and "not allowed" in m, "mailbox outside the allow-list is refused", str(m))
    err, m = c.call("send_mail", {"mailbox": "vhale@helpaccess.ca", "to": ["supplier@example.com"], "subject": "x", "body": "<p>x</p>"})
    check(err and "only email @helpaccess.ca" in m, "internal-only mailbox cannot email outside", str(m))
    err, m = c.call("send_mail", {"mailbox": "jcarter@helpaccess.ca", "to": ["not-an-email"], "subject": "x", "body": "<p>x</p>"})
    check(err and "valid email" in m, "bad address is refused", str(m))
    err, m = c.call("send_mail", {"mailbox": "jcarter@helpaccess.ca", "to": ["a@b.com"], "subject": "x", "body": "<p>x</p>", "attachment_ids": ["f" * 32]})
    check(err and "not found" in m, "unknown attachment_id is refused", str(m))

    before = len(GRAPH_CALLS)
    err, sent = c.call(
        "send_mail",
        {
            "mailbox": "jcarter@helpaccess.ca",
            "to": ["supplier@example.com"],
            "cc": ["mteskie@helpaccess.ca"],
            "subject": "Prospect Suppliers Status\r\nBcc: evil@x.com",
            "body": "<p>Hello,</p><p>Report attached.</p>",
            "attachment_ids": [att_id],
        },
    )
    check(not err and sent["status"] == "accepted", "send with attachment is accepted", str(sent))
    call = GRAPH_CALLS[-1] if len(GRAPH_CALLS) > before else {}
    msg = call.get("json", {}).get("message", {})
    check(call.get("path", "").endswith("/users/jcarter%40helpaccess.ca/sendMail"), "Graph called as the shared mailbox", call.get("path", ""))
    check(call.get("auth") == "Bearer fake-token", "Graph call carries the bearer token")
    atts = msg.get("attachments", [])
    check(len(atts) == 1 and base64.b64decode(atts[0]["contentBytes"]) == PDF and atts[0]["name"] == "Supplier_Report.pdf"
          and atts[0]["@odata.type"] == "#microsoft.graph.fileAttachment" and atts[0]["contentType"] == "application/pdf",
          "attachment bytes arrive intact", str(atts)[:200])
    check("\n" not in msg.get("subject", "") and "\r" not in msg.get("subject", ""), "subject header injection is neutralised", msg.get("subject", ""))
    check(msg.get("ccRecipients", [{}])[0].get("emailAddress", {}).get("address") == "mteskie@helpaccess.ca" and call["json"].get("saveToSentItems") is True,
          "cc and save-to-sent-items set")
    err, lst = c.call("list_staged_attachments")
    check(not err and lst == [], "staged file is deleted after sending", str(lst))

    err, m = c.call("send_mail", {"mailbox": "jcarter@helpaccess.ca", "to": ["a@b.com"], "subject": "again", "body": "x", "attachment_ids": [att_id]})
    check(err and "not found" in m, "a sent attachment cannot be reused", str(m))

    err, m = c.call("send_mail", {"mailbox": "denied@helpaccess.ca", "to": ["a@b.com"], "subject": "x", "body": "<p>x</p>"})
    check(err and "ErrorAccessDenied" in m and "Test-ServicePrincipalAuthorization" in m, "Graph 403 gives a useful message", str(m))

    # plain mail, no attachment
    err, m = c.call("send_mail", {"mailbox": "jcarter@helpaccess.ca", "to": ["a@b.com"], "subject": "plain", "body": "hi", "body_type": "text"})
    check(not err and GRAPH_CALLS[-1]["json"]["message"]["body"]["contentType"] == "Text" and "attachments" not in GRAPH_CALLS[-1]["json"]["message"], "plain text mail without attachment")

    # secret never appears in the relay's output
    relay.terminate()
    out = relay.communicate(timeout=10)[0]
    check(SECRET not in out and "fake-token" not in out and '"s"' not in out, "secret URL and tokens are not logged")
finally:
    if relay.poll() is None:
        relay.terminate()

# ------------------------------------------------------------------ dry-run instance and bad credentials
relay2, port2, data2 = start_relay({"DRY_RUN": "1", "CLIENT_SECRET": ""})
try:
    c2 = Client(port2)
    c2.rpc("initialize", {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
    err, a2 = c2.call("stage_attachment", {"filename": "r.pdf", "content_base64": PDF_B64})
    n = len(GRAPH_CALLS)
    err, r = c2.call("send_mail", {"mailbox": "jcarter@helpaccess.ca", "to": ["a@b.com"], "subject": "dry", "body": "<p>x</p>", "attachment_ids": [a2["attachment_id"]]})
    check(not err and r["status"] == "dry_run" and len(GRAPH_CALLS) == n, "DRY_RUN validates but does not send", str(r))
    err, lst = c2.call("list_staged_attachments")
    check(not err and len(lst) == 1, "DRY_RUN keeps the staged file")
finally:
    relay2.terminate()

relay3, port3, _ = start_relay({"CLIENT_SECRET": "bad-secret"})
try:
    c3 = Client(port3)
    c3.rpc("initialize", {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
    err, st = c3.call("relay_status")
    check(not err and "FAILED" in st["microsoft_login"] and "AADSTS7000215" in st["microsoft_login"] and "bad-secret" not in json.dumps(st), "bad credentials are reported clearly, without leaking the secret", str(st))
finally:
    relay3.terminate()

# startup refuses weak config
p = subprocess.run([sys.executable, "-m", "app.server"], cwd=ROOT, capture_output=True, text=True, timeout=20,
                   env={**os.environ, "PYTHONPATH": str(ROOT), "MCP_PATH_SECRET": "short", "ALLOWED_MAILBOXES": "", "DRY_RUN": ""})
check(p.returncode != 0 and "at least 32" in (p.stderr + p.stdout), "startup refuses a short secret / empty allow-list", (p.stderr + p.stdout)[-200:])

passed = sum(ok for ok, _ in results)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
