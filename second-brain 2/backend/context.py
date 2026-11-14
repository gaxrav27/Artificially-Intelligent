"""MEMBER 3 - Context Engineer. NetworkX knowledge graph = long-term memory + dependency resolution."""
import copy
import json
from pathlib import Path
import networkx as nx
from contracts import inr

DATA = Path(__file__).parent / "data"


def load(name: str):
    return json.loads((DATA / f"{name}.json").read_text())


class KnowledgeGraph:
    def __init__(self):
        self.reset()

    def reset(self):
        self.g = nx.DiGraph()
        self.events: list[dict] = []           # long-term memory of what changed
        self._snapshot_nodes: dict = {}        # for rollback
        self._snapshot_edges: dict = {}
        self._snapshot_events: list = []
        for s in load("sponsors"):
            self.g.add_node(s["id"], kind="sponsor", label=s["name"], amount=s["committed"], tier=s["tier"], email=s["email"])
        for b in load("budget"):
            self.g.add_node(b["id"], kind="budget_line", label=b["name"], amount=b["amount"])
            for sid, amt in b["funding"].items():
                self.g.add_edge(sid, b["id"], rel="funds", amount=amt)
        for d in load("deliverables"):
            self.g.add_node(d["id"], kind="deliverable", label=d["name"], required=d["required_amount"], status=d["status"])
            self.g.add_edge(d["sponsor_id"], d["id"], rel="entitled_to")
        for a in load("approvals"):
            self.g.add_node(a["id"], kind="approval", label=a["name"], amount=a["amount"], status=a["status"])
            self.g.add_edge(a["budget_line"], a["id"], rel="gates")

    # ---- snapshot / rollback so rejection leaves the graph clean ----
    def save_snapshot(self):
        """Call this BEFORE analysis so rejection can undo the KG mutations."""
        self._snapshot_nodes = {n: copy.copy(dict(d)) for n, d in self.g.nodes(data=True)}
        self._snapshot_edges = {(u, v): copy.copy(dict(d)) for u, v, d in self.g.edges(data=True)}
        self._snapshot_events = list(self.events)

    def rollback(self):
        """Restore the KG to the state at the last save_snapshot() call."""
        if not self._snapshot_nodes:
            return
        for n, data in self._snapshot_nodes.items():
            if n in self.g:
                for k, v in data.items():
                    self.g.nodes[n][k] = v
        for (u, v), data in self._snapshot_edges.items():
            if self.g.has_edge(u, v):
                for k, val in data.items():
                    self.g[u][v][k] = val
        self.events = list(self._snapshot_events)

    # ---- the function from the plan --------------------------------------------------
    def update_sponsor_commitment(self, sponsor_id: str, new_amount: float, note: str = "") -> list[dict]:
        """Mutates the graph and returns the cascading impacts (list of Impact dicts)."""
        g = self.g
        sp = g.nodes[sponsor_id]
        old = sp["amount"]
        ratio = new_amount / old if old else 1.0
        sp["amount"] = new_amount
        impacts = [dict(node_id=f"tracker:{sponsor_id}", kind="tracker", label=f"Sponsorship tracker - {sp['label']}",
                        detail=f"Commitment {inr(old)} to {inr(new_amount)}", severity="medium", before=old, after=new_amount)]

        for _, tgt, e in list(g.out_edges(sponsor_id, data=True)):
            node = g.nodes[tgt]
            if e["rel"] == "funds":                                   # budget cascade
                new_share = e["amount"] * ratio
                before = node["amount"]
                node["amount"] = before - (e["amount"] - new_share)
                e["amount"] = new_share
                impacts.append(dict(node_id=tgt, kind="budget_line", label=node["label"],
                                    detail=f"Budget {inr(before)} to {inr(node['amount'])}",
                                    severity="high" if ratio < 0.8 else "medium" if ratio < 1 else "low", before=before, after=node["amount"]))
                for _, ap, e2 in g.out_edges(tgt, data=True):         # approvals gated by that budget line
                    a = g.nodes[ap]
                    if e2["rel"] == "gates":
                        if a["status"] == "pending" and ratio < 1:
                            impacts.append(dict(node_id=ap, kind="approval", label=a["label"],
                                                detail=f"{inr(a['amount'])} payment is still pending, but its budget line shrank to {inr(node['amount'])}",
                                                severity="high", before=a["amount"], after=None))
            elif e["rel"] == "entitled_to":                           # deliverable cascade
                if new_amount < node["required"] and node["status"] != "needs_renegotiation":
                    node["status"] = "at_risk"
                    impacts.append(dict(node_id=tgt, kind="deliverable", label=node["label"],
                                        detail=f"Needs {inr(node['required'])}, but the sponsor is now at {inr(new_amount)}",
                                        severity="high" if node["required"] - new_amount > 20000 else "medium"))
        self.events.append({"type": "sponsor_commitment", "sponsor": sponsor_id, "old": old, "new": new_amount, "note": note})
        return impacts

    def sponsor_by_id(self, sid):  return self.g.nodes[sid]

    def to_json(self):
        return {
            "nodes": [{"id": n, **{k: v for k, v in d.items()}} for n, d in self.g.nodes(data=True)],
            "edges": [{"from": u, "to": v, "label": d["rel"]} for u, v, d in self.g.edges(data=True)],
            "events": self.events,
        }

    def save(self, path="memory.json"):
        Path(path).write_text(json.dumps(nx.node_link_data(self.g, edges="edges"), indent=2))


KG = KnowledgeGraph()

if __name__ == "__main__":   # quick unit check: python context.py
    out = KG.update_sponsor_commitment("s1", 60000)
    assert any(i["kind"] == "approval" for i in out) and any(i["node_id"] == "d1" for i in out)
    for i in out: print(i["severity"].upper(), i["kind"], "-", i["label"], "|", i["detail"])
