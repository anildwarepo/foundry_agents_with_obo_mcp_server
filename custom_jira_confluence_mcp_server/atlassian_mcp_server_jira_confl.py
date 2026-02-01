# mcp_server.py
import os
import httpx
import jwt  # pyjwt
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from dotenv import load_dotenv
from typing import Optional, Tuple
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
import json
import asyncio
from pathlib import Path
from fastmcp.server.auth import AuthContext
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.middleware import Middleware, MiddlewareContext
import threading
from urllib.parse import parse_qs, urlparse

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
    # print(f"Validating Atlassian token...{token}")
    print(f"Validating Atlassian token...")
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


USER_ACCESS_LIST_PATH = Path(
    os.getenv("USER_ACCESS_LIST_FILE", Path(__file__).with_name("user_access_list.json"))
)

_permissions_lock = threading.Lock()
_permissions_mtime: float | None = None
_permissions_by_sub: dict[str, set[str]] = {}


def _safe_str(s: str | None) -> str:
    return (s or "").strip()


def _decode_jwt_claims_unverified(token_str: str) -> dict:
    try:
        if isinstance(token_str, str) and token_str.count(".") == 2:
            return jwt.decode(token_str, options={"verify_signature": False}) or {}
    except Exception:
        pass
    return {}


def _load_access_list_if_needed() -> dict[str, set[str]]:
    """
    Loads user_access_list.json and returns: { sub: {tool1, tool2, ...} }
    Auto-reloads when the file changes.
    Duplicate rows for same sub are UNIONED.
    """
    global _permissions_mtime, _permissions_by_sub

    try:
        stat = USER_ACCESS_LIST_PATH.stat()
    except FileNotFoundError:
        _permissions_by_sub = {}
        _permissions_mtime = None
        return _permissions_by_sub

    if _permissions_mtime == stat.st_mtime:
        return _permissions_by_sub

    with _permissions_lock:
        # re-check inside lock
        stat = USER_ACCESS_LIST_PATH.stat()
        if _permissions_mtime == stat.st_mtime:
            return _permissions_by_sub

        raw = USER_ACCESS_LIST_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)

        new_map: dict[str, set[str]] = {}
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                sub = _safe_str(row.get("user_sub"))
                tools = row.get("allowed_tools") or []
                if not sub:
                    continue
                tool_set = {t.strip() for t in tools if isinstance(t, str) and t.strip()}
                new_map.setdefault(sub, set()).update(tool_set)

        _permissions_by_sub = new_map
        _permissions_mtime = stat.st_mtime

    return _permissions_by_sub


def _get_effective_claims(ctx: AuthContext) -> dict:
    # What FastMCP provides (might be partial)
    provided = getattr(ctx.token, "claims", None)
    provided = provided if isinstance(provided, dict) else {}

    # Raw token string
    raw = getattr(ctx.token, "token", "") or ""

    # Decode JWT (best-effort) even if provided claims exist
    decoded = _decode_jwt_claims_unverified(raw)

    # Merge: decoded JWT claims first, then provided claims override
    # (so if FastMCP *does* provide sub later, it wins)
    merged = dict(decoded)
    merged.update(provided)
    return merged


async def allow_only_tools_from_access_list(ctx: AuthContext) -> bool:
    # Deny if not authenticated
    if ctx.token is None:
        return False

    # MUST have a component to authorize; if not, fail closed
    component = getattr(ctx, "component", None)
    if component is None:
        return False

    # Determine the component name (tool name for tools)
    component_name = getattr(component, "name", None) or getattr(component, "id", None)
    if not isinstance(component_name, str) or not component_name.strip():
        return False

    # Merge claims: decode JWT (best-effort) + any claims verifier provided
    token_str = getattr(ctx.token, "token", "") or ""
    decoded = _decode_jwt_claims_unverified(token_str)
    provided = getattr(ctx.token, "claims", None) or {}
    claims = dict(decoded)
    claims.update(provided)

    user_sub = claims.get("sub")
    if not isinstance(user_sub, str) or not user_sub.strip():
        return False

    # IMPORTANT: this is sync, so DO NOT await
    perms = _load_access_list_if_needed()
    allowed_tools = perms.get(user_sub, set())

    allowed = (component_name in allowed_tools) or ("*" in allowed_tools)
    print(f"AuthZ: sub={user_sub} component={component_name} allowed={allowed}")
    return allowed



class AccessListMiddleware(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)

        token = get_access_token()
        if token is None:
            return []  # or return tools if you want public visibility

        decoded = _decode_jwt_claims_unverified(token.token)
        claims = dict(decoded)
        claims.update(getattr(token, "claims", None) or {})

        sub = claims.get("sub")
        if not sub:
            return []

        allowed = _load_access_list_if_needed().get(sub, set())
        return [t for t in tools if (t.name in allowed or "*" in allowed)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        # Enforce too (otherwise a client could call hidden tools directly)
        token = get_access_token()
        if token is None:
            raise AuthorizationError("Authentication required")

        decoded = _decode_jwt_claims_unverified(token.token)
        claims = dict(decoded)
        claims.update(getattr(token, "claims", None) or {})

        sub = claims.get("sub")
        if not sub:
            raise AuthorizationError("Missing sub claim")

        allowed = _load_access_list_if_needed().get(sub, set())
        tool_name = context.message.name
        if tool_name not in allowed and "*" not in allowed:
            raise AuthorizationError("Not authorized for this tool")

        return await call_next(context)


#mcp = FastMCP(
#    name="Custom MCP server for Jira and Confluence Rest APIs v.18.0",
#    auth=auth,
#    middleware=[AuthMiddleware(auth=allow_only_tools_from_access_list)],
#)

mcp = FastMCP(
    name="Custom MCP server for Jira and Confluence Rest APIs v.20.0",
    auth=auth,
)
mcp.add_middleware(AccessListMiddleware())





# Cache Confluence site resolution (keyed by Atlassian sub if present)
_confluence_site_cache: dict[str, tuple[str, str]] = {}  # key -> (cloud_id, site_url)

def _filter_confluence_resources(resources: list[dict]) -> list[dict]:
    return [r for r in resources if any("confluence" in s for s in (r.get("scopes") or []))]

async def _probe_confluence_cloud_id(atlassian_access_token: str, cloud_id: str) -> bool:
    # lightweight endpoint that should 200 when Confluence is reachable for that site
    url = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/space?limit=1"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {atlassian_access_token}", "Accept": "application/json"})
        return r.status_code == 200

async def _resolve_confluence_site(atlassian_access_token: str, sub: str | None = None) -> Tuple[str, str]:
    """
    Returns (cloud_id, site_url) for a Confluence-accessible site.
    site_url is the human site base like https://your-site.atlassian.net
    """

    print("Resolving Confluence site...")

    cache_k = f"sub:{sub}" if sub else _cache_key(atlassian_access_token)
    if cache_k in _confluence_site_cache:
        return _confluence_site_cache[cache_k]

    resources = await _fetch_accessible_resources(atlassian_access_token)
    conf_resources = _filter_confluence_resources(resources)
    if not conf_resources:
        raise RuntimeError(f"No Confluence resources found. Accessible resources: {resources}")

    print(f"Found {len(conf_resources)} Confluence resources.")
    print("Resources:", conf_resources)

    # If single, take it
    if len(conf_resources) == 1:
        cloud_id = conf_resources[0]["id"]
        site_url = conf_resources[0].get("url") or ""
        _confluence_site_cache[cache_k] = (cloud_id, site_url)
        return cloud_id, site_url

    # Otherwise probe
    for r in conf_resources:
        cid = r.get("id")
        if cid and await _probe_confluence_cloud_id(atlassian_access_token, cid):
            site_url = r.get("url") or ""
            _confluence_site_cache[cache_k] = (cid, site_url)
            return cid, site_url

    # Fallback
    cloud_id = conf_resources[0]["id"]
    site_url = conf_resources[0].get("url") or ""
    _confluence_site_cache[cache_k] = (cloud_id, site_url)
    return cloud_id, site_url

def _escape_cql_string(s: str) -> str:
    # basic escaping for double-quoted CQL string literals
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _build_confluence_page_url(base_url: str, webui_or_tinyui: str) -> str:
    """
    base_url should look like https://your-site.atlassian.net/wiki (no trailing slash preferred)
    webui usually looks like /spaces/KEY/pages/123/Title
    """
    if webui_or_tinyui.startswith("http://") or webui_or_tinyui.startswith("https://"):
        return webui_or_tinyui

    base = base_url.rstrip("/")
    path = webui_or_tinyui if webui_or_tinyui.startswith("/") else f"/{webui_or_tinyui}"
    return f"{base}{path}"

async def _confluence_search_with_token(
    *,
    atlassian_access_token: str,
    cloud_id: str,
    cql: str,
    limit: int,
    cursor: str | None = None,
    expand: list[str] | None = None,
) -> dict:
    url = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/search"

    params: dict = {"cql": cql, "limit": max(1, min(int(limit), 50))}
    if cursor:
        params["cursor"] = cursor
    if expand:
        # Confluence accepts comma-separated expand paths
        params["expand"] = ",".join(expand)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {atlassian_access_token}", "Accept": "application/json"},
            params=params,
        )
        r.raise_for_status()
        return r.json()



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





class ConfluencePageContent(BaseModel):
    representation: Literal["storage", "view", "export_view", "styled_view"] = Field(
        default="storage",
        description="Confluence body representation returned in content.value."
    )
    value: Optional[str] = Field(
        default=None,
        description="Page body content (HTML-like for 'storage'/'view'). May be truncated."
    )


class ConfluencePageHit(BaseModel):
    id: Optional[str] = Field(default=None, description="Confluence content ID.")
    title: Optional[str] = Field(default=None, description="Page title.")
    url: Optional[str] = Field(default=None, description="Human-friendly URL to the page.")
    content: Optional[ConfluencePageContent] = Field(
        default=None,
        description="Page body content (if include_content=True)."
    )


class ConfluenceSearchParams(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description="Search text. Converted into CQL: type=page AND siteSearch ~ \"query\"."
    )
    space_key: Optional[str] = Field(
        default=None,
        description="If provided, restrict search to this Confluence space key (e.g., 'ENG')."
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max results to return (Confluence commonly caps at 50 per request)."
    )
    cursor: Optional[str] = Field(
        default=None,
        description="Cursor for pagination (pass back the cursor from the previous response)."
    )
    include_content: bool = Field(
        default=True,
        description="If true, expands content.body.<representation> and includes page content."
    )
    body_representation: Literal["storage", "view", "export_view", "styled_view"] = Field(
        default="storage",
        description="Which Confluence body representation to return."
    )
    max_chars: int = Field(
        default=8000,
        ge=0,
        description="Truncate returned page body content to this many characters (0 disables)."
    )


class ConfluenceSearchResponse(BaseModel):
    cql: str = Field(..., description="The CQL query used for the search.")
    cloud_id: str = Field(..., description="Resolved Atlassian cloudId for Confluence.")
    count: int = Field(..., description="Number of results returned in this page.")
    urls: List[str] = Field(default_factory=list, description="Convenience list of page URLs.")
    pages: List[ConfluencePageHit] = Field(default_factory=list, description="Search results with metadata.")
    next_cursor: Optional[str] = Field(
        default=None,
        description="Cursor to fetch the next page of results (None if no next page)."
    )


@mcp.tool
async def jira_list_issues(project_key: str = "SCRUM", max_results: int = 10) -> dict:
    """
    This tools lists Jira issues for a given project key.
    

    :type project_key: the project key in Jira
    :type project_key: str
    :param max_results: the maximum number of results to return
    :type max_results: int
    :return: a dictionary containing the Jira issues
    :rtype: dict
    """
   
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




@mcp.tool
async def confluence_search_pages(
    query: str,
    space_key: str = "",          # empty string means "no space filter"
    max_results: int = 10,
    representation: str = "storage",  # "storage" or "view" recommended
    max_chars: int = 4000,
    cursor: str = "",             # empty means "first page"
) -> dict:
    """
    This tool searches Confluence pages via CQL and returns URLs + optional page content.
    Returns a JSON-serializable dict.

    :type query: The search query string.
    :type query: str
    :param space_key: (Optional) Confluence space key to restrict search (default: all spaces).
    :type space_key: str
    :param max_results: Maximum number of results to return (1-50, default: 10).
    :type max_results: int
    :param representation: Body representation to return in content (storage, view, export_view, styled_view).
    :type representation: str
    :param max_chars: Truncate returned page content to this many characters (0 = no truncation).
    :type max_chars: int
    :param cursor: (Optional) Cursor for pagination (from previous response).
    :type cursor: str   
    :return: A dictionary containing search results and metadata.
    :rtype: dict
    """

    
    token = get_access_token()
    print("Starting Confluence page search with token:", token.token)
    include_content: bool = True
    # Resolve Confluence site (cloud_id + site_url) using your existing helper
    sub = None
    try:
        if token.token.count(".") == 2:
            claims = jwt.decode(token.token, options={"verify_signature": False})
            sub = claims.get("sub")
    except Exception:
        pass

    cloud_id, site_url = await _resolve_confluence_site(token.token, sub=sub)

    # clamp results to a sane range (Confluence often caps around 50)
    if max_results < 1:
        max_results = 1
    if max_results > 50:
        max_results = 50

    rep = (representation or "storage").strip()
    if rep not in ("storage", "view", "export_view", "styled_view"):
        rep = "storage"

    q = _escape_cql_string((query or "").strip())
    if not q:
        return {"error": "query is required"}

    cql = f'type=page AND siteSearch ~ "{q}"'
    if space_key and space_key.strip():
        sk = _escape_cql_string(space_key.strip())
        cql += f' AND space = "{sk}"'

    # Build expand string (simple + safe)
    expand_parts = ["content._links"]
    if include_content:
        expand_parts.append(f"content.body.{rep}")
        # keep it safe for heavier reps
        if rep in ("export_view", "styled_view") and max_results > 25:
            max_results = 25

    search_url = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/search"
    params = {
        "cql": cql,
        "limit": max_results,
        "expand": ",".join(expand_parts),
    }
    if cursor and cursor.strip():
        params["cursor"] = cursor.strip()

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            search_url,
            headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"},
            params=params,
        )
        r.raise_for_status()
        data = r.json()

    # Base URL to build human links
    links = data.get("_links") or {}
    base_from_api = links.get("base")
    if isinstance(base_from_api, str) and base_from_api.startswith("http"):
        base_url = base_from_api.rstrip("/")
    else:
        base_url = (site_url.rstrip("/") + "/wiki") if site_url else ""

    pages = []
    urls = []

    for item in (data.get("results") or []):
        content = item.get("content") or {}
        clinks = content.get("_links") or {}

        title = content.get("title") or ""
        cid = content.get("id") or ""
        webui = clinks.get("webui") or clinks.get("tinyui") or ""

        page_url = ""
        if base_url and isinstance(webui, str) and webui:
            page_url = _build_confluence_page_url(base_url, webui)
            if page_url:
                urls.append(page_url)

        page_text = ""
        if include_content:
            body_obj = (content.get("body") or {}).get(rep) or {}
            val = body_obj.get("value")
            if isinstance(val, str):
                page_text = val
                if max_chars and max_chars > 0 and len(page_text) > max_chars:
                    page_text = page_text[:max_chars] + "\n...[truncated]"

        pages.append(
            {
                "id": cid,
                "title": title,
                "url": page_url,
                "content": page_text,   # plain string (easy schema)
                "representation": rep,  # plain string
            }
        )

    # Extract next cursor (return empty string if none)
    next_cursor = ""
    next_link = links.get("next")
    if isinstance(next_link, str) and "cursor=" in next_link:
        try:
            qs = parse_qs(urlparse(next_link).query)
            next_cursor = (qs.get("cursor") or [""])[0]
        except Exception:
            next_cursor = ""

    return {
        "cql": cql,
        "cloud_id": cloud_id,
        "count": len(pages),
        "urls": urls,
        "pages": pages,
        "next_cursor": next_cursor,  # pass this back into cursor=""
    }



@mcp.tool
async def check_server_time() -> str:
    """
    A simple health check tool that returns the server time.
    :rtype: str
    """
    return f"Server time is {asyncio.get_event_loop().time()}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8000)
