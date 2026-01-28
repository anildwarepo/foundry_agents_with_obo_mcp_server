import React from "react";
import ReactDOM from "react-dom/client";
import { PublicClientApplication, EventType } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";
import App from "./App";
import { msalConfig } from "./authConfig";
import "./index.css";

const msalInstance = new PublicClientApplication(msalConfig);

async function bootstrap() {
  await msalInstance.initialize();

  // IMPORTANT: completes the redirect flow and caches the account/tokens
  const resp = await msalInstance.handleRedirectPromise().catch((e) => {
    console.error("handleRedirectPromise error", e);
    return null;
  });

  // Prefer the account from the redirect response
  if (resp?.account) {
    msalInstance.setActiveAccount(resp.account);
  } else {
    const accounts = msalInstance.getAllAccounts();
    if (accounts.length > 0) msalInstance.setActiveAccount(accounts[0]);
  }

  // Optional: keep active account updated
  msalInstance.addEventCallback((event) => {
    if (event.eventType === EventType.LOGIN_SUCCESS && event.payload?.account) {
      msalInstance.setActiveAccount(event.payload.account);
    }
    if (event.eventType === EventType.LOGIN_FAILURE) {
      console.error("LOGIN_FAILURE", event.error);
    }
    if (event.eventType === EventType.ACQUIRE_TOKEN_FAILURE) {
      console.error("ACQUIRE_TOKEN_FAILURE", event.error);
    }
  });

  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(
    <React.StrictMode>
      <MsalProvider instance={msalInstance}>
        <App />
      </MsalProvider>
    </React.StrictMode>
  );
}

bootstrap();
