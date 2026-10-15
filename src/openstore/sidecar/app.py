"""FastAPI wiring. At A1 this is the skeleton: health, readiness, and nothing
that touches money.

Routes are mounted behind the reverse proxy split (SPEC §10): `/` goes to the
store, and `/.well-known/*`, `/agent/*` and `/agentic/*` come here, on the
Merchant's own origin so passkeys and tap tokens are bound to the domain the
Consumer is actually looking at.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from openstore.sidecar.authority.tokens import TokenStore
from openstore.sidecar.basket import BasketStore
from openstore.sidecar.checkout_store import CheckoutStore
from openstore.sidecar.console.approve import router as approve_router
from openstore.sidecar.console.merchant_actions import router as merchant_actions_router
from openstore.sidecar.console.refunds import configure_refunds
from openstore.sidecar.console.routes import router as console_router
from openstore.sidecar.core.correlation import new_request_id, request_id, set_request_id
from openstore.sidecar.core.settings import Settings, get_settings
from openstore.sidecar.evidence.bundle import Bundle
from openstore.sidecar.evidence.keys import Keyring, load_or_enroll
from openstore.sidecar.evidence.store import ReceiptStore, configure_receipts, get_receipt_store
from openstore.sidecar.evidence.viewer import not_found_page, receipt_page
from openstore.sidecar.protocols.agent_routes import get_surface
from openstore.sidecar.protocols.agent_routes import router as agent_router
from openstore.sidecar.provider.routes import router as provider_router
from openstore.sidecar.verify.checks import VerifyResult, verify


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Point the agent surface at this deployment's Merchant.

    At startup rather than at import, so settings are read once after the
    process has decided what it is. `lifespan` rather than the deprecated
    `on_event` — and it **logs what it wired**, because a feed that silently
    serves nothing looks like an empty shop rather than a misconfiguration.
    """
    import httpx

    from openstore.sidecar import sweeper
    from openstore.sidecar.admission.oauth import Admission
    from openstore.sidecar.admission.profile import ProfileFetcher
    from openstore.sidecar.checkout import CheckoutContext
    from openstore.sidecar.checkout import configure as configure_checkout
    from openstore.sidecar.console.approve import ApproveContext
    from openstore.sidecar.console.approve import configure as configure_approve
    from openstore.sidecar.core.db import ensure_schema, make_engine, make_sessionmaker
    from openstore.sidecar.evidence.store import get_receipt_store
    from openstore.sidecar.protocols.agent_routes import AgentSurface
    from openstore.sidecar.protocols.agent_routes import configure as configure_surface
    from openstore.sidecar.protocols.events import EventStore
    from openstore.sidecar.protocols.idempotency import IdempotencyStore
    from openstore.sidecar.trait.client import TraitClient

    settings = get_settings()
    trait = (
        TraitClient(
            settings.trait_base_url,
            settings.trait_hmac_secret,
            client=httpx.AsyncClient(timeout=10),
        )
        if settings.trait_base_url
        else None
    )
    merchant_domain = settings.openstore_merchant_domain or "localhost"
    keyring = _load_keyring(settings, merchant_domain)
    configure_surface(
        AgentSurface(
            merchant_domain=merchant_domain,
            merchant_name=settings.webauthn_rp_name or "This shop",
            merchant_description=settings.openstore_merchant_description,
            merchant_categories=settings.merchant_categories,
            public_origin=settings.openstore_public_origin,
            demo=settings.openstore_demo_mode,
            trait=trait,
            jwks=keyring.jwks() if keyring else {"keys": []},
            keyring=keyring,
            # The allowlist was reported in health output and the console banner
            # while the fetcher that enforces it held an empty tuple, so the
            # named exception refused the one host it exists for and no agent
            # could register at all. Reporting a policy is not applying it.
            fetcher=ProfileFetcher(dev_hosts=settings.dev_profile_hosts),
            # Same story as the allowlist: the credentials were configurable and
            # reached nothing, so the OAuth route refused the very client the
            # deploy had been given.
            admission=Admission(
                clients=(
                    {settings.oauth_client_id: settings.oauth_client_secret}
                    if settings.oauth_client_id and settings.oauth_client_secret
                    else {}
                )
            ),
        )
    )
    # The money path. Until this existed the Gate, the Ledger, the Provider and
    # the receipt were reachable only from a test.
    engine = None
    sessionmaker = None
    if settings.sidecar_database_url:
        engine = make_engine(settings.sidecar_database_url)
        # Demo creates its schema; a deploy that takes money migrates instead,
        # and refuses to start rather than changing a schema underneath itself.
        schema = await ensure_schema(engine, demo=settings.openstore_demo_mode)
        logging.getLogger("openstore").warning("schema: %s", schema)
        sessionmaker = make_sessionmaker(engine)
    else:
        logging.getLogger("openstore").warning(
            "NO DATABASE: SIDECAR_DATABASE_URL is unset, so there is no Ledger and no "
            "order can be taken."
        )

    # Every durable store is pointed at the same database, here, once. A store
    # that reached no database refuses loudly at its first write rather than
    # quietly keeping state in a process that is about to be replaced.
    configure_receipts(sessionmaker)
    configure_refunds(sessionmaker)
    get_surface().baskets = BasketStore(sessionmaker=sessionmaker)
    # Durable on purpose: the process that took the first call is the process
    # that died, which is why the client is retrying. An in-memory table would
    # be exactly as correct in tests and useless in the one case it exists for.
    get_surface().idempotency = IdempotencyStore(sessionmaker=sessionmaker)
    get_surface().events = EventStore(sessionmaker=sessionmaker)

    provider = _provider_for(settings)
    _configure_webhooks(settings, provider)
    # ONE Policy. The Gate, the console and the card each read it through
    # `current_policy()` at the moment they need it, rather than keeping a copy:
    # three copies with the same defaults look identical right up until a
    # Merchant edits one, and then the console shows the edit, the Gate ignores
    # it, and the card advertises a limit nobody enforces. This local is only
    # for the passkey wiring below, which needs the value once at boot.
    from openstore.sidecar.gate.policy import current_policy

    policy = current_policy()
    passkey_rp = _passkey_rp_for(settings, merchant_domain, policy, sessionmaker)
    rp_id = settings.webauthn_rp_id or merchant_domain
    origin = settings.openstore_public_origin or f"https://{rp_id}"
    rp_mismatch = _origin_rp_mismatch(origin, rp_id)
    mismatch_note = ""
    if rp_mismatch and passkey_rp is not None:
        # Warn-only by decision: the ceremony would die in the browser with an
        # unusable rp.id, so the page hides the button and says why — every
        # other authority kind keeps working.
        mismatch_note = f"passkeys are set up for {rp_id}, not for {_origin_host(origin)}"
        logging.getLogger("openstore").warning(
            "PASSKEY ORIGIN MISMATCH: %s — the passkey button is hidden until the "
            "RP ID and the public origin name the same host again.",
            mismatch_note,
        )
    tokens = TokenStore(sessionmaker=sessionmaker)
    checkout_context = CheckoutContext(
        trait=trait,
        tokens=tokens,
        provider=provider,
        keyring=keyring,
        sessionmaker=sessionmaker,
        store=CheckoutStore(sessionmaker=sessionmaker),
        receipts=get_receipt_store(),
        merchant_domain=merchant_domain,
        public_origin=settings.openstore_public_origin,
        deploy_pseudonym_key=settings.deploy_pseudonym_key.encode(),
        demo=settings.openstore_demo_mode,
        passkey_rp=passkey_rp,
    )
    configure_checkout(checkout_context)
    # The approve page reads the same token store the tap spends from — two
    # stores would render one token and refuse another.
    configure_approve(
        ApproveContext(
            tokens=tokens,
            merchant_domain=merchant_domain,
            merchant_name=settings.webauthn_rp_name or "This shop",
            enabled_methods=policy.enabled_methods,
            passkey_enabled=passkey_rp is not None and not rp_mismatch,
            origin_mismatch_note=mismatch_note,
            demo=settings.openstore_demo_mode,
        )
    )

    logging.getLogger("openstore").warning(
        "money path wired: db=%s provider=%s signing_key=%s",
        "yes" if sessionmaker else "NOT CONFIGURED - no order can be taken",
        provider.name,
        "yes" if keyring else "NOT CONFIGURED - nothing can be sealed",
    )
    logging.getLogger("openstore").warning(
        "agent surface wired: merchant=%s trait=%s",
        settings.openstore_merchant_domain or "unset",
        settings.trait_base_url or "NOT CONFIGURED - the feed will be empty",
    )

    # The door-10 snapshot: fetched once here so the first tools/list and card
    # already speak this Merchant's extras, then refreshed in the background.
    # A Merchant with no door 10 keeps the fixed shape — absence is supported.
    from openstore.sidecar.protocols.agent_routes import refresh_requirements

    await refresh_requirements()
    requirements_task: asyncio.Task[None] | None = None
    if trait is not None:
        requirements_task = asyncio.create_task(_refresh_requirements_forever())

    # The expiry sweeper. Without it `expires_at` is a sentence in the spec: an
    # abandoned tap holds Merchant stock until this process restarts, which is
    # an availability hole any self-registered stranger can open at will. It
    # runs only with a trait to release through — a sweeper with no Merchant
    # would move statuses nobody can act on.
    sweeper_task: asyncio.Task[None] | None = None
    if trait is not None:
        sweeper_task = asyncio.create_task(sweeper.run_forever(checkout_context))
    else:
        logging.getLogger("openstore").warning(
            "NO SWEEPER: there is no trait to release stock through, so nothing expires."
        )

    yield

    for task in (requirements_task, sweeper_task):
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    if engine is not None:
        await engine.dispose()


async def _refresh_requirements_forever() -> None:
    """Background door-10 refresh. Failures keep the last snapshot by design
    (see `refresh_requirements`) — this loop only schedules the attempt."""
    from openstore.sidecar.protocols.agent_routes import (
        REQUIREMENTS_REFRESH_SECONDS,
        refresh_requirements,
    )

    while True:
        await asyncio.sleep(REQUIREMENTS_REFRESH_SECONDS)
        await refresh_requirements()


def _origin_host(origin: str) -> str:
    """Host part of an origin, lowercased, without port or brackets."""
    from urllib.parse import urlparse

    host = urlparse(origin).hostname or ""
    return host.lower()


def _origin_rp_mismatch(origin: str, rp_id: str) -> bool:
    """True when a passkey ceremony minted for `rp_id` cannot run on `origin`.

    WebAuthn binds the ceremony to the RP ID: the browser refuses when the id
    is neither the origin host nor a registrable parent of it. Warn-only by
    decision — the deploy keeps serving every other authority kind.
    """
    host = _origin_host(origin)
    rp = rp_id.strip().lower()
    if not host or not rp:
        return False
    return host != rp and not host.endswith(f".{rp}")


def _provider_for(settings: Settings) -> Any:
    """The Provider this deploy talks to (ADR-0013).

    `PAYMENT_PROVIDER` was a declared setting that reached nothing, so the
    adapter was never chosen at all. `razorpay` refuses live keys in demo mode
    at its own boot, which is where that check belongs.
    """
    if settings.payment_provider == "razorpay":
        from openstore.sidecar.provider.razorpay import RazorpayProvider

        return RazorpayProvider(
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret,
            demo_mode=settings.openstore_demo_mode,
        )
    from openstore.sidecar.provider.fake import FakeProvider

    return FakeProvider(
        public_origin=settings.openstore_public_origin or "http://spoiledduckie.localhost"
    )


def _passkey_rp_for(
    settings: Settings, merchant_domain: str, policy: Any, sessionmaker: Any = None
) -> Any:
    """The passkey Relying Party, or `None`.

    **The RP ID decides which passkeys exist** (ADR-0008), so it is configuration
    and never derived from a request header — an origin that could name itself
    could claim another shop's credentials. It falls back to the Merchant domain,
    which is the same value in every correct install and is stated rather than
    assumed.

    `None` when the Merchant has not enabled the kind, and the approve page then
    offers no passkey button: offering a ceremony that the Gate will refuse
    teaches the Consumer that the page lies.
    """
    from openstore.sidecar.authority.passkey import PasskeyRP
    from openstore.sidecar.core.codes import AuthorityKind

    log = logging.getLogger("openstore")
    if AuthorityKind.PASSKEY not in policy.enabled_authority_kinds:
        log.warning("passkey ceremony off: this Merchant has not enabled the kind")
        return None

    rp_id = settings.webauthn_rp_id or merchant_domain
    origin = settings.openstore_public_origin or f"https://{rp_id}"
    log.warning("passkey ceremony wired: rp_id=%s origin=%s", rp_id, origin)
    return PasskeyRP(
        rp_id=rp_id,
        origin=origin,
        rp_name=settings.webauthn_rp_name or "This shop",
        sessionmaker=sessionmaker,
    )


def _export_new_keys(settings: Settings, keyring: Keyring, log: logging.Logger) -> None:
    """Write the Merchant's backup copy, the once it is worth writing.

    `SIDECAR_KEY_EXPORT_PATH` was a declared setting that reached nothing, which
    is the same bug as the allowlist and the OAuth credentials before it: a
    deploy could set it, believe it had a backup, and have none.

    Written only on enrollment, because that is the only moment a key exists
    that has never been backed up — and an export rewritten on every boot is one
    that quietly follows a rotation the operator has not yet copied anywhere.
    **A backup nobody has restored is a hope**, so the log says how to check it.
    """
    if not settings.sidecar_key_export_path:
        log.warning(
            "NO KEY EXPORT: a new signing key was enrolled and SIDECAR_KEY_EXPORT_PATH "
            "is unset, so this shop's identity exists in exactly one place. Losing it "
            "invalidates every receipt this shop ever issues."
        )
        return

    from openstore.sidecar.evidence.keys import save

    destination = Path(settings.sidecar_key_export_path)
    save(keyring, destination, passphrase=settings.sidecar_signing_key_passphrase)
    log.warning(
        "key export written to %s. Move it somewhere this host cannot reach, then "
        "prove it: `openstore-keys check %s --against %s`",
        destination,
        destination,
        settings.sidecar_signing_key_path,
    )


def _configure_webhooks(settings: Settings, provider: Any) -> None:
    """Point the callback route at this deploy's secret, or at nothing.

    With no secret there is no verifier and every webhook is refused. That is
    the conservative half of a choice with only one safe side: a route that
    accepts an unsigned body is a route anyone can use to tell the sidecar that
    money arrived.
    """
    from openstore.sidecar.provider.routes import WebhookContext
    from openstore.sidecar.provider.routes import configure as configure_webhooks
    from openstore.sidecar.provider.webhooks import WebhookVerifier

    log = logging.getLogger("openstore")
    secret = settings.webhook_secret
    # The demo rail carries its own secret so the callback path is exercised by
    # `make demo` rather than only by a test. It is not a deployment secret and
    # a real adapter never reaches this branch.
    if not secret and provider.name == "fake" and settings.openstore_demo_mode:
        secret = provider.webhook_secret
        log.warning("webhook secret: the demo rail's own, because none is configured")

    if not secret:
        configure_webhooks(WebhookContext(provider=provider))
        log.warning(
            "NO WEBHOOK SECRET: every Provider callback will be refused, so a payment can "
            "only finish through a manual status check. Set %s.",
            "RAZORPAY_WEBHOOK_SECRET"
            if settings.payment_provider == "razorpay"
            else "PROVIDER_WEBHOOK_SECRET",
        )
        return

    configure_webhooks(
        WebhookContext(
            verifier=WebhookVerifier(secret=secret),
            provider=provider,
            signature_header=provider.webhook_signature_header,
        )
    )
    log.warning(
        "webhook route wired: /provider/webhook verifying %s", provider.webhook_signature_header
    )


def _load_keyring(settings: Settings, merchant_domain: str) -> Keyring | None:
    """The Merchant's signing keys, read from disk or minted once (ADR-0014).

    With no keyfile path configured there is no key, the JWKS is empty, and
    nothing can be sealed. That is reported here and at `/healthz` rather than
    discovered by an agent whose refusal says only that this shop carries no
    keys — which is how it was discovered.
    """
    log = logging.getLogger("openstore")
    if not settings.sidecar_signing_key_path:
        log.warning(
            "NO SIGNING KEY: SIDECAR_SIGNING_KEY_PATH is unset, so the JWKS is empty, "
            "no receipt can be sealed, and agents pinning this shop's keys will refuse it."
        )
        return None

    keyring, enrolled = load_or_enroll(
        Path(settings.sidecar_signing_key_path),
        merchant_domain=merchant_domain,
        passphrase=settings.sidecar_signing_key_passphrase,
    )
    if enrolled:
        _export_new_keys(settings, keyring, log)
    log.warning(
        "signing keys %s: merchant=%s kid=%s path=%s",
        "ENROLLED (first boot for this keyfile)" if enrolled else "loaded",
        merchant_domain,
        keyring.current.kid,
        settings.sidecar_signing_key_path,
    )
    return keyring


app = FastAPI(
    lifespan=lifespan,
    title="OpenStore sidecar",
    description="Makes one Merchant site transactable by any Buyer Agent.",
    version="0.1.0",
    docs_url=None,  # no interactive docs on a money surface
    redoc_url=None,
)


@app.middleware("http")
async def correlate(request: Request, call_next: Any) -> Response:
    """Stamp every request with an id, and return it on the response.

    The id we generate is ours. An inbound `x-request-id` is echoed in
    `x-correlation-id` rather than adopted: a caller-supplied log key means two
    requests can claim to be the same one, and a Merchant chasing an incident
    would find a stranger's entries mixed into their own.
    """
    set_request_id(new_request_id())
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id()
    upstream = request.headers.get("x-request-id", "")
    if upstream:
        response.headers["x-correlation-id"] = upstream[:200]
    return response


# Order matters: the console's /agentic/{tab} catch-all would otherwise swallow
# every sibling route and answer "no console tab". The approve page in
# particular is authenticated by its one-time token and NOT by the Merchant
# session, so it must reach its own handler.
app.include_router(agent_router)
# Public, and verified by HMAC rather than by who is calling — a Provider is not
# an agent and holds no token here.
app.include_router(provider_router)
app.include_router(approve_router)
app.include_router(merchant_actions_router)
app.include_router(console_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Says nothing about whether it can work."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness: configuration loaded and the dangerous combinations refused.

    Reports the dev SSRF allowlist when it is in use, because SPEC §14 requires
    it to be visible in health output — an exception nobody can see is an
    exception that outlives its reason.
    """
    try:
        settings: Settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - the reason must reach the operator
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "reason": str(exc)},
        )

    body: dict[str, Any] = {
        "status": "ready",
        "demo_mode": settings.openstore_demo_mode,
        "merchant_domain": settings.openstore_merchant_domain or None,
        "checks": {
            "settings": "ok",
            # An empty JWKS is invisible from the outside until an agent refuses
            # the shop for carrying no keys. It is a check here for that reason.
            "signing_key": "ok" if get_surface().jwks.get("keys") else "absent",
            # A2 adds the trait, A3 the database and provider. Each lands as its
            # own named check rather than widening this one, so a partial outage
            # is legible at 2am (SPEC §14).
        },
    }
    warnings: list[dict[str, Any]] = []
    if body["checks"]["signing_key"] == "absent":
        warnings.append(
            {
                "code": "no-signing-key",
                "detail": (
                    "This sidecar holds no signing key, so its JWKS is empty, no receipt can "
                    "be sealed, and an agent pinning this shop's keys will refuse it. Set "
                    "SIDECAR_SIGNING_KEY_PATH."
                ),
            }
        )
    rp_id = settings.webauthn_rp_id or settings.openstore_merchant_domain
    origin = settings.openstore_public_origin or f"https://{rp_id}"
    if _origin_rp_mismatch(origin, rp_id):
        warnings.append(
            {
                "code": "origin-rp-mismatch",
                "detail": (
                    f"Passkeys are set up for {rp_id}, not for {_origin_host(origin)}: "
                    f"the approve page hides the passkey button until both name the "
                    f"same host. Everything else keeps working."
                ),
            }
        )
    if settings.dev_profile_hosts:
        warnings.append(
            {
                "code": "dev-profile-allowlist-active",
                "hosts": list(settings.dev_profile_hosts),
                "detail": (
                    "The SSRF profile-fetch exception is enabled. Development only — "
                    "every fetch that uses it is logged and flagged in /agentic."
                ),
            }
        )
    if warnings:
        body["warnings"] = warnings
    return JSONResponse(status_code=200, content=body)


def _receipt_body(bundle: Bundle, result: VerifyResult) -> dict[str, Any]:
    """The machine reading. One dict, so the HTML page and the JSON cannot drift
    into describing different receipts."""
    return {
        "receipt": bundle.to_dict(),
        "verification": {
            "status": result.exit_code.name,
            "claims": result.claims,
            "unopened": result.unopened,
            "findings": [
                {"ok": f.ok, "label": f.label, "detail": f.detail} for f in result.findings
            ],
        },
    }


#: The two receipt routes share this. A missing receipt and a wrong id answer
#: identically, because the difference between them is exactly the oracle the
#: unguessable id exists to close — and the HTML page must not become the
#: friendlier channel that gives it away.
_RECEIPT_404 = {"error": {"code": "not-found", "detail": "No receipt with that id."}}


@app.get("/receipt/{receipt_id}.json")
async def receipt_json(receipt_id: str) -> JSONResponse:
    """The machine reading of a receipt, and what `openstore-verify` is handed.

    **Declared above the HTML route on purpose.** FastAPI matches in
    registration order and `{receipt_id}` happily swallows a dot, so the
    catch-all registered first would answer this with an HTML page whose id was
    `rcpt_….json`.
    """
    store: ReceiptStore = get_receipt_store()
    bundle = await store.get(receipt_id)
    if bundle is None:
        return JSONResponse(status_code=404, content=_RECEIPT_404)
    return JSONResponse(status_code=200, content=_receipt_body(bundle, verify(bundle)))


@app.get("/receipt/{receipt_id}", response_class=HTMLResponse)
async def receipt(receipt_id: str) -> Response:
    """The public receipt viewer, rendered for the person who bought the thing.

    **Outside the console's auth boundary, deliberately.** `/agentic` is
    session-authenticated for the Merchant, and a receipt that opens by
    unguessable ID *with no login* is the whole point — putting it there would
    mean no Consumer could ever open their own. The ID is the only credential
    and 128 bits is the protection.

    HTML here and JSON at `.json`, rather than one path negotiating on `Accept`:
    negotiation means the bytes a verifier receives differ from the bytes the
    reader saw, gated on a header and a `Vary` some proxy in the middle may not
    honour. Two paths, one body builder, nothing to disagree.
    """
    store: ReceiptStore = get_receipt_store()
    bundle = await store.get(receipt_id)
    if bundle is None:
        return HTMLResponse(status_code=404, content=not_found_page())
    return HTMLResponse(status_code=200, content=receipt_page(bundle, verify(bundle)))
