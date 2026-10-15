# Architecture

OpenStore system architecture. Status: normative overview. `Specification.md`, `Glossary.md`, `adr/`, and `Closed-Sets.md` remain law. If this document disagrees with those sources, those sources win. Readers use this document in three ways. The merchant who runs a shop reads it. The developer or contributor who changes this repository reads it. The curious buyer who wants to know what happens to money reads it.

## How to read this document

- Curious buyer: read §1, §2, and §8. These parts cover the purchase flow, the approval tap, agent limits, and receipt proof.
- Merchant: read §1, §3, §5, and §8. These parts cover what you run, what you own, what the sidecar refuses for you, and operating cost after install.
- Developer and contributor: read everything in order. §4 maps modules. §5 traces the money path with entry points and invariants. §6 lists trust boundaries. §7 lists guardrails that break the build when crossed.

Vocabulary follows `Glossary.md`. Capitalised terms below carry exactly the meaning given there.

## 1. What OpenStore is

OpenStore is a self-hosted sidecar. It makes one merchant site transactable by any buyer agent. It gives no agent a way to spend money.

An agent can search the catalogue, build a basket, and start a checkout. It cannot complete one. Every purchase ends with a human who approves an exact amount on the merchant domain. The receipt names the kind of approval used. It never reports an unqualified verified label.

Three claims carry the whole design:

1. An agent never holds spending authority. The agent receives a scope that cannot reach money. `place-order` returns an approve URL and never an order. The envelope boundary refuses a delegated payment credential with a named code. The discovery card states the refusal before an integrator writes a line.
2. The merchant computes prices and the sidecar repeats the arithmetic. Pricing, tax, and discount logic live behind the merchant quote door. The sidecar fetches the quote again immediately before any money move. It byte-compares that quote against what the consumer tapped. Disagreement fails closed with `price-changed`.
3. Every refusal is a closed code and the toolchain generates the registry from the enum. A code that appears in prose and not in `core/codes.py` breaks the build. A float in a money path breaks the build. A naive datetime breaks the build. An import across surface boundaries breaks the build.

What OpenStore is not: no hosted mall, no ranking, no multi-merchant tenancy, no rule engine, no subscriptions, no agent-held payment credentials. Reach means findability by agents that exist today. Reach is a phase-two track gated on install (`Plan-Distribution.md`). It is never a reason to weaken the Gate.

## 2. For the curious buyer

### 2.1 The purchase flow as you experience it

1. You ask your agent to find something. It searches one or more shops through the public agent surface.
2. It builds a basket with items, destination, contact point, and fulfillment option. It shows you a verbatim signed quote with tax lines and the delivered total.
3. You approve once in one place on the merchant approve page (`/agentic/approve?t=<one-time-token>`). The page shows the exact total. A UPI PIN entered in your own PSP app authorizes that exact basket. A per-domain passkey tap authorizes that exact basket.
4. The agent resumes where it left off and shows you a receipt. The receipt opens by an unguessable identifier with no login. It carries its own key snapshot. You can prove it offline with no action from the merchant.

One Allow-once plus one tap per spend. Zero re-search and zero re-login. Exact resumed state on both sides. The team treats context loss as a bug, not as an inconvenience.

### 2.2 What the agent can and cannot do

The agent holds scopes and never holds spending power: `search`, `build-basket`, `start-checkout`, `confirm`. The Gate refuses `confirm` without a fresh accepted Authority. Admission route does not change this rule. Standing always allow covers reads and drafts only. Anything that moves toward a spend needs a fresh Allow-once modal plus a fresh tap.

Your Allow click grants a scope and never grants an amount. The spend ceiling on some demo surfaces is an explicitly advisory flag. It is not a held mandate. Nothing in this system lets an assistant cap what your bank will settle. The interface says so. It never implies enforcement it lacks.

### 2.3 What proves your purchase

Every order produces a sealed receipt in five sections: bought, tapped, decided, told, moved. The merchant signs the hash-chained receipt. It carries the attestation hash of the items as sold. Later catalogue edits cannot rewrite history. Refunds append a new signed version. The original stays provable as is.

Proof needs no network and no action from the merchant:

```
uv run python -m openstore.sidecar.verify.cli receipt.json
```

Exit `0` means valid. Exit `1` means tampered and names the exact broken link. Exit `2` means untrusted key. A receipt signed before a key rotation or revocation stays valid forever. A receipt signed after rotation or revocation fails. Personal-data sections report `unopened` rather than failing. Commitments cannot open without the merchant salt. Erasure must never break proof.

## 3. For the merchant

### 3.1 What you run

One deploy serves one merchant domain only (ADR-0007). Five services share one origin behind one reverse proxy. The proxy owns TLS. The path split is the contract:

| Path | Served by | Exposure |
|---|---|---|
| `/`, `/shop`, `/p/<slug>`, `/lookup` | merchant site | public |
| `/.well-known/*` | sidecar | public, by design |
| `/agent/*` | sidecar | agent token, rate-limited by tier |
| `/agentic/approve` | sidecar | one-time tap token, not the merchant session |
| `/agentic/*` | sidecar | merchant session |
| `/receipt/<id>` | sidecar | public. The unguessable id is the only credential |
| `/provider/*` | sidecar | public. Authenticated by HMAC on the body, not by caller identity |
| `/trait/*`, `/admin*` | none | refused at the edge. Private network only |

The two public routes inside otherwise-authenticated prefixes are deliberate. A consumer who approves a spend is not the merchant. A receipt that opens by unguessable id with no login cannot live behind the merchant session.

### 3.2 What you own

You own catalogue, prices, stock counts, tax identity, shipping zones, discount-code table, order rows, customer communications, keys, and policy. The sidecar owns the Ledger, the Transcripts, the evidence bundles, and the two expiry clocks. It never owns product truth and never computes a price.

Policy is yours and the sidecar enforces it deterministically: time windows, per-order counts and quantities, blocked items, tag rules, per-order caps. Exposure controls which of those policies agents can see. Attribution reads agent-sourced orders and revenue split by `agent_id` over a date range. It reads from orders the sidecar already tags. It tells you whether any of this is working.

### 3.3 Money in and money out

Prepaid (UPI and any method you enable) flows in fixed order. The consumer taps. The Gate decides. The sidecar reserves stock. The provider creates a payment link. The provider callback settles the order. The Ledger captures.

Cash on delivery uses the same Gate on a `confirmed-intent` authority. The sidecar holds stock with no money held at all. Dispatch assigns the legal invoice number. Cash collected at the door writes a `CAPTURE` with no preceding `RESERVE`. Refunds are yours to click with reason, amount, and per-line restock flags. Partial amounts are allowed. The sidecar derives full versus partial from `refunded_minor`. It never adds a ninth order status.

The sidecar refuses agent-held delegated payment credentials by design and on purpose with a named code. Their absence costs native in-agent completion on gated surfaces. The team pays that cost deliberately (ADR-0016). The refusal is the product.

### 3.4 Operating it

Install is one command (`openstore up <domain>`). It writes the compose file, the reference Caddyfile, a fresh environment, a generated signing key, a printed encrypted key export, and a first-run console URL. You enroll, rotate additively, revoke, and export keys in `/agentic`. Rotation never re-signs history. Revocation invalidates the future, not the past. First-run refuses to continue until you acknowledge the saved key export. If the backup is untested, prove it by restoring it (`openstore-keys check --against`). An unrestored backup is hope.

Health and observability: `/healthz` and `/readyz` on every service. Structured logs for the money path carry order, agent, consumer, reason code, and duration. They never carry secrets or plaintext personal data. Counters track gate refusals by reason code, authority outcomes by kind, provider webhook lag, reconciler drift, and holds still open past their deadline. The sidecar owns both expiry clocks and the merchant never self-expires. Overdue holds appear in `/agentic`. Release them through the sidecar. Never release them by a merchant-side write.

Notifications go out from the merchant system off its own status changes. The set is fixed: order accepted message with receipt link, shipped notice with tracking and invoice number, expiry, refund, new-order alert. The merchant system owns customer communications and the contact-point plaintext. The sidecar sends no email or SMS.

Data retention: destination, contact point, and payer-handle plaintext live only in the merchant order row under a merchant-set retention window and erasure action. Evidence commits by salted hash, so erasure never breaks proof. Data-protection obligations sit with the merchant as data fiduciary. The sidecar avoids every design that will make compliance impossible.

## 4. System architecture

### 4.1 The three surfaces and the one rule

| Surface | Root | Stack | Owns |
|---|---|---|---|
| Sidecar (the product) | `src/openstore/sidecar/` | Python 3.12, FastAPI, SQLite to Postgres, `uv` | Gate, Ledger, Authority, Evidence, protocols, `/agentic` |
| Merchant site (demo) | `demo/merchant-site/` | SvelteKit 2, Svelte 5, Postgres, `pnpm` | Catalogue, stock, orders, prices, GST, the nine doors |
| Buyer chat (demo) | `demo/buyer-chat/` | SvelteKit 2, Svelte 5, SQLite, `pnpm` | Threads, tool loop, rendering, nothing else |
| Design (assets only) | `design/` | CSS and fonts | Tokens and type. No logic, ever. |

The firewall never bends. No imports cross the three product roots in either direction for runtime, types, or tests. HTTP and signed webhooks are the only channels. `design/` opens to the two SvelteKit roots and to nothing else. It holds asset extensions only. `scripts/lint_firewall.py` breaks the build on any crossing, with planted crossings covered. The guardrail itself owns a test. That test plants a violation and proves the tool blocks it.

### 4.2 Sidecar module map

```
src/openstore/sidecar/
  app.py                FastAPI wiring, /healthz, /readyz, receipt viewer routes
  core/                 settings, db, canonical bytes, codes (the closed sets),
                        correlation ids, feed rendering, structured logging
  trait/                the nine-door client, signing, buckets, conformance fake
  gate/                 decide(), settle()/collect(), transcript, policy
  ledger/               append-only entries and the two invariants
  authority/            kinds, passkey ceremony, tap-token store
  admission/            OAuth allowlist, Agent Profile self-registration with
                        SSRF hardening, RFC 9421 signatures, rate-limit tiers
  provider/             provider trait, fake rail, Razorpay adapter, webhooks
  evidence/             bundle sealing, hash chain, keyring, receipt store,
                        public receipt viewer
  protocols/            agent routes, MCP, UCP, ACP, AP2 translators, tool
                        definitions, well-known card and manifests, events,
                        idempotency
  console/              /agentic routes, approve ceremony, merchant actions,
                        refund queue, server rendering
  verify/               offline verifier and CLI (exits 0/1/2)
  conform/              trait conformance suite and CLI (openstore-conform)
  sweeper.py            the 60-second expiry pass over the three deadlines
```

HTTP exposes six entry points:

- the well-known card and manifests
- the agent tool surface (MCP JSON-RPC plus UCP, ACP, and AP2 translators over the same core)
- the provider webhook
- the approve page
- the merchant console
- the public receipt viewer Every protocol hits one money core. Translators accept only a well-formed foreign envelope. They call the core. They translate the answer back. Core Transcript bytes stay identical across envelopes. Conformance replays assert that property.

### 4.3 Merchant truth and the trait doors

The merchant system owns Product Groups, Catalogue Items, stock counts, and order rows. Stock, price, and thresholds live on the Catalogue Item. The Catalogue Item is the resolved variant. They never live on the group. The sidecar reads fresh at every Gate. It mirrors back idempotently over a narrow HTTP trait on the private network. Each call carries HMAC over the raw body. Each call enforces a 60-second replay window with nonce. Every mutating door takes an idempotency key. The trait answers refusals with closed codes only.

Nine doors are required: `catalog.read`, `stock.read`, `reserve`, `commit`, `release`, `restock`, `orders.create`/`orders.read`, `orders.set-status`, `quote`. Door 9 (`quote`) is read-only and side-effect-free. Same inputs give same bytes any number of times. It carries no clock. Fulfillment ETAs are day counts, never dates. Midnight cannot produce a spurious `price-changed`. Door 2 returns exact integers. Every agent-facing response carries only an Availability Bucket (`in-stock`, `low-stock`, `sold-out`) cut at that item own threshold. An exact count handed to any self-registered stranger is an inventory-probing oracle. An optional tenth door, `requirements.read`, declares buyer details beyond address and contact; Merchants without it serve the fixed buyer shape.

Stock discipline: always `int >= 0`, never null, database-checked. Reserve is an atomic compare-and-set. It never drives stock negative. Version one never oversells. External (non-agent) sales win automatically. The next gate re-reads the lowered count. Webhooks and events are triggers only: unordered, duplicated, never guaranteed. The sidecar dedupes by `event_id` and runs a periodic reconciler poll.

### 4.4 Discovery

Version one is direct-add. The consumer pastes a merchant URL. The agent fetches the sidecar card (`/.well-known/agent-commerce.json`, UCP manifest, JWKS) directly. A list-file format ships scaffolded for later. No crawler exists in version one. No ranking exists in version one. No hosted search exists in version one. Reach without an index ships in two ingested formats. One is schema.org product markup on the own merchant pages. The other is a read-only product feed over the exposed catalogue. It uses merchant-center attribute names. It holds one entry per Catalogue Item grouped by variant group. The sidecar never crawls, ranks, or submits on merchant behalf. An index later reads the same listing format with zero money-core changes. It is a phonebook filtered by category and region. It ranks nothing and is not a mall (ADR-0022).

### 4.5 Order lifecycle

Eight canonical statuses, never a ninth: `pending`, `confirmed`, `paid`, `cancelled`, `expired`, `failed`, `refunded`, `completed`. Dispatch is a merchant-recorded event available from `confirmed` onward, not a status. Recording tracking details sets `dispatched_at`. On first call it assigns the sequential `invoice_number` for the financial year. It is distinct from the unguessable `receipt_id` that the bundle opens by. A legal tax invoice and an IDOR-proof lookup key need opposite properties. Partial refunds are an amount on the `REFUND` entry. The sidecar derives full versus partial from `refunded_minor`.

Three writers move order status: the tap, the sweeper, and the own merchant actions. The sidecar serializes them on the `orders.set-status` idempotency key and nowhere else. The merchant is the arbiter. The sweeper re-reads merchant truth before acting and corrects itself rather than holding a lock.

## 5. The money path

One pipeline, four stages. Every protocol hits it. No second money path exists.

### 5.1 Cart and Quote

A basket slip holds resolved Catalogue Items (`sku`, `qty`, `price`), never a product group. A group id refuses with `variant-required`. The sidecar never guesses an option for it. The slip adds destination, contact point, and chosen fulfillment option. The merchant prices the whole thing through door 9 and returns the Quote. The Quote holds six parts:

- subtotal
- discount lines
- fulfillment options and chosen cost
- tax lines with GST, CGST/SGST or IGST by per-line place of supply
- GSTIN and HSN/SAC on the invoice
- total in paise integers with intrinsic signs The sidecar ships no rate table, tax engine, or discount engine. A pending cart holds zero money with an expiry. It is distinct from a `pending` order row.

### 5.2 The Gate

`decide()` runs twelve checks in fixed order. It stops at the first failure. It records `reason_code` equal to that failure:

`authority-present-and-accepted`, `currency`, `merchant`, `window`, `count`, `qty`, `blocked`, `tags`, `caps`, `quote-consistent`, `quote-fresh`, `method-enabled`.

Count, quantity, and caps evaluate at the Product Group. Two colors of one limited item cannot walk through a per-order cap.

Two checks do the heavy lifting. `quote-consistent` repeats the merchant arithmetic in five steps:

- lines sum to the total
- signs are correct
- currency matches
- subtotal equals quantity times attested price from the pinned attestation
- tax is additive or informational per flag Sum repetition is not price computation. Without it a buggy or compromised merchant gets its total signed unchallenged. `quote-fresh` runs once immediately before `RESERVE` and never after payment. It re-calls `quote` with identical inputs. It byte-compares the result against the pinned Quote. If the pinned Quote drifts, fail closed with `price-changed`. After money moves the pinned Quote is frozen. The only remaining proof is the provider amount and currency reconcile. A paid order that fails over a merchant price edit will strand real money against no order.

The `cart_hash` binds what the consumer authorized. SHA-256 covers canonical bytes of eight fields:

- sorted lines
- quote hash
- salted destination and contact commitments
- fulfillment option
- total
- currency
- merchant domain
- expiry The consumer authorizes the delivered total, not the item subtotal. Anything shown on the approve page is covered on display. If anything changes after render, invalidate the token even when the total did not move.

Authority lands at different moments by kind and the Gate records the difference honestly. `passkey` and `confirmed-intent` land before the Gate and resolve to pass or fail there. `upi-pin` lands with the money and resolves to `deferred` at `decide()`. `settle()` must turn that `deferred` into pass before it captures. A bundle that carries an unresolved `deferred` fails proof. The sidecar writes every result (pass, fail, or deferred) into the byte-stable Transcript stored with the decision.

### 5.3 The Ledger

The Ledger is sidecar-owned and append-only: `RESERVE`, `CAPTURE`, `RELEASE`, `REFUND`, `REVERSAL`. Every money entry carries `amount_minor`. Two invariants stay deliberately separate. Holds close: each `RESERVE` ends in exactly one `CAPTURE` or `RELEASE`. Refunds stay bounded: `sum(REFUND)` does not exceed captured minus already refunded. A `REVERSAL` is money removed by someone other than the merchant, as the provider reports it. It obeys neither bound. The sidecar records it even when it drives net cash negative. Refusal to record money that already left is how books go wrong. One idempotency key guards each attempt (`order_id:attempt`, `cart_id:attempt` at `orders.create`, the door that produces the order id) with a unique constraint. That key closes read-then-act races. A crash mid-link adopts through `check-status`. It never double-creates.

### 5.4 Evidence

The sealed receipt uses the tight five-section form described in §2.3. It is hash-chained and merchant-signed. Version one uses no Merkle tree. It carries its JWKS snapshot so offline proof is really offline. Each order pins the attestation hash it used. The passkey challenge that authorizes a spend is itself the binding: SHA-256 over cart hash, amount, currency, domain, and expiry. A signature over a swapped basket is a signature over a different challenge. It fails.

### 5.5 Authority and admission

Two admission routes, one spending rule. An allowlisted agent uses merchant-issued OAuth client credentials. A stranger self-registers by publishing an Agent Profile at a well-known URL. The profile holds name, contact, and ES256 JWKS. The stranger signs every request under RFC 9421 message signatures. Each signature carries digest, timestamps, and nonce. The profile fetcher blocks SSRF with six controls:

- HTTPS only
- no private ranges
- resolve-then-pin against DNS rebinding
- no cross-host redirects
- size and timeout caps
- its own rate limit Both routes receive the same four scopes with short expiry. No tier unlocks money. Reputation buys throughput only. The sidecar refuses blocklisted profiles at registration.

Consumer authority is a closed set: `upi-pin`, `passkey`, `confirmed-intent`, `mandate`. The Gate accepts a kind only where the merchant enabled it. It records what each grant bound (`cart`, `amount`, or `none`, by `payer-device`, `payer-bank`, or `merchant`). In India the default is `upi-pin`. The payer authenticates in their own PSP app against a named payee and an exact amount. `mandate` is defined and refused in version one. Enrollment is per merchant domain. A first-time consumer enrolls inside the first approve ceremony, not on a separate visit.

Tap delivery: the agent shows an approve URL (five-minute, single-use). The consumer opens it on the merchant domain. An optional private-code field re-quotes, re-renders, and rebinds before anything is signed. Private codes never transit the agent. The sidecar takes the authority over the post-application total. Return uses a resume URL that carries an unguessable single-use session-bound token. That token resolves to order and thread server-side, never as bare URL parameters.

## 6. Trust boundaries

- Agent to sidecar. Agents are proposal-only and hold no spending authority. A self-registered agent signs its own requests and carries no payment credential. Agent-facing order reads and refund requests resolve only against orders that carry that `agent_id`. An order from another agent is indistinguishable from one that does not exist. Open admission plus unscoped reads will otherwise form an enumeration oracle over order state of other consumers.
- Sidecar to merchant. The merchant is truth and is also untrusted on arithmetic. The sidecar reads stock fresh and repeats quote proofs before every money move. Status flips alone never move money.
- Sidecar to provider. The provider is the own merchant account and is untrusted beyond the money-moved fact. Webhooks pass HMAC proof on raw bytes. The event is a trigger, never an instruction. The handler re-asks the provider what it holds. Payloads dedupe by `event_id`. Amount or currency mismatch fails the order with `amount-mismatch` and never auto-captures. If no secret exists, refuse every webhook, demo mode included.
- Merchant to consumer data. Plaintext destination, contact, and payer handle live only in the merchant order row. Evidence carries salted commitments. The 128-bit salt lives only in that row and never in the bundle. The sidecar holds it in request memory for the length of one decision and never writes it anywhere. Erasure deletes row and salt together. The commitments stay permanently unopenable and proof still passes.
- Keys. The sidecar generates keys and the keys never leave it. Rotation is additive by key id. Recovery works only from the own encrypted enrollment export of the merchant. The demo refuses to boot with live payment keys while demo mode is on.

Rate limits run per agent tier and per IP on every `/agent/*` route, with separate throttles on tap-token issuance, approve attempts, and discount-code attempts. Every wrong code refuses as the same `code-invalid` with no message or timing tell. Policy caps bound an order. Rate limits bound an attacker.

## 7. Guardrails and working agreements

A build tool enforces closed sets in both directions. One authoritative registry comes from the code enum. CI diffs it. Fail loud: no coercion, no defaults, no silent fallbacks. Money uses paise integers. Time uses UTC. The server recomputes totals, hashes, discounts, and caps. It never trusts agent supply. No model output enters the money path.

```
make check        # guardrails, lint, types, and every suite (what CI runs)
make test         # the three test suites
make guardrails   # firewall, registry, money lint, time lint, trait vectors
make demo         # the conformance suite against the running store
make down         # stop, and remove the volumes
```

The guardrails are not advisory. Each guardrail owns a test. That test plants a violation and proves the tool blocks it. A guardrail with no observed failure is a guardrail with no known force. Golden vectors pin the `cart_hash` preimage and the core Transcript bytes. If a change breaks them, the change is wrong. Three independent implementations of the tax and rounding rules (merchant code, conformance fake, Gate proof) share no module. They must agree to the paise on the pinned worked example.

## 8. Operations, limits, and verification

Deploy per `Deployment.md`. Migrate before starting. The demo creates its own schema. A deploy that takes money refuses to start against a schema it did not migrate. Use per-deploy secrets with no safe defaults. Prove key backup by restore. Back up the database. Wire the provider. Serve health endpoints. Put the store on the other side of the nine doors. Disputes and reversals post a `REVERSAL` ledger entry plus a timeline event, whatever the rail produced. The sealed bundle is the evidence pack of the merchant. Hand it over without personal data.

Deliberately out of version one:

- campaigns, rules, bots, hosted mall and ranked search
- multi-merchant tenancy, multi-location, fees and store credit
- bulk cancel, auto-fulfillment, shipping labels, subscriptions
- multi-currency and duties, consumer accounts and returns portals
- fraud scoring, abandoned-cart recovery, Merkle batch-anchoring
- order-management write paths beyond the nine-door trait

The sidecar refuses delegated agent-held payment credentials on purpose. They are not unimplemented. E-invoicing thresholds, credit-note timing, and the remaining counsel questions in `Counsel-Brief.md` travel with counsel, not with code.

Status: demo. The sidecar marks every receipt as one. It refuses to boot with live payment keys while demo mode is on. The fonts in `design/` include one commercial face. Read `design/README.md` before you record, host, or share anything built from this.

## Appendix A. Document map

| Document | What it is |
|---|---|
| `Architecture.md` (this file) | normative overview for all three stakeholders |
| `Specification.md` | the rebuild specification, law |
| `Glossary.md` | the domain vocabulary, law |
| `Closed-Sets.md` | the generated closed-set registry, law (edit the enum, regenerate) |
| `Build-Plan.md` | the 48-hour build plan as executed, historical |
| `Plan-Index.md`, `Plan-Sidecar.md`, `Plan-Merchant-Site.md`, `Plan-Buyer-Chat.md`, `Plan-Distribution.md` | the phased build plans with DONE WHEN gates, historical |
| `Build-Log.md` | the running build record, historical |
| `Deployment.md` | what a real install needs beyond the demo |
| `Expansion-Guide.md` | how to grow the plans without breaking the contracts |
| `Stakeholder-Overview.md` | the proof-carrying-commerce briefing |
| `Deterministic-Authorization.md` | the technical note for payment counterparties |
| `Counsel-Brief.md` | the single legal engagement, four questions |
| `Review-Findings.md`, `Review-Remediation.md`, `Review-Stakeholder-Findings.md` | pre-build reviews and their remediation, historical |
| `Discovery-Registry-Draft.md` | superseded reasoning trail behind ADR-0022, historical |
| `Placement-Pitch.md` | the consolidated placement pitch |
| `adr/` | the numbered, dated decisions, law |

Historical documents are frozen. They record what was decided and built. New documents correct them. The team never edits them in place.
