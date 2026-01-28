import React, { useState } from "react";

function App() {
  const [issues, setIssues] = useState([]);
  const [error, setError] = useState(null);

  const connectJira = () => {
    // This sends the user to the Python backend, which redirects to Atlassian
    window.location.href = "http://localhost:8765/login";
  };

  const loadIssues = async () => {
  try {
    const res = await fetch("http://localhost:8765/issues");
    const data = await res.json();

    if (!res.ok) {
      setError(data.error || JSON.stringify(data) || "Failed to load issues");
      setIssues([]);
      return;
    }

    setError(null);
    setIssues(data.issues || []);
  } catch (err) {
    setError(err.message);
  }
};


  return (
    <div style={{ padding: "2rem", fontFamily: "sans-serif" }}>
      <h1>Jira 3LO Demo</h1>

      <p>
        1. Click <b>Connect Jira</b>, log in & approve.<br />
        2. Come back here and click <b>Load SCRUM Issues</b>.
      </p>

      <button onClick={connectJira}>Connect Jira</button>
      <button onClick={loadIssues} style={{ marginLeft: "1rem" }}>
        Load SCRUM Issues
      </button>

      {error && <p style={{ color: "red" }}>{error}</p>}

      <ul style={{ marginTop: "1.5rem" }}>
        {issues.map((issue) => (
          <li key={issue.id}>
            <strong>{issue.key}</strong> – {issue.fields.summary}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default App;
