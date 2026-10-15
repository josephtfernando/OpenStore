"""MCP OAuth DCR flow: register → authorize → token → tools/call → refresh.

Proves a stock client connects with a pasted URL and one Allow click — no
Bearer ever pasted, no config file edited. Money safety is unchanged: OAuth
tokens carry the same four scopes at SELF_REGISTERED tier, and spend still
needs the human tap.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from mcp_helpers import mcp_request
from openstore.sidecar.admission.oauth import Admission
from openstore.sidecar.admission.oauth_server import OAuthServer
from openstore.sidecar.admission.ratelimit import RateLimiter
from openstore.sidecar.app import app
from openstore.sidecar.protocols.agent_routes import AgentSurface
from openstore.sidecar.protocols.agent_routes import configure as configure_surface


@pytest.fixture
def surface() -> Iterator[AgentSurface]:
    state = AgentSurface(
        admission=Admission(),
        limiter=RateLimiter(),
        oauth_server=OAuthServer(),
        merchant_name="SpoiledDuckie",
        public_origin="https://shop.example",
    )
    configure_surface(state)
    yield state
    configure_surface(AgentSurface())


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _pkce() -> tuple[str, str]:
    verifier = "verifier-" + "x" * 40
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _register(client: TestClient) -> dict:
    response = client.post(
        "/agent/oauth/register",
        json={
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "Claude",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _authorize(client: TestClient, client_id: str, redirect_uri: str, challenge: str) -> str:
    page = client.get(
        "/agent/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    assert page.status_code == 200, page.text
    assert "never" in page.text and "spend" in page.text
    answered = client.post(
        "/agent/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "s1",
            "code_challenge": challenge,
            "decision": "allow",
        },
    )
    assert answered.status_code == 302, answered.text
    location = answered.headers["location"]
    assert location.startswith(redirect_uri)
    return parse_qs(urlparse(location).query)["code"][0]


def test_oauth_metadata_is_public(client: TestClient, surface: AgentSurface) -> None:
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    assert client.get("/.well-known/oauth-protected-resource").status_code == 200
    suffixed = client.get("/.well-known/oauth-protected-resource/agent/mcp")
    assert suffixed.status_code == 200
    assert suffixed.json()["resource"].endswith("/agent/mcp")


def test_oauth_401_carries_resource_metadata(client: TestClient, surface: AgentSurface) -> None:
    response = client.post("/agent/mcp", json=mcp_request("add-line", sku="X", qty=1))
    assert response.status_code == 401
    assert "oauth-protected-resource" in response.headers.get("www-authenticate", "")


def test_full_flow_connects_and_calls(client: TestClient, surface: AgentSurface) -> None:
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _authorize(client, reg["client_id"], reg["redirect_uris"][0], challenge)
    token = client.post(
        "/agent/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": reg["redirect_uris"][0],
            "code_verifier": verifier,
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert body["token_type"] == "Bearer" and body["refresh_token"]

    # The access token reaches the money core like any other stranger token —
    # here it fails for no checkout, which proves scope+dispatch, not auth.
    placed = client.post(
        "/agent/mcp",
        json=mcp_request("place-order"),
        headers={"authorization": f"Bearer {body['access_token']}"},
    )
    assert placed.status_code == 200
    assert "start-checkout" in placed.json()["result"]["content"][0]["text"]

    # Refresh rotates: old refresh dies, new access works.
    rotated = client.post(
        "/agent/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != body["refresh_token"]
    replay = client.post(
        "/agent/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert replay.status_code == 401


def test_deny_redirects_with_access_denied(client: TestClient, surface: AgentSurface) -> None:
    reg = _register(client)
    answered = client.post(
        "/agent/oauth/authorize",
        data={
            "client_id": reg["client_id"],
            "redirect_uri": reg["redirect_uris"][0],
            "state": "s9",
            "code_challenge": "abc",
            "decision": "deny",
        },
    )
    assert answered.status_code == 302
    assert "access_denied" in answered.headers["location"]


def test_pkce_and_redirect_are_enforced(client: TestClient, surface: AgentSurface) -> None:
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _authorize(client, reg["client_id"], reg["redirect_uris"][0], challenge)
    bad_verifier = client.post(
        "/agent/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": reg["redirect_uris"][0],
            "code_verifier": "wrong",
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert bad_verifier.status_code == 401

    rejected = client.post(
        "/agent/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}
    )
    assert rejected.status_code == 404
