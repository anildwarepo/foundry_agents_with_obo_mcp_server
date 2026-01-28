# mcp_server.py
import os
import httpx
import jwt  # pyjwt
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from dotenv import load_dotenv

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

mcp = FastMCP(name="Jira MCP", auth=auth)



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
