"""MEMBER 2 - Agent Architect. Each agent = plain function (input dict -> output dict). No LangGraph in here,
so you can test in a notebook: `from agents import *`. Edit the PROMPTS and the LLM paths; fallbacks keep the demo alive."""
import json, re
from contracts import Action, SponsorUpdate, HIGH_CONSEQUENCE_TOOLS, inr
from context import KG, load
from llm import llm_json, llm_text
import tools

# ============================== PROMPTS (iterate on these!) ==============================
COMM_SYSTEM = """You are the Communication Agent for a college event team. Extract from the message:
{"sponsor_name": str, "new_amount": number (rupees, plain number), "deadline": str|null, "note": str (<=25 words)}
Return ONLY JSON."""

DRAFT_SYSTEM = """You are the Communication Agent. Write a short, warm, professional email to a sponsor acknowledging a revised
commitment and asking to renegotiate affected deliverables. Return ONLY JSON: {"subject": str, "body": str}"""

KNOW_SYSTEM = "You are the Knowledge Agent. Summarise the cascading impacts below in 3 crisp sentences for the organising team."

PLAN_SYSTEM = f"""You are the Planning Agent. Given a sponsor change and its impacts, output JSON {{"actions":[{{"tool":str,"args":object,"rationale":str}}]}}.
Allowed tools and args: update_sponsor_tracker(sponsor_id, committed, note) | update_budget(line_id, new_amount) |
update_deliverable(deliverable_id, status) | hold_approval(approval_id, reason) | send_email(to, subject, body) |
create_calendar_event(title, date, attendees). Cover EVERY impact. Return ONLY JSON."""


# ============================== helpers ==============================
_AMT = re.compile(r"(?:₹|\$|\bRs\.?|\bINR)\s?(\d[\d,]*(?:\.\d+)?)(?:\s?((?i:k|lakhs?|l))\b)?")
_OLD_CUE = re.compile(r"(from|instead of|rather than|previously|earlier|currently|was)\s*$", re.I)


def parse_amounts(t: str) -> list[float]:
    out = []
    for m in _AMT.finditer(t):
        v = float(m.group(1).replace(",", "")); u = (m.group(2) or "").lower()
        v *= 1e3 if u == "k" else 1e5 if u.startswith("l") else 1
        out.append(v)
    return out


def extract_new_amount(t: str):
    """The sponsor's NEW figure. 'from X to Y' -> Y, but also 'Y instead of X' -> Y."""
    found = []
    for m in _AMT.finditer(t):
        v = float(m.group(1).replace(",", "")); u = (m.group(2) or "").lower()
        v *= 1e3 if u == "k" else 1e5 if u.startswith("l") else 1
        is_old = bool(_OLD_CUE.search(t[max(0, m.start() - 20):m.start()]))
        found.append((v, is_old))
    if not found:
        return None
    fresh = [v for v, is_old in found if not is_old]
    return (fresh or [v for v, _ in found])[-1]


def direction(update: dict) -> str:
    return "up" if update["new_amount"] > update["old_amount"] else "down" if update["new_amount"] < update["old_amount"] else "same"


def parse_deadline(t: str):
    m = re.search(r"by\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2})", t)
    return m.group(1) if m else None


def resolve_sponsor(text: str):
    t = (text or "").lower()
    for s in load("sponsors"):
        if s["name"].lower() in t or s["name"].split()[0].lower() in t or s["email"].split("@")[1].split(".")[0] in t:
            return s
    return None


def action(id_, tool, args, why):
    return Action(id=id_, tool=tool, args=args, rationale=why, high_consequence=tool in HIGH_CONSEQUENCE_TOOLS).model_dump()


# ============================== 1. Communication Agent ==============================
def communication_parse(raw: str) -> dict:
    def fb():
        sp = resolve_sponsor(raw)
        return {"sponsor_name": sp["name"] if sp else "", "new_amount": extract_new_amount(raw),
                "deadline": parse_deadline(raw), "note": raw.strip().split("\n")[2][:160] if raw.count("\n") > 2 else raw[:160]}
    d = llm_json(COMM_SYSTEM, raw, fb)
    sp = resolve_sponsor(d.get("sponsor_name", "")) or resolve_sponsor(raw)
    if not sp or not d.get("new_amount"):
        raise ValueError("Could not identify sponsor or new amount in message")
    return SponsorUpdate(sponsor_id=sp["id"], sponsor_name=sp["name"], old_amount=KG.g.nodes[sp["id"]]["amount"],
                         new_amount=float(d["new_amount"]), deadline=d.get("deadline"), note=d.get("note", "")).model_dump()


def communication_draft(update: dict, impacts: list) -> dict:
    at_risk = [i["label"] for i in impacts if i["kind"] == "deliverable"]
    by = f" by {update['deadline']}" if update.get("deadline") else ""
    def fb():
        d = direction(update)
        if d == "up":
            return {"subject": "Re: Thank you for upgrading your sponsorship",
                    "body": (f"Hi,\n\nThank you for raising your commitment to {inr(update['new_amount'])}. We are delighted by the support.\n\n"
                             f"We would like to talk through additional benefits that fit the new level, such as title sponsorship perks{by}.\n\n"
                             f"Warm regards,\nEvent Organising Team")}
        if at_risk:
            ask = (f"With the updated amount, the following deliverables need to be revisited: {', '.join(at_risk)}. "
                   f"We have proposed adjusted benefits and would like to confirm them with you{by}.")
        else:
            ask = "All of the agreed deliverables can stay as planned."
        return {"subject": "Re: Update on our sponsorship - next steps",
                "body": (f"Hi,\n\nThank you for letting us know about the revised commitment of {inr(update['new_amount'])}. "
                         f"We understand and appreciate your continued support.\n\n{ask}\n\nWarm regards,\nEvent Organising Team")}
    return llm_json(DRAFT_SYSTEM, json.dumps({"update": update, "at_risk_deliverables": at_risk}), fb)


# ============================== 2. Knowledge Agent ==============================
def knowledge_analyse(update: dict) -> tuple[list, str]:
    impacts = KG.update_sponsor_commitment(update["sponsor_id"], update["new_amount"], update.get("note", ""))
    def fb():
        h = sum(i["severity"] == "high" for i in impacts)
        old, new = update["old_amount"], update["new_amount"]
        if direction(update) == "up":
            return (f"{update['sponsor_name']} raised its commitment from {inr(old)} to {inr(new)}. "
                    f"The extra {inr(new - old)} flows into {len(impacts) - 1} budget line(s). Nothing is put at risk, "
                    f"and the higher tier opens room for new sponsor benefits.")
        return (f"{update['sponsor_name']} cut its commitment from {inr(old)} to {inr(new)}. "
                f"This reaches {len(impacts)} items ({h} high severity) across budget lines, pending approvals and promised deliverables.")
    return impacts, llm_text(KNOW_SYSTEM, json.dumps(impacts), fb)


# ============================== 3. Specialized Domain Planning Agents ==============================
FINANCE_SYSTEM = """You are the Finance Agent (Treasury Desk). Analyze budget line impacts and output JSON:
{"summary": str, "rationale": str, "actions": [{"tool": "update_budget", "args": {"line_id": str, "new_amount": number}}]}"""

DELIV_SYSTEM = """You are the Deliverables Agent (Corporate Relations). Analyze deliverable impacts and output JSON:
{"summary": str, "rationale": str, "actions": [{"tool": "update_deliverable", "args": {"deliverable_id": str, "status": str}}]}"""

GOV_SYSTEM = """You are the Governance Agent (Executive Approvals). Analyze pending vendor approval risks and output JSON:
{"summary": str, "rationale": str, "actions": [{"tool": "hold_approval", "args": {"approval_id": str, "reason": str}}]}"""


def finance_propose(update: dict, impacts: list) -> dict:
    """Finance Agent: inspects budget lines and determines required funding adjustments."""
    b_impacts = [i for i in impacts if i["kind"] == "budget_line"]
    def fb():
        diff = update["new_amount"] - update["old_amount"]
        act_list = [action(f"budget-{i['node_id']}", "update_budget", {"line_id": i["node_id"], "new_amount": i["after"]}, i["detail"]) for i in b_impacts]
        if diff < 0:
            summary = (f"{inr(-diff)} less funding across {len(b_impacts)} budget line(s). "
                       f"Reduce each line in proportion to this sponsor's share so the accounts stay balanced.")
        elif diff > 0:
            summary = (f"{inr(diff)} more funding across {len(b_impacts)} budget line(s). "
                       f"Raise each line in proportion to this sponsor's share.")
        else:
            summary = "No change in funding. Budget lines stay as they are."
        return {"agent": "Finance Agent", "desk": "Finance", "summary": summary, "actions": act_list}
    return fb()


def deliverables_propose(update: dict, impacts: list) -> dict:
    """Deliverables Agent: evaluates sponsor entitlements and determines benefits renegotiation."""
    d_impacts = [i for i in impacts if i["kind"] == "deliverable"]
    def fb():
        act_list = [action(f"deliv-{i['node_id']}", "update_deliverable", {"deliverable_id": i["node_id"], "status": "needs_renegotiation"}, i["detail"]) for i in d_impacts]
        if d_impacts:
            summary = (f"The new amount of {inr(update['new_amount'])} no longer covers {len(d_impacts)} promised deliverable(s). "
                       f"Flag them for renegotiation and keep the rest as agreed.")
        elif direction(update) == "up":
            summary = "Every promised deliverable is still covered. The higher amount can be used to offer extra benefits."
        else:
            summary = "Every promised deliverable is still covered at the new amount."
        return {"agent": "Deliverables Agent", "desk": "Sponsor deliverables", "summary": summary, "actions": act_list}
    return fb()


def governance_propose(update: dict, impacts: list) -> dict:
    """Governance Agent: scans pending vendor requisitions and determines emergency disbursement holds."""
    a_impacts = [i for i in impacts if i["kind"] == "approval"]
    def fb():
        act_list = [action(f"hold-{i['node_id']}", "hold_approval", {"approval_id": i["node_id"], "reason": "Sponsor funding reduced"}, i["detail"]) for i in a_impacts]
        if a_impacts:
            summary = (f"{len(a_impacts)} pending payment(s) now draw on reduced budget lines. "
                       f"Hold them until the committee re-approves.")
        else:
            summary = "No pending payments are affected."
        return {"agent": "Governance Agent", "desk": "Approvals", "summary": summary, "actions": act_list}
    return fb()


# ============================== 4. Master Planning & Synthesis Agent ==============================
def plan_deterministic(update, impacts, draft) -> list[dict]:
    sp_email = KG.g.nodes[update["sponsor_id"]]["email"]
    f_prop = finance_propose(update, impacts)
    d_prop = deliverables_propose(update, impacts)
    g_prop = governance_propose(update, impacts)

    A = [action("track", "update_sponsor_tracker",
                {"sponsor_id": update["sponsor_id"], "committed": update["new_amount"], "note": "Revised by sponsor email"},
                "Keep the sponsorship tracker as the source of truth")]
    A.extend(f_prop["actions"])
    A.extend(d_prop["actions"])
    A.extend(g_prop["actions"])
    A.append(action("email-sponsor", "send_email", {"to": sp_email, **draft}, "Acknowledge change and renegotiate deliverables"))
    A.append(action("cal-renegotiate", "create_calendar_event",
                    {"title": f"Renegotiate deliverables with {update['sponsor_name']}", "date": update.get("deadline") or "TBD",
                     "attendees": ["organizers", sp_email]}, "Track the sponsor's response deadline"))
    return A


def planning_make(update, impacts, draft) -> list[dict]:
    def fb(): return {"_fallback": True}
    out = llm_json(PLAN_SYSTEM, json.dumps({"update": update, "impacts": impacts}), fb)
    try:
        if out.get("_fallback"): raise ValueError
        plan = []
        for n, a in enumerate(out["actions"]):
            assert a["tool"] in tools.REGISTRY and isinstance(a["args"], dict)
            plan.append(action(f"{a['tool']}-{n}", a["tool"], a["args"], a.get("rationale", "")))
        return plan
    except Exception:
        return plan_deterministic(update, impacts, draft)


def planning_replan(plan: list, failed: list, attempt: int) -> list[dict]:
    """Adaptive replanning: 1st replan = retry the failed step; later = escalate to a human task."""
    out = []
    for a in plan:
        if a["id"] in failed and attempt >= 2:
            out.append(action(a["id"] + "-manual", "create_task",
                              {"title": f"Do by hand: {a['tool'].replace('_', ' ')}", "detail": json.dumps(a["args"])},
                              f"Automation failed repeatedly for {a['tool']}; escalating to a human"))
        else:
            out.append(a)
    return out


# ============================== 5. Execution Agent ==============================
def execution_run(plan: list, approved: list, results: dict) -> dict:
    new = {}
    for a in plan:
        if results.get(a["id"], {}).get("status") == "ok":
            continue                                           # already done (idempotent re-entry)
        if a["high_consequence"] and a["id"] not in approved:
            new[a["id"]] = {"status": "skipped", "detail": "not approved by human"}
            continue
        try:
            out = tools.REGISTRY[a["tool"]](**a["args"])
            new[a["id"]] = {"status": "ok", "detail": out}
        except Exception as e:                                 # ToolFailure, bad args, anything
            new[a["id"]] = {"status": "failed", "detail": str(e)}
    return new


# ============================== 5. Verification Agent ==============================
def verification_check(plan: list, results: dict) -> tuple[list, list]:
    failed, checks = [], []
    for a in plan:
        r = results.get(a["id"], {})
        if r.get("status") == "skipped":
            continue
        ok = False
        if r.get("status") == "ok":
            try: ok = bool(tools.VERIFIERS[a["tool"]](a["args"]))   # read real state, don't trust the tool's claim
            except Exception: ok = False
        checks.append({"id": a["id"], "tool": a["tool"], "passed": ok})
        if not ok: failed.append(a["id"])
    return failed, checks
