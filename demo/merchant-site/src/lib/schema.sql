-- SpoiledDuckie: Merchant truth.
--
-- The Merchant owns Product Groups, Catalogue Items, stock and order rows
-- (ADR-0001). The sidecar owns the Ledger, Transcripts and evidence, in its own
-- database, with no cross-grant. Neither reaches into the other's tables — the
-- nine doors are the whole of the contract.
--
-- Cut 5 is in force: `site_carts` is NOT here. It existed only to hold stock
-- for the direct cart, and the direct cart is not built.

CREATE TABLE IF NOT EXISTS product_groups (
  id            TEXT PRIMARY KEY,
  slug          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  description   TEXT NOT NULL DEFAULT '',
  media         JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- The axes and their allowed values, e.g. {"colour": ["black","red"]}.
  -- Presentation only: a group is never sellable and never a cart line.
  option_axes   JSONB NOT NULL DEFAULT '{}'::jsonb,
  tags          JSONB NOT NULL DEFAULT '[]'::jsonb,
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived'))
);

CREATE TABLE IF NOT EXISTS catalogue_items (
  sku                 TEXT PRIMARY KEY,
  group_id            TEXT NOT NULL REFERENCES product_groups(id),
  -- This item's *resolved* values on the group's axes. Unique per group, so one
  -- combination cannot exist twice.
  options             JSONB NOT NULL DEFAULT '{}'::jsonb,
  name                TEXT NOT NULL,
  price_minor         BIGINT NOT NULL CHECK (price_minor >= 0),
  tags                JSONB NOT NULL DEFAULT '[]'::jsonb,
  media               JSONB NOT NULL DEFAULT '[]'::jsonb,
  status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  low_stock_threshold INTEGER NOT NULL DEFAULT 3 CHECK (low_stock_threshold >= 0),
  hsn_sac             TEXT NOT NULL,
  gst_rate_bp         INTEGER NOT NULL CHECK (gst_rate_bp BETWEEN 0 AND 10000),
  UNIQUE (group_id, options)
);

-- One row per Catalogue Item, never per group: one count for "the tote" would
-- let black selling out mark red sold out, and would point reserve's
-- compare-and-set at the wrong row.
CREATE TABLE IF NOT EXISTS stock (
  sku       TEXT PRIMARY KEY REFERENCES catalogue_items(sku),
  available INTEGER NOT NULL CHECK (available >= 0)
);

CREATE TABLE IF NOT EXISTS stock_moves (
  id        BIGSERIAL PRIMARY KEY,
  at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  sku       TEXT NOT NULL REFERENCES catalogue_items(sku),
  delta     INTEGER NOT NULL,
  channel   TEXT NOT NULL CHECK (channel IN (
              'site-direct','agent-reserve','agent-commit','agent-release',
              'refund','restock','admin-adjust','rto')),
  actor     TEXT NOT NULL DEFAULT '',
  reason    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shipping_zones (
  id         TEXT PRIMARY KEY,
  label      TEXT NOT NULL,
  states     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- empty = catch-all
  cost_minor BIGINT NOT NULL CHECK (cost_minor >= 0),
  eta_days   INTEGER NOT NULL CHECK (eta_days >= 0)  -- a day count, never a date
);

CREATE TABLE IF NOT EXISTS unserviceable_postal_ranges (
  prefix TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS discount_codes (
  code         TEXT PRIMARY KEY,
  label        TEXT NOT NULL,
  amount_minor BIGINT,
  percent_bp   INTEGER,
  active_from  TIMESTAMPTZ,
  active_until TIMESTAMPTZ,
  visibility   TEXT NOT NULL CHECK (visibility IN ('public','private')),
  max_uses     INTEGER NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
  uses_count   INTEGER NOT NULL DEFAULT 0 CHECK (uses_count >= 0),
  CHECK (amount_minor IS NOT NULL OR percent_bp IS NOT NULL)
);

-- The row that makes single-use codes race-safe inside reserve: unique on the
-- code while held, so two carts cannot both spend it.
CREATE TABLE IF NOT EXISTS code_reservations (
  code     TEXT NOT NULL REFERENCES discount_codes(code),
  order_id TEXT NOT NULL,
  state    TEXT NOT NULL CHECK (state IN ('held','consumed','released')),
  PRIMARY KEY (code, order_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_code_held
  ON code_reservations (code) WHERE state = 'held';

CREATE TABLE IF NOT EXISTS merchant_tax_identity (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  legal_name      TEXT NOT NULL,
  gstin           TEXT NOT NULL,
  registered_state TEXT NOT NULL,
  tax_inclusive   BOOLEAN NOT NULL DEFAULT TRUE,
  -- The Merchant-set first segment of ADR-0020's {prefix}/{FY}/{sequence}.
  invoice_prefix  TEXT NOT NULL DEFAULT 'SD'
);

CREATE TABLE IF NOT EXISTS orders (
  order_id        TEXT PRIMARY KEY,
  cart_id         TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN (
                    'pending','confirmed','paid','cancelled',
                    'expired','failed','refunded','completed')),
  lines           JSONB NOT NULL,
  quote           JSONB NOT NULL,
  total_minor     BIGINT NOT NULL CHECK (total_minor >= 0),
  refunded_minor  BIGINT NOT NULL DEFAULT 0 CHECK (refunded_minor >= 0),

  -- The ONLY plaintext copy of these three (ADR-0011). Retention window and an
  -- erasure action apply; erasure is refused while the order is live.
  destination     JSONB,
  contact         JSONB,
  -- Door-10 buyer details (delivery slot, instructions). Non-PII only until
  -- generic receipt commitments land; never part of tax/address semantics.
  details         JSONB NOT NULL DEFAULT '{}',
  payer_handle    TEXT,
  erased_at       TIMESTAMPTZ,

  agent_id        TEXT NOT NULL DEFAULT '',
  consumer_id     TEXT NOT NULL DEFAULT '',
  cart_hash       TEXT NOT NULL DEFAULT '',
  quote_hash      TEXT NOT NULL DEFAULT '',
  attestation_hash TEXT NOT NULL DEFAULT '',
  -- 128-bit, generated here, returned by orders.create and in NO other response
  -- ever. Dies with the row, and the evidence commitments become unopenable.
  order_salt      TEXT NOT NULL,

  tracking_number TEXT,
  carrier         TEXT,
  dispatched_at   TIMESTAMPTZ,
  invoice_number  TEXT UNIQUE,

  -- One column, three deadlines: the pending Quote validity, the prepaid
  -- payment window, and the COD delivery window. Always "the next deadline the
  -- sidecar will act on".
  expires_at      TIMESTAMPTZ,

  authority       JSONB,   -- kind + mechanism + Binding
  payment_method  TEXT NOT NULL DEFAULT 'upi',
  collected_at    TIMESTAMPTZ,   -- COD cash collection
  cancel_reason   TEXT,
  receipt_id      TEXT UNIQUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
  id            BIGSERIAL PRIMARY KEY,
  order_id      TEXT NOT NULL REFERENCES orders(order_id),
  amount_minor  BIGINT NOT NULL CHECK (amount_minor > 0),
  reason        TEXT NOT NULL DEFAULT '',
  restock_lines JSONB NOT NULL DEFAULT '[]'::jsonb,
  provider_ref  TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Gapless, per financial year, assigned by atomic increment on first dispatch
-- only. The FY is the April–March year of dispatched_at in Asia/Kolkata — never
-- UTC, or the new year's first invoices file into the year that just closed.
CREATE TABLE IF NOT EXISTS invoice_sequences (
  financial_year TEXT PRIMARY KEY,
  next_number    INTEGER NOT NULL DEFAULT 1 CHECK (next_number >= 1)
);

CREATE TABLE IF NOT EXISTS notifications (
  id        BIGSERIAL PRIMARY KEY,
  at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  order_id  TEXT,
  kind      TEXT NOT NULL,
  channel   TEXT NOT NULL DEFAULT 'log',
  body      JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
  email         TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
  key         TEXT PRIMARY KEY,
  door        TEXT NOT NULL,
  status      INTEGER NOT NULL,
  body        JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Door-10 details column for databases created before it existed.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}';
