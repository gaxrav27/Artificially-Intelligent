"""SHARED CONTRACTS - agree on these in hour 0 and don't change without telling everyone."""
import operator
import re
from typing import Annotated, Any, Literal, Optional, TypedDict
from pydantic import BaseModel

# Policy lives in CODE, not in prompts: the LLM can never downgrade these.
HIGH_CONSEQUENCE_TOOLS = {"send_email", "hold_approval", "update_budget", "update_sponsor_tracker", "update_deliverable"}


class SponsorUpdate(BaseModel):          # Communication Agent output
    sponsor_id: str
    sponsor_name: str
    old_amount: float
    new_amount: float
    deadline: Optional[str] = None
    note: str = ""


class Impact(BaseModel):                 # Knowledge Agent / context.py output
    node_id: str
    kind: Literal["budget_line", "deliverable", "approval", "tracker"]
    label: str
    detail: str
    severity: Literal["low", "medium", "high"] = "medium"
    before: Optional[float] = None
    after: Optional[float] = None


class Action(BaseModel):                 # Planning Agent output -> Execution Agent input
    id: str
    tool: str
    args: dict[str, Any]
    rationale: str
    high_consequence: bool = False


def inr(n) -> str:
    """Rupees with Indian digit grouping: 100000 -> ₹1,00,000"""
    n = int(round(n or 0))
    digits = str(abs(n))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        digits = f"{head},{tail}"
    return f"{'-' if n < 0 else ''}₹{digits}"


def merge_dict(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):     # LangGraph shared state
    raw_message: str
    update: Optional[dict]
    impacts: list
    knowledge_summary: str
    draft: dict
    finance_proposal: dict
    deliverables_proposal: dict
    governance_proposal: dict
    deliberations: Annotated[list, operator.add]
    plan: list
    approved_ids: list
    rejected_ids: list
    results: Annotated[dict, merge_dict]
    failed: list
    checks: list
    attempt: int
    final_report: str
    trace: Annotated[list, operator.add]
    path: Annotated[list, operator.add]     # names of nodes visited, in order (drives the flow diagram)


def log(agent: str, msg: str, level: str = "info") -> dict:
    import time
    return {"agent": agent, "msg": msg, "level": level, "ts": time.strftime("%H:%M:%S")}
