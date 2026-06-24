import logging
import os
import re
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool, McpToolset
from google.adk.tools.mcp_tool.mcp_toolset import StdioConnectionParams, StdioServerParameters
from google.adk.workflow import Workflow, FunctionNode, node, START
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.config import config

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize model
model_instance = Gemini(model=config.model)

# Set up MCP Toolsets
project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

expense_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
            cwd=project_dir,
        )
    ),
    tool_filter=["get_balances", "log_expense", "calculate_utility_split", "generate_payment_link"]
)

chore_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
            cwd=project_dir,
        )
    ),
    tool_filter=["get_chore_schedule", "mark_chore_done", "rotate_chores", "check_house_rules"]
)

# Define sub-agents
expense_reconciler = LlmAgent(
    name="expense_reconciler",
    model=model_instance,
    tools=[expense_toolset],
    description="Analyzes expenses, splits bills, and drafts roommate payment reminders.",
    instruction="""You are the Expense Reconciler Agent. Your job is to analyze shared roommate expenses, calculate fair splits, track debts, and draft friendly, polite payment reminders.
Rules:
1. Present clear, itemized calculations when splitting bills.
2. For utility spikes, suggest reasonable or proportional splits.
3. Draft reminders that are polite and non-aggressive.
""",
)

chore_mediator = LlmAgent(
    name="chore_mediator",
    model=model_instance,
    tools=[chore_toolset],
    description="Organizes chore schedules, mediates cleanliness disputes, and checks equity.",
    instruction="""You are the Chore Mediator Agent. Your job is to organize household chores, propose rotating chore schedules, and resolve roommate disputes about cleanliness.
Rules:
1. Propose structured, rotating schedules for chores.
2. Suggest compromises on disputes as a neutral party.
3. Promote household equity and chore tracking.
""",
)

# Define orchestrator agent
orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model=model_instance,
    tools=[AgentTool(expense_reconciler), AgentTool(chore_mediator)],
    instruction="""You are the Roommate Mediator Orchestrator. You help roommates coordinate expenses, bills, chores, and household disputes.

You have access to two specialized agents as tools:
1. `expense_reconciler`: for bill splitting, tracking debts, and drafting friendly payment reminders.
2. `chore_mediator`: for schedules, disputes, and rule moderation.

Always delegate roommate-specific complex tasks to the appropriate specialized agent.
If you draft a reminder message or a dispute compromise that will be sent to the other roommates, you MUST include the keyword `[NEEDS_APPROVAL]` in your response so the user can approve or edit it before it is finalized.
""",
)

# Workflow Function Nodes

import json

def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """Checks the input for prompt injections, scrubs PII, checks domain-specific rules, and logs structured JSON."""
    text = ""
    if isinstance(node_input, str):
        text = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        text = "".join(part.text for part in node_input.parts if part.text)
    elif isinstance(node_input, dict):
        text = str(node_input)

    # 1. Domain-specific rule: Check for huge financial numbers or budget spikes
    # If the roommate tries to split an amount > $5000 in a single bill, we block it to prevent fat-finger entry errors.
    amounts = re.findall(r"\b\d{4,}(?:\.\d{2})?\b", text)
    has_large_amount = False
    for amt_str in amounts:
        val = float(amt_str)
        if val > 5000:
            has_large_amount = True
            break
            
    if has_large_amount:
        log_entry = {
            "event": "security_check",
            "decision": "BLOCKED",
            "reason": "Exceeded maximum single expense threshold ($5000)",
            "severity": "WARNING",
            "session_id": ctx.session.id
        }
        logger.warning(json.dumps(log_entry))
        return Event(
            output="Security Block: Shared expenses cannot exceed $5,000 in a single transaction to prevent entry errors.",
            route="SECURITY_EVENT"
        )

    # 2. Prompt injection detection
    injection_keywords = [
        "ignore previous instructions", 
        "system prompt", 
        "override rules", 
        "ignore rules", 
        "dan mode", 
        "jailbreak"
    ]
    is_injection = any(kw in text.lower() for kw in injection_keywords)
    if is_injection:
        log_entry = {
            "event": "security_check",
            "decision": "BLOCKED",
            "reason": "Prompt injection attempt detected",
            "severity": "CRITICAL",
            "session_id": ctx.session.id
        }
        logger.warning(json.dumps(log_entry))
        return Event(
            output="Security Block: Input contains potential prompt injection keywords.",
            route="SECURITY_EVENT"
        )

    # 3. PII scrubbing: email, phone, credit card numbers
    scrubbed_text = text
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    phone_pattern = r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"
    cc_pattern = r"\b(?:\d[ -]*?){13,16}\b"
    
    pii_detected = bool(re.search(email_pattern, text) or re.search(phone_pattern, text) or re.search(cc_pattern, text))
    
    if pii_detected:
        scrubbed_text = re.sub(email_pattern, "[EMAIL_REDACTED]", scrubbed_text)
        scrubbed_text = re.sub(phone_pattern, "[PHONE_REDACTED]", scrubbed_text)
        scrubbed_text = re.sub(cc_pattern, "[CREDIT_CARD_REDACTED]", scrubbed_text)

    log_entry = {
        "event": "security_check",
        "decision": "ALLOWED",
        "pii_scrubbed": pii_detected,
        "severity": "INFO",
        "session_id": ctx.session.id
    }
    logger.info(json.dumps(log_entry))

    # Create a safe content object to pass
    safe_input = types.Content(role="user", parts=[types.Part.from_text(text=scrubbed_text)])
    return Event(output=safe_input, route="__DEFAULT__")


def security_failure(node_input: str) -> Event:
    """Handles blocked requests by returning a security response."""
    content = types.Content(role="model", parts=[types.Part.from_text(text=node_input)])
    return Event(output=node_input, content=content)


def parse_orchestrator_output(ctx: Context, node_input: Any) -> Event:
    """Parses output from orchestrator_agent and checks for approval keywords."""
    text = ""
    if isinstance(node_input, str):
        text = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        text = "".join(part.text for part in node_input.parts if part.text)
    elif isinstance(node_input, dict):
        text = str(node_input)

    # Check if approval is needed based on keyword
    needs_approval = "[needs_approval]" in text.lower() or "drafted reminder" in text.lower()
    
    # Store in context state
    state_delta = {
        "latest_response": text,
        "needs_approval": needs_approval
    }
    return Event(output=text, state=state_delta)


async def approval_node(ctx: Context, node_input: str) -> AsyncGenerator[Any, None]:
    """Human-in-the-loop node to approve drafted messages or proposals."""
    needs_approval = ctx.state.get("needs_approval", False)
    
    if not needs_approval:
        yield Event(output=node_input, route="__DEFAULT__")
        return
        
    latest_response = ctx.state.get("latest_response", node_input)
    revision_count = ctx.state.get("revision_count", 0)
    interrupt_id = f"roommate_approval_{revision_count}"
    
    if not ctx.resume_inputs or interrupt_id not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id=interrupt_id,
            message=f"✋ HUMAN-IN-THE-LOOP APPROVAL REQUIRED:\n\nProposed Draft:\n\"{latest_response}\"\n\nDo you approve sending this? (Reply with 'yes' or describe modifications/revision)"
        )
        return
        
    user_decision = ctx.resume_inputs[interrupt_id]
    if str(user_decision).strip().lower() in ["yes", "approve", "y"]:
        yield Event(
            output=f"[Approved & Sent] {latest_response}",
            route="__DEFAULT__",
            state={"needs_approval": False, "revision_count": 0}
        )
    else:
        # Pass feedback as output to orchestrator and increment revision counter
        yield Event(
            output=f"The roommate has requested revisions on the draft. Feedback: {user_decision}",
            route="revision",
            state={"needs_approval": False, "revision_count": revision_count + 1}
        )


def final_output(ctx: Context, node_input: Any) -> Event:
    """Formats and returns final output."""
    text = str(node_input)
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    return Event(output=text, content=content)


# Build graph edges (Single-edge constraint respected)
edges = [
    (START, security_checkpoint),
    (security_checkpoint, {
        "SECURITY_EVENT": security_failure,
        "__DEFAULT__": orchestrator_agent
    }),
    (orchestrator_agent, parse_orchestrator_output),
    (parse_orchestrator_output, approval_node),
    (approval_node, {
        "revision": orchestrator_agent,
        "__DEFAULT__": final_output
    }),
    (security_failure, final_output),
]

root_agent = Workflow(
    name="roommate_mediator_workflow",
    edges=edges,
    description="Workflow coordinating roommate dispute resolution, expense reconciliation, and chore mediation.",
)

app = App(
    root_agent=root_agent,
    name="app",
)
