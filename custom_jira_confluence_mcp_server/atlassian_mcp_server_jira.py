# mcp_server.py
import os
import httpx
import jwt  # pyjwt
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from dotenv import load_dotenv
import json
import asyncio
from pathlib import Path
from fastmcp.server.auth import AuthContext
from fastmcp.server.middleware import AuthMiddleware


load_dotenv()


ATLASSIAN_CLIENT_ID = os.environ["ATLASSIAN_CLIENT_ID"]
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# Cache: key by Atlassian sub when possible, else by token prefix
_cloud_cache: dict[str, str] = {}

def _cache_key(token_str: str) -> str:
    # If token is a JWT, we can get a stable-ish sub
    try:
        if token_str.count(".") == 2:
            claims = jwt.decode(token_str, options={"verify_signature": False})
            sub = claims.get("sub")
            if sub:
                return f"sub:{sub}"
    except Exception:
        pass
    return f"tok:{token_str[:32]}"  # fallback

async def validate_atlassian_token(token: str) -> bool:
    print(f"Validating Atlassian token...{token}")
    # If Atlassian accepts the token, we accept it.
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            ACCESSIBLE_RESOURCES_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    return r.status_code == 200

auth = DebugTokenVerifier(
    validate=validate_atlassian_token,
    client_id=ATLASSIAN_CLIENT_ID,  # shows up in AccessToken metadata
    scopes=[],                      # we’ll enforce scopes ourselves if needed
)

USER_PERMISSIONS_PATH = Path(
    os.getenv("USER_PERMISSIONS_FILE", Path(__file__).with_name("user_permissions.json"))
)

_permissions_lock = asyncio.Lock()
_permissions_mtime: float | None = None
_permissions_by_email: dict[str, set[str]] = {}

_identity_email_cache: dict[str, str] = {}  # sub -> email


def _safe_lower(s: str | None) -> str:
    return (s or "").strip().lower()


async def _load_permissions_if_needed() -> dict[str, set[str]]:
    """
    Loads user_permissions.json and returns: { email_lower: {tool1, tool2, ...} }.
    Auto-reloads when the file changes.
    """
    global _permissions_mtime, _permissions_by_email

    try:
        stat = USER_PERMISSIONS_PATH.stat()
    except FileNotFoundError:
        _permissions_by_email = {}
        _permissions_mtime = None
        return _permissions_by_email

    if _permissions_mtime == stat.st_mtime:
        return _permissions_by_email

    async with _permissions_lock:
        # Re-check inside lock
        stat = USER_PERMISSIONS_PATH.stat()
        if _permissions_mtime == stat.st_mtime:
            return _permissions_by_email

        raw = USER_PERMISSIONS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)

        new_map: dict[str, set[str]] = {}
        for row in data if isinstance(data, list) else []:
            email = _safe_lower(row.get("user_email"))
            tools = row.get("allowed_tools") or []
            if email:
                new_map[email] = {t for t in tools if isinstance(t, str) and t.strip()}

        _permissions_by_email = new_map
        _permissions_mtime = stat.st_mtime

    return _permissions_by_email


def _decode_jwt_claims_unverified(token_str: str) -> dict:
    """
    Best-effort decode (NO signature verification). Your DebugTokenVerifier is already
    doing an online validity check, so this is used only to read claims.
    """
    try:
        if token_str.count(".") == 2:
            return jwt.decode(token_str, options={"verify_signature": False}) or {}
    except Exception:
        pass
    return {}


def _extract_email_from_claims(claims: dict) -> str | None:
    """
    Tries common email claim names across IdPs.
    """
    # Common IdP keys
    for k in ("email", "upn", "preferred_username", "unique_name"):
        v = claims.get(k)
        if isinstance(v, str) and "@" in v:
            return v

    # Atlassian-specific keys you might see (varies by token type)
    for k in (
        "https://atlassian.com/systemAccountEmail",
        "https://id.atlassian.com/email",
    ):
        v = claims.get(k)
        if isinstance(v, str) and "@" in v:
            return v

    # Sometimes emails are in lists
    v = claims.get("emails")
    if isinstance(v, list):
        for item in v:
            if isinstance(item, str) and "@" in item:
                return item

    return None


async def _fetch_email_via_atlassian_identity_api(atlassian_access_token: str) -> str | None:
    """
    Optional fallback: GET https://api.atlassian.com/me
    Requires adding the User Identity API and scope `read:me` to your 3LO app.
    """
    url = "https://api.atlassian.com/me"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {atlassian_access_token}"})
        if r.status_code != 200:
            return None
        data = r.json() if r.content else {}
        # field names can vary; "email" is commonly present when allowed
        email = data.get("email")
        if isinstance(email, str) and "@" in email:
            return email
    except Exception:
        return None
    return None


def _get_component_name(ctx: AuthContext) -> str | None:
    comp = ctx.component
    if comp is None:
        return None
    # Tools typically have .name; be defensive
    name = getattr(comp, "name", None) or getattr(comp, "id", None)
    return name if isinstance(name, str) and name else None


def _get_raw_token_from_ctx(ctx: AuthContext) -> str | None:
    """
    AccessToken implementations vary; try common attribute names.
    """
    t = ctx.token
    if t is None:
        return None
    for attr in ("token", "access_token", "value", "raw"):
        v = getattr(t, attr, None)
        if isinstance(v, str) and v:
            return v
    return None


async def allow_only_tools_from_user_permissions(ctx: AuthContext) -> bool:
    """
    AuthMiddleware uses this for:
      - filtering listTools (so clients only see allowed tools)
      - blocking execution of tools not allowed
    """
    if ctx.token is None:
        return False

    tool_name = _get_component_name(ctx)
    if not tool_name:
        # If it's not a tool/prompt/resource with a name, deny by default
        return False

    raw_token = _get_raw_token_from_ctx(ctx) or ""
    claims = getattr(ctx.token, "claims", None)
    if not isinstance(claims, dict) or not claims:
        claims = _decode_jwt_claims_unverified(raw_token)

    email = _extract_email_from_claims(claims)

    # If no email claim, optionally resolve via Atlassian Identity API using `sub` cache
    if not email:
        sub = claims.get("sub") if isinstance(claims.get("sub"), str) else None
        if sub and sub in _identity_email_cache:
            email = _identity_email_cache[sub]
        else:
            resolved = await _fetch_email_via_atlassian_identity_api(raw_token)
            if resolved:
                email = resolved
                if sub:
                    _identity_email_cache[sub] = resolved

    if not email:
        # No usable identity -> deny (or switch to sub-based permissions if you prefer)
        return False

    perms = await _load_permissions_if_needed()
    allowed = perms.get(_safe_lower(email), set())
    return tool_name in allowed


mcp = FastMCP(
    name="Custom MCP server for Jira and Confluence Rest APIs",
    auth=auth,
    middleware=[AuthMiddleware(auth=allow_only_tools_from_user_permissions)],
)



async def _fetch_accessible_resources(atlassian_access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            ACCESSIBLE_RESOURCES_URL,
            headers={"Authorization": f"Bearer {atlassian_access_token}", "Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

def _filter_jira_resources(resources: list[dict]) -> list[dict]:
    return [r for r in resources if any("jira" in s for s in (r.get("scopes") or []))]

async def _probe_jira_cloud_id(atlassian_access_token: str, cloud_id: str) -> bool:
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/serverInfo"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {atlassian_access_token}", "Accept": "application/json"})
        return r.status_code == 200

async def _resolve_jira_cloud_id(atlassian_access_token: str, sub: str | None = None) -> str:
    if sub and sub in _cloud_cache:
        return _cloud_cache[sub]

    resources = await _fetch_accessible_resources(atlassian_access_token)
    jira_resources = _filter_jira_resources(resources)
    if not jira_resources:
        raise RuntimeError(f"No Jira resources found. Accessible resources: {resources}")

    if len(jira_resources) == 1:
        cloud_id = jira_resources[0]["id"]
        if sub:
            _cloud_cache[sub] = cloud_id
        return cloud_id

    for r in jira_resources:
        cid = r.get("id")
        if cid and await _probe_jira_cloud_id(atlassian_access_token, cid):
            if sub:
                _cloud_cache[sub] = cid
            return cid

    cloud_id = jira_resources[0]["id"]
    if sub:
        _cloud_cache[sub] = cloud_id
    return cloud_id


async def _jira_search_with_token(*, atlassian_access_token: str, cloud_id: str, jql: str, max_results: int) -> dict:
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search/jql"
    body = {"jql": jql, "maxResults": max_results, "fields": ["summary", "status", "issuetype", "priority", "created"]}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {atlassian_access_token}", "Accept": "application/json", "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        return r.json()

@mcp.tool
async def jira_list_issues(project_key: str = "SCRUM", max_results: int = 10) -> dict:
    token = get_access_token()  # token.token is the Atlassian access token string

    # Optional: decode for "sub" to cache cloud id; do NOT treat this as secure identity by itself
    claims = jwt.decode(token.token, options={"verify_signature": False})
    sub = claims.get("sub")

    cloud_id = await _resolve_jira_cloud_id(token.token, sub=sub)

    jql = f"project = {project_key} ORDER BY created DESC"
    return await _jira_search_with_token(
        atlassian_access_token=token.token,
        cloud_id=cloud_id,
        jql=jql,
        max_results=max_results,
    )



if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8000)
