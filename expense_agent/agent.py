"""Smart Expense Compliance agent — ADK 2.0 graph workflow.

Built up in layers:
  Layer 2 (this step): ingestion (parse) + $100 triage routing + auto-approve.
  Later layers add: security checkpoint, LLM auditor, human-in-the-loop.
"""

import base64
import json
import re
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow
from google.genai import types
from pydantic import BaseModel, Field

from .config import config

# ---------------------------------------------------------------------------
# Pydantic schemas for structured data flow between nodes
# ---------------------------------------------------------------------------


class ExpenseData(BaseModel):
    """Expense report data extracted from the incoming payload."""

    amount: float = Field(description="Expense amount in USD")
    submitter: str = Field(description="Email of the person who submitted")
    category: str = Field(description="Expense category, e.g. travel, meals")
    description: str = Field(description="What the expense is for")
    date: str = Field(description="Date of the expense (YYYY-MM-DD)")


class ApprovalDecision(BaseModel):
    """Structured response schema representing a manager's decision."""

    decision: Literal["approve", "reject"] = Field(
        description="Manager's decision: 'approve' to accept, 'reject' to deny."
    )


# ---------------------------------------------------------------------------
# Security defense configuration and helpers
# ---------------------------------------------------------------------------

# Signature list of phrases typical of prompt-injection attempts. Cheap,
# deterministic, and runs BEFORE the LLM so the model never sees the payload.
INJECTION_KEYWORDS = [
    "ignore",
    "bypass",
    "system prompt",
    "system instruction",
    "override",
    "auto-approve",
    "approve instantly",
    "do not review",
    "skip review",
    "you must approve",
    "always approve",
    "instruction",
    "prompt injection",
    "ignore previous",
    "disregard",
    "new instruction",
]

# Regex patterns for PII that must never reach logs or the LLM.
SSN_RE = r"\b\d{3}-\d{2}-\d{4}\b"
CC_RE = r"\b(?:\d{4}[- ]?){3}\d{4}\b|\b\d{13,16}\b"


def detect_prompt_injection(text: str) -> bool:
    """Return True if the text contains any injection signature keyword."""
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in INJECTION_KEYWORDS)


def redact_pii(text: str) -> tuple[str, bool]:
    """Strip SSNs and credit-card numbers. Returns (clean_text, was_redacted)."""
    text, n_ssn = re.subn(SSN_RE, "[REDACTED_SSN]", text)
    text, n_cc = re.subn(CC_RE, "[REDACTED_CC]", text)
    return text, (n_ssn + n_cc) > 0


# ---------------------------------------------------------------------------
# Node 1 — Ingestion: parse the incoming payload into a clean dict
# ---------------------------------------------------------------------------


def parse_expense_email(node_input: str) -> Event:
    """Parse a trigger payload and extract expense data.

    The payload may arrive as:
      * a plain JSON object with the expense fields, or
      * a Pub/Sub-style envelope with a ``data`` field that is either a
        nested JSON object or a base64-encoded JSON string (real Pub/Sub).
    """
    print(f"[parse] received: {node_input!r}", flush=True)
    try:
        event = json.loads(node_input)
    except json.JSONDecodeError as e:
        print(f"[parse] JSONDecodeError: {e}", flush=True)
        return Event(output={"error": f"Invalid JSON: {node_input[:200]}"})

    if not isinstance(event, dict):
        return Event(output={"error": "Input JSON must be a dictionary/object"})

    # The expense fields may be under `data` (Pub/Sub envelope) or at top level.
    data = event.get("data")
    if data is None:
        data = event

    # Real Pub/Sub base64-encodes the data field.
    if isinstance(data, str):
        try:
            data = json.loads(base64.b64decode(data))
        except Exception:
            return Event(output={"error": f"Failed to decode data: {data[:200]}"})

    if not isinstance(data, dict):
        return Event(output={"error": "Expense data must be a dictionary/object"})

    return Event(
        output={
            "amount": float(data.get("amount", 0)),
            "submitter": data.get("submitter", "unknown"),
            "category": data.get("category", "other"),
            "description": data.get("description", ""),
            "date": data.get("date", ""),
        }
    )


# ---------------------------------------------------------------------------
# Node 2 — Triage: deterministic $100 threshold routing
# ---------------------------------------------------------------------------


def route_by_amount(node_input: dict, ctx: Context) -> Event:
    """Route by the $100 threshold.

    Under $100  -> AUTO_APPROVE (no manual overhead for low-risk items).
    $100 and up -> NEEDS_REVIEW (compliance checks + manager override).

    Also stashes the expense in workflow state so later nodes (and the
    human-approval pause) can read it back.
    """
    # An upstream parse error has no 'amount'; send it down the approve path
    # where the error branch reports it cleanly.
    ctx.state["expense_data"] = node_input
    amount = node_input.get("amount", 0)
    if amount >= config.review_threshold:
        return Event(route="NEEDS_REVIEW", output=node_input)  # type: ignore
    return Event(route="AUTO_APPROVE", output=node_input)  # type: ignore


# ---------------------------------------------------------------------------
# Node 3 — Auto-approve terminal for low-value expenses
# ---------------------------------------------------------------------------


def auto_approve(node_input: dict) -> Event:
    """Auto-approve a low-value expense and log the decision as JSON."""
    if "error" in node_input:
        error_msg = node_input["error"]
        print(
            json.dumps(
                {"severity": "ERROR", "message": f"Invalid input: {error_msg}",
                 "decision": "error"}
            ),
            flush=True,
        )
        return Event(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=f"Error: {error_msg}")],
            ),
            output={"status": "error", "message": error_msg},
        )

    amount = node_input.get("amount", 0.0)
    submitter = node_input.get("submitter", "unknown")
    message_text = f"Expense auto-approved: ${amount:.2f} from {submitter}."

    print(
        json.dumps(
            {
                "severity": "INFO",
                "message": f"Expense auto-approved: ${amount:.2f} from {submitter}",
                "decision": "approved",
                "amount": amount,
                "submitter": submitter,
                "category": node_input.get("category", "other"),
            }
        ),
        flush=True,
    )
    return Event(
        content=types.Content(
            role="model", parts=[types.Part.from_text(text=message_text)]
        ),
        output={"status": "approved", **node_input},
    )


# ---------------------------------------------------------------------------
# Node 4 — Zero-trust security checkpoint (runs for expenses >= $100)
# ---------------------------------------------------------------------------


def security_checkpoint(node_input: dict, ctx: Context) -> Event:
    """Scrub PII and detect prompt injection BEFORE the LLM sees anything.

    * Prompt injection detected -> flag a CRITICAL security event and route
      straight to the manager (SECURITY_EVENT), bypassing the LLM entirely so
      the model can never be manipulated by the payload.
    * Clean -> redact any PII and route to the LLM auditor (CLEAN).
    """
    desc = node_input.get("description", "")
    submitter = node_input.get("submitter", "unknown")
    category = node_input.get("category", "other")

    if detect_prompt_injection(desc):
        ctx.state["security_flag"] = True
        print(
            json.dumps(
                {
                    "severity": "CRITICAL",
                    "message": f"Prompt injection attempt detected from {submitter}",
                    "alert_type": "security_checkpoint",
                    "submitter": submitter,
                    "category": category,
                }
            ),
            flush=True,
        )
        # Redact anyway for hygiene, then hand to the human.
        desc, _ = redact_pii(desc)
        node_input["description"] = desc
        ctx.state["expense_data"] = node_input
        return Event(route="SECURITY_EVENT", output=node_input)  # type: ignore

    # Clean payload — scrub PII before it reaches the LLM or any log.
    desc, was_redacted = redact_pii(desc)
    if was_redacted:
        print(
            json.dumps(
                {
                    "severity": "INFO",
                    "message": f"PII redacted from expense description ({submitter})",
                    "alert_type": "pii_redaction",
                }
            ),
            flush=True,
        )
    node_input["description"] = desc
    ctx.state["expense_data"] = node_input
    return Event(route="CLEAN", output=node_input)  # type: ignore


# ---------------------------------------------------------------------------
# Node 6 — Human-in-the-loop: pause for a manager, then process the decision
# ---------------------------------------------------------------------------


def request_approval(node_input, ctx: Context):  # type: ignore[no-untyped-def]
    """Pause the workflow and wait for a human decision.

    Yields a ``RequestInput`` that the ADK runtime surfaces to the dashboard.
    The workflow state is saved and execution stays paused until the session
    is resumed (by the dashboard POSTing back a decision). The human's
    response then flows into ``process_decision`` as this node's output.
    """
    expense = ctx.state.get("expense_data", {})
    if ctx.state.get("security_flag"):
        message = (
            "WARNING: Security Event! Prompt injection attempt detected. "
            "Approve or reject."
        )
    else:
        message = "Expense requires manager approval. Approve or reject."
    yield RequestInput(
        message=message,
        payload=expense,
        response_schema=ApprovalDecision,
    )


def process_decision(node_input, ctx: Context) -> Event:  # type: ignore[no-untyped-def]
    """Process the human's approval decision and log the final outcome."""
    decision = "unknown"
    if isinstance(node_input, dict):
        decision = node_input.get("decision", "unknown")
    elif isinstance(node_input, str):
        decision = "approve" if "approve" in node_input.lower() else "reject"

    approved = decision == "approve"
    expense = ctx.state.get("expense_data", {})
    status = "approved" if approved else "rejected"
    is_security_event = ctx.state.get("security_flag", False)

    severity = "CRITICAL" if is_security_event else ("INFO" if approved else "WARNING")
    print(
        json.dumps(
            {
                "severity": severity,
                "message": f"Expense {status} by manager"
                + (" (Security Event flagged)" if is_security_event else ""),
                "decision": status,
                "security_event": is_security_event,
            }
        ),
        flush=True,
    )

    submitter = expense.get("submitter", "unknown")
    amount = expense.get("amount", 0)
    category = expense.get("category", "")
    description = expense.get("description", "")
    date = expense.get("date", "")

    parts = []
    if is_security_event:
        parts.append(
            "[SECURITY WARNING]: This expense was flagged for a potential "
            "prompt injection security policy violation."
        )
    parts.append(f"${amount:.2f} expense from {submitter} has been {status}.")
    if description:
        parts.append(f'"{description}" ({category}) on {date}.')
    if approved:
        parts.append("The expense has been logged for reimbursement.")
    else:
        parts.append("The submitter will be notified and may resubmit.")

    message_text = " ".join(parts)
    return Event(
        content=types.Content(
            role="model", parts=[types.Part.from_text(text=message_text)]
        ),
        output={"status": status, "message": message_text},
    )


# ---------------------------------------------------------------------------
# Node 5 — LLM compliance auditor (CLEAN expenses >= $100 only)
# ---------------------------------------------------------------------------


def emit_expense_alert(
    submitter: str,
    amount: float,
    category: str,
    risk_summary: str,
) -> dict:
    """Emit a structured WARNING log so finance systems can alert on it.

    In production, Cloud Logging captures JSON stdout as structured logs that
    drive log-based metrics and email alerts. Locally, it just prints JSON.

    Args:
        submitter: Who submitted the expense.
        amount: The expense amount in USD.
        category: The expense category.
        risk_summary: Why this expense needs review.

    Returns:
        Confirmation that the alert was emitted.
    """
    print(
        json.dumps(
            {
                "severity": "WARNING",
                "message": f"Expense review alert: ${amount:.2f} from {submitter} — {risk_summary}",
                "alert_type": "expense_review",
                "submitter": submitter,
                "amount": amount,
                "category": category,
                "risk_summary": risk_summary,
            }
        ),
        flush=True,
    )
    return {"status": "alert_emitted", "submitter": submitter, "amount": amount}


review_agent = LlmAgent(
    name="review_agent",
    model=config.model,
    instruction="""You are an expense review agent. You receive expense reports
of $100 or more that need review before approval.

Analyze the expense and:
1. Check for risk factors: unusual category for the amount, vague description,
   suspiciously round numbers, very high value (>$1000), or potential policy
   violations.
2. Call the `emit_expense_alert` tool with the submitter, amount, category,
   and a brief risk summary explaining why this expense needs human review.
3. Return a structured review.

Your review MUST include:
- **Amount**: The expense amount
- **Submitter**: Who submitted it
- **Category**: The expense category
- **Risk level**: low, medium, or high
- **Risk factors**: What flags you found (if any)
- **Recommendation**: approve, request-more-info, or escalate""",
    input_schema=ExpenseData,
    tools=[emit_expense_alert],
)


# ---------------------------------------------------------------------------
# Graph-based workflow — the root agent
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="expense_processor",
    edges=[
        # START -> parse -> route
        ("START", parse_expense_email, route_by_amount),
        # route branches to one of two nodes
        (
            route_by_amount,
            {
                "AUTO_APPROVE": auto_approve,
                "NEEDS_REVIEW": security_checkpoint,
            },
        ),
        # security checkpoint branches to LLM (clean) or manager (attack)
        (
            security_checkpoint,
            {
                "CLEAN": review_agent,
                "SECURITY_EVENT": request_approval,  # attack skips the LLM
            },
        ),
        # LLM audit and the security path both pause for a human, then finish
        (review_agent, request_approval, process_decision),
    ],
)

app = App(
    name="expense_agent",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
