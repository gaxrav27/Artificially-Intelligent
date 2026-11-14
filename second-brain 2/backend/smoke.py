"""Hour-0 gate + regression check. Run from /backend:  python smoke.py
Proves the base pipeline works before anyone changes contracts.py. Exit code != 0 means broken."""
import sys
from langgraph.types import Command

import context, tools
from graph import graph

CFG = {"configurable": {"thread_id": ""}}


def _run(tid, email=0):
    global CFG
    CFG = {"configurable": {"thread_id": tid}}
    graph.invoke({"raw_message": context.load("emails")[email]["body"]}, CFG)
    return graph.get_state(CFG)


def happy_path():
    snap = _run("smoke-happy")
    v = snap.values
    assert snap.next == ("approval",), f"expected pause at approval, got {snap.next}"
    kinds = {i["kind"] for i in v["impacts"]}
    assert kinds == {"tracker", "budget_line", "approval", "deliverable"}, kinds
    need = [a["id"] for a in v["plan"] if a["high_consequence"]]
    assert need, "no high-consequence actions gated"

    v = graph.invoke(Command(resume={"approved_ids": need}), CFG)
    assert v["final_report"].startswith("Finished"), v["final_report"]
    assert all(r["status"] == "ok" for r in v["results"].values()), v["results"]
    assert tools.STORE["outbox"], "no email drafted to outbox"
    return len(v["plan"]), len(v["impacts"])


def failure_and_replan():
    tools.CHAOS["update_sponsor_tracker"] = 2      # exhaust retries once
    snap = _run("smoke-chaos")
    need = [a["id"] for a in snap.values["plan"] if a["high_consequence"]]
    v = graph.invoke(Command(resume={"approved_ids": need}), CFG)
    assert any(t["agent"] == "Planning" and "Replanning" in t["msg"] for t in v["trace"]), "no replan trace"
    assert all(r["status"] == "ok" for r in v["results"].values()), v["results"]
    assert not v.get("failed"), v.get("failed")


def rejection_path():
    tools.reset_store(); context.KG.reset()
    snap = _run("smoke-reject")
    v = graph.invoke(Command(resume={"approved_ids": []}), CFG)
    assert all(r["status"] == "skipped" for r in v["results"].values() if r["status"] != "ok")
    assert "skipped" in v["final_report"], v["final_report"]


def scenarios():
    """Every sample email must be read correctly, and a raise must never freeze payments."""
    expected = {0: 60000, 1: 30000, 2: 15000, 3: 150000}
    for i, e in enumerate(context.load("emails")):
        tools.reset_store(); context.KG.reset()
        snap = _run(f"smoke-scn-{i}", i)
        v = snap.values
        assert v["update"]["new_amount"] == expected[i], f"scenario {i}: read {v['update']['new_amount']}"
        holds = [a for a in v["plan"] if a["tool"] == "hold_approval"]
        if v["update"]["new_amount"] > v["update"]["old_amount"]:
            assert not holds, "a sponsor increase must not put payments on hold"
            assert not [a for a in v["plan"] if a["tool"] == "update_deliverable"], "increase flagged deliverables"
        else:
            assert holds, f"scenario {i}: a cut should hold pending payments"


if __name__ == "__main__":
    fails = 0
    for name, fn in [("happy path", happy_path), ("failure + replan", failure_and_replan), ("rejection", rejection_path), ("all sample emails", scenarios)]:
        tools.reset_store(); context.KG.reset()
        try:
            print(f"PASS  {name}", fn() or "")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {name}: {e}")
    sys.exit(1 if fails else 0)
