"""
THE LANGGRAPH PART - READ THIS FIRST
====================================

What the workflow looks like (each box is one NODE, each arrow is an EDGE):

    coordinator -> communication -> knowledge -> planning -> approval -> execution -> verification -> report
                        |                           ^                                      |
                        | (message not understood)  |______ some check failed, <3 tries ___|
                        '--------------------------------------------------------------> report

Four ideas are all you need:

1. STATE   One shared dictionary (see State in contracts.py). Every node receives it and returns ONLY the keys it
           wants to change; LangGraph merges that into the state. Keys declared with a reducer (trace, path, results)
           are merged/appended instead of overwritten.
2. NODE    A plain Python function: node(state) -> dict of changes. Our nodes are thin glue: call one agent from
           agents.py, store its answer in the state, add a line to the trace. The intelligence lives in agents.py.
3. EDGE    A fixed arrow (add_edge) or a decision point (add_conditional_edges). A decision point calls a small
           function that looks at the state and returns a LABEL; the label is mapped to the next node (see BRANCHES).
4. PAUSE   The approval node calls interrupt(). LangGraph freezes the run and saves the state under a thread_id
           (that is what the checkpointer is for). main.py resumes it with Command(resume=...) as soon as it pauses,
           so the website runs every email hands-free.
           Note: on resume the approval node runs again from its first line, so code before interrupt() must be safe
           to repeat.

To see the graph LangGraph actually built:   python graph.py     (prints a Mermaid diagram)
Paste the output into https://mermaid.live
"""
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

import agents
from contracts import State, log, inr

MAX_PASSES = 3     # how many execute->verify rounds before we give up and report


# =====================================================================================================
# NODES   (each one: read some state -> call an agent -> return the changes)
# =====================================================================================================
def coordinator(s: State):
    """Start of a run: reset the counters. Also snapshots the KG so rejection can undo mutations."""
    agents.KG.save_snapshot()   # <-- must happen before knowledge_analyse mutates the graph
    return {"path": ["coordinator"], "approved_ids": [], "rejected_ids": [], "results": {}, "failed": [], "attempt": 0,
            "deliberations": [], "trace": [log("Coordinator", "New event received. Starting the workflow.")]}


def communication(s: State):
    """Turn the raw message into a structured SponsorUpdate (or fail cleanly)."""
    try:
        u = agents.communication_parse(s["raw_message"])
    except Exception as e:
        return {"path": ["communication"], "update": None,
                "trace": [log("Communication", f"Could not parse message: {e}", "error")]}
    dl = f" (deadline {u['deadline']})" if u["deadline"] else ""
    return {"path": ["communication"], "update": u,
            "trace": [log("Communication", f"Understood: {u['sponsor_name']} changed {inr(u['old_amount'])} to {inr(u['new_amount'])}{dl}")]}


def knowledge(s: State):
    """Update the knowledge graph and find everything the change touches. Also drafts the sponsor email."""
    impacts, summary = agents.knowledge_analyse(s["update"])
    draft = agents.communication_draft(s["update"], impacts)
    return {"path": ["knowledge"], "impacts": impacts, "knowledge_summary": summary, "draft": draft,
            "trace": [log("Knowledge", f"{len(impacts)} things are affected by this change"),
                      log("Knowledge", summary),
                      log("Communication", "Drafted the reply email to the sponsor")]}


def finance_agent(s: State):
    """Finance Agent: evaluates funding gap and proposes budget reallocations."""
    prop = agents.finance_propose(s["update"], s["impacts"])
    return {"path": ["finance_agent"], "finance_proposal": prop, "deliberations": [prop],
            "trace": [log("Finance", prop["summary"])]}


def deliverables_agent(s: State):
    """Deliverables Agent: evaluates contractual deliverables and proposes renegotiations."""
    prop = agents.deliverables_propose(s["update"], s["impacts"])
    return {"path": ["deliverables_agent"], "deliverables_proposal": prop, "deliberations": [prop],
            "trace": [log("Deliverables", prop["summary"])]}


def governance_agent(s: State):
    """Governance Agent: evaluates pending vendor payments and proposes holds."""
    prop = agents.governance_propose(s["update"], s["impacts"])
    return {"path": ["governance_agent"], "governance_proposal": prop, "deliberations": [prop],
            "trace": [log("Governance", prop["summary"])]}


def planning(s: State):
    """Master Planning Agent: gathers proposals from Finance, Deliverables, Governance, and Communication agents to synthesize the finalized action plan.
    Visits after a failed check: repair the plan instead."""
    if s.get("failed"):
        plan = agents.planning_replan(s["plan"], s["failed"], s["attempt"])
        what = "trying them again" if s["attempt"] < 2 else "handing them to a human as manual tasks"
        return {"path": ["planning"], "plan": plan, "failed": [],
                "trace": [log("Planning", f"{len(s['failed'])} step(s) failed. Replanning: {what}", "warn")]}

    # First visit: Synthesize from specialized domain proposals
    sp_email = agents.KG.g.nodes[s["update"]["sponsor_id"]]["email"]
    A = [agents.action("track", "update_sponsor_tracker",
                       {"sponsor_id": s["update"]["sponsor_id"], "committed": s["update"]["new_amount"], "note": "Revised by sponsor email"},
                       "Keep the sponsorship tracker as the source of truth")]
    if s.get("finance_proposal"):
        A.extend(s["finance_proposal"].get("actions", []))
    if s.get("deliverables_proposal"):
        A.extend(s["deliverables_proposal"].get("actions", []))
    if s.get("governance_proposal"):
        A.extend(s["governance_proposal"].get("actions", []))
    up = agents.direction(s["update"]) == "up"
    A.append(agents.action("email-sponsor", "send_email", {"to": sp_email, **s["draft"]},
                           "Thank the sponsor and open talks on extra benefits" if up else "Acknowledge the change and settle the deliverables"))
    A.append(agents.action("cal-renegotiate", "create_calendar_event",
                           {"title": f"{'Discuss added benefits' if up else 'Renegotiate deliverables'} with {s['update']['sponsor_name']}",
                            "date": s["update"].get("deadline") or "TBD", "attendees": ["organizers", sp_email]},
                           "Track the sponsor's response deadline"))

    risky = sum(a["high_consequence"] for a in A)
    master_delib = {
        "agent": "Master Planning Agent",
        "desk": "Planning",
        "summary": f"Combined the Finance, Deliverables and Governance proposals into {len(A)} actions. {risky} of them need a person's approval.",
        "actions": A
    }
    return {"path": ["planning"], "plan": A, "deliberations": [master_delib],
            "trace": [log("Planning", f"Combined the three proposals into {len(A)} actions ({risky} high-consequence gated)")]}


def approval(s: State):
    """THE PAUSE. Ask a human about risky actions we haven't asked about yet."""
    asked = set(s["approved_ids"]) | set(s["rejected_ids"])
    need = [a for a in s["plan"] if a["high_consequence"] and a["id"] not in asked]
    if not need:                                   # nothing risky (or already decided) -> carry on
        return {"path": ["approval"]}

    decision = interrupt({"type": "approval_request", "actions": need})   # <- run stops here; the server resumes it

    valid = {a["id"] for a in need}
    yes = [i for i in decision.get("approved_ids", []) if i in valid]
    no = [i for i in valid if i not in yes]
    auto = bool(decision.get("auto"))
    who = "Coordinator" if auto else "Human"
    verb = "Auto-approved" if auto else "Approved"
    return {"path": ["approval"], "approved_ids": s["approved_ids"] + yes, "rejected_ids": s["rejected_ids"] + no,
            "trace": [log(who, f"{verb} {len(yes)}, rejected {len(no)} risky action(s)")]}


def execution(s: State):
    """Run every approved action through the tools. Already-finished actions are skipped."""
    res = agents.execution_run(s["plan"], s["approved_ids"], s["results"])
    ok = sum(r["status"] == "ok" for r in res.values())
    skipped = sum(r["status"] == "skipped" for r in res.values())
    bad = [k for k, r in res.items() if r["status"] == "failed"]
    t = [log("Execution", f"Ran {len(res)} action(s): {ok} ok, {len(bad)} failed, {skipped} skipped")]
    t += [log("Execution", f"{k}: {res[k]['detail']}", "error") for k in bad]
    return {"path": ["execution"], "results": res, "trace": t}


def verification(s: State):
    """Check the real system state. Anything that did not really happen goes into 'failed'."""
    failed, checks = agents.verification_check(s["plan"], s["results"])
    return {"path": ["verification"], "failed": failed, "checks": checks, "attempt": s["attempt"] + 1,
            "trace": [log("Verification", f"{len(checks) - len(failed)} of {len(checks)} checks passed against the real systems",
                          "warn" if failed else "success")]}


def report(s: State):
    """End of a run: one summary line.
    
    KG rollback strategy:
    - If nothing ran (pure rejection) → rollback fully, graph returns to baseline.
    - If some things ran → rollback then re-run only the approved sponsor update so
      the graph reflects the actual committed state, nothing more.
    """
    res, plan = s.get("results", {}), s.get("plan", [])
    ok = sum(r["status"] == "ok" for r in res.values())
    skipped = sum(r["status"] == "skipped" for r in res.values())
    manual = sum(a["tool"] == "create_task" for a in plan)
    bits = [f"{ok} action{'s' if ok != 1 else ''} ran"]
    if skipped: bits.append(f"{skipped} skipped because they were not approved")
    if manual: bits.append(f"{manual} handed to a person")
    if s.get("failed"): bits.append(f"{len(s['failed'])} still unresolved")
    txt = "Finished: " + ", ".join(bits) + "."
    if not s.get("update"):
        txt = "Stopped: the email could not be understood."

    # Roll back the KG to its pre-analysis snapshot, then replay only the
    # approved tracker update (if it actually ran) so the graph is consistent
    # with what truly happened in the data store.
    agents.KG.rollback()
    if s.get("update") and ok > 0:
        # Only re-mutate if the tracker action actually succeeded
        track_ok = any(
            r["status"] == "ok"
            for aid, r in res.items()
            if aid == "track" or any(a["id"] == aid and a["tool"] == "update_sponsor_tracker" for a in plan)
        )
        if track_ok:
            u = s["update"]
            agents.KG.update_sponsor_commitment(u["sponsor_id"], u["new_amount"], "confirmed by execution")

    return {"path": ["report"], "final_report": txt, "trace": [log("Coordinator", txt, "success")]}


# =====================================================================================================
# DECISION POINTS  (look at the state, return a label; BRANCHES below maps label -> next node)
# =====================================================================================================
def check_understood(s: State) -> str:
    return "understood" if s.get("update") else "not_understood"


def check_verified(s: State) -> str:
    return "retry" if s.get("failed") and s["attempt"] < MAX_PASSES else "done"


# =====================================================================================================
# THE WIRING - single source of truth. The graph below AND the diagram in the web UI are both built from this.
# =====================================================================================================
STEPS = [   # in running order; consecutive steps are joined by a plain arrow
    dict(id="coordinator", fn=coordinator, title="Coordinator", owner="Orchestrator",
         does="Starts the run and resets the session counters.",
         reads=["raw_message"], writes=["approved_ids", "rejected_ids", "results", "attempt", "deliberations"]),
    dict(id="communication", fn=communication, title="Communication", owner="Communication agent",
         does="Parses the sponsor's message and extracts sponsor identity, new commitment, and deadlines.",
         reads=["raw_message"], writes=["update"]),
    dict(id="knowledge", fn=knowledge, title="Knowledge", owner="Knowledge agent",
         does="Updates the context graph and traces cascading ripple effects across budgets, deliverables, and approvals.",
         reads=["update"], writes=["impacts", "knowledge_summary", "draft"]),
    dict(id="finance_agent", fn=finance_agent, title="Finance", owner="Finance agent",
         does="Evaluates funding shortfalls across budget lines and determines proportional budget reallocations.",
         reads=["update", "impacts"], writes=["finance_proposal", "deliberations"]),
    dict(id="deliverables_agent", fn=deliverables_agent, title="Deliverables", owner="Deliverables agent",
         does="Audits contractual deliverable benefits against sponsor tier and determines renegotiations.",
         reads=["update", "impacts"], writes=["deliverables_proposal", "deliberations"]),
    dict(id="governance_agent", fn=governance_agent, title="Governance", owner="Governance agent",
         does="Scans pending vendor disbursements against reduced budget lines and determines emergency spending holds.",
         reads=["impacts"], writes=["governance_proposal", "deliberations"]),
    dict(id="planning", fn=planning, title="Planning", owner="Planning agent",
         does="Reconciles domain proposals from Finance, Deliverables, and Governance agents into a unified, policy-gated action plan.",
         reads=["finance_proposal", "deliverables_proposal", "governance_proposal", "draft", "failed"], writes=["plan", "deliberations", "failed"]),
    dict(id="approval", fn=approval, title="Approval", owner="Policy gate",
         does="Marks high-consequence actions for sign-off. The website signs them off automatically so email processing stays hands-free.",
         reads=["plan", "approved_ids", "rejected_ids"], writes=["approved_ids", "rejected_ids"], pauses=True),
    dict(id="execution", fn=execution, title="Execution", owner="Execution agent",
         does="Runs approved actions through tool integrations with bounded retries and SHA-256 idempotency.",
         reads=["plan", "approved_ids", "results"], writes=["results"]),
    dict(id="verification", fn=verification, title="Verification", owner="Verification agent",
         does="Audits ground-truth system state independently to confirm real execution.",
         reads=["plan", "results"], writes=["failed", "checks", "attempt"]),
    dict(id="report", fn=report, title="Report", owner="Coordinator",
         does="Generates the final multi-agent reconciliation summary.",
         reads=["results", "plan", "failed"], writes=["final_report"]),
]

BRANCHES = {   # node -> (decision function, {label: next node}, {label: plain-English note for the UI})
    "communication": (check_understood,
                      {"understood": "knowledge", "not_understood": "report"},
                      {"understood": "Message parsed successfully: proceed to deliberation.",
                       "not_understood": "Message unparseable: skip straight to report."}),
    "verification": (check_verified,
                     {"retry": "planning", "done": "report"},
                     {"retry": f"A check failed and fewer than {MAX_PASSES} passes were used: go back and replan.",
                      "done": "All checks passed, or we ran out of tries: write the report."}),
}

PLAIN_EDGES = [("coordinator", "communication"), ("knowledge", "finance_agent"),
               ("finance_agent", "deliverables_agent"), ("deliverables_agent", "governance_agent"),
               ("governance_agent", "planning"), ("planning", "approval"),
               ("approval", "execution"), ("execution", "verification"), ("report", END)]


def build():
    g = StateGraph(State)
    for step in STEPS:
        g.add_node(step["id"], step["fn"])
    g.add_edge(START, "coordinator")
    for a, b in PLAIN_EDGES:
        g.add_edge(a, b)
    for node, (decide, mapping, _notes) in BRANCHES.items():
        g.add_conditional_edges(node, decide, mapping)
    return g.compile(checkpointer=MemorySaver())      # the checkpointer is what makes pause/resume possible


graph = build()


def describe_flow() -> dict:
    """JSON description of the wiring for the web UI (GET /api/flow)."""
    return {
        "max_passes": MAX_PASSES,
        "steps": [{k: v for k, v in s.items() if k != "fn"} for s in STEPS],
        "branches": [{"from": n, "label": lab, "to": to, "note": notes[lab]}
                     for n, (_f, mapping, notes) in BRANCHES.items() for lab, to in mapping.items()],
    }


if __name__ == "__main__":
    print(graph.get_graph().draw_mermaid())
