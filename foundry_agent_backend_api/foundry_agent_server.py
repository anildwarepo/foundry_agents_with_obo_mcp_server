from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# OpenAI SDK internals for Fabric client
from openai import OpenAI
from openai._models import FinalRequestOptions
from openai._types import Omit
from openai._utils import is_given

from azure.ai.projects import AIProjectClient
from azure.core.credentials import AccessToken, TokenCredential

load_dotenv()

TENANT_ID = os.getenv("tenant_id")
FOUNDRY_AUDIENCE = "https://ai.azure.com"
FABRIC_AUDIENCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE_URL = "https://api.fabric.microsoft.com/.default"

# Foundry config
foundry_account_name = os.getenv("foundry_account_name")
foundry_project_name = os.getenv("foundry_project_name")
FOUNDRY_PROJECT_ENDPOINT = f"https://{foundry_account_name}.services.ai.azure.com/api/projects/{foundry_project_name}"

# Fabric config - use env vars or fallback to defaults from working code
FABRIC_WORKSPACE_ID = os.getenv("fabric_workspace_id", "b1a1dad3-61f0-4438-be14-1651717fcaf7")
FABRIC_DATAAGENT_ID = os.getenv("fabric_dataagent_id", "ea0d6159-6ec3-46d0-a640-8202356b07bf")
FABRIC_BASE_URL = (
    f"https://api.fabric.microsoft.com/v1/workspaces/{FABRIC_WORKSPACE_ID}/"
    f"dataagents/{FABRIC_DATAAGENT_ID}/aiassistant/openai"
)

print("FOUNDRY_PROJECT_ENDPOINT:", FOUNDRY_PROJECT_ENDPOINT)
print("FABRIC_BASE_URL:", FABRIC_BASE_URL)

JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
jwk_client = PyJWKClient(JWKS_URL)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3500", "http://127.0.0.1:3500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BearerTokenCredential(TokenCredential):
    def __init__(self, token: str, expires_on: int):
        self._token = token
        self._expires_on = expires_on

    def get_token(self, *scopes: str, **kwargs) -> AccessToken:
        return AccessToken(self._token, self._expires_on)


def decode_and_validate_bearer(auth_header: Optional[str], token_scope: Optional[str] = None) -> Dict[str, Any]:
    """Decode and validate bearer token. Audience is chosen based on token_scope header."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer token")

    token = auth_header.split(" ", 1)[1].strip()

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token).key
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token signing key")

    # Determine expected audience based on scope
    is_fabric = token_scope and "fabric" in token_scope.lower()
    expected_audience = FABRIC_AUDIENCE if is_fabric else FOUNDRY_AUDIENCE

    # Decode token without verification first to check claims (for debugging)
    try:
        unverified_claims = jwt.decode(token, options={"verify_signature": False})
        token_issuer = unverified_claims.get("iss", "")
        token_aud = unverified_claims.get("aud", "")
        print(f"[DEBUG] Token issuer: {token_issuer}, audience: {token_aud}, expected_aud: {expected_audience}")
    except Exception as e:
        print(f"[DEBUG] Could not decode token for debugging: {e}")
        token_issuer = ""

    # All possible Azure AD issuer formats
    allowed_issuers = {
        f"https://sts.windows.net/{TENANT_ID}/",
        f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        f"https://login.microsoftonline.com/{TENANT_ID}/",
        f"https://login.microsoftonline.com/{TENANT_ID}",
    }

    last_err = None
    for issuer in allowed_issuers:
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=expected_audience,
                issuer=issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                },
            )

            # For Foundry tokens, optionally enforce user_impersonation scope
            # Fabric tokens use different scope format, so skip this check for Fabric
            if not is_fabric:
                scp = (claims.get("scp") or "").split()
                if scp and ("user_impersonation" not in scp):
                    raise HTTPException(status_code=403, detail="Missing required scope: user_impersonation")

            return {"token": token, "claims": claims, "is_fabric": is_fabric}
        except HTTPException:
            raise  # Re-raise HTTP exceptions immediately
        except Exception as e:
            last_err = e

    # Include actual token issuer in error for debugging
    raise HTTPException(
        status_code=401,
        detail=f"Token validation failed: {type(last_err).__name__}. Token issuer: {token_issuer}, expected one of: {allowed_issuers}"
    )


def create_foundry_client_from_token(token: str, claims: Dict[str, Any]) -> AIProjectClient:
    exp = int(claims.get("exp", time.time() + 3600))
    cred = BearerTokenCredential(token, exp)
    return AIProjectClient(endpoint=FOUNDRY_PROJECT_ENDPOINT, credential=cred)


# -----------------------------
# Fabric OpenAI client wrapper
# -----------------------------
class FabricOpenAI(OpenAI):
    """OpenAI client configured for Fabric Data Agent API."""

    def __init__(
        self,
        *,
        auth_token: str,
        api_version: str = "2024-05-01-preview",
        **kwargs: Any,
    ) -> None:
        self._auth_token = auth_token
        self.api_version = api_version

        default_query = kwargs.pop("default_query", {})
        default_query["api-version"] = self.api_version

        super().__init__(
            api_key="",  # auth handled via Bearer token
            base_url=FABRIC_BASE_URL,
            default_query=default_query,
            **kwargs,
        )

    def _prepare_options(self, options: FinalRequestOptions) -> None:
        headers: dict[str, str | Omit] = {**options.headers} if is_given(options.headers) else {}
        options.headers = headers

        headers["Authorization"] = f"Bearer {self._auth_token}"
        headers.setdefault("Accept", "application/json")
        headers.setdefault("ActivityId", str(uuid.uuid4()))

        return super()._prepare_options(options)


def create_fabric_client_from_token(token: str) -> FabricOpenAI:
    """Create a FabricOpenAI client from the bearer token."""
    return FabricOpenAI(auth_token=token)


# -----------------------------
# Fabric message helpers
# -----------------------------
def _extract_text_from_fabric_message(msg) -> str:
    """Extract text content from a Fabric assistant message."""
    parts: list[str] = []
    content = getattr(msg, "content", None) or []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_obj = getattr(block, "text", None)
            value = getattr(text_obj, "value", None)
            parts.append(value if isinstance(value, str) else str(text_obj))
        else:
            if btype:
                parts.append(f"[{btype}]")
    return "\n".join(p for p in parts if p).strip()


def _poll_fabric_run_until_done(
    client: FabricOpenAI,
    thread_id: str,
    run_id: str,
    *,
    timeout_seconds: int = 300,
    poll_interval: float = 2.0,
):
    """Poll a Fabric run until it reaches a terminal state."""
    terminal_states = {"completed", "failed", "cancelled", "requires_action"}
    start = time.time()

    run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)
    while run.status not in terminal_states:
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Run polling exceeded {timeout_seconds} seconds (last status={run.status})")
        time.sleep(poll_interval)
        run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)

    return run


def _get_fabric_assistant_response(client: FabricOpenAI, thread_id: str) -> str:
    """Get the latest assistant message from a Fabric thread."""
    page = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
    messages = list(page)
    for msg in messages:
        if getattr(msg, "role", None) == "assistant":
            return _extract_text_from_fabric_message(msg)
    return ""


# In-memory storage for Fabric thread/assistant state per user session
# Key: (user_oid, agent_name) -> {"thread_id": str, "assistant_id": str}
_fabric_sessions: Dict[tuple, Dict[str, str]] = {}


class ApprovalItem(BaseModel):
    approval_request_id: str
    approve: bool


class ChatRequest(BaseModel):
    agent_name: str
    message: Optional[str] = None
    previous_response_id: Optional[str] = None
    approvals: Optional[List[ApprovalItem]] = None
    action: Optional[str] = None  # "continue" to resume after oauth consent


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/agents/{agent_name}")
def get_agent(
    agent_name: str,
    authorization: Optional[str] = Header(default=None),
    x_token_scope: Optional[str] = Header(default=None, alias="X-Token-Scope"),
):
    ctx = decode_and_validate_bearer(authorization, x_token_scope)

    if ctx.get("is_fabric"):
        # Fabric doesn't have a get-agent endpoint; return placeholder
        return {"name": agent_name, "id": None, "type": "fabric_dataagent"}

    project_client = create_foundry_client_from_token(ctx["token"], ctx["claims"])

    try:
        agent = project_client.agents.get(agent_name=agent_name)
        return {"name": agent.name, "id": getattr(agent, "id", None), "type": "foundry_agent"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Foundry call failed: {type(e).__name__}: {e}")


def _extract_special_outputs(response) -> Dict[str, Any]:
    """
    Returns:
      - consent_link if oauth_consent_request present
      - approval_requests if mcp_approval_request present
    """
    consent_link = None
    approval_requests = []

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


@app.post("/chat")
def chat(
    req: ChatRequest,
    authorization: Optional[str] = Header(default=None),
    x_token_scope: Optional[str] = Header(default=None, alias="X-Token-Scope"),
):
    ctx = decode_and_validate_bearer(authorization, x_token_scope)

    # Route to Fabric or Foundry based on token scope
    if ctx.get("is_fabric"):
        return _chat_fabric(req, ctx)
    else:
        return _chat_foundry(req, ctx)


def _chat_fabric(req: ChatRequest, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Handle chat via Fabric Data Agent API (Assistants API style)."""
    token = ctx["token"]
    claims = ctx["claims"]
    user_oid = claims.get("oid", "unknown")
    session_key = (user_oid, req.agent_name)

    print(f"[DEBUG] Fabric chat - workspace: {FABRIC_WORKSPACE_ID}, dataagent: {FABRIC_DATAAGENT_ID}")
    print(f"[DEBUG] Fabric base URL: {FABRIC_BASE_URL}")

    try:
        fabric_client = create_fabric_client_from_token(token)

        # Check for existing session or create new one
        session = _fabric_sessions.get(session_key)

        # If previous_response_id is None, treat as new conversation
        if not req.previous_response_id and session:
            # Clean up old thread
            try:
                fabric_client.beta.threads.delete(thread_id=session["thread_id"])
            except Exception:
                pass
            session = None
            _fabric_sessions.pop(session_key, None)

        if not session:
            # Create new assistant and thread
            print("[DEBUG] Creating new Fabric assistant...")
            assistant = fabric_client.beta.assistants.create(model="not used")
            print(f"[DEBUG] Assistant created: {assistant.id}")
            
            print("[DEBUG] Creating new Fabric thread...")
            thread = fabric_client.beta.threads.create()
            print(f"[DEBUG] Thread created: {thread.id}")
            
            session = {"assistant_id": assistant.id, "thread_id": thread.id}
            _fabric_sessions[session_key] = session

        thread_id = session["thread_id"]
        assistant_id = session["assistant_id"]

        # Fabric doesn't support MCP approvals or OAuth consent in the same way
        # Just handle normal messages
        if not req.message:
            raise HTTPException(status_code=400, detail="message is required for Fabric chat")

        # Add user message to thread
        print(f"[DEBUG] Adding message to thread {thread_id}...")
        fabric_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=req.message,
        )
        print("[DEBUG] Message added")

        # Create and poll run
        run = fabric_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
        )

        run = _poll_fabric_run_until_done(fabric_client, thread_id, run.id)

        if run.status != "completed":
            return {
                "status": "error",
                "response_id": thread_id,  # Use thread_id as response_id for continuity
                "output_text": f"Run finished with status: {run.status}",
            }

        # Get assistant response
        output_text = _get_fabric_assistant_response(fabric_client, thread_id)

        return {
            "status": "ok",
            "response_id": thread_id,  # Use thread_id as response_id for continuity
            "output_text": output_text,
        }

    except HTTPException:
        raise
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Fabric chat failed: {type(e).__name__}: {e}")


def _chat_foundry(req: ChatRequest, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Handle chat via Foundry Agent API (Responses API style)."""
    project_client = create_foundry_client_from_token(ctx["token"], ctx["claims"])

    try:
        agent = project_client.agents.get(agent_name=req.agent_name)
        openai_client = project_client.get_openai_client()

        # 1) Resume after OAuth consent
        if req.action == "continue":
            if not req.previous_response_id:
                raise HTTPException(status_code=400, detail="previous_response_id is required for action=continue")

            # Try a true "resume" (empty input). If server rejects empty input, fallback to a minimal nudge.
            try:
                response = openai_client.responses.create(
                    input=[],
                    previous_response_id=req.previous_response_id,
                    extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
                )
            except Exception:
                response = openai_client.responses.create(
                    input=[{"role": "user", "content": "continue"}],
                    previous_response_id=req.previous_response_id,
                    extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
                )

        # 2) Submit MCP approvals
        elif req.approvals and len(req.approvals) > 0:
            if not req.previous_response_id:
                raise HTTPException(status_code=400, detail="previous_response_id is required when submitting approvals")

            input_list = [
                {
                    "type": "mcp_approval_response",
                    "approve": a.approve,
                    "approval_request_id": a.approval_request_id,
                }
                for a in req.approvals
            ]

            response = openai_client.responses.create(
                input=input_list,
                previous_response_id=req.previous_response_id,
                extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
            )

        # 3) Normal user message
        else:
            if not req.message:
                raise HTTPException(status_code=400, detail="message is required when approvals are not provided")

            response = openai_client.responses.create(
                input=[{"role": "user", "content": req.message}],
                extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
            )

        # Detect special output items (oauth consent / approvals)
        special = _extract_special_outputs(response)

        # OAuth consent takes precedence: user must complete it before tool auth works
        if special["consent_link"]:
            return {
                "status": "oauth_consent_required",
                "response_id": response.id,
                "consent_link": special["consent_link"],
            }

        if special["approval_requests"]:
            return {
                "status": "approval_required",
                "response_id": response.id,
                "approval_requests": special["approval_requests"],
            }

        return {
            "status": "ok",
            "response_id": response.id,
            "output_text": getattr(response, "output_text", "") or "",
        }

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        # Detect tool_user_error with embedded consent URL from Foundry
        if "tool_user_error" in error_msg or "Failed Dependency" in error_msg:
            import re
            consent_match = re.search(r'(https://logic-apis[^\s\'"\)]+)', error_msg)
            if consent_match:
                consent_url = consent_match.group(1)
                return {
                    "status": "oauth_consent_required",
                    "response_id": None,
                    "consent_link": consent_url,
                    "output_text": "MCP tool authentication expired. Please re-authenticate.",
                }
        raise HTTPException(status_code=502, detail=f"Chat failed: {type(e).__name__}: {e}")
