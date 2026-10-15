"""The cards a stranger reads before it knows anything about this shop.

Three documents, all public and unauthenticated by design:

- `/.well-known/agent-commerce.json` — who this Merchant is, what protocols the
  sidecar speaks, where the agent endpoints are, and **what it refuses**.
- `/.well-known/ucp.json` — the UCP manifest.
- `/.well-known/jwks.json` — the Merchant's signing keys, so a receipt can be
  verified by anyone holding it.

Public is the point. A card behind authentication is a card no new agent can
read, and the whole admission story (ADR-0012) starts with a stranger fetching
one without asking permission first.

**The card states the refusals inline.** An agent that reads it knows before it
starts that there is no delegated-credential path here and that every spend
terminates in a human tap — which is kinder than finding out at the last step.
"""

from __future__ import annotations

from typing import Any

from openstore.sidecar.core.codes import AuthorityKind, OrderStatus, PaymentMethod, Protocol, Scope
from openstore.sidecar.gate.policy import Policy
from openstore.sidecar.protocols.events import BACKOFF_SECONDS, MAX_ATTEMPTS
from openstore.sidecar.protocols.idempotency import MAX_KEY_LENGTH, RETENTION
from openstore.sidecar.protocols.registry import TOGGLE_ORDER, badge, spec_for
from openstore.sidecar.trait.models import Destination


def agent_commerce_card(
    *,
    merchant_domain: str,
    merchant_name: str,
    origin: str,
    enabled_methods: frozenset[PaymentMethod],
    enabled_authority_kinds: frozenset[AuthorityKind],
    demo: bool,
    description: str = "",
    categories: tuple[str, ...] = (),
    limits: dict[str, Any] | None = None,
    requirement_fields: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """`origin` is the origin an agent should dial back on, and it is passed in
    rather than derived: a card that assumes `https://<domain>` advertises an
    origin that does not answer wherever the deployment is plain http, and every
    endpoint on it is then unreachable to the agent that just read it.

    `description` and `categories` exist so an agent holding ten shops can put
    the likely ones first instead of asking all ten about a mug. They are
    **declared by the Merchant and verified by nobody** — which is why the card
    says so in `note` rather than leaving an integrator to assume otherwise.
    A shop that sells something its own description never mentions is the
    ordinary case, not the exception, so these order a search and must never
    decide one: absence is a thing only a `search` can establish.
    """
    merchant: dict[str, Any] = {"name": merchant_name, "domain": merchant_domain}
    if description:
        merchant["description"] = description
    if categories:
        merchant["categories"] = list(categories)
    if description or categories:
        merchant["note"] = (
            "Declared by this Merchant and verified by no one. A hint for deciding which "
            "shops to ask first, never grounds for concluding a shop does not stock "
            "something — only a search can establish that."
        )
    return {
        "version": "1",
        "api": api_version(),
        "idempotency": idempotency_contract(),
        "events": events_contract(),
        "merchant": merchant,
        "demo": demo,
        "endpoints": {
            "mcp": f"{origin}/agent/mcp",
            "tools": f"{origin}/agent/tools",
            "register": f"{origin}/agent/register",
            "token": f"{origin}/agent/token",
            "feed": f"{origin}/agent/feed.json",
            # One core, four envelopes. Advertising a protocol with no endpoint
            # behind it is how an agent discovers the gap at the checkout step.
            "ucp_checkout": f"{origin}/agent/ucp/checkout",
            "acp_checkout_sessions": f"{origin}/agent/acp/checkout_sessions",
            "ap2_checkout": f"{origin}/agent/ap2/checkout",
            "approve": f"{origin}/agentic/approve",
            "jwks": f"{origin}/.well-known/jwks.json",
        },
        "protocols": [spec_for(p).protocol.value for p in TOGGLE_ORDER],
        "scopes": [s.value for s in Scope],
        "admission": {
            # Two routes, one authority. No Merchant action is needed for the
            # second: that is the product, not a convenience.
            "self_registration": True,
            "oauth_client_credentials": True,
            "signature": "RFC 9421 over @method, @target-uri and content-digest",
            "agent_id": "RFC 7638 JWK thumbprint",
        },
        "authority": {
            "kinds_accepted": sorted(k.value for k in enabled_authority_kinds),
            "completion": "redirect-only",
            "note": (
                "Every spend terminates in a fresh human Authority at the approve URL on "
                "this domain. An agent may build a basket; it may not spend."
            ),
        },
        "payment": {
            "methods_enabled": sorted(m.value for m in enabled_methods),
            "delegated_credentials": "refused",
            "note": (
                "This Merchant does not accept agent-held payment credentials. Their "
                "absence is deliberate (ADR-0008, ADR-0013), not unimplemented."
            ),
        },
        "limits": limits or {},
        "requires": checkout_requirements(requirement_fields=requirement_fields),
        "conformance": [badge(protocol, enabled_methods) for protocol in TOGGLE_ORDER],
    }


#: The dated version of this sidecar's own agent surface — the tool set, the
#: result shapes, the refusal fields.
#:
#: Dated rather than numbered, and for the reason `acp.py` already gives about
#: ACP: "conformant" unqualified ages badly against a specification that moves.
#: An integrator who built against a date can say which one, and a sidecar that
#: has moved can say so without anybody comparing field lists by hand.
API_VERSION = "2026-09-23"

#: Versions this deployment still answers on. One today; the point is that the
#: key exists before it needs to, so an agent can check membership rather than
#: equality and does not break on the first revision.
SUPPORTED_API_VERSIONS = (API_VERSION,)

#: How long a version keeps working after a newer one ships. Published because
#: an integrator's real question is never "what is current" but "how long do I
#: have" — and a support window that lives only in someone's intentions is one
#: nobody can plan against.
DEPRECATION_NOTICE_DAYS = 180


def events_contract() -> dict[str, Any]:
    """How to stop polling.

    Published because the alternative to knowing this is `order-status` in a
    loop, per order, per shop — and an agent cannot decide to stop polling on
    the strength of a feature it has to discover by experiment.
    """
    return {
        "supported": True,
        # Declared in the Agent Profile, not passed at registration: that way it
        # is a fact the agent published at a URL this sidecar already fetched
        # and hardened, rather than a string an unauthenticated caller handed
        # over.
        "declared_in": "agent profile: callback_url",
        "delivery": "POST, at least once, retried with backoff",
        "max_attempts": MAX_ATTEMPTS,
        "backoff_seconds": list(BACKOFF_SECONDS),
        "signature": {
            "header": "x-openstore-signature",
            "key_id_header": "x-openstore-key-id",
            "algorithm": "Ed25519 over the canonical body",
            "verify_with": "/.well-known/jwks.json",
        },
        "kinds": [f"order.{status.value}" for status in OrderStatus],
        "note": (
            "At least once, so deduplicate on `event_id`. The body carries an order id, a "
            "kind and a receipt id — never PII and never a total; call `order-status` with "
            "your own token for anything more. Answer 410 to stop delivery: it parks after "
            f"{MAX_ATTEMPTS} failed attempts."
        ),
    }


def idempotency_contract() -> dict[str, Any]:
    """How to retry safely, published so nobody has to find out by trying.

    An agent whose call timed out is deciding between a double line and none,
    and the honest answer — "send a key and retry" — is only useful if it can
    learn the key's name, its scope and how long it lasts without an experiment
    against a live shop.
    """
    return {
        "supported": True,
        "parameter": "params.idempotency_key",
        "max_length": MAX_KEY_LENGTH,
        "scope": "per agent",
        "retention_hours": int(RETENTION.total_seconds() // 3600),
        # Named rather than described: an agent branching on "is this tool
        # replayable" should not be parsing a sentence to find out.
        "not_stored_for": ["search", "read-item", "order-status"],
        "replay_marker": "replayed",
        "note": (
            "A repeat of the same call with the same key returns the first answer, marked "
            "`replayed: true`. The same key with different arguments is refused with "
            "`idempotency-conflict` rather than answered — returning the first result for a "
            "second, different request would answer a question nobody asked. Refusals are "
            "not stored, so correcting your arguments and retrying with the same key works."
        ),
    }


def api_version() -> dict[str, Any]:
    """What an integrator needs to know about how this surface changes."""
    return {
        "current": API_VERSION,
        "supported": list(SUPPORTED_API_VERSIONS),
        "deprecation_notice_days": DEPRECATION_NOTICE_DAYS,
        "note": (
            "Dated, not numbered. Additive changes — a new result field, a new refusal "
            f"field — ship without a new version; a removal or a changed meaning gets one, "
            f"and the version it replaces keeps answering for {DEPRECATION_NOTICE_DAYS} days "
            "after the successor ships."
        ),
    }


def checkout_requirements(
    requirement_fields: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """What a basket needs before this shop will quote it.

    An agent learns all of this today by being refused: the Destination shape
    from a validation message, the Contact rule from a `not-found`, the ordering
    from watching `needs` shrink. The buyer chat's own system prompt tells a
    model to *"ask for exactly what this shop needs ... write the fields from the
    shop's own tool results, never from habit"* — an instruction it could not
    follow, because the shop never published them.

    **Derived from the model the trait actually validates against**, never
    retyped. A hand-written copy of `Destination` here would be a second
    statement of the same rule, and the interesting failure is the one where the
    published shape and the enforced shape disagree — an agent building exactly
    what the card described and being refused for it.
    """
    schema = Destination.model_json_schema()
    required = set(schema.get("required", []))
    fields = []
    for name, spec in schema.get("properties", {}).items():
        field_: dict[str, Any] = {"name": name, "required": name in required}
        if isinstance(spec.get("pattern"), str):
            field_["pattern"] = spec["pattern"]
        if isinstance(spec.get("default"), str) and spec["default"]:
            field_["default"] = spec["default"]
        fields.append(field_)

    requirements: dict[str, Any] = {
        "destination": {"fields": fields},
        "contact": {
            # Not a schema: the rule is "at least one of these", which a field
            # list cannot express and which is the only thing an agent needs to
            # know before asking a person for their details.
            "any_of": ["email", "phone"],
            "note": "A Contact Point is an email, a phone, or both — and one is needed.",
        },
        "order": ["lines", "destination", "fulfillment"],
        "note": (
            "Fill these in this order. Changing the basket un-chooses fulfillment, so an "
            "option picked before the last line went in has to be picked again."
        ),
    }
    if requirement_fields:
        # Door-10 extras, rendered from the snapshot the tools serve — one
        # vocabulary in card and tools, or an agent trusts one and is refused
        # by the other.
        requirements["details"] = {
            "fields": list(requirement_fields),
            "note": "Set with set-details, in pieces if needed. Required keys must hold before checkout.",
        }
        requirements["order"] = ["lines", "destination", "details", "fulfillment"]
    return requirements


def policy_limits(policy: Policy) -> dict[str, Any]:
    """The Gate's own limits, published so an agent can plan against them.

    Every one of these is a refusal an agent otherwise discovers by building a
    basket and being turned away at `decide()` — the most expensive possible way
    to learn a number that was never a secret. `window_open` is the sharpest:
    a closed shop currently answers searches normally and refuses at checkout,
    so an agent spends a whole conversation to find out the door was shut.

    Read from the live Policy, never a copy. These are only worth publishing if
    they are the values actually enforced, and a card that advertised a cap the
    Gate had stopped applying would be worse than a card that said nothing.

    `blocked_tags` and the per-group quantity overrides are the Merchant's own
    refusal surface and are published as counts rather than contents: an agent
    needs to know a tag rule exists so it can read the refusal, and nobody
    outside needs the list of what this shop will not sell.
    """
    return {
        "currency": policy.currency,
        "window_open": policy.window_open,
        "per_order_cap_minor": policy.per_order_cap_minor,
        "per_order_line_count": policy.per_order_line_count,
        "per_group_qty": policy.per_group_qty,
        "per_group_qty_overrides": len(policy.per_group_qty_overrides),
        "blocked_tag_rules": len(policy.blocked_tags),
        "note": (
            "Enforced by the Gate on every basket. An agent that plans inside these "
            "spends no round trips discovering them; exceeding one is refused with a "
            "named code and never silently trimmed."
        ),
    }


def ucp_manifest(*, merchant_domain: str, merchant_name: str, origin: str) -> dict[str, Any]:
    spec = spec_for(Protocol.UCP)
    return {
        "ucp_version": spec.version,
        "merchant": {"name": merchant_name, "domain": merchant_domain},
        "capabilities": {
            "catalog": True,
            "checkout": True,
            # Completion is a buyer escalation to this Merchant's own approve
            # page, which is UCP's own path rather than a deviation from it.
            "direct_completion": False,
            "buyer_escalation": True,
        },
        "escalation_url": f"{origin}/agentic/approve",
        "deviations": badge(Protocol.UCP, frozenset())["deviations"],
    }


def jwks_document(keyring_jwks: dict[str, Any]) -> dict[str, Any]:
    """The Merchant's public keys, revoked ones included.

    A revoked key stays published because a receipt it signed **before**
    revocation must still verify, and a verifier with no copy of the key cannot
    check that at all (ADR-0014).
    """
    return keyring_jwks
