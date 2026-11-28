"""Authentication with Amazon Cognito (OAuth 2.0 / OpenID Connect, authorization code flow).

    Browser ── GET /login ──▶ app ── 302 ──▶ Cognito managed login page
    user signs in on Cognito
    Cognito ── 302 /callback?code=...&state=... ──▶ app
    app ── POST /oauth2/token (code + client secret) ──▶ Cognito  → ID token (JWT)
    app verifies the JWT signature (Cognito public keys), stores the user in a
    signed session cookie, and redirects to the page the user wanted.

Enabled only when COGNITO_CLIENT_ID is set: without it the app stays open (steps 1-3).

Environment variables:
  COGNITO_USER_POOL_ID   e.g. eu-north-1_AbCdEf123
  COGNITO_CLIENT_ID      app client ID
  COGNITO_CLIENT_SECRET  app client secret ("Traditional web application" clients have one)
  COGNITO_DOMAIN         e.g. https://eu-north-1abcdef.auth.eu-north-1.amazoncognito.com
  SESSION_SECRET         random string used to sign the session cookie
"""

import base64
import json
import os
import secrets
import urllib.parse
import urllib.request

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
CLIENT_ID = os.getenv("COGNITO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET", "")
DOMAIN = os.getenv("COGNITO_DOMAIN", "").rstrip("/")
if DOMAIN and not DOMAIN.startswith("http"):
    DOMAIN = "https://" + DOMAIN
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)

AUTH_ENABLED = bool(CLIENT_ID)
POOL_REGION = USER_POOL_ID.split("_")[0]
ISSUER = f"https://cognito-idp.{POOL_REGION}.amazonaws.com/{USER_POOL_ID}"
SCOPES = "openid email profile"

# Pages reachable without being signed in
PUBLIC_PATHS = ("/login", "/callback", "/logout", "/health", "/static/")

router = APIRouter()
_jwks = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json") if AUTH_ENABLED else None


def check_config() -> None:
    missing = [n for n, v in (("COGNITO_USER_POOL_ID", USER_POOL_ID),
                              ("COGNITO_DOMAIN", DOMAIN)) if not v]
    if AUTH_ENABLED and missing:
        raise RuntimeError(f"Cognito enabled but {', '.join(missing)} not set")


def base_url(request: Request) -> str:
    """Public URL of the app as seen by the browser (behind nginx)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


async def require_login(request: Request, call_next):
    """Middleware: every page needs a signed-in user (when Cognito is enabled)."""
    path = request.url.path
    if AUTH_ENABLED and not path.startswith(PUBLIC_PATHS) and not current_user(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        if request.method != "GET":
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse(f"/login?next={urllib.parse.quote(path)}", status_code=303)
    return await call_next(request)


@router.get("/login")
def login(request: Request, next: str = "/"):
    if not AUTH_ENABLED:
        return RedirectResponse("/")
    state = secrets.token_urlsafe(16)  # protects against forged callbacks (CSRF)
    request.session["oauth_state"] = state
    request.session["next"] = next if next.startswith("/") else "/"
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{base_url(request)}/callback",
        "scope": SCOPES,
        "state": state,
    }
    return RedirectResponse(f"{DOMAIN}/oauth2/authorize?{urllib.parse.urlencode(params)}")


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"Cognito error: {error}")
    if not code or state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "Invalid login state, please try again from /login")

    tokens = exchange_code(code, f"{base_url(request)}/callback")
    claims = verify_id_token(tokens["id_token"])
    request.session["user"] = {
        "sub": claims["sub"],
        "email": claims.get("email", ""),
        "username": claims.get("cognito:username", ""),
        "groups": claims.get("cognito:groups", []),
    }
    return RedirectResponse(request.session.pop("next", "/"), status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    if not AUTH_ENABLED:
        return RedirectResponse("/")
    params = {"client_id": CLIENT_ID, "logout_uri": f"{base_url(request)}/"}
    return RedirectResponse(f"{DOMAIN}/logout?{urllib.parse.urlencode(params)}")


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Back-channel call: the authorization code is exchanged for tokens."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(f"{DOMAIN}/oauth2/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if CLIENT_SECRET:
        basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise HTTPException(400, f"Token exchange failed: {exc.read().decode()}") from exc


def verify_id_token(id_token: str) -> dict:
    """Check signature (Cognito's public keys), issuer, audience and expiry."""
    try:
        key = _jwks.get_signing_key_from_jwt(id_token)
        return jwt.decode(id_token, key.key, algorithms=["RS256"],
                          audience=CLIENT_ID, issuer=ISSUER)
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"Invalid ID token: {exc}") from exc
