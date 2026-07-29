"""Agent web server (port 8080).

Built on ADK's `get_fast_api_app`, so it serves the **ADK Dev UI** at
`/dev-ui/` (the developer console: chat with the agent, inspect the graph
events/traces, run evals) — exactly like the course reference.

On top of that we add three custom endpoints the manager dashboard uses:
  POST /pubsub          - trigger the workflow with an expense payload
  GET  /pending         - list expenses paused awaiting manager approval
  POST /action/{sid}    - resume a paused session with approve/reject

Sessions persist to SQLite, shared between the Dev UI and our endpoints.
"""

import json
import os

from fastapi import HTTPException, Request
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel

from expense_agent.agent import root_agent

APP_NAME = "expense_agent"
# One constant "user" so the dashboard can list every session in one call.
USER_ID = "expense-reports-push"

# --- Paths & persistent session store --------------------------------------
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))        # .../expense_agent
_PROJECT_DIR = os.path.dirname(_AGENT_DIR)                     # agents_dir root
_DB_DIR = os.path.join(_AGENT_DIR, ".adk")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "session.db").replace("\\", "/")
SESSION_DB_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

# --- Build the FastAPI app WITH the ADK Dev UI -----------------------------
# agents_dir is the folder that contains the `expense_agent` package, so the
# Dev UI's "Select an app" dropdown discovers it.
app = get_fast_api_app(
    agents_dir=_PROJECT_DIR,
    web=True,
    session_service_uri=SESSION_DB_URL,
)
app.title = "Smart Expense Compliance Agent"

# Our own session service + runner for the custom endpoints (same DB file).
session_service = DatabaseSessionService(db_url=SESSION_DB_URL)
runner = Runner(agent=root_agent, session_service=session_service, app_name=APP_NAME)


class ActionPayload(BaseModel):
    action: str  # "approve" or "reject"
    interrupt_id: str


# ---------------------------------------------------------------------------
# Trigger — feed an expense payload into the graph
# ---------------------------------------------------------------------------


@app.post("/pubsub")
async def handle_pubsub(request: Request):
    """Receive an expense (raw JSON or Pub/Sub envelope) and run the workflow."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    message_dict = body.get("message", body)
    payload_str = json.dumps(message_dict)

    session = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )
    new_message = types.Content(
        role="user", parts=[types.Part.from_text(text=payload_str)]
    )

    paused = False
    decision = None
    message_text = ""
    async for event in runner.run_async(
        new_message=new_message, user_id=USER_ID, session_id=session.id
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None) == "adk_request_input":
                    paused = True
                if getattr(part, "text", None):
                    message_text += part.text + " "
        if event.output and isinstance(event.output, dict):
            status = event.output.get("status")
            if status in ("approved", "rejected"):
                decision = status

    if paused:
        return {
            "status": "paused",
            "session_id": session.id,
            "message": "Expense requires manager approval.",
        }
    return {
        "status": "completed",
        "decision": decision or "auto_approved",
        "session_id": session.id,
        "message": message_text.strip(),
    }


# ---------------------------------------------------------------------------
# Pending — scan sessions for unresolved human-approval interrupts
# ---------------------------------------------------------------------------


@app.get("/pending")
async def pending():
    """Return expenses paused awaiting a manager decision."""
    resp = await session_service.list_sessions(app_name=APP_NAME, user_id=USER_ID)
    items = []
    for s in resp.sessions:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=s.user_id, session_id=s.id
        )
        if not session:
            continue

        calls, responses, report = {}, set(), ""
        for ev in session.events:
            if ev.author == "review_agent" and ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if getattr(part, "text", None):
                        report = part.text
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name == "adk_request_input":
                        args = fc.args if isinstance(fc.args, dict) else {}
                        calls[fc.id] = args
                    fr = getattr(part, "function_response", None)
                    if fr and fr.name == "adk_request_input":
                        responses.add(fr.id)

        for fid in set(calls) - responses:
            args = calls[fid]
            payload = args.get("payload") or {}
            if not isinstance(payload, dict):
                payload = getattr(payload, "model_dump", lambda: {})()
            items.append(
                {
                    "session_id": session.id,
                    "interrupt_id": fid,
                    "amount": payload.get("amount"),
                    "submitter": payload.get("submitter"),
                    "category": payload.get("category"),
                    "description": payload.get("description"),
                    "date": payload.get("date"),
                    "message": args.get("message"),
                    "compliance_report": report,
                    "security_event": "Security Event" in (args.get("message") or ""),
                }
            )
    return {"pending": items}


# ---------------------------------------------------------------------------
# Action — resume a paused session with the manager's decision
# ---------------------------------------------------------------------------


@app.post("/action/{session_id}")
async def take_action(session_id: str, payload: ActionPayload):
    """Resume a paused workflow with approve/reject."""
    decision = "approve" if payload.action == "approve" else "reject"
    resume = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=payload.interrupt_id,
                    name="adk_request_input",
                    response={"decision": decision},
                )
            )
        ],
    )

    final_status, message_text = None, ""
    async for event in runner.run_async(
        new_message=resume, user_id=USER_ID, session_id=session_id
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    message_text += part.text + " "
        if event.output and isinstance(event.output, dict):
            if event.output.get("status") in ("approved", "rejected"):
                final_status = event.output["status"]

    return {"status": final_status or decision, "message": message_text.strip()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
