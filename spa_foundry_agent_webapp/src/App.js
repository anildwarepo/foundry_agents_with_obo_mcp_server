// src/App.js
import React, { useEffect, useMemo, useRef, useState } from "react";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";
import { InteractionRequiredAuthError, InteractionStatus } from "@azure/msal-browser";
import { foundryLoginRequest } from "./authConfig";

const BACKEND_BASE = "http://localhost:8765";
const DEFAULT_AGENT_NAME = "MultiToolAgentV1";

function decodeJwtPayload(token) {
  try {
    const [, payload] = token.split(".");
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(base64)
        .split("")
        .map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2))
        .join("")
    );
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function Row({ label, value }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "180px 1fr",
        gap: 12,
        padding: "8px 0",
        borderTop: "1px solid #f1f5f9",
      }}
    >
      <div style={{ color: "#475569", fontSize: 13 }}>{label}</div>
      <div style={{ fontSize: 13, overflowWrap: "anywhere" }}>{value ?? "—"}</div>
    </div>
  );
}

function Bubble({ role, text }) {
  const isUser = role === "user";
  return (
    <div
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        margin: "8px 0",
      }}
    >
      <div
        style={{
          maxWidth: "78%",
          padding: "10px 12px",
          borderRadius: 14,
          background: isUser ? "#2563eb" : "#0f172a",
          color: "white",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          lineHeight: 1.4,
          fontSize: 13,
        }}
      >
        {text}
      </div>
    </div>
  );
}

export default function App() {
  const { instance, accounts, inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  const account = useMemo(
    () => instance.getActiveAccount() ?? accounts?.[0] ?? null,
    [instance, accounts]
  );

  const [error, setError] = useState(null);
  const [needsConsent, setNeedsConsent] = useState(false);

  const [accessToken, setAccessToken] = useState(null);
  const [showToken, setShowToken] = useState(false);
  const [tokenClaims, setTokenClaims] = useState(null);

  const [backendLoading, setBackendLoading] = useState(false);
  const [backendResult, setBackendResult] = useState(null);

  // Chat state
  const [agentName, setAgentName] = useState(DEFAULT_AGENT_NAME);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [messages, setMessages] = useState([]); // { role: "user"|"assistant", text: string }
  const [previousResponseId, setPreviousResponseId] = useState(null);

  // MCP approvals
  const [pendingApprovals, setPendingApprovals] = useState(null); // [{id, server_label, tool_name, arguments}]
  const [approvalDecisions, setApprovalDecisions] = useState({}); // { [id]: boolean }
  const [pendingResponseIdForApproval, setPendingResponseIdForApproval] = useState(null);

  // OAuth consent (from Foundry oauth_consent_request)
  const [oauthConsentLink, setOauthConsentLink] = useState(null);
  const [pendingResponseIdForOauth, setPendingResponseIdForOauth] = useState(null);

  const chatEndRef = useRef(null);

  const scrollChatToBottom = () => {
    requestAnimationFrame(() => chatEndRef.current?.scrollIntoView({ behavior: "smooth" }));
  };

  useEffect(() => {
    scrollChatToBottom();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, pendingApprovals, oauthConsentLink, chatLoading]);

  const login = async () => {
    setError(null);
    setNeedsConsent(false);
    setBackendResult(null);
    await instance.loginRedirect(foundryLoginRequest);
  };

  const logout = async () => {
    setError(null);
    setNeedsConsent(false);
    setAccessToken(null);
    setTokenClaims(null);
    setBackendResult(null);

    // clear chat
    setMessages([]);
    setPreviousResponseId(null);
    setPendingApprovals(null);
    setApprovalDecisions({});
    setPendingResponseIdForApproval(null);
    setOauthConsentLink(null);
    setPendingResponseIdForOauth(null);

    await instance.logoutRedirect({ account });
  };

  const tryGetTokenSilent = async () => {
    if (!account) return null;

    setError(null);
    try {
      const res = await instance.acquireTokenSilent({
        ...foundryLoginRequest,
        account,
      });

      setAccessToken(res.accessToken);
      setTokenClaims(decodeJwtPayload(res.accessToken));
      setNeedsConsent(false);

      return res.accessToken;
    } catch (e) {
      if (e instanceof InteractionRequiredAuthError) {
        setNeedsConsent(true);
        return null;
      }
      setError(e?.message || String(e));
      return null;
    }
  };

  const consentAndGetToken = async () => {
    if (!account) return;
    setError(null);
    setNeedsConsent(false);
    await instance.acquireTokenRedirect({ ...foundryLoginRequest, account });
  };

  const callBackend = async () => {
    if (!account) return;

    setError(null);
    setBackendResult(null);
    setBackendLoading(true);

    try {
      let token = accessToken;
      if (!token) token = await tryGetTokenSilent();
      if (!token) return;

      const apiRes = await fetch(`${BACKEND_BASE}/agents/${encodeURIComponent(agentName)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      const data = await apiRes.json().catch(() => ({}));
      if (!apiRes.ok) throw new Error(data?.detail || data?.error || `Backend error: ${apiRes.status}`);

      setBackendResult(data);
    } catch (e) {
      if (e instanceof InteractionRequiredAuthError) setNeedsConsent(true);
      else setError(e?.message || String(e));
    } finally {
      setBackendLoading(false);
    }
  };

  const resetChat = () => {
    setMessages([]);
    setPreviousResponseId(null);
    setPendingApprovals(null);
    setApprovalDecisions({});
    setPendingResponseIdForApproval(null);
    setOauthConsentLink(null);
    setPendingResponseIdForOauth(null);
    setError(null);
  };

  const postChat = async (body) => {
    let token = accessToken;
    if (!token) token = await tryGetTokenSilent();
    if (!token) return { ok: false, error: "No access token (consent required?)" };

    const res = await fetch(`${BACKEND_BASE}/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, error: data?.detail || data?.error || `HTTP ${res.status}`, data };
    return { ok: true, data };
  };

  const applyChatResponse = (data) => {
    // 1) OAuth consent required (Foundry oauth_consent_request)
    if (data.status === "oauth_consent_required") {
      setOauthConsentLink(data.consent_link || null);
      setPendingResponseIdForOauth(data.response_id || null);

      // Keep chain moving from this response id
      setPreviousResponseId(data.response_id || null);

      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          text:
            "OAuth consent required to access the MCP tools. Open the consent link below, complete sign-in, then click “I completed sign-in”.",
        },
      ]);
      return;
    }

    // 2) MCP approvals required
    if (data.status === "approval_required") {
      setPendingApprovals(data.approval_requests || []);
      setPendingResponseIdForApproval(data.response_id || null);

      // Keep chain moving from this response id
      setPreviousResponseId(data.response_id || null);

      const defaults = {};
      for (const r of data.approval_requests || []) defaults[r.id] = false;
      setApprovalDecisions(defaults);
      return;
    }

    // 3) Final output
    const out = data.output_text || "";
    setMessages((m) => [...m, { role: "assistant", text: out || "(no output_text)" }]);
    setPreviousResponseId(data.response_id || null);

    // Clear pending flows
    setPendingApprovals(null);
    setPendingResponseIdForApproval(null);
    setApprovalDecisions({});
    setOauthConsentLink(null);
    setPendingResponseIdForOauth(null);
  };

  const sendMessage = async () => {
    const text = chatInput.trim();
    if (!text || chatLoading) return;

    setError(null);
    setChatInput("");
    setChatLoading(true);

    // add user message
    setMessages((m) => [...m, { role: "user", text }]);

    try {
      const { ok, data, error: err } = await postChat({
        agent_name: agentName,
        message: text,
        previous_response_id: previousResponseId,
      });

      if (!ok) throw new Error(err || "Chat request failed");
      applyChatResponse(data);
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setChatLoading(false);
    }
  };

  // User completed OAuth consent in the external tab; continue the Foundry response chain.
  const resumeAfterOauthConsent = async () => {
    if (!pendingResponseIdForOauth || chatLoading) return;

    setError(null);
    setChatLoading(true);

    try {
      const { ok, data, error: err } = await postChat({
        agent_name: agentName,
        previous_response_id: pendingResponseIdForOauth,
        action: "continue",
      });

      if (!ok) throw new Error(err || "Continue after OAuth consent failed");
      applyChatResponse(data);
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setChatLoading(false);
    }
  };

  const submitApprovals = async () => {
    if (!pendingApprovals?.length || !pendingResponseIdForApproval || chatLoading) return;

    setError(null);
    setChatLoading(true);

    try {
      const approvals = pendingApprovals.map((r) => ({
        approval_request_id: r.id,
        approve: !!approvalDecisions[r.id],
      }));

      const { ok, data, error: err } = await postChat({
        agent_name: agentName,
        previous_response_id: pendingResponseIdForApproval,
        approvals,
      });

      if (!ok) throw new Error(err || "Approval submit failed");
      applyChatResponse(data);
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setChatLoading(false);
    }
  };

  // After authentication completes (and MSAL is idle), attempt silent token acquisition once.
  useEffect(() => {
    if (!isAuthenticated || !account) return;
    if (inProgress !== InteractionStatus.None) return;
    tryGetTokenSilent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, account, inProgress]);

  const idClaims = account?.idTokenClaims;

  return (
    <div
      style={{
        padding: "2rem",
        fontFamily: "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial",
        background: "#f6f7fb",
        minHeight: "100vh",
      }}
    >
      <div style={{ maxWidth: 980, margin: "0 auto" }}>
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: 16,
            marginBottom: 16,
          }}
        >
          <div>
            <h1 style={{ margin: 0 }}>Foundry Auth Demo</h1>
            <p style={{ marginTop: 6, color: "#475569" }}>
              Scope requested: <code>https://ai.azure.com/.default</code>
            </p>
          </div>

          {!isAuthenticated ? (
            <button onClick={login}>Sign in</button>
          ) : (
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              <button onClick={tryGetTokenSilent}>Refresh token (silent)</button>
              <button onClick={callBackend} disabled={backendLoading}>
                {backendLoading ? "Calling backend..." : "Call Backend (get agent)"}
              </button>
              <button onClick={logout}>Sign out</button>
            </div>
          )}
        </header>

        {error && (
          <div
            style={{
              background: "#fff1f2",
              border: "1px solid #fecdd3",
              color: "#9f1239",
              padding: "10px 12px",
              borderRadius: 12,
              marginBottom: 14,
            }}
          >
            {error}
          </div>
        )}

        {!isAuthenticated && (
          <div
            style={{
              background: "white",
              border: "1px solid #e2e8f0",
              borderRadius: 14,
              padding: 14,
            }}
          >
            <h2 style={{ marginTop: 0, fontSize: 16 }}>What this does</h2>
            <ul style={{ margin: "8px 0 0 18px", color: "#334155" }}>
              <li>Redirect sign-in with MSAL</li>
              <li>Requests an access token for <code>https://ai.azure.com/.default</code></li>
              <li>Calls your backend with <code>Authorization: Bearer &lt;token&gt;</code></li>
              <li>Chat UI supports MCP approvals + OAuth consent links</li>
            </ul>
          </div>
        )}

        {isAuthenticated && account && (
          <div style={{ display: "grid", gap: 14, marginTop: 14 }}>
            {/* Chat UI */}
            <div
              style={{
                background: "white",
                border: "1px solid #e2e8f0",
                borderRadius: 14,
                padding: 14,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                <div>
                  <h2 style={{ marginTop: 0, fontSize: 16 }}>Chat</h2>
                  <p style={{ margin: "6px 0 0 0", color: "#475569", fontSize: 13 }}>
                    Agent:{" "}
                    <input
                      value={agentName}
                      onChange={(e) => setAgentName(e.target.value)}
                      style={{
                        border: "1px solid #e2e8f0",
                        borderRadius: 10,
                        padding: "6px 8px",
                        fontSize: 13,
                        width: 320,
                        maxWidth: "100%",
                      }}
                    />
                  </p>
                </div>

                <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                  <button onClick={resetChat} disabled={chatLoading}>
                    New chat
                  </button>
                </div>
              </div>

              <div
                style={{
                  marginTop: 12,
                  background: "#0b1220",
                  borderRadius: 14,
                  padding: 12,
                  height: 340,
                  overflowY: "auto",
                }}
              >
                {messages.length === 0 ? (
                  <div style={{ color: "#cbd5e1", fontSize: 13 }}>
                    Send a message to start chatting with your backend agent.
                  </div>
                ) : (
                  messages.map((m, idx) => <Bubble key={idx} role={m.role} text={m.text} />)
                )}

                {chatLoading && (
                  <div style={{ color: "#cbd5e1", fontSize: 13, marginTop: 8 }}>…thinking</div>
                )}

                <div ref={chatEndRef} />
              </div>

              {/* OAuth consent required */}
              {oauthConsentLink && (
                <div
                  style={{
                    marginTop: 12,
                    background: "#ecfeff",
                    border: "1px solid #a5f3fc",
                    borderRadius: 14,
                    padding: 12,
                  }}
                >
                  <div style={{ fontWeight: 700, color: "#155e75", marginBottom: 6 }}>
                    Sign in required (OAuth consent)
                  </div>
                  <div style={{ color: "#155e75", fontSize: 13, marginBottom: 10 }}>
                    Open the consent link in a new tab and complete sign-in. Then come back and click “I completed sign-in”.
                  </div>

                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                    <a
                      href={oauthConsentLink}
                      target="_blank"
                      rel="noreferrer"
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 8,
                        padding: "10px 12px",
                        borderRadius: 12,
                        background: "#0891b2",
                        color: "white",
                        textDecoration: "none",
                        fontSize: 13,
                        fontWeight: 700,
                      }}
                    >
                      Open consent link
                    </a>

                    <button onClick={resumeAfterOauthConsent} disabled={chatLoading}>
                      I completed sign-in
                    </button>
                  </div>

                  {pendingResponseIdForOauth && (
                    <div style={{ marginTop: 10, fontSize: 12, color: "#0e7490" }}>
                      (Debug) pending_response_id: <code>{pendingResponseIdForOauth}</code>
                    </div>
                  )}
                </div>
              )}

              {/* MCP approvals */}
              {pendingApprovals?.length > 0 && (
                <div
                  style={{
                    marginTop: 12,
                    background: "#fff7ed",
                    border: "1px solid #fed7aa",
                    borderRadius: 14,
                    padding: 12,
                  }}
                >
                  <div style={{ fontWeight: 600, color: "#9a3412", marginBottom: 6 }}>
                    MCP approval required
                  </div>
                  <div style={{ color: "#9a3412", fontSize: 13, marginBottom: 10 }}>
                    Review each tool call and approve/deny. Then click “Submit approvals”.
                  </div>

                  <div style={{ display: "grid", gap: 10 }}>
                    {pendingApprovals.map((r) => (
                      <div
                        key={r.id}
                        style={{
                          background: "white",
                          border: "1px solid #fed7aa",
                          borderRadius: 12,
                          padding: 10,
                        }}
                      >
                        <Row label="Server" value={r.server_label} />
                        <Row label="Tool" value={r.tool_name} />
                        <Row label="Request ID" value={r.id} />

                        <div style={{ marginTop: 8, fontSize: 13, color: "#334155" }}>
                          Arguments:
                          <pre
                            style={{
                              marginTop: 6,
                              marginBottom: 0,
                              padding: 10,
                              background: "#0b1220",
                              color: "white",
                              borderRadius: 10,
                              overflowX: "auto",
                              fontSize: 12,
                              lineHeight: 1.4,
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-word",
                            }}
                          >
                            {JSON.stringify(r.arguments ?? {}, null, 2)}
                          </pre>
                        </div>

                        <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
                          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                            <input
                              type="checkbox"
                              checked={!!approvalDecisions[r.id]}
                              onChange={(e) =>
                                setApprovalDecisions((prev) => ({
                                  ...prev,
                                  [r.id]: e.target.checked,
                                }))
                              }
                            />
                            Approve
                          </label>
                          {!approvalDecisions[r.id] && (
                            <span style={{ color: "#64748b", fontSize: 13 }}>Denied by default</span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>

                  <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
                    <button onClick={submitApprovals} disabled={chatLoading}>
                      Submit approvals
                    </button>
                    <button
                      onClick={() => {
                        const denied = {};
                        for (const r of pendingApprovals) denied[r.id] = false;
                        setApprovalDecisions(denied);
                      }}
                      disabled={chatLoading}
                    >
                      Deny all
                    </button>
                  </div>
                </div>
              )}

              {/* Input row */}
              <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
                <input
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      sendMessage();
                    }
                  }}
                  placeholder="Type a message…"
                  disabled={chatLoading || needsConsent || !!oauthConsentLink}
                  style={{
                    flex: 1,
                    border: "1px solid #e2e8f0",
                    borderRadius: 12,
                    padding: "10px 12px",
                    fontSize: 13,
                  }}
                />
                <button
                  onClick={sendMessage}
                  disabled={chatLoading || needsConsent || !!oauthConsentLink || !chatInput.trim()}
                >
                  Send
                </button>
              </div>

              {oauthConsentLink && (
                <div style={{ marginTop: 10, color: "#155e75", fontSize: 13 }}>
                  Complete OAuth consent above to enable MCP tools, then continue.
                </div>
              )}

              {needsConsent && (
                <div style={{ marginTop: 10, color: "#b45309", fontSize: 13 }}>
                  Consent required to get token. Use “Continue to consent” in the Token status panel below.
                </div>
              )}
            </div>

            {/* User + Token panels */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 14 }}>
              <div
                style={{
                  background: "white",
                  border: "1px solid #e2e8f0",
                  borderRadius: 14,
                  padding: 14,
                }}
              >
                <h2 style={{ marginTop: 0, fontSize: 16 }}>User (from ID token)</h2>
                <Row label="Name" value={account.name} />
                <Row label="Username" value={account.username} />
                <Row label="Tenant (tid)" value={idClaims?.tid} />
                <Row label="Object ID (oid)" value={idClaims?.oid} />
                <Row label="preferred_username" value={idClaims?.preferred_username} />
              </div>

              <div
                style={{
                  background: "white",
                  border: "1px solid #e2e8f0",
                  borderRadius: 14,
                  padding: 14,
                }}
              >
                <h2 style={{ marginTop: 0, fontSize: 16 }}>Token status</h2>

                {needsConsent ? (
                  <>
                    <p style={{ color: "#475569", fontSize: 13 }}>
                      Consent / interaction is required to get the access token.
                    </p>
                    <button onClick={consentAndGetToken}>Continue to consent</button>
                  </>
                ) : (
                  <>
                    <p style={{ color: "#475569", fontSize: 13 }}>
                      Showing a few decoded claims (not the raw token).
                    </p>
                    <Row label="aud" value={tokenClaims?.aud} />
                    <Row label="scp" value={tokenClaims?.scp} />
                    <Row label="appid" value={tokenClaims?.appid} />
                    <Row
                      label="exp"
                      value={tokenClaims?.exp ? new Date(tokenClaims.exp * 1000).toISOString() : "—"}
                    />
                  </>
                )}

                <div style={{ marginTop: 14 }}>
                  <h3 style={{ margin: "0 0 8px 0", fontSize: 14 }}>Access token (debug)</h3>

                  {!accessToken ? (
                    <p style={{ color: "#475569", fontSize: 13, margin: 0 }}>
                      No access token acquired yet. Click “Refresh token (silent)” or complete consent.
                    </p>
                  ) : (
                    <>
                      <div style={{ display: "flex", gap: 10, marginBottom: 10 }}>
                        <button onClick={() => setShowToken((v) => !v)}>
                          {showToken ? "Hide token" : "Show token"}
                        </button>
                        <button onClick={() => navigator.clipboard.writeText(accessToken)} title="Copy token">
                          Copy
                        </button>
                      </div>

                      <pre
                        style={{
                          margin: 0,
                          padding: 12,
                          background: "#0b1220",
                          color: "white",
                          borderRadius: 10,
                          overflowX: "auto",
                          fontSize: 12,
                          lineHeight: 1.4,
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-all",
                        }}
                      >
                        {showToken
                          ? accessToken
                          : `${accessToken.slice(0, 30)}…${accessToken.slice(-30)}`}
                      </pre>
                    </>
                  )}
                </div>
              </div>

              <div
                style={{
                  background: "white",
                  border: "1px solid #e2e8f0",
                  borderRadius: 14,
                  padding: 14,
                }}
              >
                <h2 style={{ marginTop: 0, fontSize: 16 }}>Backend response</h2>

                {!backendResult ? (
                  <p style={{ color: "#475569", fontSize: 13, margin: 0 }}>
                    Click “Call Backend (get agent)” to test the authenticated API call.
                  </p>
                ) : (
                  <pre
                    style={{
                      margin: 0,
                      padding: 12,
                      background: "#0b1220",
                      color: "white",
                      borderRadius: 10,
                      overflowX: "auto",
                      fontSize: 12,
                      lineHeight: 1.4,
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}
                  >
                    {JSON.stringify(backendResult, null, 2)}
                  </pre>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
