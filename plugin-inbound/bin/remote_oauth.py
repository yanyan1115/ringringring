"""OAuth for the remote MCP: a small local provider (dynamic client registration, PKCE,
a connection-passphrase page). Adapted from an existing MCP OAuth recipe; see README credits."""
from __future__ import annotations
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pydantic import AnyUrl
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

try:
    from mcp.server.transport_security import TransportSecuritySettings
except Exception:  # older MCP SDKs
    TransportSecuritySettings = None  # type: ignore[assignment]

OAUTH_SCOPE = "phone"
OAUTH_ACCESS_TOKEN_TTL_SECONDS = 3600
OAUTH_REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 90
OAUTH_AUTH_CODE_TTL_SECONDS = 300


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()]




def _oauth_store_path() -> Path:
    return Path(os.getenv("MCP_OAUTH_STORE",
                         "~/.local/share/ringringring/oauth/oauth.json")).expanduser()


class LocalOAuthProvider:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logging.warning("Could not read OAuth store; starting with an empty store")
            return
        for client_id, payload in data.get("clients", {}).items():
            self.clients[client_id] = OAuthClientInformationFull.model_validate(payload)
        for code, payload in data.get("auth_codes", {}).items():
            self.auth_codes[code] = AuthorizationCode.model_validate(payload)
        for token, payload in data.get("refresh_tokens", {}).items():
            self.refresh_tokens[token] = RefreshToken.model_validate(payload)
        for token, payload in data.get("access_tokens", {}).items():
            self.access_tokens[token] = AccessToken.model_validate(payload)
        self._prune_expired(save=False)

    def _save(self) -> None:
        self._prune_expired(save=False)
        self.store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "clients": {key: value.model_dump(mode="json") for key, value in self.clients.items()},
            "auth_codes": {key: value.model_dump(mode="json") for key, value in self.auth_codes.items()},
            "refresh_tokens": {key: value.model_dump(mode="json") for key, value in self.refresh_tokens.items()},
            "access_tokens": {key: value.model_dump(mode="json") for key, value in self.access_tokens.items()},
        }
        tmp_path = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.store_path)
        try:
            os.chmod(self.store_path, 0o600)
        except OSError:
            pass

    def _prune_expired(self, *, save: bool = True) -> None:
        now = int(time.time())
        changed = False
        for mapping in (self.auth_codes, self.refresh_tokens, self.access_tokens):
            for key, value in list(mapping.items()):
                expires_at = getattr(value, "expires_at", None)
                if expires_at is not None and expires_at < now:
                    mapping.pop(key, None)
                    changed = True
        if changed and save:
            self._save()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client_id is required")
        self.clients[client_info.client_id] = client_info
        self._save()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        query = {
            "client_id": client.client_id or "",
            "response_type": "code",
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
        }
        if params.state is not None:
            query["state"] = params.state
        if params.scopes:
            query["scope"] = " ".join(params.scopes)
        if params.resource is not None:
            query["resource"] = params.resource
        return "/oauth/authorize?" + urlencode(query)

    def authorize_url(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not client.client_id:
            raise AuthorizeError("invalid_request", "client_id is required")
        scopes = params.scopes or [OAUTH_SCOPE]
        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=int(time.time()) + OAUTH_AUTH_CODE_TTL_SECONDS,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._save()
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.auth_codes.pop(authorization_code.code, None)
        if authorization_code.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code is not valid for this client")
        return self._issue_tokens(client, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self.refresh_tokens.pop(refresh_token.token, None)
        if refresh_token.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token is not valid for this client")
        requested_scopes = scopes or refresh_token.scopes
        if not set(requested_scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope", "requested scope was not granted")
        return self._issue_tokens(client, requested_scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self.access_tokens.get(token)
        if access_token is None:
            return None
        if access_token.expires_at is not None and access_token.expires_at < time.time():
            self.access_tokens.pop(token, None)
            self._save()
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        if isinstance(token, RefreshToken):
            for access_token, value in list(self.access_tokens.items()):
                if value.client_id == token.client_id:
                    self.access_tokens.pop(access_token, None)
        self._save()

    def _issue_tokens(self, client: OAuthClientInformationFull, scopes: list[str], resource: str | None = None) -> OAuthToken:
        if not client.client_id:
            raise TokenError("invalid_client", "client_id is required")
        access_token_value = secrets.token_urlsafe(32)
        refresh_token_value = secrets.token_urlsafe(32)
        now = int(time.time())
        self.access_tokens[access_token_value] = AccessToken(
            token=access_token_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + OAUTH_ACCESS_TOKEN_TTL_SECONDS,
            resource=resource,
        )
        self.refresh_tokens[refresh_token_value] = RefreshToken(
            token=refresh_token_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + OAUTH_REFRESH_TOKEN_TTL_SECONDS,
        )
        self._save()
        return OAuthToken(
            access_token=access_token_value,
            expires_in=OAUTH_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token_value,
        )


def _build_transport_security() -> Any:
    allowed_hosts = _env_list("MCP_ALLOWED_HOSTS")
    allowed_origins = _env_list("MCP_ALLOWED_ORIGINS")
    if TransportSecuritySettings is None or (not allowed_hosts and not allowed_origins):
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _oauth_enabled() -> bool:
    return os.getenv("MCP_OAUTH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _build_oauth(provider: LocalOAuthProvider) -> tuple[AuthSettings, LocalOAuthProvider]:
    issuer_url = os.getenv("MCP_OAUTH_ISSUER_URL", "").strip()
    resource_url = os.getenv("MCP_OAUTH_RESOURCE_URL", "").strip() or issuer_url.rstrip("/") + "/mcp"
    if not issuer_url:
        raise RuntimeError("MCP_OAUTH_ISSUER_URL is required when MCP_OAUTH_ENABLED is set")
    return (
        AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_url,
            required_scopes=[OAUTH_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[OAUTH_SCOPE],
                default_scopes=[OAUTH_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        provider,
    )


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _auth_params_from_query(query: dict[str, str], redirect_uri_provided: bool) -> AuthorizationParams:
    scopes = query.get("scope", OAUTH_SCOPE).split()
    return AuthorizationParams(
        state=query.get("state"),
        scopes=scopes or [OAUTH_SCOPE],
        code_challenge=query["code_challenge"],
        redirect_uri=query["redirect_uri"],
        redirect_uri_provided_explicitly=redirect_uri_provided,
        resource=query.get("resource"),
    )


def _register_oauth_routes(server: FastMCP, provider: LocalOAuthProvider) -> None:
    @server.custom_route("/oauth/authorize", methods=["GET", "POST"])
    async def oauth_authorize(request: Request) -> Response:
        connect_secret = os.getenv("MCP_OAUTH_CONNECT_SECRET", "")
        if not connect_secret:
            return PlainTextResponse("MCP_OAUTH_CONNECT_SECRET is not configured", status_code=503)

        if request.method == "POST":
            form = await request.form()
            query = {key: str(value) for key, value in form.items() if key != "connect_secret"}
            submitted_secret = str(form.get("connect_secret", ""))
            if not secrets.compare_digest(submitted_secret, connect_secret):
                return _oauth_form(query, "Invalid connection secret.", status_code=403)
            return await _complete_oauth_authorization(provider, query)

        query = {key: value for key, value in request.query_params.items()}
        return _oauth_form(query)


def _oauth_form(query: dict[str, str], error: str | None = None, *, status_code: int = 200) -> HTMLResponse:
    hidden = "\n".join(
        f'<input type="hidden" name="{_html_escape(key)}" value="{_html_escape(value)}">'
        for key, value in query.items()
    )
    error_html = f"<p class=\"error\">{_html_escape(error)}</p>" if error else ""
    client_name = _html_escape(query.get("client_id", "Claude connector"))
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize ringringring phone</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 36rem; line-height: 1.5; }}
    label {{ display: block; font-weight: 600; margin: 1rem 0 0.35rem; }}
    input[type=password] {{ box-sizing: border-box; width: 100%; padding: 0.65rem; font: inherit; }}
    button {{ margin-top: 1rem; padding: 0.65rem 1rem; font: inherit; cursor: pointer; }}
    .error {{ color: #b00020; }}
    .muted {{ color: #555; }}
  </style>
</head>
<body>
  <h1>Authorize ringringring phone</h1>
  <p class="muted">Connect the phone tool: enter the connection passphrase and this app will be able to call you.</p>
  <p class="muted">Client: {client_name}</p>
  {error_html}
  <form method="post">
    {hidden}
    <label for="connect_secret">Connection secret</label>
    <input id="connect_secret" name="connect_secret" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Authorize</button>
  </form>
</body>
</html>"""
    return HTMLResponse(body, status_code=status_code)


async def _complete_oauth_authorization(provider: LocalOAuthProvider, query: dict[str, str]) -> Response:
    required = {"client_id", "response_type", "code_challenge", "redirect_uri"}
    missing = sorted(required - set(query))
    if missing:
        return PlainTextResponse(f"Missing OAuth parameter: {', '.join(missing)}", status_code=400)
    if query["response_type"] != "code":
        return PlainTextResponse("Unsupported response_type", status_code=400)
    client = await provider.get_client(query["client_id"])
    if client is None:
        return PlainTextResponse("Unknown OAuth client", status_code=400)
    try:
        redirect_uri = client.validate_redirect_uri(AnyUrl(query["redirect_uri"]))
        params = _auth_params_from_query({**query, "redirect_uri": str(redirect_uri)}, True)
        url = provider.authorize_url(client, params)
    except Exception as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


