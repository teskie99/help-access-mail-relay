"""Help Access Mail Relay.

A small remote MCP server that lets Claude send email AS a shared mailbox, with attachments,
through Microsoft Graph. Two-step flow:

    1. stage_attachment(...)  -> uploads a file, returns an attachment_id
    2. send_mail(...)         -> sends from an allow-listed mailbox, attaches the staged files,
                                 then deletes them

Everything is configured through environment variables (see .env.example).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import anyio
import requests
import uvicorn
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mail-relay")


class RelayError(Exception):
    """A failure we raise on purpose, with a message that is safe and useful to show to Claude.

    Anything else that goes wrong is treated as a crash: the MCP SDK hides its text from the client.
    """


async def _run(fn, *args):
    """Run blocking work off the event loop and pass RelayError messages through to Claude."""
    try:
        return await anyio.to_thread.run_sync(fn, *args)
    except RelayError as e:
        raise ToolError(str(e)) from None


# --------------------------------------------------------------------------- config


def _csv(name: str, default: str = "") -> list[str]:
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


TENANT_ID = os.environ.get("TENANT_ID", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
PATH_SECRET = os.environ.get("MCP_PATH_SECRET", "")
PORT = int(os.environ.get("PORT", "8000"))

ALLOWED_MAILBOXES = set(_csv("ALLOWED_MAILBOXES"))
INTERNAL_ONLY_MAILBOXES = set(_csv("INTERNAL_ONLY_MAILBOXES"))
INTERNAL_DOMAIN = os.environ.get("INTERNAL_DOMAIN", "helpaccess.ca").strip().lower()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STAGE_DIR = DATA_DIR / "staged"
STAGE_TTL_HOURS = float(os.environ.get("STAGE_TTL_HOURS", "24"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", "2000000"))
MAX_TOTAL_BYTES = int(os.environ.get("MAX_TOTAL_BYTES", "2500000"))
MAX_STAGED_FILES = int(os.environ.get("MAX_STAGED_FILES", "50"))
MAX_SENDS_PER_HOUR = int(os.environ.get("MAX_SENDS_PER_HOUR", "30"))
MAX_RECIPIENTS = 50
MAX_ATTACHMENTS_PER_MAIL = 10

ALLOWED_EXT = set(
    _csv(
        "ALLOWED_EXTENSIONS",
        ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.txt,.png,.jpg,.jpeg",
    )
)
DRY_RUN = _flag("DRY_RUN")

# Overridable so the test suite can point at a fake Microsoft.
GRAPH_BASE = os.environ.get("GRAPH_BASE", "https://graph.microsoft.com/v1.0").rstrip("/")
TOKEN_URL = os.environ.get(
    "TOKEN_URL", f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
)

EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")

MAGIC = {
    ".pdf": [b"%PDF-"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".docx": [b"PK\x03\x04"],
    ".xlsx": [b"PK\x03\x04"],
    ".pptx": [b"PK\x03\x04"],
}


def validate_config() -> None:
    problems = []
    if len(PATH_SECRET) < 32:
        problems.append("MCP_PATH_SECRET must be at least 32 characters (long and random).")
    if not ALLOWED_MAILBOXES:
        problems.append("ALLOWED_MAILBOXES is empty; list the mailboxes this relay may send from.")
    if not DRY_RUN:
        for name, value in (("TENANT_ID", TENANT_ID), ("CLIENT_ID", CLIENT_ID), ("CLIENT_SECRET", CLIENT_SECRET)):
            if not value:
                problems.append(f"{name} is required (or set DRY_RUN=1 to test without Microsoft).")
    if problems:
        raise SystemExit("Configuration problems:\n- " + "\n- ".join(problems))


# --------------------------------------------------------------------------- staging store

_lock = threading.Lock()


def _paths(att_id: str) -> tuple[Path, Path]:
    return STAGE_DIR / f"{att_id}.bin", STAGE_DIR / f"{att_id}.json"


def _safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(". ")
    if not name:
        raise RelayError("filename is empty.")
    return name[:120]


def _remove(att_id: str) -> None:
    for p in _paths(att_id):
        p.unlink(missing_ok=True)


def _stage_sync(filename: str, content_base64: str, expected: int | None) -> dict[str, Any]:
    name = _safe_filename(filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise RelayError(f"File type {ext or '(none)'} is not allowed. Allowed: {', '.join(sorted(ALLOWED_EXT))}.")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise RelayError(
            "content_base64 is not valid base64. It must be one unbroken standard base64 string "
            "(no spaces, line breaks or data: prefix). Generate it again and pass it unchanged."
        ) from None
    if not data:
        raise RelayError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise RelayError(f"File is {len(data)} bytes; the limit is {MAX_FILE_BYTES} bytes per file.")
    if expected is not None and expected != len(data):
        raise RelayError(
            f"Decoded file is {len(data)} bytes but expected_size_bytes was {expected}. "
            "The content was probably truncated or altered. Stage it again."
        )
    sigs = MAGIC.get(ext)
    if sigs and not any(data.startswith(s) for s in sigs):
        raise RelayError(f"The bytes do not look like a real {ext} file (bad header). Stage it again.")

    with _lock:
        STAGE_DIR.mkdir(parents=True, exist_ok=True)
        if len(list(STAGE_DIR.glob("*.json"))) >= MAX_STAGED_FILES:
            raise RelayError("Too many staged files waiting. Send or delete some first (list_staged_attachments).")
        att_id = uuid.uuid4().hex
        binp, metap = _paths(att_id)
        binp.write_bytes(data)
        meta = {
            "filename": name,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "created": time.time(),
        }
        metap.write_text(json.dumps(meta))
    log.info(json.dumps({"event": "staged", "attachment_id": att_id, "filename": name, "size_bytes": len(data)}))
    return {
        "attachment_id": att_id,
        "filename": name,
        "size_bytes": len(data),
        "expires_in_hours": STAGE_TTL_HOURS,
    }


def _load(att_id: str) -> tuple[dict[str, Any], bytes]:
    if not ID_RE.match(att_id):
        raise RelayError(f"'{att_id}' is not a valid attachment_id.")
    binp, metap = _paths(att_id)
    if not binp.exists() or not metap.exists():
        raise RelayError(f"Attachment {att_id} was not found. It may have expired or already been sent. Stage it again.")
    meta = json.loads(metap.read_text())
    data = binp.read_bytes()
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise RelayError(f"Attachment {att_id} failed its integrity check. Stage it again.")
    return meta, data


def _list_sync() -> list[dict[str, Any]]:
    out = []
    with _lock:
        for metap in sorted(STAGE_DIR.glob("*.json")):
            try:
                m = json.loads(metap.read_text())
            except ValueError:
                continue
            out.append(
                {
                    "attachment_id": metap.stem,
                    "filename": m["filename"],
                    "size_bytes": m["size_bytes"],
                    "age_minutes": int((time.time() - m["created"]) / 60),
                }
            )
    return out


def _delete_sync(att_id: str) -> dict[str, Any]:
    if not ID_RE.match(att_id):
        raise RelayError(f"'{att_id}' is not a valid attachment_id.")
    with _lock:
        existed = _paths(att_id)[1].exists()
        _remove(att_id)
    return {"deleted": existed, "attachment_id": att_id}


def _cleanup_once() -> int:
    cutoff = time.time() - STAGE_TTL_HOURS * 3600
    removed = 0
    with _lock:
        if not STAGE_DIR.exists():
            return 0
        for p in STAGE_DIR.iterdir():
            if p.suffix not in {".bin", ".json"}:
                continue
            try:
                created = json.loads(p.read_text())["created"] if p.suffix == ".json" else p.stat().st_mtime
            except (ValueError, KeyError, OSError):
                created = p.stat().st_mtime
            if created < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
    return removed


def _cleanup_loop() -> None:
    while True:
        time.sleep(600)
        try:
            n = _cleanup_once()
            if n:
                log.info(json.dumps({"event": "cleanup", "files_removed": n}))
        except Exception:  # noqa: BLE001 - never let the cleaner die
            log.exception("cleanup failed")


# --------------------------------------------------------------------------- Microsoft Graph

_token: dict[str, Any] = {"value": None, "exp": 0.0}
_token_lock = threading.Lock()


def _err_text(r: requests.Response) -> str:
    try:
        j = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    err = j.get("error")
    if isinstance(err, dict):  # Graph style
        return f"{err.get('code', 'error')}: {str(err.get('message', ''))[:300]}"
    if isinstance(err, str):  # token endpoint style
        return f"{err}: {str(j.get('error_description', '')).splitlines()[0][:300]}"
    return f"HTTP {r.status_code}"


def _get_token(force: bool = False) -> str:
    with _token_lock:
        if not force and _token["value"] and time.time() < _token["exp"] - 120:
            return _token["value"]
        try:
            r = requests.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
                timeout=20,
            )
        except requests.RequestException:
            raise RelayError("Could not reach Microsoft to sign in (network error). Try again in a minute.") from None
        if r.status_code != 200:
            raise RelayError(
                f"Could not get a Microsoft token ({_err_text(r)}). Check TENANT_ID, CLIENT_ID and CLIENT_SECRET."
            )
        j = r.json()
        _token["value"] = j["access_token"]
        _token["exp"] = time.time() + int(j.get("expires_in", 3600))
        return _token["value"]


def _graph_send(mailbox: str, message: dict[str, Any]) -> None:
    url = f"{GRAPH_BASE}/users/{quote(mailbox)}/sendMail"
    payload = {"message": message, "saveToSentItems": True}
    for attempt in (1, 2):
        token = _get_token(force=attempt == 2)
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=90)
        except requests.RequestException:
            raise RelayError(
                "Could not reach Microsoft to send (network error). The email may or may not have gone out: "
                "check the mailbox's Sent Items before retrying."
            ) from None
        if r.status_code == 202:
            return
        if r.status_code == 401 and attempt == 1:
            continue
        hint = ""
        if r.status_code == 403:
            hint = (
                " The app is not allowed to send as this mailbox. Check the Exchange role assignment "
                "and scope (Test-ServicePrincipalAuthorization), and allow up to 2 hours after changes."
            )
        raise RelayError(f"Microsoft rejected the send ({_err_text(r)}).{hint}")


_sends: deque[float] = deque()
_sends_lock = threading.Lock()


def _check_rate() -> None:
    with _sends_lock:
        now = time.time()
        while _sends and _sends[0] < now - 3600:
            _sends.popleft()
        if len(_sends) >= MAX_SENDS_PER_HOUR:
            raise RelayError(f"Send limit reached ({MAX_SENDS_PER_HOUR} per hour). Try again later.")
        _sends.append(now)


def _norm_recipients(label: str, items: list[str] | None) -> list[str]:
    out = []
    for a in items or []:
        a = a.strip()
        if not EMAIL_RE.match(a):
            raise RelayError(f"{label} address '{a}' is not a valid email address.")
        out.append(a)
    return out


def _send_sync(
    mailbox: str,
    to: list[str],
    subject: str,
    body: str,
    body_type: str,
    cc: list[str] | None,
    bcc: list[str] | None,
    attachment_ids: list[str] | None,
) -> dict[str, Any]:
    mb = mailbox.strip().lower()
    if mb not in ALLOWED_MAILBOXES:
        raise RelayError(f"Mailbox '{mailbox}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_MAILBOXES))}.")
    to_l, cc_l, bcc_l = _norm_recipients("To", to), _norm_recipients("Cc", cc), _norm_recipients("Bcc", bcc)
    if not to_l:
        raise RelayError("At least one 'to' recipient is required.")
    if len(to_l) + len(cc_l) + len(bcc_l) > MAX_RECIPIENTS:
        raise RelayError(f"Too many recipients (limit {MAX_RECIPIENTS}).")
    if mb in INTERNAL_ONLY_MAILBOXES:
        outside = [a for a in to_l + cc_l + bcc_l if not a.lower().endswith("@" + INTERNAL_DOMAIN)]
        if outside:
            raise RelayError(f"{mb} may only email @{INTERNAL_DOMAIN} addresses. Not allowed: {', '.join(outside)}.")
    subject = re.sub(r"[\r\n]+", " ", subject).strip()
    if not 1 <= len(subject) <= 255:
        raise RelayError("subject must be 1 to 255 characters.")
    if not body.strip() or len(body) > 500_000:
        raise RelayError("body must be non-empty and under 500,000 characters.")
    if body_type not in {"html", "text"}:
        raise RelayError("body_type must be 'html' or 'text'.")
    ids = list(dict.fromkeys(attachment_ids or []))
    if len(ids) > MAX_ATTACHMENTS_PER_MAIL:
        raise RelayError(f"At most {MAX_ATTACHMENTS_PER_MAIL} attachments per email.")

    graph_attachments, summary, total = [], [], 0
    for att_id in ids:
        meta, data = _load(att_id)
        total += len(data)
        graph_attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": meta["filename"],
                "contentType": meta["mime"],
                "contentBytes": base64.b64encode(data).decode("ascii"),
            }
        )
        summary.append({"filename": meta["filename"], "size_bytes": meta["size_bytes"]})
    if total > MAX_TOTAL_BYTES:
        raise RelayError(f"Attachments total {total} bytes; the limit is {MAX_TOTAL_BYTES} bytes per email.")

    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "HTML" if body_type == "html" else "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to_l],
    }
    if cc_l:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc_l]
    if bcc_l:
        message["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc_l]
    if graph_attachments:
        message["attachments"] = graph_attachments

    audit = {"mailbox": mb, "to": to_l, "cc": cc_l, "bcc": bcc_l, "subject": subject, "attachments": summary}
    if DRY_RUN:
        log.info(json.dumps({"event": "dry_run_send", **audit}))
        return {"status": "dry_run", "would_send_from": mb, "to": to_l, "attachments": summary,
                "note": "DRY_RUN is on: nothing was sent and the staged files were kept."}

    _check_rate()
    _graph_send(mb, message)
    with _lock:
        for att_id in ids:
            _remove(att_id)
    log.info(json.dumps({"event": "sent", **audit}))
    return {
        "status": "accepted",
        "sent_from": mb,
        "to": to_l,
        "attachments": summary,
        "note": "Microsoft 365 accepted the message for delivery and saved a copy in the mailbox's Sent Items. "
        "The staged attachments were deleted.",
    }


# --------------------------------------------------------------------------- MCP server

INSTRUCTIONS = (
    "Sends email from Help Access shared mailboxes, with attachments. "
    "To send a file: call stage_attachment once per file, then call send_mail with the returned attachment_ids. "
    "Do not add a signature or sign-off to the body; Exchange adds each mailbox's signature automatically. "
    "Send HTML bodies using <p>, <br>, <ul><li>."
)

mcp = MCPServer("help-access-mail-relay", instructions=INSTRUCTIONS)


@mcp.tool()
async def stage_attachment(
    filename: Annotated[str, Field(description="File name with extension, e.g. Supplier_Report.pdf")],
    content_base64: Annotated[
        str,
        Field(description="The file's bytes as ONE unbroken standard base64 string: no spaces, line breaks or data: prefix."),
    ],
    expected_size_bytes: Annotated[
        int | None,
        Field(description="The exact size of the original file in bytes, if you know it. Lets the relay detect truncated uploads."),
    ] = None,
) -> dict[str, Any]:
    """Upload a file so it can be attached to an email with send_mail. Returns an attachment_id.

    Staged files are deleted after they are sent, or after the expiry shown in the result.
    """
    return await _run(_stage_sync, filename, content_base64, expected_size_bytes)


@mcp.tool()
async def send_mail(
    mailbox: Annotated[str, Field(description="The mailbox to send AS, e.g. jcarter@helpaccess.ca. Must be on the relay's allowed list.")],
    to: Annotated[list[str], Field(description="Recipient email addresses.")],
    subject: Annotated[str, Field(description="Email subject line.")],
    body: Annotated[str, Field(description="Email body. Use simple HTML (<p>, <br>, <ul><li>). No signature or sign-off.")],
    body_type: Annotated[str, Field(description="'html' (default) or 'text'.")] = "html",
    cc: Annotated[list[str] | None, Field(description="Cc addresses.")] = None,
    bcc: Annotated[list[str] | None, Field(description="Bcc addresses.")] = None,
    attachment_ids: Annotated[list[str] | None, Field(description="attachment_ids returned by stage_attachment.")] = None,
) -> dict[str, Any]:
    """Send an email from a shared mailbox, optionally with staged attachments. Sends immediately; there is no draft step."""
    return await _run(
        _send_sync, mailbox, to, subject, body, body_type, cc, bcc, attachment_ids
    )


@mcp.tool()
async def list_staged_attachments() -> list[dict[str, Any]]:
    """List files that are staged and waiting to be attached."""
    return await _run(_list_sync)


@mcp.tool()
async def delete_staged_attachment(
    attachment_id: Annotated[str, Field(description="The attachment_id to remove.")],
) -> dict[str, Any]:
    """Delete a staged file without sending it."""
    return await _run(_delete_sync, attachment_id)


@mcp.tool()
async def relay_status() -> dict[str, Any]:
    """Check that the relay is up and can authenticate to Microsoft. Shows its limits. Sends nothing."""

    def _status() -> dict[str, Any]:
        if DRY_RUN:
            graph = "skipped (DRY_RUN is on)"
        else:
            try:
                _get_token()
                graph = "ok"
            except Exception as e:  # noqa: BLE001
                graph = f"FAILED: {e}"
        return {
            "dry_run": DRY_RUN,
            "microsoft_login": graph,
            "allowed_mailboxes": sorted(ALLOWED_MAILBOXES),
            "internal_only_mailboxes": sorted(INTERNAL_ONLY_MAILBOXES),
            "limits": {
                "max_file_bytes": MAX_FILE_BYTES,
                "max_total_bytes_per_email": MAX_TOTAL_BYTES,
                "max_sends_per_hour": MAX_SENDS_PER_HOUR,
                "staged_files_expire_after_hours": STAGE_TTL_HOURS,
                "allowed_file_types": sorted(ALLOWED_EXT),
            },
            "staged_files_waiting": len(_list_sync()),
        }

    return await _run(_status)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app():
    # The URL path contains a secret, so only callers who know the full URL reach the MCP endpoint.
    return mcp.streamable_http_app(
        streamable_http_path=f"/{PATH_SECRET}/mcp",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
        max_request_body_size=4 * 1024 * 1024,
    )


def main() -> None:
    validate_config()
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    log.info(
        json.dumps(
            {
                "event": "start",
                "dry_run": DRY_RUN,
                "allowed_mailboxes": sorted(ALLOWED_MAILBOXES),
                "internal_only": sorted(INTERNAL_ONLY_MAILBOXES),
                "port": PORT,
            }
        )
    )
    # access_log is off on purpose: request lines would put the secret URL path into the logs.
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
