# Second Brain: sponsor change desk (Track B)

When a sponsor changes what they will pay, this app reads the email, works out what it touches
(budget lines, payments, promised deliverables), drafts a plan, asks a person to approve the risky
steps, runs them, and checks the results against the records.

## Run
```bash
./run.sh
```
The first run sets up a virtual environment, runs the smoke tests, then opens http://localhost:8000.

Manual start:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend && python smoke.py && python -m uvicorn main:app --port 8000
```
No API key is needed: every agent has a built-in fallback. To use an LLM, copy `.env.example`, fill it in and export the variables.

## Try it
1. Pick a sample email (or paste your own). It is read and the actions run automatically — there is no review step.
2. Under **Live email**, enter your email address and a Gmail App Password. The desk checks the inbox every
   20 seconds; each new email is read and its actions are executed on its own.
3. Tick **Simulate a tool outage** to watch a check fail and the plan get reworked.
   (Setting `CHAOS["update_sponsor_tracker"] = 4` in `main.py` shows the hand-off to a person instead.)

## Files
| File | Job |
|---|---|
| `backend/graph.py` | LangGraph steps, branches, the approval pause, the replanning loop |
| `backend/agents.py`, `llm.py` | Agent logic and prompts, with offline fallbacks |
| `backend/context.py` | NetworkX knowledge graph and the cascade of impacts |
| `backend/tools.py` | Mock email/sheet/calendar tools: retries, idempotency, failure injection, verifiers |
| `backend/gmail_watcher.py` | Gmail IMAP watcher: polls for new mail and hands each one to the pipeline |
| `backend/contracts.py` | Shared schemas and state |
| `backend/smoke.py` | Tests: happy path, outage, rejection, all sample emails |
| `frontend/index.html` | The whole UI, no build step |

Flow: Coordinator, Communication, Knowledge, Finance, Deliverables, Governance, Planning, Approval, Execution, Verification, Report.
A failed check sends the run back to Planning (up to 3 passes). The policy gate still marks high-consequence
actions, but the server signs them off immediately so email processing never waits for a click.

## Design decisions
- Approval policy lives in code (`HIGH_CONSEQUENCE_TOOLS`), never in a prompt.
- Verification reads the real records and does not trust a tool's success message.
- Idempotency keys make retries safe: no duplicate emails or payments.
- Everything works offline, so the demo does not depend on wifi.
