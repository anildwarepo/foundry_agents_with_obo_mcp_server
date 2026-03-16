export const msalConfig = {
  auth: {
    clientId: "48449491-8390-4af0-8121-da7af091ad56",
    authority: "https://login.microsoftonline.com/150305b3-cc4b-46dd-9912-425678db1498",
    redirectUri: "http://localhost:3500",
  },
  cache: {
    cacheLocation: "sessionStorage",
    storeAuthStateInCookie: false,
  },
};

export const loginRequest = {
  scopes: ["api://48449491-8390-4af0-8121-da7af091ad56/read"],
};

// Azure AD doesn't allow multiple .default scopes in one request
// Request tokens separately for each resource
export const foundryLoginRequest = {
  scopes: ["https://ai.azure.com/.default"],
};

export const fabricLoginRequest = {
  scopes: ["https://api.fabric.microsoft.com/.default"],
};