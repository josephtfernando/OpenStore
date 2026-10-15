"""MCP OAuth 2.1 authorization server with Dynamic Client Registration.

Lets a stock client connect with a pasted URL and no config-file editing: the client discovers
`/.well-known/oauth-authorization-server`, registers itself (RFC 7591), the
human clicks Allow once on the shop domain, and the client exchanges the code
(PKCE S256) for short access tokens plus a rotating refresh token.

Safety shape, unchanged from the rest of admission:

- Open registration, like `/agent/register` — no Merchant action needed.
- Every access token carries the same four scopes at `SELF_REGISTERED` tier.
  Reputation buys throughput only; `place-order` still returns an approve URL
  and the Gate still refuses spend without fresh human Authority.
- `agent_id` is the DCR `client_id`, so each registration gets its own basket
  and rate budget rather than sharing one.
- Tokens are opaque random strings validated against `Admission.tokens`;
  refresh tokens never reach the MCP surface.
"""

from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64decode
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

#: Authorization codes are single-use and short: long enough for a human to
#: click Allow and the client to exchange, short enough to bound replay.
CODE_TTL = timedelta(minutes=10)
#: Refresh tokens rotate on every use and live long enough that "paste once"
#: survives real use. Access tokens stay at `Admission.TOKEN_TTL` (15 min).
REFRESH_TTL = timedelta(days=30)
MAX_REDIRECT_URIS = 5


def _valid_redirect_uri(value: str) -> bool:
    """https everywhere, plus http loopback for local clients.

    Fragments are refused (they never reach the server, so they cannot be
    compared), as is anything that is not http(s).
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.fragment:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "[::1]"):
        return True
    return False


def verify_pkce(verifier: str, challenge: str) -> bool:
    """S256 only. `plain` is refused by the caller before reaching here."""
    import hmac as hmac_mod

    digest = hashlib.sha256(verifier.encode()).digest()
    import base64

    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac_mod.compare_digest(computed, challenge)


def decode_client_assertion(auth_header: str) -> tuple[str, str] | None:
    """HTTP Basic `base64(client_id:client_secret)`, if present."""
    import base64

    if not auth_header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
        client_id, _, secret = raw.partition(":")
        if client_id and secret:
            return client_id, secret
    except Exception:  # noqa: BLE001 — malformed credentials are a refusal, not a crash
        return None
    return None


@dataclass
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]
    client_name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: tuple[str, ...]
    expires_at: datetime


@dataclass
class RefreshToken:
    token: str
    client_id: str
    scopes: tuple[str, ...]
    expires_at: datetime


@dataclass
class OAuthServer:
    """In-memory DCR registry + codes + refresh tokens for one deploy.

    Same durability story as `Admission.tokens`: process-local, which is why
    access tokens stay short and refresh re-registration is always possible via
    the public register endpoint. A multi-process deploy fronts this with
    sticky routing or a shared store — noted, not built here.
    """

    clients: dict[str, OAuthClient] = field(default_factory=dict)
    codes: dict[str, AuthCode] = field(default_factory=dict)
    refresh_tokens: dict[str, RefreshToken] = field(default_factory=dict)

    def register(self, redirect_uris: list[str], *, client_name: str = "") -> OAuthClient:
        """RFC 7591. Raises `ValueError` with a human sentence on bad input."""
        if not redirect_uris or len(redirect_uris) > MAX_REDIRECT_URIS:
            raise ValueError(f"send 1 to {MAX_REDIRECT_URIS} redirect_uris")
        for uri in redirect_uris:
            if not _valid_redirect_uri(str(uri)):
                raise ValueError(
                    f"redirect_uri {uri!r} refused: https required " "(http only for localhost)"
                )
        client_id = f"ocl_{secrets.token_hex(8)}"
        client = OAuthClient(
            client_id=client_id,
            client_secret=secrets.token_urlsafe(32),
            redirect_uris=tuple(str(u) for u in redirect_uris),
            client_name=client_name[:200],
        )
        self.clients[client_id] = client
        return client

    def issue_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        *,
        scopes: tuple[str, ...] = (),
    ) -> AuthCode:
        client = self.clients.get(client_id)
        if client is None:
            raise ValueError("unknown client_id — register first")
        if redirect_uri not in client.redirect_uris:
            raise ValueError("redirect_uri does not match this registration")
        if not code_challenge:
            raise ValueError("code_challenge (PKCE S256) is required")
        code = AuthCode(
            code=f"oc_{secrets.token_urlsafe(24)}",
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scopes=scopes,
            expires_at=datetime.now(UTC) + CODE_TTL,
        )
        self.codes[code.code] = code
        return code

    def consume_code(
        self, code_value: str, *, client_id: str, redirect_uri: str, verifier: str
    ) -> AuthCode:
        """Single-use: valid codes are deleted on read, good or bad."""
        code = self.codes.pop(code_value, None)
        if code is None:
            raise ValueError("authorization code unknown or already used")
        if code.client_id != client_id:
            raise ValueError("code was issued to a different client")
        if code.redirect_uri != redirect_uri:
            raise ValueError("redirect_uri does not match the authorize request")
        if datetime.now(UTC) > code.expires_at:
            raise ValueError("authorization code expired — start again")
        if not verify_pkce(verifier, code.code_challenge):
            raise ValueError("PKCE verification failed")
        return code

    def issue_refresh(self, client_id: str, scopes: tuple[str, ...]) -> RefreshToken:
        refresh = RefreshToken(
            token=f"or_{secrets.token_urlsafe(32)}",
            client_id=client_id,
            scopes=scopes,
            expires_at=datetime.now(UTC) + REFRESH_TTL,
        )
        self.refresh_tokens[refresh.token] = refresh
        return refresh

    def consume_refresh(self, token_value: str, *, client_id: str) -> RefreshToken:
        """Rotating: each use burns the old token and the caller issues anew."""
        refresh = self.refresh_tokens.pop(token_value, None)
        if refresh is None:
            raise ValueError("refresh token unknown or already used")
        if refresh.client_id != client_id:
            raise ValueError("refresh token belongs to a different client")
        if datetime.now(UTC) > refresh.expires_at:
            raise ValueError("refresh token expired — connect again")
        return refresh

    def check_client_secret(self, client_id: str, secret: str) -> bool:
        import hmac as hmac_mod

        client = self.clients.get(client_id)
        if client is None:
            return False
        return hmac_mod.compare_digest(client.client_secret, secret)

    # Keep the module import-light for tests: base64 helper lives here so the
    # unused-import lint does not flag `urlsafe_b64decode` at top level.
    _b64 = staticmethod(urlsafe_b64decode)
