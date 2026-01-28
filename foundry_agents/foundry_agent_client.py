# Before running the sample:
#    pip install --pre "azure-ai-projects>=2.0.0b1"
#    pip install azure-identity

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from openai.types.responses.response_input_param import McpApprovalResponse, ResponseInputParam

load_dotenv()


def create_project_client(endpoint: str) -> AIProjectClient:
    # DefaultAzureCredential will try multiple auth methods (env vars, managed identity, az login, etc.)
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def _extract_special_outputs(response) -> Dict[str, Any]:
    """
    Returns:
      - consent_link if oauth_consent_request present
      - approval_requests if mcp_approval_request present
    """
    consent_link = None
    approval_requests: List[Dict[str, Any]] = []

    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None)

        if item_type == "oauth_consent_request":
            consent_link = getattr(item, "consent_link", None)

        if item_type == "mcp_approval_request" and getattr(item, "id", None):
            approval_requests.append(
                {
                    "id": item.id,
                    "server_label": getattr(item, "server_label", None),
                    "tool_name": getattr(item, "name", None),
                    "arguments": getattr(item, "arguments", None),
                }
            )

    return {"consent_link": consent_link, "approval_requests": approval_requests}


def _print_approval_requests(approval_requests: List[Dict[str, Any]]) -> List[McpApprovalResponse]:
    """
    Prompt user to approve/deny each approval request and return McpApprovalResponse inputs.
    """
    inputs: List[McpApprovalResponse] = []

    for req in approval_requests:
        print("\nMCP approval requested")
        print(f"  Server: {req.get('server_label')}")
        print(f"  Tool: {req.get('tool_name')}")
        print(f"  Arguments: {json.dumps(req.get('arguments'), indent=2, default=str)}")

        should_approve = input("Approve this MCP tool call? (y/N): ").strip().lower() == "y"
        inputs.append(
            McpApprovalResponse(
                type="mcp_approval_response",
                approve=should_approve,
                approval_request_id=req["id"],
            )
        )

    return inputs


def _run_response_until_blocked_or_done(
    *,
    openai_client,
    agent_name: str,
    initial_input: ResponseInputParam,
    previous_response_id: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Executes the Responses API call and handles:
      - OAuth consent blocking (returns status oauth_consent_required + consent_link)
      - MCP approvals (loops, prompting user, then continuing)
      - final completion (returns status ok + output_text)

    Returns: (status, response_id, extra)
      - status: "ok" | "oauth_consent_required"
      - response_id: latest response id
      - extra: output_text when ok, consent_link when oauth required
    """
    # Keep going until we either:
    # - need OAuth consent, or
    # - get a final "ok" response without approvals/consent.
    resp = openai_client.responses.create(
        input=initial_input,
        previous_response_id=previous_response_id,
        extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
    )

    while True:
        special = _extract_special_outputs(resp)

        # OAuth consent takes precedence: user must complete it before tool auth works
        if special["consent_link"]:
            return ("oauth_consent_required", resp.id, special["consent_link"])

        # If approvals exist, prompt user, submit, and continue the loop
        if special["approval_requests"]:
            approval_inputs = _print_approval_requests(special["approval_requests"])

            # Submit MCP approvals
            resp = openai_client.responses.create(
                input=approval_inputs,
                previous_response_id=resp.id,
                extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
            )
            continue

        # No consent and no approvals => final
        return ("ok", resp.id, getattr(resp, "output_text", "") or "")


def _resume_after_consent(
    *,
    openai_client,
    agent_name: str,
    previous_response_id: str,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    # 1) Resume after OAuth consent
    Tries a true resume with empty input; if server rejects empty input,
    falls back to a minimal "continue".
    Then runs the same approval/consent handling loop as normal.
    """
    try:
        return _run_response_until_blocked_or_done(
            openai_client=openai_client,
            agent_name=agent_name,
            initial_input=[],
            previous_response_id=previous_response_id,
        )
    except Exception:
        return _run_response_until_blocked_or_done(
            openai_client=openai_client,
            agent_name=agent_name,
            initial_input=[{"role": "user", "content": "continue"}],
            previous_response_id=previous_response_id,
        )


def main() -> int:
    my_endpoint = f"https://{os.getenv('foundry_account_name')}.services.ai.azure.com/api/projects/{os.getenv('foundry_project_name')}"
    my_agent_name = os.getenv("agent_name")

    if not my_agent_name:
        print("error> Missing env var: agent_name")
        return 2

    project_client = create_project_client(my_endpoint)

    # Get an existing agent
    agent = project_client.agents.get(agent_name=my_agent_name)
    print(f"Retrieved agent: {agent.name}")

    openai_client = project_client.get_openai_client()

    print("\nType your message and press Enter.")
    print("Commands: /exit, /quit, /continue\n")

    pending_consent_response_id: Optional[str] = None

    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return 0

        if not user_text:
            continue

        if user_text.lower() in {"/exit", "/quit", "exit", "quit"}:
            print("bye.")
            return 0

        try:
            # # 1) Resume after OAuth consent
            if user_text.lower() in {"/continue", "continue"}:
                if not pending_consent_response_id:
                    print("agent> Nothing to continue (no pending OAuth consent).")
                    continue

                status, resp_id, extra = _resume_after_consent(
                    openai_client=openai_client,
                    agent_name=agent.name,
                    previous_response_id=pending_consent_response_id,
                )

                if status == "oauth_consent_required":
                    # Still blocked (or re-blocked) on consent
                    pending_consent_response_id = resp_id
                    print("\nagent> OAuth consent required:")
                    print(extra)  # consent_link
                    print("agent> Complete consent in the browser, then type /continue.\n")
                    continue

                # ok
                pending_consent_response_id = None
                print(f"agent> {extra}\n")
                continue

            # # 3) Normal user message
            status, resp_id, extra = _run_response_until_blocked_or_done(
                openai_client=openai_client,
                agent_name=agent.name,
                initial_input=[{"role": "user", "content": user_text}],
                previous_response_id=None,
            )

            if status == "oauth_consent_required":
                pending_consent_response_id = resp_id
                print("\nagent> OAuth consent required:")
                print(extra)  # consent_link
                print("agent> Complete consent in the browser, then type /continue.\n")
                continue

            # ok
            pending_consent_response_id = None
            print(f"agent> {extra}\n")

        except Exception as e:
            print(f"error> {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    raise SystemExit(main())
