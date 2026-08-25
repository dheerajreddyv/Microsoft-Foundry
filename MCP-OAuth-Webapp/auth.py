"""
auth.py — Entra ID sign-in (auth-code flow) + per-user TokenCredential.

How it works:
  1. /login redirects the browser to Microsoft, asking for an access token
     scoped to Azure AI Foundry on the user's behalf.
  2. Microsoft redirects back to /auth/callback with a one-time `code`.
  3. exchange_code_for_user(code) swaps that code for tokens and stashes them
     in a per-user MSAL cache keyed by `home_account_id`.
  4. Later, AIProjectClient asks `MsalUserCredential.get_token(...)` for a
     token; MSAL silently uses the user's cached refresh token to mint a
     fresh access token. No browser interaction needed.

The result: every Foundry call carries the SIGNED-IN USER'S delegated token,
so Foundry sees the user's `oid` in the JWT and routes per-user MCP-OAuth
consent to that user.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import msal
from azure.core.credentials import AccessToken, TokenCredential


# ---------- Config (read once at import time) ----------
TENANT_ID = os.environ["AZURE_TENANT_ID"]
CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
REDIRECT_URI = os.environ["REDIRECT_URI"]
AUTHORITY = os.environ.get("AZURE_AUTHORITY") or f"https://login.microsoftonline.com/{TENANT_ID}"
AI_FOUNDRY_SCOPE = os.environ.get("AI_FOUNDRY_SCOPE", "https://ai.azure.com/.default")
DELEGATED_SCOPES = [AI_FOUNDRY_SCOPE]


# ---------- Per-user MSAL token cache registry ----------
# In production, swap this in-memory dict for Redis / encrypted DB so caches
# survive process restarts and span multiple worker instances.
_caches: dict[str, msal.SerializableTokenCache] = {}
_caches_lock = threading.Lock()


def _get_cache(home_account_id: str) -> msal.SerializableTokenCache:
    with _caches_lock:
        cache = _caches.get(home_account_id)
        if cache is None:
            cache = msal.SerializableTokenCache()
            _caches[home_account_id] = cache
        return cache


def _msal_app(cache: Optional[msal.SerializableTokenCache] = None) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache,
    )


# ---------- Identity dataclass (what we put in the session cookie) ----------
@dataclass
class SignedInUser:
    home_account_id: str   # stable MSAL primary key (= "<oid>.<tid>")
    oid: str               # Entra Object ID — THE per-user identifier
    upn: str               # email-like username (display)
    name: str              # display name
    tid: str               # tenant id

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ---------- Public API ----------
def build_auth_url(state: str) -> str:
    """Return the Microsoft login URL we redirect the browser to."""
    return _msal_app().get_authorization_request_url(
        scopes=DELEGATED_SCOPES,
        redirect_uri=REDIRECT_URI,
        state=state,
        prompt="select_account",
    )


def exchange_code_for_user(code: str) -> SignedInUser:
    """Swap the one-time auth code for tokens, store them in the per-user cache,
    and return a SignedInUser dataclass with the identity claims."""
    # Use a throwaway cache to receive the tokens, then move them into the
    # right per-user cache once we know who signed in.
    bootstrap_cache = msal.SerializableTokenCache()
    app = _msal_app(bootstrap_cache)
    result = app.acquire_token_by_authorization_code(
        code=code, scopes=DELEGATED_SCOPES, redirect_uri=REDIRECT_URI,
    )
    if "error" in result:
        raise RuntimeError(f"Token exchange failed: {result.get('error')} - {result.get('error_description')}")

    claims = result.get("id_token_claims", {}) or {}
    oid = claims.get("oid") or ""
    upn = (claims.get("preferred_username") or claims.get("upn")
           or claims.get("email") or "")
    name = claims.get("name") or upn
    tid = claims.get("tid") or TENANT_ID

    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError("MSAL returned no accounts after token exchange.")
    home_account_id = accounts[0]["home_account_id"]

    # Move tokens from the throwaway cache to this user's permanent cache.
    _get_cache(home_account_id).deserialize(bootstrap_cache.serialize())

    return SignedInUser(
        home_account_id=home_account_id, oid=oid, upn=upn, name=name, tid=tid,
    )


# ---------- Azure SDK adapter ----------
class MsalUserCredential(TokenCredential):
    """Azure SDK TokenCredential backed by a per-user MSAL cache.

    When AIProjectClient needs a token, it calls get_token(scope). We look up
    the user's cache, silently refresh (or return cached) via MSAL, and hand
    back the JWT. The JWT carries the user's `oid` claim — that's what Foundry
    uses to key per-user MCP-OAuth consent.
    """

    def __init__(self, home_account_id: str):
        self.home_account_id = home_account_id

    def get_token(self, *scopes: str, **kwargs) -> AccessToken:
        if not scopes:
            raise ValueError("get_token requires at least one scope")
        cache = _get_cache(self.home_account_id)
        app = _msal_app(cache)
        account = next((a for a in app.get_accounts()
                        if a.get("home_account_id") == self.home_account_id), None)
        if account is None:
            raise RuntimeError(f"No MSAL account for {self.home_account_id}. User must sign in again.")
        result = app.acquire_token_silent_with_error(scopes=list(scopes), account=account)
        if not result or "access_token" not in result:
            err = (result or {}).get("error_description") or "unknown error"
            raise RuntimeError(f"Silent token acquisition failed: {err}. User must sign in again.")
        expires_on = int(time.time()) + int(result.get("expires_in", 3600))
        return AccessToken(result["access_token"], expires_on)

    def close(self) -> None: pass
    def __enter__(self): return self
    def __exit__(self, *_): pass


def purge_user_cache(home_account_id: str) -> None:
    """Drop a user's MSAL cache (used by /logout)."""
    with _caches_lock:
        _caches.pop(home_account_id, None)
