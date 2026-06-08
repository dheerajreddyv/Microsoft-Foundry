# MCP OAuth Web App (Local)

A FastAPI app for **local development and testing**. End-users sign in with
**Entra ID** and an Azure AI **Foundry agent with an OAuth-protected MCP
tool** is invoked **on their behalf** using their **delegated** access token.

The app has **no identity of its own** — no `DefaultAzureCredential`, no
managed identity. Every Foundry call carries the signed-in user's JWT, so
Foundry sees the real user's `oid` and routes per-user MCP-OAuth consent
correctly.

```
Browser ── / ──▶ Landing page ── click "Sign in" ──▶ /login ──▶ Microsoft login
                                                                       │
                                                                       ▼ user signs in
                                                       /auth/callback?code=...
                                                       Exchange code for tokens (server-side)
                                                       (stored in per-user MSAL cache;
                                                        identity in signed cookie)
                                                                       │
                                                                       ▼
Browser ──/api/chat──▶ Web App ──responses.create──▶ Foundry agent
                          │   (Authorization: Bearer <user JWT>)   │
                          │                                        │ keys MCP-OAuth cache by
                          │                                        │ (project, connection, user.oid)
                          │                                        ▼
                          │ ◀── consent_link (first call only) ──── MCP server
                          │                                          (OAuth-protected)
                          │                                        ▲
                          │     (after user consents in browser)   │
                          ▼                                        │
                       /api/chat/resume ──────────────────▶ same agent — now mcp_call succeeds
```

## Architecture in 3 sentences

1. **Sign-in** uses MSAL's **OAuth 2.0 Authorization Code Grant** for a
   **confidential client** with **OpenID Connect** for identity. Tokens
   land in a per-user in-memory MSAL cache keyed by `home_account_id`.
2. **`MsalUserCredential`** (a custom `azure.core.TokenCredential`) hands
   delegated user JWTs to `AIProjectClient` on demand; MSAL silently
   refreshes them using the cached refresh token.
3. **Foundry** validates each JWT, extracts the `oid` claim, and uses it
   as part of the cache key for per-user MCP-OAuth tokens — so consent is
   per-real-user end-to-end.

## Files

```
mcp-oauth-webapp/
├── app.py                  # FastAPI app — 7 routes + 2 helpers (~245 lines)
├── auth.py                 # MSAL helpers + MsalUserCredential (~128 lines)
├── static/
│   ├── landing.html        # Public welcome page with "Sign in" button
│   └── index.html          # Chat UI (vanilla HTML/JS, shown after sign-in)
├── requirements.txt        # 8 packages
├── .env.example            # 12 environment variables (8 are required)
├── .gitignore
└── README.md               # this file
```

## Prerequisites

1. **Python 3.10+** on Windows / Linux / macOS.
2. **An existing Foundry agent** in your project with the OAuth-protected
   MCP tool attached. This app only **invokes** the agent — it does not
   create or modify it. The OAuth client app used by the MCP connection is
   configured inside the Foundry portal, separately from this web app.
3. **An Entra ID app registration** for this web app (steps below).

## Setup

### 1. Register the app in Entra ID

In the Azure portal → **Microsoft Entra ID → App registrations → New
registration**:

- **Name:** `mcp-oauth-webapp-local` (or anything you like)
- **Supported account types:** *Accounts in this organizational directory only*
- **Redirect URI:** Platform = **Web**, value =
  `http://localhost:8765/auth/callback`
- Click **Register**

From the app's **Overview** page, copy:
- **Application (client) ID** → goes into `.env` as `AZURE_CLIENT_ID`
- **Directory (tenant) ID** → goes into `.env` as `AZURE_TENANT_ID`

**Certificates & secrets → New client secret** → copy the **Value**
immediately (only shown once) → goes into `.env` as `AZURE_CLIENT_SECRET`.

**API permissions → Add a permission → Delegated:**
- Microsoft Graph: `openid`, `profile`, `email`, `offline_access`
- Azure Machine Learning Services (or Cognitive Services):
  `user_impersonation`
- Click **Grant admin consent for <tenant>**

**Authentication:**
- Confirm the redirect URI is `http://localhost:8765/auth/callback`.
- Leave "Allow public client flows" = **No** (this is a confidential
  client).

> **Important:** this web app does **NOT** need any permissions on your
> backend MCP server. The OAuth client that talks to the MCP server is the
> one registered inside the Foundry MCP connection — a different app
> registration with different permissions.

### 2. Configure `.env`

```powershell
cd C:\Users\dvalluru\mcp-oauth-webapp
Copy-Item .env.example .env
notepad .env
```

Fill in your values:

```ini
AZURE_TENANT_ID=<from step 1>
AZURE_CLIENT_ID=<from step 1>
AZURE_CLIENT_SECRET=<from step 1>
REDIRECT_URI=http://localhost:8765/auth/callback
AI_FOUNDRY_SCOPE=https://ai.azure.com/.default
SESSION_SECRET=<any long random string — see below>

AZURE_AI_PROJECT_ENDPOINT=https://<your-project>.services.ai.azure.com/api/projects/<your-project>
AGENT_NAME=<name of the existing agent in your Foundry project>
```

Generate a `SESSION_SECRET`:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 3. Install + run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Expected startup output:
```
🚀 Listening. agent=<your-agent> endpoint=https://...
👤 Auth: DELEGATED ONLY (no app credential).
INFO:     Uvicorn running on http://0.0.0.0:8765 (Press CTRL+C to quit)
```

### 4. Open the app

Browse to <http://localhost:8765/> → you'll see a **landing page** with a
**Sign in with Microsoft** button. Click it → sign in → land on the chat
UI.

## Trying the consent flow

1. Send a chat message like `multiply 17 and 23 using the MCP tool`.
2. The first time, the response will say **OAuth consent required** and
   auto-open a Microsoft consent page in a new tab.
3. Approve consent (this authorizes the **MCP connection's** OAuth client
   to call the backend MCP server on your behalf — separate from your
   sign-in to this web app).
4. Return to the chat tab and click **Continue**.
5. The agent now invokes the MCP tool and replies with the result.
6. Send another message — no consent prompt this time. Foundry has cached
   the MCP-OAuth token for `(project, connection, your_oid)`.

### Automatic retry on the MCP token-refresh race

Foundry's cached MCP-OAuth token occasionally returns a transient `401
Unauthorized` right before it's silently refreshed on Foundry's side. The
app handles this automatically by retrying up to **3 times** with a small
backoff (0s → 2s → 4s).

You'll see this in the server log only — the user doesn't see an error:
```
⚠️  MCP 401 on attempt #1 — retrying after 2.0s
🔄 succeeded on attempt #2
```

If all 3 attempts fail with 401, the cached token is genuinely revoked.
Recreate the MCP connection in the Foundry portal (see Troubleshooting).

### Verifying per-user isolation

1. Click **Sign out** in the header.
2. Open an **InPrivate / Incognito** browser window → go to
   <http://localhost:8765/> → click Sign in → sign in as a **different**
   user.
3. Send the same chat message. Consent is requested **again** — proving
   the per-user isolation: each user's Foundry-side MCP token is keyed
   by their own `oid`.

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Landing page (if not signed in) or chat UI (if signed in) |
| `GET` | `/login` | Begin Entra ID auth code flow |
| `GET` | `/auth/callback` | OAuth redirect target (don't call manually) |
| `GET` | `/logout` | Clear session + Entra sign-out |
| `GET` | `/api/whoami` | Return the signed-in user's identity (or `signed_in: false`) |
| `POST` | `/api/chat` | Start a new conversation. Body: `{"message": "..."}` |
| `POST` | `/api/chat/resume` | Resume after consent. Body: `{"conversation_id": "..."}` |

`POST /api/chat` returns one of:
```json
{ "conversation_id": "conv_...", "status": "completed", "answer": "..." }
```
or
```json
{ "conversation_id": "conv_...", "status": "consent_required",
  "consent_links": [{"server_label": "...", "consent_link": "https://..."}] }
```
or
```json
{ "conversation_id": "conv_...", "status": "error", "answer": "..." }
```

## How the user identity flows end-to-end

`oid` is the single thread that ties the whole chain together:

| Step | Location | Where `oid` lives |
|---|---|---|
| 1 | `/auth/callback` → `exchange_code_for_user` | Decoded from ID-token claims |
| 2 | `/auth/callback` → `request.session["user"]` | Stored in signed browser cookie |
| 3 | Every request → `require_user` dependency | Reconstructed from cookie |
| 4 | `_get_user_clients(user)` | Used as key in `_user_clients` dict + as `MsalUserCredential`'s `home_account_id` |
| 5 | `MsalUserCredential.get_token` → MSAL silent refresh | Embedded in the access-token JWT |
| 6 | Foundry receives `Authorization: Bearer <JWT>` | Validates JWT, extracts `oid` as user context |
| 7 | Foundry MCP cache | Lookup key = `(project, mcp_connection_id, user.oid)` |
| 8 | MCP server receives a per-user OAuth token from Foundry | Token's `oid` claim = same end-user's oid |

The web app **never explicitly passes** the `oid` anywhere — it travels
inside the cryptographically-signed JWT in the `Authorization` header.

## Authentication flow used

**OAuth 2.0 Authorization Code Grant** for a **confidential client**, with
**OpenID Connect** for identity, and **Refresh Token grant** for silent
renewal of access tokens. No PKCE (not required for confidential clients),
no Implicit / Device Code / Client Credentials / OBO / ROPC.

## What this app does NOT do (by design)

- **Does not create or modify Foundry agents.** Create the agent in the
  Foundry portal with the MCP tool attached.
- **Does not authenticate the app itself.** No `DefaultAzureCredential`,
  no managed identity, no client-credentials grant — everything is
  delegated.
- **Does not store tokens in cookies.** Cookies hold only identity claims;
  access/refresh tokens stay server-side in the MSAL cache.
- **Does not call the MCP server directly.** Only Foundry talks to the
  MCP server, using its own per-user OAuth token cache.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `AADSTS50011: reply URL doesn't match` | Redirect URI in Entra app registration must EXACTLY match `REDIRECT_URI` in `.env`. |
| `Token exchange failed: invalid_client` | `AZURE_CLIENT_SECRET` wrong or expired — create a new one in Entra. |
| First `/api/chat` returns 401/403 from Foundry | Your account lacks the `Azure AI User` role on the Foundry project. Assign it. |
| `tool_user_error … 401 Unauthorized` from MCP server, even after auto-retry | After 3 attempts the cached MCP-OAuth token at Foundry is genuinely revoked. In the Foundry portal → Connections → recreate the MCP connection → next call triggers a fresh user consent. |
| `tool_user_error … 401` appears intermittently and recovers | Normal MCP token-refresh race; the app retries automatically (look for `🔄 succeeded on attempt #N` in logs). No action needed. |
| `server_error` (500) from Foundry | Transient. Retry; if persistent, check the Foundry / Azure OpenAI status page. |
| Landing page never appears (goes straight to chat) | You're still signed in from a prior session. Click **Sign out** or use an Incognito window. |
| Sign-in prompt skipped (auto-SSO from another Microsoft session) | Expected if you're already signed in elsewhere; the chat UI is shown immediately because `prompt=select_account` honors existing SSO. Use Incognito for a clean test. |
| Port 8765 already in use | Either change `PORT` in `.env` (and add a matching redirect URI in Entra) or kill the other process. |
