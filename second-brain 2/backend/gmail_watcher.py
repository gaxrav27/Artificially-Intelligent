"""
Gmail live-email watcher using IMAP + App Password (no extra packages needed).

Setup (one-time, 2 min):
  1. Go to https://myaccount.google.com/apppasswords
  2. Create an App Password for "Mail" / "Mac" (or any device)
  3. Set environment variables before starting the server:
       export GMAIL_ADDRESS=artemisgaurav@gmail.com
       export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"

The watcher polls every POLL_INTERVAL seconds for new UNSEEN emails.
Each new email is run through the full agent pipeline:
  - No high-impact changes → auto-applied, no human needed.
  - High-impact changes → stored for human approval via the Live Inbox UI.
"""

import email
import email.header
import imaplib
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Callable

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL = 20       # seconds between checks
MAX_PER_POLL = 10        # don't let a huge unseen backlog block the polling loop
NET_TIMEOUT = 10         # seconds per address before reporting a network error

# ── State ──────────────────────────────────────────────────────────────────────
_state: dict = {
    "running": False,
    "authenticated": False,
    "address": "",
    "error": "",
    "last_checked": None,
    "emails_fetched": 0,
    "poll_interval": POLL_INTERVAL,
    "thread": None,
}

# Callback set by main.py so the watcher can push emails into the pipeline
_process_callback: Callable | None = None


def set_process_callback(fn: Callable):
    """Register the function that processes an incoming email dict."""
    global _process_callback
    _process_callback = fn


def status() -> dict:
    return {
        "running": _state["running"],
        "authenticated": _state["authenticated"],
        "address": _state["address"],
        "error": _state["error"],
        "last_checked": _state["last_checked"],
        "emails_fetched": _state["emails_fetched"],
        "poll_interval": _state["poll_interval"],
    }


# ── IMAP helpers ───────────────────────────────────────────────────────────────

def _decode_header(raw) -> str:
    parts = email.header.decode_header(raw or "")
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def _get_body(msg: email.message.Message) -> str:
    """Extract plaintext body from a (possibly multipart) message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace") if payload else ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""
    return ""


def _fetch_unseen(imap: imaplib.IMAP4_SSL) -> list[dict]:
    """Return a list of {from_, subject, body} for the newest UNSEEN messages."""
    imap.select("INBOX")
    _, data = imap.search(None, "UNSEEN")
    uids = data[0].split()
    results = []

    # Oldest first, but cap the batch so the pipeline stays responsive.
    if len(uids) > MAX_PER_POLL:
        uids = uids[-MAX_PER_POLL:]
    for uid in uids:
        _, msg_data = imap.fetch(uid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        from_ = _decode_header(msg.get("From", ""))
        subject = _decode_header(msg.get("Subject", ""))
        body = _get_body(msg)
        results.append({"from_": from_, "subject": subject, "body": body})
        # Mark as seen
        imap.store(uid, "+FLAGS", "\\Seen")
    return results


# ── Polling loop ───────────────────────────────────────────────────────────────

def _poll_loop(address: str, app_password: str):
    _state["running"] = True
    _state["error"] = ""
    print(f"[gmail] Watcher started for {address}, polling every {_state['poll_interval']}s")

    while _state["running"]:
        try:
            with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=NET_TIMEOUT) as imap:
                imap.login(address, app_password)
                _state["authenticated"] = True
                _state["error"] = ""

                while _state["running"]:
                    try:
                        msgs = _fetch_unseen(imap)
                        _state["last_checked"] = datetime.now().strftime("%H:%M:%S")
                        for m in msgs:
                            _state["emails_fetched"] += 1
                            print(f"[gmail] New email from {m['from_']}: {m['subject']}")
                            if _process_callback:
                                try:
                                    _process_callback(m)
                                except Exception as e:
                                    print(f"[gmail] Pipeline error: {e}")
                        # sleep in small slices so Stop reacts within a second or two
                        for _ in range(int(_state["poll_interval"] * 2)):
                            if not _state["running"]:
                                break
                            time.sleep(0.5)
                    except imaplib.IMAP4.abort:
                        # Connection dropped — reconnect
                        break
        except imaplib.IMAP4.error as e:
            _state["authenticated"] = False
            _state["running"] = False
            _state["error"] = f"Gmail sign-in failed: {e}. Use an App Password, not your normal password."
            print(f"[gmail] {_state['error']}")
        except OSError as e:
            _state["authenticated"] = False
            _state["error"] = f"Could not reach Gmail: {e}"
            print(f"[gmail] {_state['error']}")
            time.sleep(5)

    _state["running"] = False
    _state["authenticated"] = False
    print("[gmail] Watcher stopped.")


# ── Public API ─────────────────────────────────────────────────────────────────

def start(address: str | None = None, app_password: str | None = None,
          poll_interval: int | None = None) -> dict:
    """Start the background watcher thread. Uses env vars if args not given."""
    if _state["running"]:
        return {"ok": False, "error": "Already watching this mailbox"}

    addr = (address or os.getenv("GMAIL_ADDRESS", "")).strip()
    pwd  = (app_password or os.getenv("GMAIL_APP_PASSWORD", "")).strip()

    if not addr or not pwd:
        return {"ok": False, "error": "Enter an email address and an app password."}
    if "@" not in addr:
        return {"ok": False, "error": "That email address looks incomplete."}

    _state["address"] = addr
    _state["authenticated"] = False
    _state["emails_fetched"] = 0
    _state["error"] = ""
    _state["last_checked"] = None
    _state["poll_interval"] = max(5, min(int(poll_interval or POLL_INTERVAL), 600))

    t = threading.Thread(target=_poll_loop, args=(addr, pwd), daemon=True)
    _state["thread"] = t
    t.start()
    return {"ok": True, "address": addr, "poll_interval": _state["poll_interval"]}


def stop() -> dict:
    if not _state["running"]:
        _state["authenticated"] = False
        return {"ok": False, "error": "Not running"}
    _state["running"] = False
    return {"ok": True}
