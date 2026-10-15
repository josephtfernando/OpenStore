# OpenStore Rebuild Spec (v1)

Sidecar means transaction service for one store.
Merchant means human owner of store and catalog.
Consumer means human buyer who pays per order.
Buyer Agent means external software that proposes orders.
This spec describes a sidecar-only rebuild.
It keeps the same premise as before.
It sets solid requirements from the start.
It applies guardrails from commit one.
Glossary in `Glossary.md` (formerly `CONTEXT.md`) is law.
Decisions in `docs/adr/` are law.

## 1. Surfaces (3 roots)

The build has three roots.
Each root deploys separately.
Each root owns separate code and data.

- `src/openstore/sidecar/` holds the product. It makes one Merchant site transactable by any Buyer Agent for Consumers. One deploy serves one Merchant domain only (ADR-0007). It acts as middleware only. It holds no product information of its own. It provides no shopping UI. Layout: `src/openstore/sidecar/`.
- `demo/merchant-site/` holds the demo accessory store SpoiledDuckie (Surface 2). It uses SvelteKit (`adapter-node`) plus Postgres. It runs in its own container. It uses its own DB. The portfolio design system already uses SvelteKit. The demo therefore reuses it as a dependency. It never re-implements it. The demo is a real shop. It provides a home, shop, item, direct-checkout, and order-lookup storefront. It provides a shop-ops admin. The admin covers dashboard, catalog plus variants, media, CSV handling, inventory moves audit, orders Timeline, and refund queue. Prices are manually set only. It has no offers engine and no discounts engine (cut per Q44). It adds only pricing that law and the post office require.
That pricing is three things.
It is shipping zones with flat rates.
It is GST rates per item (GSTIN, HSN/SAC, CGST/SGST against IGST by place of supply).
It is a hand-entered discount code table with no rules behind it. It serves prices through door 9 `quote` (ADR-0010). It sends its own order-confirmation, refund, and new-order notifications. It proves that a Merchant can operate the store.
- `demo/buyer-chat/` holds the demo Buyer Agent (Surface 3). It works as a Claude-like chat with tool calls over Ollama (swappable). It runs as an air-gapped stranger. It uses its own folder, process, dependencies, and DB. It uses zero sidecar imports. It uses HTTP only. The sidecar admits it like any external agent. It presents its own published Agent Profile (ADR-0012). Merchant-issued OAuth acts as the allowlisted alternative. It proves the Consumer flow end to end.
- A build-breaking import firewall guards both directions. It covers runtime, types, and tests. CI turns red on any crossing.

## 2. Actors (Glossary.md)

Payment Provider means company that moves money for the Merchant.
Authority means fresh buyer approval for one spend.

- Merchant is a human. The Merchant owns store, keys, catalog, and policies.
- Consumer is a human. The Consumer buys goods. The Consumer pays manually every time.
- Buyer Agent is any external agent. It acts proposal-only. It holds no spending authority. A self-registered stranger signs its own requests with its own key (ADR-0012). It never holds a payment credential. It never authorizes a spend.
- Payment Provider is the Merchant account. It is Justpay, Airpay, or Razorpay through one trait in v1. It is untrusted. It never decides authority.
- One sidecar serves one Merchant (ADR-0007).

Admission mechanism means the Gate that accepts any agent that speaks the protocol.
Reach means which assistants actually send traffic to Merchant endpoints.
The phrase Any Buyer Agent describes the admission mechanism.
It does not describe reach.
Self-registration accepts any agent that speaks the protocol.
Today that client is a hand-rolled MCP client.
It is not a mainstream assistant surface.
ChatGPT and Perplexity source shopping catalogues through feed submission and gated merchant programmes.
Neither pulls an arbitrary merchant-hosted endpoint.
Which agents speak the protocol is a distribution problem.
[`Plan-Distribution.md`](./Plan-Distribution.md) tracks it.
It is not a limitation of the Gate.
Install-facing copy must carry this distinction.
It must not inherit the unqualified claim (`Review-Remediation.md` item #3).

The Payment Provider account belongs to the Merchant.
This fact carries load beyond this section.
The ADR-0021 ECO and TCS boundary rests on it.
The payment-orchestration boundary rests on it.
Any change here reopens those ADRs.
It is never a configuration detail.

## 3. Discovery (ADR-0005)

Direct-add means Consumer pastes a Merchant URL to start.
Listing means name plus key plus protocols for one Merchant.
Index means later phonebook of Listings with no ranking.

V1 uses direct-add only.
Consumer pastes the Merchant URL.
The agent fetches the sidecar card directly.
The card is `/.well-known/agent-commerce.json` plus UCP manifest plus JWKS.
The list-file format is `lists/seed.json`.
It holds name, category, region, sidecar URL, public key, and protocols.
It also holds the `verified_as_of` date and self-published signature that a Listing needs to count as more than a claim.
The format ships scaffolded in v1.
It ships empty plus one example.
V1 has no crawler.
V1 has no ranking.
V1 has no hosted search.
A later Index reads the same format with zero money-core changes.
The Index is a phonebook filtered by category and region.
It ranks nothing.
It is not a mall (ADR-0022).
`Plan-Distribution.md` D4 specifies it.
The sidecar never crawls on behalf of the Merchant.
It never ranks on behalf of the Merchant.
It never submits on behalf of the Merchant.

Reach without an Index uses the two formats that readers actually ingest.
First, the Merchant site emits schema.org `Product` plus `Offer` JSON-LD on product pages.
Crawlers and AI channels read that markup.
It belongs with the pages that own the SEO.
Second, the sidecar serves `/agent/feed.json` over the exposed catalogue.
It uses Google Merchant Center product-data attribute names.
The names are `id`, `item_group_id`, `title`, `description`, `link`, `image_link`, `availability`, `price`, `brand`, `condition`, `color`, `size`, `gtin`, and `mpn` where known.
`availability` takes the reader published enum.
It never takes the Availability Bucket verbatim.
`low-stock` is not a member of that enum.
`low-stock` folds into in-stock.
A feed that carries an invented value fails with the only reader that matters.
The feed carries one feed item per Catalogue Item.
Catalogue Item means one resolved variant for sale.
It groups items by `item_group_id`.
That grouping expresses variants in every feed that matters.
The schema.org equivalent is `ProductGroup` plus `hasVariant`.
A Merchant can therefore submit the feed by hand with no translation step.
A third private schema produces a file that nobody ingests.

## 4. Money core (ADR-0002)

Quote means Merchant priced total for one basket.
Gate means deterministic checklist before money moves.
Ledger means sidecar-owned append-only money log.
Transcript means signed record of Gate inputs and decision.
Attestation means Merchant signed statement of item prices and tags.

One pipeline serves every protocol.
It has four stages.

### Cart plus Quote

Basket slip means lines of `{sku, qty, price}` for one order.
Fulfillment Option means Merchant delivery choice with cost.
Destination means delivery address for the order.
Contact Point means buyer phone or email for notices.
Discount Code means Merchant code that lowers the total.
Place of Supply means state that decides GST type.

Every `sku` is a Catalogue Item.
That is one resolved variant, for example black tote size M.
It is never a Product Group.
Product Group means parent of related variants.
If an agent sends a group, the sidecar refuses with `variant-required`.
The sidecar never guesses a size for the agent.
Before any spend the basket needs a Destination.
It needs a Contact Point.
It needs a chosen Fulfillment Option.
The Merchant prices the whole basket through door 9 `quote`.
The Merchant returns the Quote.
The Quote holds a subtotal.
It holds discount lines.
The Merchant makes sure that a Merchant Discount Code is correct and consumes it.
The sidecar never interprets the code.
`public` codes ride in from the Buyer Agent checkout card.
`private` codes are refused there.
Buyers enter `private` codes only on the approve page (ADR-0015).
The Quote holds fulfillment choices with costs.
It holds tax lines for GST.
GST uses CGST and SGST or IGST by per-line Place of Supply.
The invoice carries GSTIN and HSN/SAC.
Goods take the Destination state.
A service sold at the premises takes the place of performance.
One basket can therefore carry IGST on one line and CGST and SGST on the other.
For example one basket can hold a tote plus a charm-bar seat.
An Add-on is a composite supply.
Add-on means small extra attached to a parent line.
It is never a line of its own.
Its amount folds into the taxable value of the parent.
It inherits the rate of the parent.
It inherits the HSN and SAC of the parent.
It inherits the Place of Supply of the parent.
Gift-wrap on a charm-bar seat is therefore taxed as that service.
If an Add-on arrives without a parent, the sidecar refuses with `addon-without-parent`.
The Quote ends with a total.
The Quote carries no validity of its own.
The sidecar already owns both expiry clocks (§5).
The sidecar issues the checkout expiry that `expiry_utc` binds.
The agent renders that expiry as a countdown.
Door 9 stays a pure pricing answer with no clock in it.
The sidecar ships no rate table.
It ships no tax engine.
It ships no discount engine (ADR-0010).
A Pending Cart holds ₹0 with an expiry.
Pending Cart means pre-order draft with no money and no stock.
It is distinct from a `pending` order row.
A dry-run compile is available with zero side effects.

### Compiler (Gate)

`cart_hash` means sha256 of canonical basket bytes.
Binding means what an Authority locks to.
Quote-fresh means re-priced Quote matches the pinned Quote.

The Gate runs a deterministic checklist only.
It runs each review in fixed order.
The order is authority-present-and-accepted plus currency, Merchant, window, count, qty, blocked, tags, caps, quote-fresh, and method-enabled.
The Authority kind must be one that the Merchant enabled.
The Transcript records which kind authorized the spend.
It records what that kind bound (ADR-0017).
Count, qty, and caps evaluate at the Product Group.
Buying two of each color therefore cannot pass a two-per-order cap.
The Gate records all failures in the Transcript.
It sets `reason_code` to the first failure.
It uses closed reason codes only.
It stores a byte-stable transcript with the decision.
`cart_hash` equals sha256 of canonical bytes.
The bytes are UTF-8 JSON with sorted keys and no whitespace.
The object is `{lines: [{sku, qty, price_minor}] sorted by sku, quote_hash, destination_hash, contact_hash, fulfillment_option_id, total_minor, currency, merchant_domain, expiry_utc}`.
The Consumer authorizes the delivered total, not the item total.
Any change to the preimage produces a fresh `cart_hash`.
Whether it also owes a fresh Authority follows that Authority Binding (ADR-0017).
A `passkey` binds the cart, so any change re-taps.
A `upi-pin` binds the amount, so only a move in `total_minor` or `currency` re-approves.
Display rules close the gap that this leaves.
Anything shown on the approve page is covered.
A change after render invalidates the token.
A Destination edit that leaves the total unchanged therefore cannot redirect a paid parcel.
`quote_hash` equals sha256 of the canonical Quote bytes.
The Quote bytes sort all breakdown lines by type then label.
They use minor units with intrinsic sign.
`destination_hash` and `contact_hash` equal `HMAC-SHA256(order_salt, canonical PII bytes)`.
The 128-bit `order_salt` lives only in the Merchant order row.
It never lands in the bundle.
Erasure removes the salt with the row.
The commitment then cannot be opened again.
A bare hash of a phone number allows 10^10 guesses.
That is no protection at all (ADR-0011).
The core freezes after ADR-0010 lands and before S3 and S6.
Translators then produce byte-identical core transcripts.
Quote-fresh runs once, immediately before `RESERVE`.
`RESERVE` means stock hold at the confirm after the tap.
The Gate re-calls `quote` with identical inputs.
It byte-compares the answer against the pinned Quote.
If the answer drifts, it fails closed with `price-changed`.
The Consumer then re-taps the new number.
Quote-fresh never runs after payment.
Once money has moved, the pinned Quote is frozen.
The only post-payment review is the Provider amount and currency reconcile.
Its failure is `amount-mismatch`.
A Merchant price edit must never fail a paid order.
That failure strands real money with no order.
The Gate also reviews Quote arithmetic.
It makes sure that lines sum to the total.
It makes sure that signs are correct and discounts are negative.
It makes sure that currency matches.
It makes sure that the subtotal equals `sum(qty × attested price)` from the pinned Attestation.
It makes sure that tax lines are additive or informational per the Merchant tax-inclusive flag.
If the sums disagree, it refuses with `quote-inconsistent`.
Reviewing Merchant sums is not computing prices.
Without it a buggy or compromised Merchant gets its total signed unchallenged.
Authority does not always arrive at the same moment.
It arrives before the Gate under `passkey`.
It arrives with the money under `upi-pin`.
The Gate therefore splits two jobs (ADR-0017).
`decide()` runs every check including quote-fresh.
It writes the Transcript.
It permits the payment request.
`settle()` reviews the Provider record.
It records the Authority with its Binding.
It captures the money.
Policy is Merchant-owned.
Merchants edit policy in `/agentic`.
The Gate enforces policy here.
Agent output never executes.
The server makes sure that every request passes every review.

### Ledger

`RESERVE` means stock and money hold for one attempt.
`CAPTURE` means money taken for the order.
`RELEASE` means hold returned with no money taken.
`REFUND` means money returned after capture.
`REVERSAL` means money removed by a party other than the Merchant.

The Ledger is sidecar-owned and append-only.
Its path is `RESERVE → CAPTURE / RELEASE / REFUND / REVERSAL`.
Every money entry carries `amount_minor`.
Two invariants stay deliberately separate.
First, holds close.
Each `RESERVE` ends in exactly one `CAPTURE` or `RELEASE`.
Open holds sum to zero once closed.
This is the escrow-zero rule.
It says nothing about cash.
Second, refunds are bounded.
The rule is `sum(REFUND) ≤ captured − already_refunded`.
A `REVERSAL` is money removed by someone other than the Merchant as the Provider reports it.
Examples are an adjudicated UPI complaint, a post-settlement adjustment, a bank correction, or a card chargeback.
It is bounded by neither invariant.
The sidecar records it even when it drives net cash negative.
Refusing to record money that already left corrupts the books.
Negative net raises an alert.
It never causes a refusal.
The Ledger uses paise integers only.
It uses UTC.
One idempotency key guards each attempt.
The key is (`order_id:attempt`) with a unique constraint.
It closes read-then-act races.
`orders.create` is the one exception.
That door produces `order_id`.
It keys on `cart_id:attempt` instead.
The Pending Cart is the thing that an order is made from.
A crash between request and response is exactly where duplicate orders are born.
After a crash-mid-link the sidecar adopts through `check-status`.
It never double-creates.
Pending holds no money.
A tap converts Pending to an order.
It never auto-converts.

### Evidence

Receipt means sealed five-section proof of one order.
Receipt ID means unguessable 128-bit opener for one receipt.
`digest` means sha256 of canonical per-item bytes.

The receipt has five tight sealed sections.
The sections are bought, tapped, decided, told, and moved.
The chain is hash-chained.
The Merchant signs it with ES256.
V1 has no Merkle (ADR-0009).
It carries its JWKS snapshot.
Offline review is therefore really offline.
It opens by unguessable 128-bit receipt ID.
It needs no login.
Each order pins the Attestation hash that it used.
Later edits therefore cannot rewrite history.
Refunds version the receipt.
The original bundle stays verifiable as is.
A refund appends a new `moved` entry (`REFUND`).
It adds a fresh signature as v2.
Attestation `digest` equals sha256 of canonical per-item bytes.
The bytes are (`sku | price_minor | sorted tags | canonical options JSON`).
They use UTF-8.
They freeze before S3 and S6.
The options JSON holds the resolved values of the Catalogue Item.
An example is `{colour: black, size: M}`.
A later rename of an option axis therefore cannot rewrite what was bought.

## 5. Merchant truth + stock trait (ADR-0001, Q40)

Merchant system means store software that owns catalog and orders.
Trait means narrow HTTP contract with 9 required doors plus 1 optional door.
Availability Bucket means agent-facing stock label per variant.

The Merchant system owns Product Groups.
It owns Catalogue Items.
It owns stock counts.
It owns order rows.
Stock, price, and thresholds live on the Catalogue Item.
The variant is the level that counts.
They never live on the group.
One count for the tote lets black selling out mark red sold out.
It points `reserve` compare-and-set at the wrong row.
The sidecar reads fresh at every Gate.
It mirrors back idempotently.
The trait is narrow HTTP only with 9 required doors plus 1 optional door.
The doors are `catalog.read (1) / stock.read (2) / reserve (3) / commit (4) / release (5) / restock (6) / orders.create+read (7) / orders.set-status (8) / quote (9)` and the optional `requirements.read (10)`.
It uses HMAC plus idempotency keys.
It never shares a DB, demo included.
Door 9 is read-only and side-effect-free.
Door 10 is read-only and side-effect-free.
A Merchant without door 10 serves the fixed buyer shape.
Door 10 declares buyer details beyond address and contact.
Field types are string and enum only.
At most 20 fields.
Keys match `^[a-z][a-z0-9_]{0,63}$`.
Values travel to door 9 and door 7 when set.
Door 9 validates provided values and never refuses absence.
Requiredness is enforced by the sidecar at start-checkout.
Fields marked `pii: true` are refused until generic receipt commitments exist.
Same inputs give same bytes, any number of times (ADR-0010).
The sidecar owns the expiry clock.
It calls `orders.set-status(expired)`.
The Merchant never self-expires.
Tap and expiry serialize on `orders.set-status` with same-key-same-result.
`.yaml` counts exist only where the basic site is the Merchant system.

Stock rules:

- Stock is always `int ≥ 0`. It is never null. The DB enforces `CHECK (available >= 0)`. Missing, negative, or float stock fails catalog load loud. `0` refuses with `sold-out`. V1 never oversells. `continue-selling-when-out-of-stock` is false. Exact counts stay Merchant-and-sidecar-internal. Door 2 `stock.read` returns integers over the private network. Every agent-facing reply returns an Availability Bucket. The bucket is `in-stock`, `low-stock`, or `sold-out` per Catalogue Item. Each item cuts at its own low-stock threshold. A Product Group shows in-stock when any of its items is in stock. An exact count handed to any self-registered stranger leaks competitive intelligence. It also acts as an inventory-probing oracle. One exception exists, taken with eyes open against the smoothness law (§11). A quantity refusal that the Consumer actively waits on names the exact remaining count. Advice to try fewer without a number is not a fix. That path is rate-limited and counted like any other oracle. Reserve is atomic compare-and-set. It uses `WHERE available >= qty`. It never relies on negative-proof by test. `failed` auto-restores stock that it reduced (Woo 11.0 parity). Services sell countable slots. Restock equals Merchant edit. A refund restores only lines with the restock flag ticked. A refund with none ticked moves no stock.
- External non-agent sales win automatically. The next gate re-reads the lowered `available`. Shopify uses `available`, `committed`, `reserved`, plus webhooks. Woo uses `stock_quantity` plus hold-stock. Events are triggers only. They arrive unordered. They arrive duplicated. They carry no guarantee. The sidecar dedupes by `event_id`. It re-fetches fresh. It runs a periodic reconciler poll.

## 6. Orders — 8 canonical statuses (Q41)

Order status means lifecycle state of one order row.
Timeline means ordered history of Transcript plus stock plus status.
Invoice number means sequential tax number assigned at dispatch.

The eight statuses are `pending`, `confirmed`, `paid`, `cancelled`, `expired`, `failed`, `refunded`, and `completed`.
Core stays platform-blind.

- `pending` means agent cart with ₹0 moved and no stock held. The 24h window is Quote validity, not an inventory hold. Nothing is reserved until the tap. It is a Merchant row. It is distinct from a Pending Cart. Only sidecar-created agent orders auto-expire. Admin and manual orders never auto-expire.
- `confirmed` means Consumer initiated payment and stock is held by `RESERVE`. The Authority already landed under `passkey` or arrives with the money under `upi-pin` (ADR-0017). Payment request is out. The state is time-boxed. The sidecar releases at the expiry of the link. Default is 15 min and never longer than the Provider link lifetime. An abandoned tap must not hold stock. Woo `hold-stock` default is 60 min and a UPI collect request expires in minutes. Leaving `confirmed` open for 24h opens an inventory-denial hole to any self-registered stranger agent.
- `paid` means money captured for the order.
- `cancelled` means ended pre-money only. It covers `pending/confirmed` to `cancelled` by Consumer walk-away, decline, shop reject with reason, or the Buyer Agent own `cancel-order` over its `start-checkout` scope. That scope is bounded exactly like its order read. It covers its own orders only. It covers pre-money only. It runs `RELEASE` where a hold exists. It uses `consumer-walkaway` as the reason. It returns `cancel-not-allowed` on anything at or past `paid`. An agent that can build a basket must be able to put it down. Without that, abandonment is the only exit and it holds a Quote for 24h and held stock until the payment window lapses. That is an inventory-denial path open to any stranger. Cancel frees the hold through `RELEASE`. It kills the UPI link through `cancel`. It hides once `completed`, like Shopify.
- `expired` means ended by time with two sweeps and one status. `pending` expires at 24h with `time-limit-reached` and untouched stock. That window is Quote validity, not a stock hold. `confirmed` expires at the payment-link expiry with `payment-window-elapsed` plus a `RELEASE` that returns held stock. Re-add retries the order.
- `failed` means ended by error with two causes and different ledger effects. The two causes must not be written as one. `sold-out` is door 3 refusing, so no `RESERVE` ever existed and none is closed. Releasing a hold that was never taken is refused by the trait. `amount-mismatch` arrives after a successful `RESERVE` and auto-releases it with `RELEASE`. Both causes restore any stock that this order reduced.
- `refunded` means ended post-money only. It covers `paid/completed` to `refunded` by shop click with reason plus per-line restock yes or no. Partial amounts are allowed. The refund carries `amount_minor ≤ captured − already_refunded` and the Ledger enforces it. Full against partial is derived from `refunded_minor`. It never gets a ninth status. The canonical set therefore stays 8 and every adapter mapping holds. Fees, cancellation charges, and store credit stay phase-two.
- `completed` means Merchant-fulfilled and observed.

Dispatch is a Merchant-recorded event.
It is not a ninth status.
It is not tied to any single status.
It is available from `confirmed` onward.
It is refused on `cancelled`, `expired`, and `failed` with `dispatch-not-allowed`.
Goods that never left cannot carry a tax invoice.
A number burned on one is a gap to explain.
Recording optional `tracking_number` plus `carrier` sets `dispatched_at`.
On the first call for the order it assigns the sequential `invoice_number`.
The number is financial-year-scoped as April-March.
It is evaluated in `Asia/Kolkata`.
IST is UTC+5:30 and the boundary falls at 18:30 UTC on 31 March.
Every dispatch in the first five and a half hours of 1 April IST still carries a 31-March UTC date.
A UTC-derived bucket therefore files the first invoices of the new year into the year that just closed.
The increment is gapless and atomic.
It is distinct from the unguessable `receipt_id` that the bundle opens by.
A legal tax invoice and an IDOR-proof lookup key need opposite properties (ADR-0020).
CGST Rule 46 ties the invoice to removal of goods, not to payment.
Dispatch therefore fires at dispatch rather than at `paid`.
It can land while a COD order is still `confirmed`, before `paid` exists at all.
Cash is collected at delivery for COD (ADR-0018).
Both fields are Merchant-entered and non-PII.
They mirror through door 7 `orders.create+read`.
They surface in the Consumer polled order status like any other field.
`invoice_number` is deliberately not agent-facing.
It crosses door 7.
It renders on the Consumer receipt page and in the shipped notification.
It never lands in an agent-facing response.
A gapless per-financial-year sequence differenced across two of an agent own orders reads out Merchant dispatch volume between them.
That is the same oracle that §5 closes by bucketing exact stock counts.
Dispatch and its tracking fields fire their own shipped notification off the Merchant own comms channel.
SPEC §10 already owns that job.
This only adds an event to the set that it sends.
It fires independently of the later `completed` transition.
`completed` stays the Merchant own closure signal.
COD runs the same eight (ADR-0018).
`confirmed` means intent is verified and door 3 holds stock with no money held at all.
`paid` is the cash collected at delivery.
An RTO is `cancelled` with an `rto` reason and a restock.
That is already what `cancelled` means: pre-money with stock returned.
`expires_at` takes a third deadline there, the delivery window.
Refund needs no return.
The per-line restock checkboxes decide stock return.
Timeline and receipt show `Refunded ₹X of ₹Y`.
Partial is therefore legible without a new status.
Refund requests share one queue.
It is `refund-requested` from agent through sidecar, or recorded manually for direct buyers.
The same button settles both.
Status flip alone never moves money (Woo parity).
Every status change lands in the order Timeline.
The Timeline holds Transcript plus `stock_moves` plus status history.
Adapters map 1:1 to Shopify (`pending/authorized/paid/voided/expired/refunded` plus `unfulfilled/fulfilled`) and Woo (`pending/on-hold/processing/completed/cancelled/failed/refunded` plus hold-stock timeout).
One honest seam remains.
A partial refund maps to Shopify `partially_refunded` and to Woo plain `refunded`.
Woo has no partial status.
The adapter therefore carries `refunded_minor` alongside the status.
It never pretends that the target platform can express it.
Destination, Contact Point, and the resolved option values of the line ride inside the order row in v1.
Destination and Contact Point act as the only plaintext copy (ADR-0011).
The options act as a denormalized copy for history that matches the pinned Attestation.

## 7. Authority: passkeys + scoped agent keys (ADR-0004, Q28/Q53)

Passkey means phishing-resistant credential bound to one Merchant domain.
Agent Profile means published stranger identity with name and JWKS.
Scope means named permission for one agent action.

Consumer rules:

- Consumer gives a fresh Authority per spend. It is one member of the closed set `upi-pin / passkey / confirmed-intent / mandate` (ADR-0017). The sidecar accepts it only where the Merchant enabled that kind. It records what the kind bound. In India the default is `upi-pin`. The payer authenticates in their own PSP app against a named payee and an exact amount. The amount is therefore bound by the payer own bank. The basket is bound by reference through the Quote that the approve page rendered. `mandate` is defined and refused in v1. The `passkey` member is the strongest binding and the original ceremony. It uses a fresh passkey tap per spend. It binds exact `cart_hash` plus amount plus expiry with UV on. Enrollment is per Merchant domain (ADR-0008). Roaming five stores costs five enrollments. Platform keys work on laptop and mobile. QR covers cross-device. Enrollment equals login. There are no passwords, OTPs, or magic links to money. Enrollment is never a separate visit. A first-time Consumer enrolls inside the first approve ceremony. It is one page and one biometric prompt that enrolls and taps. Roaming five stores therefore costs five taps, not five signup flows. Mechanically it is a single WebAuthn `create()`. Its challenge is the `cart_hash` plus amount plus expiry binding with UV required. The `create()` is the authorization only where the response carries an attestation statement that actually signs over `clientDataHash`. Examples are `packed` self-attestation or a platform format doing the same. Under `none`, which is what a platform authenticator returns by default, `attStmt` is empty and nothing in the response is signed. Resting a spend on `clientDataJSON` alone therefore authorizes against an unsigned string that the client supplied. The sidecar therefore requests attestation. It reviews the signature. Where it comes back `none` it falls back to an immediate `get()` over the same challenge. That is two prompts, named and counted, rather than a binding that is asserted and not proven. The Transcript records which ceremony carried it. `ceremony = enrollment` marks enrollment and `assertion` marks assertion. It is distinct from the Authority kind itself. One prompt is therefore an assertion that the golden replays can review rather than a hope. Sign-count is tracked. It warns on reset instead of hard-locking. Virtual tap in demo runs identical checks with test keys.

Buyer Agent rules:

- Two admission routes give one authority. An allowlisted agent uses Merchant-issued OAuth client-credentials. A stranger self-registers by publishing an Agent Profile at a well-known URL. The profile holds name, contact, and ES256 JWKS. The stranger signs every request against it. It is issued a short token on the spot (ADR-0012). The design uses standards rather than invention. Request signatures are RFC 9421 HTTP Message Signatures. They cover method, target, `Content-Digest` (RFC 9530), `created`, and `expires`. They use a nonce and a short acceptance window so a captured request cannot be replayed. `agent_id` for a stranger is the RFC 7638 JWK thumbprint. Fetching an attacker-chosen profile URL is an SSRF sink, so the fetcher is hardened by rule. It allows HTTPS only. It allows public IP ranges only. It refuses loopback, RFC 1918, link-local, and 169.254.169.254. It resolves then pins against DNS rebinding. It allows no cross-host redirects. It applies hard size and timeout caps. It has its own rate limit. A registration attempt is a stranger making the sidecar issue an outbound request. One exception exists and it is deliberately narrow. A dev-mode named-host allowlist (`OPENSTORE_DEV_PROFILE_HOSTS`) holds host and port entries. It never holds a CIDR and never allows blanket private access. It lets the compose demo admit its own chat at `buyer-chat:3001`. Without it nobody can run the self-registration story. It is loud and fenced. The cloud metadata address stays refused unconditionally even in dev. Resolve-then-pin still applies. Redirect, size, and timeout caps still apply. Every fetch that used the exception logs a warning. It is flagged in health output and the `/agentic` banner. The sidecar refuses to boot when the allowlist is non-empty alongside live provider keys. The same refusal already guards live keys in demo mode. A bypass that is silent, broad, or bootable in production is how bypasses reach production. Both routes get the same four scopes. The scopes are (`search / build-basket / start-checkout / confirm`). They use pinned ES256 with short expiry. No tier unlocks money. `confirm` without a fresh accepted Authority is rejected by the Gate regardless of admission route. Reputation buys throughput only. Self-registered agents sit in the low rate-limit tier. Allowlisted agents sit in the high tier. Blocklisted profiles are refused registration. `agent_id` is the profile key thumbprint for strangers and the client ID for allowlisted agents. Both are revocable. Both land on the order. Name badge only and never buys authority.
- Permissions: `Always allow` covers reads and drafts only. Any cart-with-spend, checkout, or place-order needs a fresh Allow-once modal plus a fresh tap (ADR-0008). The Gate requires both identities valid. Either side revokes independently.
- Tap delivery: the agent shows `Tap to approve ₹X`. It links to same-domain `/agentic/approve?t=<one-time-token, 5-min, single-use>`. It offers an optional `Have a code?` field for `private` Discount Codes. That field re-calls `quote`, re-renders the new total, and rebinds before anything is signed. The code never transits the agent (ADR-0015). The Authority then covers the post-application total.
Under `upi-pin` (the default) it is a UPI intent or collect.
The Consumer approves it in their own PSP app.
Under `passkey` it is a biometric over the post-application `cart_hash`. The page is the display of record either way. Any change after render invalidates the token. Auto-return uses a resume URL. It carries an unguessable, single-use, session-bound resume token. It resolves to `order_id` plus `chat_thread_id` server-side. It never exposes those two as bare URL parameters. Bare parameters let anyone holding the link resume someone else checkout. Cart plus chat state stay intact. The sidecar restores the cart snapshot. The chat restores its thread. The approve page behind `/agentic/approve` is display of record. If the total moves after render, the Consumer re-taps.

## 8. Who signs what

Signing key means sidecar-generated key that stamps Merchant statements.
Provider truth means dashboard plus webhook fact that money moved.

- Merchant signs through sidecar-held keys. It signs per-item attestation, policy, the Quote (breakdown plus total plus ETA) plus `cart_hash`, and evidence root. It auto-stamps with no human per sale. Keys are generated in the sidecar. They never leave it. They rotate additively by `kid`. They are recoverable only from the Merchant own encrypted enrollment export (ADR-0014).
- Consumer gives one fresh Authority per bag, of a kind that the Merchant enabled. It carries its Binding (ADR-0017). It is a UPI PIN entered in the payer own app by default. It is a per-domain passkey enrollment plus tap where the stronger cart binding is wanted.
- Agent signs nothing authoritative.
- Provider gives HMAC webhooks plus dashboard truth for the money-moved fact only. The sidecar reviews, dedupes, and reconciles that fact. Amount or currency mismatch against the order leads to `failed` plus `RELEASE` plus `amount-mismatch`. It never auto-captures. V1 ships fake plus Razorpay behind `make-link / check-status / cancel / refund`. Justpay and Airpay land later in the same shape. Each adapter declares its supported method set at boot. The Merchant enables a subset in `/agentic`. Anything outside the enabled set refuses with `method-not-supported` and names what is enabled (ADR-0013). Razorpay ships declaring UPI, with cards and netbanking enableable without touching the money core. Agent-held delegated payment credentials are refused by design (ADR-0008).
- Refund and shop-reject are both clicked in demo-admin. Both call the sidecar over HMAC rather than writing the Merchant row directly. The sidecar holds the `RESERVE`, so a local write strands a Ledger hold. Routing through it serialises shop-reject against a Consumer tap and the expiry sweep on one `orders.set-status` key. Refund dialog asks reason plus amount plus per-line restock-to-sale. The admin server calls sidecar refund over HMAC with an amount and per-line restock flags. The sidecar runs Ledger `REFUND` plus provider `refund` plus conditional `restock` plus `orders.set-status`. A Provider-reported dispute or reversal posts a `REVERSAL` Ledger entry and a Timeline event. Money moved by someone else is still money moved. The escrow-zero invariant must close over it.
- List publisher signs list file only.
- Demo keys are test-marked. The sidecar refuses live keys at boot.

## 9. Protocols + proof (Q47/Q52)

Translator means thin mapping from UCP, ACP, or AP2 onto the core.
Golden replay means end-to-end test that pins core bytes.
Badge means UI label that names protocol deviations.

- MCP is live in demo chat. UCP, ACP, and AP2 are thin translators over the same core. Each has one golden end-to-end replay. The replay runs search, allow, cart, tap, fake-UPI, receipt, and review. Core Transcript is pinned byte-identical in CI. Envelopes differ and core decision bytes do not.
- Completion posture is deliberate and in-spec rather than deviant. Foreign direct-checkout-inside-AI (UCP-native and ACP-instant) maps to the same-domain approve handoff. That is what UCP own buyer escalation and its embedded binding already describe. Every spend still ends in a fresh Authority at `/agentic/approve`. What the build declines is the trust tier that completes without the buyer, not conformance. Completion without any handoff is phase-two through mandates (ADR-0017).
- Payment deviation: translators map any instrument outside the Merchant enabled method set to `method-not-supported` and name what is enabled (ADR-0013). A card attempt therefore refuses on a UPI-only store and succeeds on one that enabled cards. An agent-held delegated payment credential is refused on purpose rather than left unimplemented.
- Demo header toggle `[MCP|UCP|ACP|AP2]` replays the same flow through the selected envelope plus conformance badge. The badge names its deviations inline. It names capability supported and redirect-only completion. It names which payment instruments this Merchant enabled. It therefore never reads as unqualified conformance. S6 golden replays assert that the deviation text is present. UCP totals breakdown (`subtotal / items_discount / fulfillment / tax / total`) maps 1:1 onto the Quote. Translators therefore carry no pricing special cases. No second money path exists anywhere.

## 10. Mounting + consoles (Q43/Q46/Q51)

Mounting means proxy rules that route store and sidecar under one origin.
Sidecar console means `/agentic` UI for keys and policy.
Exposure means which policies are public to agents.
Attribution means agent-sourced orders and revenue split by agent.

- Compose runs `store:3000` plus `sidecar:8000` plus reverse proxy. The proxy owns TLS. Dev ships a reference `Caddyfile`. Routes are `/ → store` and `/.well-known/agent-commerce.json + /.well-known/ucp.json + /.well-known/jwks.json + /agent + /agentic → sidecar`. One origin keeps passkeys happy. Containers and deploys stay separate. The public demo runs the same split on Fly.io as three apps. The apps are (`edge`, `store`, `sidecar`) over 6PN rather than one app process groups. Fly scopes secrets per app, not per process (ADR-0019). Mount paths and origin contract are unchanged.
- Demo-admin is shop ops only. It covers catalog, stock, and orders.
- Sidecar console at `/agentic` covers keys (enroll, rotate, revoke, export), policy CRUD, provider plus enabled methods, exposure, receipts, agent allowlist plus blocklist, and attribution. Exposure controls which policies are public to agents. Attribution reports agent-sourced orders and revenue split by `agent_id` over a date range. It reads from orders that the sidecar already tags. That number tells a Merchant whether any of this is working.
- `order.changed` is consumed by the sidecar only. The chat never gets pushed PII. It polls order status through its closed `order status` action.
- Consumer-facing and Merchant-facing notifications go out from the Merchant system off its own status changes. The set is order confirmation with receipt link, shipped notice carrying tracking, carrier and invoice number, expiry notice, refund notice, and new-order alert. The Merchant already owns customer comms and the Contact Point plaintext. The sidecar sends no email and no SMS.

## 11. Smoothness law (Q53)

Smoothness law means the build engineers convenience and tests it.
`approve_url` means single-use link to the approve page.

- Convenience is sidecar-engineered, never agent-hoped. The sidecar signs verbatim totals. It orders results. It issues one `approve_url` (5-min single-use). It shows an expiry countdown. It issues resume links. A resume link is an unguessable, single-use, session-bound token that resolves server-side. It restores exact state on both sides. Errors name the exact fix. Golden replays assert one Allow-once plus one tap per spend with zero re-search and zero re-login and exact resumed state. Inconvenient equals red build.

## 12. Guardrails (law from commit one)

Closed set means fixed vocabulary enforced by build break.
Registry means `Closed-Sets.md` generated from code enums.
Reason code means closed refusal label in evidence.

- One authoritative registry governs closed sets. It is `Closed-Sets.md`, generated from the code enum and diffed in CI. It covers reason codes, scopes, statuses, routes, tool names, and Ledger entry kinds. It is enforced both directions by build break. A code named in prose but missing from the registry is a red build. Prose is where closed sets quietly stop being closed.
- The build fails loud. It uses no coercion, no defaults, and no silent fallbacks. Every denial carries a closed reason code into signed evidence.
- No LLM output lands in the money path. Agents are proposal-only and hold no spending authority. A self-registered agent signs its own requests (ADR-0012) but carries no payment credential. `confirm` without a fresh accepted Authority is refused regardless (ADR-0017).
- Money uses integers in paise. Time uses UTC. The server recomputes totals, hashes, discounts, and caps. It never trusts agent supply.
- Agent-facing order reads and refund requests resolve only against orders carrying that `agent_id`. An order that belongs to someone else is indistinguishable from one that does not exist. Admission is open by design (ADR-0012), so every stranger holds these scopes. An unscoped order read is an enumeration oracle over other Consumer order state.
- Each tool and scope gets least privilege. Tool and manifest definitions are signed and pinned. Idempotency (`order_id:attempt`) guards every mutation. Atomic compare-and-set guards stock and never lets it go negative. Webhooks plus events are replay-safe. They use `event_id` dedupe, stay out-of-order safe, and use a reconciler poll. Every query is scoped to a single Merchant.
- Rate limits apply per agent tier and per IP on every `/agent/*` route. A separate throttle guards tap-token issuance, approve attempts, and Discount Code attempts. Every wrong code refuses as the same `code-invalid` with no message or timing tell. Door 9 otherwise answers if a string is a code all day. Policy caps bound an order and limits bound an attacker.
- PII is committed to by hash in evidence. It is stored in plaintext only in the Merchant row (ADR-0011).
- Every money-path decision emits a structured log line. The line holds order, agent, consumer, reason code, and duration. It sits alongside the Transcript. Transcripts are forensic and logs are operational.

## 13. Explicitly out (phase two, same core untouched)

Phase two means deferred work that leaves the money core unchanged.
Unranked Index means later phonebook of Listings with no ranking.

The following stay out in v1:

- Campaigns, rules, bots, and stalled-loops.
- Hosted mall, ranked search, and paid placement. An unranked Index of Listings is phase two rather than never (ADR-0022, `Plan-Distribution.md` D4).
- Multi-Merchant tenancy.
- Multi-location.
- Cancellation and restocking fees.
- Store credit.
- Bulk cancel.
- Auto-fulfilment.
- Shipping labels.
- Subscriptions.
- Multi-currency and international duties.
- Consumer accounts and a self-serve returns portal.
- Fraud scoring.
- Abandoned-cart recovery.
- Merkle batch-anchoring.
- OMS write paths beyond the 9-door trait.
- Manual prices and a hand-entered code table only. No rule engine anywhere.
- Preorder and backorder. This is not a cut so much as a consequence already made elsewhere. Stock is `int ≥ 0` with `CHECK (available >= 0)` and v1 never oversells (§5). Selling against future stock needs a second inventory model, not a flag. The same reasoning covers auto-fulfilment.
- Staff roles inside one Merchant own admin. Shop-ops-only against refund and pricing-capable is a real and common ask past solo-founder scale. It is explicitly deferred rather than assumed away. `demo-admin` stays single-operator (`Plan-Merchant-Site.md` M2) until that gets its own grill.

One exclusion needs a reason written down, because it looks like an omission and is not.
COD was the other apparent omission, and it is now in through ADR-0018.
The Ledger holds stock and money separately, so an order that holds no money is expressible.
The cash path writes a `CAPTURE` with no preceding `RESERVE`.
No money is held across a COD delivery.
UPI has no primitive for it.
Reserve Pay is a prepaid reserve rather than the authorize-then-capture it was taken for.
OpenStore holding the funds itself is refused by ADR-0021 and ADR-0023.
ADR-0018 keeps that gap open with named candidates rather than closing it.

- Delegated agent-held payment credentials (Shop Pay tokens, ACP shared payment tokens) are refused on purpose. They are exactly the authority removed from agents (ADR-0008, ADR-0013). Their absence is the product, not a gap. It costs native in-agent completion on the gated surfaces and the build pays it (ADR-0016). The Indian regime favors the tap above the small-value delegation band. The band involves additional-factor requirements on card-not-present spend.
It involves tokenization rules for stored credentials.
It involves a UPI PIN entered by the payer in the payer own app. NPCI unlaunched Unified Agent Protocol is building sanctioned delegation below it. That delegation is bank-side, capped, and revocable. It reaches the build as a Provider method (ADR-0013, ADR-0016) rather than as an agent-held credential. Where the line falls is a matter for a legal opinion, not an argument this document gets to win.

Reach is deliberately absent from v1 and is not the same kind of exclusion.
A conformant sidecar that no agent can see is not a product.
The permissionless doors are therefore specified as a phase-two track in [`Plan-Distribution.md`](./Plan-Distribution.md).
The doors are four permissionless tracks.
They are an ingestible Product Feed and JSON-LD.
They are an MCP endpoint any client can add.
They are a second trait implementation distributed where merchants already install things.
They are an unranked, mirrorable Index of Listings (ADR-0022).
The track is gated on install (`Plan-Index.md` step 7).
It changes nothing in the money core.

## 14. Operations (what a Merchant lives with after install)

Install means one command that writes compose files and keys.
Health means `/healthz` plus `/readyz` on every service.
Retention window means Merchant-set period for plaintext PII.

- Install: one command. Run `openstore up <domain>`. It writes the compose file, the reference `Caddyfile`, and a fresh `.env` from the template. It generates the signing key. It prints the encrypted key export to save. It prints the `/agentic` first-run URL. A Merchant who can point DNS can run it. If anything needs hand-edited YAML before first boot, that is a bug in this gate.
- Keys: enroll, rotate, revoke, and export in `/agentic`, per ADR-0014. Rotation is additive and never re-signs history. Revocation invalidates the future, not the past. First-run refuses to continue until the export is acknowledged as saved.
- Health plus observability: `/healthz` plus `/readyz` on every service. Structured money-path logs hold order, agent, consumer, reason code, and duration, never secrets or PII plaintext. Counters track gate refusals by reason code, authority outcomes by kind, provider webhook lag, reconciler drift, and holds still open past their deadline. The sidecar owns both expiry clocks and the Merchant does not self-expire (§5). A wedged or stopped sidecar therefore holds stock indefinitely. The only defence is that somebody can see it. `/agentic` lists overdue holds. The documented release path goes through the sidecar, never a Merchant-side write. A Merchant at 2am needs what is failing and why. The Transcript alone does not answer that.
- Abuse: per-tier and per-IP rate limits guard `/agent/*`. A separate throttle guards tap-token issuance and approve attempts. Merchant allowlist and blocklist cover Agent Profiles (ADR-0012).
- Disputes: Provider-reported disputes and reversals post a `REVERSAL` Ledger entry plus a Timeline event and never silently vanish, whatever the rail produced them. On UPI that is an adjudicated complaint, a post-settlement adjustment, or a bank correction rather than an issuer chargeback. The adapter carries the dispute turnaround deadline because a Merchant needs to know when evidence is owed. The sealed bundle is the Merchant evidence pack and can be handed over without PII (ADR-0011).
- Data retention: Destination and Contact Point plaintext live only in the Merchant order row with a Merchant-set retention window and an erasure action. Evidence commits by hash, so erasure never breaks review (ADR-0011). DPDP obligations sit with the Merchant, who is the data fiduciary. The job of the sidecar is to not make compliance impossible.
