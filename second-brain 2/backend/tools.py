"""MEMBER 4 - Tooling Lead. Mock integrations + reliability layer (retries, idempotency, failure injection)."""
import functools, hashlib, json, time
from context import load

class ToolFailure(Exception): ...

STORE: dict = {}        # fake "email / sheets / calendar / docs" backends
IDEMP: dict = {}        # idempotency cache: key -> result
CHAOS: dict = {}        # {tool_name: n} -> next n attempts of that tool fail (failure injection for demos)
REGISTRY: dict = {}     # tool name -> callable
VERIFIERS: dict = {}    # tool name -> fn(args) -> bool  (reads REAL state, not tool's claim)


def reset_store():
    STORE.clear()
    STORE.update(
        sponsors=load("sponsors"), budget=load("budget"), deliverables=load("deliverables"),
        approvals=load("approvals"), emails=load("emails"), calendar=load("calendar"),
        tracker=[dict(sponsor_id=s["id"], name=s["name"], committed=s["committed"], status="confirmed", note="") for s in load("sponsors")],
        outbox=[], tasks=[],
    )
    IDEMP.clear(); CHAOS.clear()


def tool(retries: int = 2, idempotent: bool = True, verifier=None):
    """Decorator: idempotency key from (tool,args), bounded retries with backoff, failure injection."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(**kwargs):
            key = hashlib.sha256((fn.__name__ + json.dumps(kwargs, sort_keys=True, default=str)).encode()).hexdigest()[:16]
            if idempotent and key in IDEMP:
                return {**IDEMP[key], "deduped": True}
            last = None
            for attempt in range(1, retries + 1):
                try:
                    if CHAOS.get(fn.__name__, 0) > 0:
                        CHAOS[fn.__name__] -= 1
                        raise ToolFailure("simulated outage (503)")
                    out = fn(**kwargs)
                    if idempotent: IDEMP[key] = out
                    return out
                except ToolFailure as e:
                    last = e; time.sleep(0.15 * attempt)
            raise ToolFailure(f"{fn.__name__} failed after {retries} attempts: {last}")
        REGISTRY[fn.__name__] = wrapper
        if verifier: VERIFIERS[fn.__name__] = verifier
        return wrapper
    return deco


def _find(coll, id_): return next(x for x in STORE[coll] if x["id"] == id_)

# ---------------------------------- tools ----------------------------------
@tool(verifier=lambda a: next(r for r in STORE["tracker"] if r["sponsor_id"] == a["sponsor_id"])["committed"] == a["committed"])
def update_sponsor_tracker(sponsor_id: str, committed: float, note: str = ""):
    row = next(r for r in STORE["tracker"] if r["sponsor_id"] == sponsor_id)
    row.setdefault("was", row["committed"])
    row.update(committed=committed, status="revised", note=note)
    return {"ok": True}

@tool(verifier=lambda a: _find("budget", a["line_id"])["amount"] == a["new_amount"])
def update_budget(line_id: str, new_amount: float):
    line = _find("budget", line_id)
    line.setdefault("was", line["amount"])
    line["amount"] = new_amount
    return {"ok": True}

@tool(verifier=lambda a: _find("deliverables", a["deliverable_id"])["status"] == a["status"])
def update_deliverable(deliverable_id: str, status: str):
    _find("deliverables", deliverable_id)["status"] = status
    return {"ok": True}

@tool(verifier=lambda a: _find("approvals", a["approval_id"])["status"] == "on_hold")
def hold_approval(approval_id: str, reason: str = ""):
    ap = _find("approvals", approval_id); ap["status"] = "on_hold"; ap["reason"] = reason
    return {"ok": True}

@tool(verifier=lambda a: any(m["to"] == a["to"] and m["subject"] == a["subject"] for m in STORE["outbox"]))
def send_email(to: str, subject: str, body: str):
    STORE["outbox"].append(dict(to=to, subject=subject, body=body, sent_at=time.strftime("%H:%M:%S")))
    return {"ok": True}

@tool(verifier=lambda a: any(c["title"] == a["title"] for c in STORE["calendar"]))
def create_calendar_event(title: str, date: str = "TBD", attendees: list | None = None):
    STORE["calendar"].append(dict(id=f"c{len(STORE['calendar'])+1}", title=title, date=date, attendees=attendees or []))
    return {"ok": True}

@tool(verifier=lambda a: any(t["title"] == a["title"] for t in STORE["tasks"]))
def create_task(title: str, detail: str = ""):          # fallback used when automation fails repeatedly
    STORE["tasks"].append(dict(title=title, detail=detail, owner="human"))
    return {"ok": True}


reset_store()
