from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from azure.ai.projects import AIProjectClient
from azure.core.credentials import AccessToken, TokenCredential

load_dotenv()

TENANT_ID = os.getenv("tenant_id")
EXPECTED_AUDIENCE = "https://ai.azure.com"

foundry_account_name = os.getenv("foundry_account_name")
foundry_project_name = os.getenv("foundry_project_name")


FOUNDRY_PROJECT_ENDPOINT = f"https://{foundry_account_name}.services.ai.azure.com/api/projects/{foundry_project_name}"

print("FOUNDRY_PROJECT_ENDPOINT:", FOUNDRY_PROJECT_ENDPOINT)

JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
jwk_client = PyJWKClient(JWKS_URL)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
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


def decode_and_validate_bearer(auth_header: Optional[str]) -> Dict[str, Any]:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer token")

    token = auth_header.split(" ", 1)[1].strip()

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token).key
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token signing key")

    allowed_issuers = {
        f"https://sts.windows.net/{TENANT_ID}/",
        f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
    }

    last_err = None
    for issuer in allowed_issuers:
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=EXPECTED_AUDIENCE,
                issuer=issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                },
            )

            # Optional: enforce at least user_impersonation if present
            scp = (claims.get("scp") or "").split()
            if scp and ("user_impersonation" not in scp):
                raise HTTPException(status_code=403, detail="Missing required scope: user_impersonation")

            return {"token": token, "claims": claims}
        except Exception as e:
            last_err = e

    raise HTTPException(status_code=401, detail=f"Token validation failed: {type(last_err).__name__}")


def create_foundry_client_from_token(token: str, claims: Dict[str, Any]) -> AIProjectClient:
    exp = int(claims.get("exp", time.time() + 3600))
    cred = BearerTokenCredential(token, exp)
    return AIProjectClient(endpoint=FOUNDRY_PROJECT_ENDPOINT, credential=cred)


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
def get_agent(agent_name: str, authorization: Optional[str] = Header(default=None)):
    ctx = decode_and_validate_bearer(authorization)
    project_client = create_foundry_client_from_token(ctx["token"], ctx["claims"])

    try:
        agent = project_client.agents.get(agent_name=agent_name)
        return {"name": agent.name, "id": getattr(agent, "id", None)}
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
def chat(req: ChatRequest, authorization: Optional[str] = Header(default=None)):
    ctx = decode_and_validate_bearer(authorization)
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
        raise HTTPException(status_code=502, detail=f"Chat failed: {type(e).__name__}: {e}")
