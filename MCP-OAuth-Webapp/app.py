"""
FastAPI app: sign users in with Entra ID, then invoke a Foundry agent on
their behalf using their DELEGATED token (no app-level credential).

End-to-end flow:
  1. Browser opens / → redirected to /login → redirected to Microsoft.
  2. User signs in; Microsoft redirects to /auth/callback with a code.
  3. We exchange the code for tokens, store identity in a signed session
     cookie, and tokens in a per-user MSAL cache (auth.py).
  4. POST /api/chat builds (lazily, once per user) an AIProjectClient bound
     to that user's MsalUserCredential. Every Foundry call therefore carries
     a delegated JWT with the user's `oid`.
  5. Foundry uses (project, mcp_connection, user_oid) as its MCP-OAuth cache
     key. First call → returns an oauth_consent_request; UI opens the link;
     POST /api/chat/resume continues the same conversation.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # must run before auth.py is imported (it reads env at import-time)

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from azure.ai.projects import AIProjectClient
from openai import APIStatusError, BadRequestError

import auth as auth_mod
from auth import (
    AUTHORITY, MsalUserCredential, SignedInUser,
    build_auth_url, exchange_code_for_user, purge_user_cache,
)

# ---------- Logging ----------
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S", stream=sys.stdout,
)
log = logging.getLogger("mcp-oauth-webapp")
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("msal").setLevel(logging.WARNING)

# ---------- Config ----------
ENDPOINT = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
AGENT_NAME = os.environ["AGENT_NAME"]
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)


# ---------- Per-user Foundry client cache ----------
# Each signed-in user gets their own AIProjectClient + OpenAI client, bound to
# their MsalUserCredential. Built lazily on first /api/chat for that user.
_user_clients: dict[str, dict] = {}
_user_clients_lock = threading.Lock()


def _get_user_clients(user: SignedInUser) -> dict:
    with _user_clients_lock:
        entry = _user_clients.get(user.home_account_id)
        if entry is not None:
            return entry
        log.info("🔧 Building Foundry client for user=%s oid=%s", user.upn, user.oid)
        cred = MsalUserCredential(user.home_account_id)
        proj = AIProjectClient(endpoint=ENDPOINT, credential=cred)
        oai = proj.get_openai_client()
        entry = {"credential": cred, "project": proj, "openai": oai}
        _user_clients[user.home_account_id] = entry
        return entry


# ---------- FastAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Listening. agent=%s endpoint=%s", AGENT_NAME, ENDPOINT)
    log.info("👤 Auth: DELEGATED ONLY (no app credential).")
    yield
    log.info("🛑 Shutting down.")


app = FastAPI(title="MCP OAuth Web App", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- Session helpers ----------
def _current_user(request: Request) -> Optional[SignedInUser]:
    u = request.session.get("user")
    return SignedInUser(**u) if u else None


def require_user(request: Request) -> SignedInUser:
    u = _current_user(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return u


# ---------- Auth routes ----------
@app.get("/login")
async def login(request: Request, next: str = "/"):
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["post_login_redirect"] = next
    return RedirectResponse(build_auth_url(state))


@app.get("/auth/callback")
async def auth_callback(request: Request):
    qp = request.query_params
    if "error" in qp:
        return JSONResponse(
            {"error": qp.get("error"), "description": qp.get("error_description")},
            status_code=400,
        )
    if qp.get("state") != request.session.get("oauth_state"):
        raise HTTPException(status_code=400, detail="Invalid OAuth state (possible CSRF).")
    code = qp.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
    try:
        user = exchange_code_for_user(code)
    except Exception as e:
        log.exception("Token exchange failed")
        return JSONResponse({"error": "token_exchange_failed", "detail": str(e)}, status_code=400)

    request.session["user"] = user.to_dict()
    request.session.pop("oauth_state", None)
    log.info("✅ Signed in: upn=%s oid=%s", user.upn, user.oid)
    return RedirectResponse(request.session.pop("post_login_redirect", "/"))


@app.get("/logout")
async def logout(request: Request):
    user = _current_user(request)
    request.session.clear()
    if user:
        log.info("👋 Signed out: upn=%s", user.upn)
        with _user_clients_lock:
            _user_clients.pop(user.home_account_id, None)
        purge_user_cache(user.home_account_id)
    post_logout = urllib.parse.quote(str(request.url_for("index")), safe="")
    return RedirectResponse(
        f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={post_logout}"
    )


# ---------- Diagnostics ----------
@app.get("/api/whoami")
async def whoami(request: Request):
    u = _current_user(request)
    if u is None:
        return {"signed_in": False}
    return {"signed_in": True, "identity": u.to_dict(), "agent": AGENT_NAME}


# ---------- Chat ----------
class ChatRequest(BaseModel):
    message: str


class ResumeRequest(BaseModel):
    conversation_id: str
    message: Optional[str] = "Please continue and complete the task using the MCP tools."


class ChatResponse(BaseModel):
    conversation_id: str
    response_id: Optional[str] = None
    status: str  # "completed" | "consent_required" | "error"
    answer: Optional[str] = None
    consent_links: Optional[list[dict]] = None


def _looks_like_mcp_auth_error(info: dict) -> bool:
    """Detect the Foundry MCP-OAuth 401 (cached token refresh race).

    The OpenAI SDK doesn't always populate `e.body` cleanly — sometimes the
    whole error payload (including the nested `code: tool_user_error`) only
    appears inside the message string. So we look in BOTH places.
    """
    msg = (info.get("message") or "").lower()
    code = (info.get("code") or "").lower()
    has_tool_user_error = (code == "tool_user_error") or ("tool_user_error" in msg)
    has_401 = ("401" in msg) or ("unauthorized" in msg) or ("authentication failed" in msg)
    return has_tool_user_error and has_401


def _extract_err(e: Exception) -> dict:
    info = {"code": None, "message": str(e), "status": getattr(e, "status_code", None)}
    body = getattr(e, "body", None) or {}
    err = (body.get("error") if isinstance(body, dict) else None) or {}
    if isinstance(err, dict):
        info["code"] = err.get("code") or info["code"]
        info["message"] = err.get("message") or info["message"]
    return info


def _invoke_agent(*, conversation_id: str, user_input, user: SignedInUser):
    """Call Foundry's Responses API with the user's delegated token.

    Foundry's cached MCP-OAuth token can briefly return 401 right before it
    is silently refreshed on Foundry's side. We retry up to 3 times with a
    small backoff to let that refresh complete. All other errors propagate.
    """
    oai = _get_user_clients(user)["openai"]
    log.info("➡️  Calling Foundry: user=%s agent=%s conv=%s", user.upn, AGENT_NAME, conversation_id)
    backoffs = [0, 2.0, 4.0]   # 3 attempts total; wait 2s then 4s before retries
    last_info: dict = {}
    for attempt, wait in enumerate(backoffs, start=1):
        if wait:
            time.sleep(wait)
        try:
            response = oai.responses.create(
                conversation=conversation_id,
                input=user_input,
                extra_body={"agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}},
                timeout=200,
            )
            if attempt > 1:
                log.info("🔄 succeeded on attempt #%d", attempt)
            return response
        except (BadRequestError, APIStatusError) as e:
            last_info = _extract_err(e)
            if attempt < len(backoffs) and _looks_like_mcp_auth_error(last_info):
                log.warning("⚠️  MCP 401 on attempt #%d — retrying after %.1fs",
                            attempt, backoffs[attempt])
                continue
            log.error("❌ Foundry call failed: %s", last_info)
            raise RuntimeError(last_info["message"]) from e


def _extract_consent_links(response) -> list[dict]:
    """Return any oauth_consent_request items as {server_label, consent_link}."""
    return [
        {"server_label": getattr(it, "server_label", "unknown"),
         "consent_link": getattr(it, "consent_link", "")}
        for it in response.output
        if getattr(it, "type", "") == "oauth_consent_request"
    ]


def _build_response(response, conversation_id: str) -> ChatResponse:
    """Foundry response → ChatResponse. Logs whether an MCP tool was used."""
    types = [getattr(it, "type", "?") for it in response.output]
    log.info("⬅️  Foundry returned items=%s", types)
    links = _extract_consent_links(response)
    if links:
        return ChatResponse(
            conversation_id=conversation_id, response_id=response.id,
            status="consent_required", consent_links=links,
        )
    return ChatResponse(
        conversation_id=conversation_id, response_id=response.id,
        status="completed", answer=response.output_text,
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: SignedInUser = Depends(require_user)):
    log.info("📨 POST /api/chat user=%s msg=%r", user.upn, req.message[:100])
    oai = _get_user_clients(user)["openai"]
    conversation = oai.conversations.create()
    try:
        response = _invoke_agent(
            conversation_id=conversation.id, user_input=req.message, user=user,
        )
    except RuntimeError as e:
        return ChatResponse(conversation_id=conversation.id, status="error", answer=str(e))
    return _build_response(response, conversation.id)


@app.post("/api/chat/resume", response_model=ChatResponse)
async def chat_resume(req: ResumeRequest, user: SignedInUser = Depends(require_user)):
    log.info("📨 POST /api/chat/resume user=%s conv=%s", user.upn, req.conversation_id)
    try:
        response = _invoke_agent(
            conversation_id=req.conversation_id, user_input=req.message, user=user,
        )
    except RuntimeError as e:
        return ChatResponse(conversation_id=req.conversation_id, status="error", answer=str(e))
    return _build_response(response, req.conversation_id)


# ---------- Index ----------
@app.get("/", name="index")
async def index(request: Request):
    """Public landing page for visitors; chat UI for signed-in users."""
    if _current_user(request) is None:
        return FileResponse("static/landing.html")
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8765")))
