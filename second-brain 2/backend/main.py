"""FastAPI server: python -m uvicorn main:app --reload   (run from /backend)"""
import json
import threading
import uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langgraph.types import Command

import context, tools, llm, agents, gmail_watcher
from graph import graph, describe_flow

app = FastAPI(title="Second Brain - Agentic Workflow Automation")
FRONTEND = Path(__file__).parent.parent / "frontend" / "index.html"
DASHBOARD = Path(__file__).parent.parent / "frontend" / "dashboard.html"

# ---- live-email inbox (in-memory, persists for the session) ----
# Each entry: {id, from_, subject, body, processed, thread_id, auto_applied, high_impact, snap}
LIVE_INBOX: list[dict] = []
MAX_INBOX = 50
_pipeline_lock = threading.Lock()   # watcher thread and manual requests share the mock backends


class RunReq(BaseModel):
    message: Optional[str] = None
    simulate_failure: bool = False
    auto_reset: bool = True

class LiveEmailReq(BaseModel):
    from_: str         # sender address
    subject: str
    body: str

class GmailConnectReq(BaseModel):
    address: str
    app_password: str
    poll_interval: Optional[int] = None


def cfg(tid): return {"configurable": {"thread_id": tid}}


def overview():
    return {
        "graph": context.KG.to_json(),
        "store": tools.STORE,
        "llm_enabled": llm.enabled(),
        "scenarios": tools.STORE.get("emails", [])
    }


def snapshot(tid):
    snap = graph.get_state(cfg(tid))
    v = snap.values
    pending = None
    for t in snap.tasks:
        if t.interrupts:
            pending = t.interrupts[0].value
    return {
        "thread_id": tid,
        "status": "awaiting_approval" if snap.next else "done",
        "pending": pending,
        "trace": v.get("trace", []),
        "path": v.get("path", []),
        "waiting_at": list(snap.next),
        "update": v.get("update"),
        "impacts": v.get("impacts", []),
        "plan": v.get("plan", []),
        "results": v.get("results", {}),
        "deliberations": v.get("deliberations", []),
        "report": v.get("final_report"),
        "checks": v.get("checks", []),
        "summary": v.get("knowledge_summary"),
        **overview()
    }


# ============================================================
# AUTOMATIC EXECUTION
# Every email is carried all the way through by itself: the policy gate still
# marks high-consequence actions, but the server signs them off immediately.
# ============================================================

def _pending_ids(snap: dict) -> list[str]:
    return [a["id"] for a in ((snap.get("pending") or {}).get("actions", []))]


def _resume(tid: str, ids: list[str]) -> dict:
    for chunk in graph.stream(Command(resume={"approved_ids": ids, "auto": True}), cfg(tid), stream_mode="updates"):
        pass
    return snapshot(tid)


def _auto_finish(tid: str) -> dict:
    """Resume any approval pauses automatically until the run is complete."""
    snap = snapshot(tid)
    guard = 0
    while snap.get("status") == "awaiting_approval" and guard < 5:
        snap = _resume(tid, _pending_ids(snap))
        guard += 1
    return snap


def _run_message(msg: str, reset: bool = True) -> tuple[str, dict]:
    """Run the whole graph for one message and return (thread_id, final snapshot)."""
    if reset:
        context.KG.reset()
        tools.reset_store()
    tid = uuid.uuid4().hex[:8]
    graph.invoke({"raw_message": msg}, cfg(tid))
    return tid, _auto_finish(tid)


def _has_high_impact(impacts: list) -> bool:
    return any(i.get("severity") == "high" for i in impacts)


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/dashboard")
def dashboard():
    return FileResponse(DASHBOARD)


@app.get("/api/overview")
def api_overview():
    default_body = tools.STORE["emails"][0]["body"] if tools.STORE.get("emails") else ""
    return {**overview(), "scenario": default_body}


@app.get("/api/flow")
def api_flow():
    return describe_flow()


@app.get("/api/scenarios")
def api_scenarios():
    return tools.STORE.get("emails", [])


@app.post("/api/run")
def api_run(req: RunReq):
    if req.auto_reset:
        context.KG.reset()
        tools.reset_store()
    tid = uuid.uuid4().hex[:8]
    if req.simulate_failure:
        tools.CHAOS["update_sponsor_tracker"] = 2          # set to 4 to see the manual-task fallback
    msg = req.message or tools.STORE["emails"][0]["body"]
    graph.invoke({"raw_message": msg}, cfg(tid))
    return _auto_finish(tid)


@app.post("/api/stream_run")
def api_stream_run(req: RunReq):
    if req.auto_reset:
        context.KG.reset()
        tools.reset_store()
    tid = uuid.uuid4().hex[:8]
    if req.simulate_failure:
        tools.CHAOS["update_sponsor_tracker"] = 2
    msg = req.message or tools.STORE["emails"][0]["body"]

    def event_generator():
        # Stream intermediate graph steps
        for chunk in graph.stream({"raw_message": msg}, cfg(tid), stream_mode="updates"):
            for node in chunk.keys():
                yield json.dumps({"event": "step", "node": node, "thread_id": tid, "snapshot": snapshot(tid)}) + "\n"

        # No review step: sign off the policy gate and keep going, streaming the rest.
        snap = snapshot(tid)
        guard = 0
        while snap.get("status") == "awaiting_approval" and guard < 5:
            for chunk in graph.stream(Command(resume={"approved_ids": _pending_ids(snap), "auto": True}),
                                      cfg(tid), stream_mode="updates"):
                for node in chunk.keys():
                    yield json.dumps({"event": "step", "node": node, "thread_id": tid,
                                      "snapshot": snapshot(tid)}) + "\n"
            snap = snapshot(tid)
            guard += 1

        yield json.dumps({"event": "complete", "thread_id": tid, "snapshot": snapshot(tid)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.post("/api/reset")
def api_reset():
    context.KG.reset()
    tools.reset_store()
    LIVE_INBOX.clear()
    return api_overview()


# ============================================================
# LIVE EMAIL INBOX  – every fetched message is executed automatically
# ============================================================

def _process_live_email(m: dict) -> dict:
    """Fetch → think → execute for one email. Shared by the Gmail watcher and manual injection."""
    eid = uuid.uuid4().hex[:8]
    entry: dict = {
        "id": eid,
        "from_": m.get("from_", ""),
        "subject": m.get("subject", ""),
        "body": m.get("body", ""),
        "processed": False,
        "auto_applied": False,
        "high_impact": False,
        "thread_id": None,
        "snap": None,
    }
    LIVE_INBOX.append(entry)
    del LIVE_INBOX[:-MAX_INBOX]

    try:
        with _pipeline_lock:
            tid, snap = _run_message(entry["body"])
        entry["thread_id"] = tid
        entry["snap"] = snap
        entry["processed"] = True
        entry["high_impact"] = _has_high_impact(snap.get("impacts", []))
        entry["auto_applied"] = True
    except Exception as e:
        entry["processed"] = True
        entry["error"] = str(e)

    return entry


# The watcher thread hands each new email to the same automatic pipeline.
gmail_watcher.set_process_callback(_process_live_email)


@app.get("/api/live_inbox")
def api_live_inbox():
    """Return the current live-inbox entries (newest first)."""
    return list(reversed(LIVE_INBOX))


@app.post("/api/live_inbox")
def api_send_live_email(req: LiveEmailReq):
    """Inject an email manually (useful for testing without a mailbox connection)."""
    return _process_live_email({"from_": req.from_, "subject": req.subject, "body": req.body})


@app.get("/api/live_inbox/{email_id}")
def api_live_inbox_detail(email_id: str):
    entry = next((e for e in LIVE_INBOX if e["id"] == email_id), None)
    if not entry:
        raise HTTPException(404, "Email not found")
    return entry


# ============================================================
# MAILBOX CONNECTION  – Gmail IMAP with an App Password
# ============================================================

@app.get("/api/gmail/status")
def api_gmail_status():
    return gmail_watcher.status()


@app.post("/api/gmail/connect")
def api_gmail_connect(req: GmailConnectReq):
    result = gmail_watcher.start(req.address, req.app_password, req.poll_interval)
    return {**result, "status": gmail_watcher.status()}


@app.post("/api/gmail/disconnect")
def api_gmail_disconnect():
    result = gmail_watcher.stop()
    return {**result, "status": gmail_watcher.status()}
