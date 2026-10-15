/**
 * The trait doors, implemented natively over HTTP on the private network.
 *
 * Nine doors are required; `requirements.read` (door 10) is optional. This
 * shop implements it: two non-PII extras. Anything `pii: true` stays out
 * until the sidecar can commit to dynamic PII in receipts.
 *
 * This is the second implementation of the trait contract (§6.1). The sidecar's
 * conformance suite runs against it unmodified — that is the only real proof
 * the trait is a contract and not a description of one codebase.
 *
 * Every refusal carries a closed reason code. The HTTP status is derived from
 * the code by one table, never chosen at the call site: two implementers
 * choosing statuses independently is how one client ends up retrying what the
 * other treats as fatal.
 */
import type { Sql } from 'postgres';
import { randomBytes } from 'node:crypto';
import { buildQuote, PricingError, type Line, type PricingItem, type Quote } from '../pricing.ts';

export type Destination = {
	line1: string;
	line2?: string;
	city: string;
	state: string;
	postal_code: string;
	country: string;
};

/** Mirrors `core/codes.py`. A code that exists here and not there is a bug. */
export const HTTP_STATUS: Record<string, number> = {
	'sold-out': 409,
	'price-changed': 409,
	'code-invalid': 409,
	'destination-unserviceable': 409,
	'no-hold': 409,
	'dispatch-not-allowed': 409,
	'cancel-not-allowed': 409,
	'variant-required': 400,
	'addon-without-parent': 400,
	'quote-inconsistent': 400,
	'signature-invalid': 401,
	'not-found': 404,
	'rate-limited': 429
};

export class DoorError extends Error {
	constructor(
		readonly code: string,
		message: string,
		readonly fields: Record<string, unknown> = {}
	) {
		super(message);
	}
	get status(): number {
		const status = HTTP_STATUS[this.code];
		if (!status) throw new Error(`reason code ${this.code} has no HTTP status mapping`);
		return status;
	}
	toPayload() {
		return {
			error: {
				code: this.code,
				detail: this.message,
				...(Object.keys(this.fields).length ? { fields: this.fields } : {})
			}
		};
	}
}

async function resolveItem(sql: Sql, sku: string): Promise<PricingItem & { threshold: number }> {
	const groups = await sql<{ id: string; option_axes: unknown }[]>`
		SELECT id, option_axes FROM product_groups WHERE id = ${sku}`;
	if (groups.length) {
		// A group id anywhere a SKU belongs refuses rather than having a size
		// guessed for it — a guess wearing a helpful expression.
		throw new DoorError(
			'variant-required',
			`${sku} is a Product Group; choose one of its Catalogue Items`,
			{ group_id: sku, axes: groups[0]!.option_axes }
		);
	}
	const rows = await sql<
		{ sku: string; price_minor: bigint; hsn_sac: string; gst_rate_bp: number; tags: string[]; low_stock_threshold: number }[]
	>`SELECT sku, price_minor, hsn_sac, gst_rate_bp, tags, low_stock_threshold
	    FROM catalogue_items WHERE sku = ${sku}`;
	const row = rows[0];
	if (!row) throw new DoorError('not-found', `no Catalogue Item ${sku}`, { sku });
	return {
		sku: row.sku,
		price_minor: Number(row.price_minor),
		hsn_sac: row.hsn_sac,
		gst_rate_bp: row.gst_rate_bp,
		tags: row.tags,
		threshold: row.low_stock_threshold
	};
}

/** Door 1. */
export async function catalogRead(sql: Sql) {
	const groups = await sql`SELECT id, slug, name, description, media, option_axes, tags, status
	                           FROM product_groups ORDER BY id`;
	const items = await sql`SELECT sku, group_id, options, name, price_minor, tags, media, status,
	                               low_stock_threshold, hsn_sac, gst_rate_bp
	                          FROM catalogue_items ORDER BY sku`;
	return {
		groups: groups.map((g) => ({ ...g })),
		items: items.map((i) => ({ ...i, price_minor: Number(i.price_minor) }))
	};
}

/** Door 2 — **exact integers, private network only**. */
export async function stockRead(sql: Sql, skus: string[]) {
	const stock: Record<string, number> = {};
	for (const sku of skus) {
		await resolveItem(sql, sku);
		const rows = await sql<{ available: number }[]>`SELECT available FROM stock WHERE sku = ${sku}`;
		const row = rows[0];
		// Missing or negative fails loud: stock is int >= 0, never null, and a
		// silent 0 here would read as "sold out" for an item nobody has counted.
		if (!row) throw new DoorError('not-found', `no stock row for ${sku}`, { sku });
		stock[sku] = row.available;
	}
	return { stock };
}

/**
 * Door 3 — atomic compare-and-set, all or nothing.
 *
 * `WHERE available >= qty` inside one transaction is the whole mechanism. It is
 * checked here under real Postgres isolation, which is a different claim from
 * the conformance fake's single-threaded check and is why B2 repeats the
 * concurrency test.
 */
export async function reserve(
	sql: Sql,
	{ order_id, lines, discount_code }: { order_id: string; lines: Line[]; discount_code?: string }
) {
	const wanted = new Map<string, number>();
	for (const line of lines) {
		const item = await resolveItem(sql, line.sku);
		wanted.set(item.sku, (wanted.get(item.sku) ?? 0) + line.qty);
	}

	await sql.begin(async (tx) => {
		for (const [sku, qty] of [...wanted].sort()) {
			const updated = await tx`
				UPDATE stock SET available = available - ${qty}
				 WHERE sku = ${sku} AND available >= ${qty}
				RETURNING available`;
			if (!updated.length) {
				const rows = await tx<{ available: number }[]>`SELECT available FROM stock WHERE sku = ${sku}`;
				const available = rows[0]?.available ?? 0;
				// The one place a count is named to an agent, because "try fewer"
				// without a number is not a fix (SPEC §5). Rate-limited upstream.
				throw new DoorError(
					'sold-out',
					available ? `Only ${available} left; try ${available} or fewer.` : 'Sold out.',
					{ sku, requested: qty, available }
				);
			}
			await tx`INSERT INTO stock_moves (sku, delta, channel, actor, reason)
			         VALUES (${sku}, ${-qty}, 'agent-reserve', 'sidecar', ${order_id})`;
		}

		if (discount_code) {
			const codes = await tx<{ code: string; max_uses: number; uses_count: number }[]>`
				SELECT code, max_uses, uses_count FROM discount_codes WHERE code = ${discount_code}`;
			const code = codes[0];
			if (!code || code.uses_count >= code.max_uses) {
				throw new DoorError('code-invalid', 'That code is not valid.');
			}
			// The unique-where-held index is what makes this race-safe: two carts
			// cannot both hold the same single-use code.
			try {
				await tx`INSERT INTO code_reservations (code, order_id, state)
				         VALUES (${discount_code}, ${order_id}, 'held')`;
			} catch {
				throw new DoorError('code-invalid', 'That code is not valid.');
			}
			await tx`UPDATE discount_codes SET uses_count = uses_count + 1 WHERE code = ${discount_code}`;
		}
	});

	return { reserved: true };
}

/** Door 4. */
export async function commit(sql: Sql, order_id: string) {
	const held = await sql`SELECT 1 FROM stock_moves WHERE reason = ${order_id} AND channel = 'agent-reserve' LIMIT 1`;
	if (!held.length) throw new DoorError('no-hold', `no hold to commit for ${order_id}`);
	await sql`UPDATE code_reservations SET state = 'consumed' WHERE order_id = ${order_id} AND state = 'held'`;
	return { committed: true };
}

/**
 * Door 5 — releasing a hold that was never taken refuses `no-hold`.
 *
 * A `sold-out` failure took no hold, and accepting a release for it would make
 * the sidecar's escrow-zero invariant close on an entry that balances nothing.
 */
export async function release(sql: Sql, order_id: string) {
	const moves = await sql<{ sku: string; delta: number }[]>`
		SELECT sku, delta FROM stock_moves
		 WHERE reason = ${order_id} AND channel = 'agent-reserve'`;
	if (!moves.length) throw new DoorError('no-hold', `no hold to release for ${order_id}`);

	const already = await sql`SELECT 1 FROM stock_moves WHERE reason = ${order_id} AND channel = 'agent-release' LIMIT 1`;
	if (already.length) return { released: true };

	await sql.begin(async (tx) => {
		for (const move of moves) {
			await tx`UPDATE stock SET available = available + ${-move.delta} WHERE sku = ${move.sku}`;
			await tx`INSERT INTO stock_moves (sku, delta, channel, actor, reason)
			         VALUES (${move.sku}, ${-move.delta}, 'agent-release', 'sidecar', ${order_id})`;
		}
		const releasedCodes = await tx<{ code: string }[]>`
			UPDATE code_reservations SET state = 'released'
			 WHERE order_id = ${order_id} AND state = 'held' RETURNING code`;
		for (const { code } of releasedCodes) {
			await tx`UPDATE discount_codes SET uses_count = GREATEST(uses_count - 1, 0) WHERE code = ${code}`;
		}
	});
	return { released: true };
}

/** Door 6. */
export async function restock(sql: Sql, order_id: string, lines: Line[]) {
	await sql.begin(async (tx) => {
		for (const line of lines) {
			await resolveItem(sql, line.sku);
			await tx`UPDATE stock SET available = available + ${line.qty} WHERE sku = ${line.sku}`;
			await tx`INSERT INTO stock_moves (sku, delta, channel, actor, reason)
			         VALUES (${line.sku}, ${line.qty}, 'restock', 'sidecar', ${order_id})`;
		}
	});
	return { restocked: true };
}

/** Door 9 — read-only, side-effect-free, and carrying no clock. */
export async function quote(
	sql: Sql,
	{
		lines,
		destination,
		fulfillment_option_id,
		discount_code,
		details
	}: {
		lines: Line[];
		destination: Destination;
		fulfillment_option_id?: string;
		discount_code?: string;
		details?: Record<string, string>;
	}
): Promise<Quote> {
	const unserviceable = await sql<{ prefix: string }[]>`SELECT prefix FROM unserviceable_postal_ranges`;
	for (const { prefix } of unserviceable) {
		if (destination.postal_code.startsWith(prefix)) {
			throw new DoorError(
				'destination-unserviceable',
				`We do not deliver to ${destination.postal_code} yet.`,
				{ postal_code: destination.postal_code }
			);
		}
	}

	// Door-10 values, validated for meaning (the sidecar already validated
	// shape). Absence is never refused here: requiredness is the sidecar's
	// gate at start-checkout, and `fulfillment_options` quotes before details
	// exist. An invalid *provided* value refuses with the valid set as data.
	if (details && Object.keys(details).length) {
		const slot = details['delivery_slot'];
		if (slot !== undefined && !DELIVERY_SLOTS.includes(slot)) {
			throw new DoorError('not-found', `delivery_slot ${JSON.stringify(slot)} is not a slot we run.`, {
				invalid: { delivery_slot: 'must be one of the offered slots' },
				valid_values: DELIVERY_SLOTS
			});
		}
	}

	const items = new Map<string, PricingItem>();
	for (const line of lines) {
		const item = await resolveItem(sql, line.sku);
		items.set(item.sku, item);
	}

	const zones = await sql<{ id: string; label: string; states: string[]; cost_minor: bigint; eta_days: number }[]>`
		SELECT id, label, states, cost_minor, eta_days FROM shipping_zones ORDER BY id`;
	const match =
		zones.find((z) => z.states.length && z.states.includes(destination.state)) ??
		zones.find((z) => !z.states.length);
	if (!match) {
		throw new DoorError('destination-unserviceable', `no zone covers ${destination.state}`);
	}
	if (fulfillment_option_id && fulfillment_option_id !== match.id) {
		throw new DoorError(
			'destination-unserviceable',
			`fulfillment option ${fulfillment_option_id} does not serve ${destination.state}`,
			{ available: [match.id] }
		);
	}

	const identity = await sql<{ registered_state: string; tax_inclusive: boolean }[]>`
		SELECT registered_state, tax_inclusive FROM merchant_tax_identity WHERE id = 1`;
	const tax = identity[0];
	if (!tax) throw new DoorError('not-found', 'no merchant tax identity seeded');

	let discount: { code: string; label: string; amount_minor: number } | undefined;
	if (discount_code) {
		const rows = await sql<{ code: string; label: string; amount_minor: bigint | null }[]>`
			SELECT code, label, amount_minor FROM discount_codes WHERE code = ${discount_code}`;
		const row = rows[0];
		// Every wrong code refuses the same way, with no message and no timing
		// tell — otherwise door 9 answers "is this a code?" all day.
		if (!row || row.amount_minor === null) throw new DoorError('code-invalid', 'That code is not valid.');
		discount = { code: row.code, label: row.label, amount_minor: -Math.abs(Number(row.amount_minor)) };
	}

	try {
		return buildQuote({
			lines,
			items,
			destinationState: destination.state,
			homeState: tax.registered_state,
			zone: {
				id: match.id,
				label: match.label,
				cost_minor: Number(match.cost_minor),
				eta_days: match.eta_days
			},
			discount,
			taxInclusive: tax.tax_inclusive
		});
	} catch (error) {
		if (error instanceof PricingError) throw new DoorError(error.code, error.message);
		throw error;
	}
}

/**
 * Door 7 (create) — the door that produces the `order_id`, and the only
 * response that ever carries the `order_salt` (§6.3a).
 */
export async function ordersCreate(
	sql: Sql,
	payload: {
		cart_id: string;
		lines: Line[];
		destination: Destination;
		contact: Record<string, string>;
		fulfillment_option_id: string;
		agent_id?: string;
		consumer_id?: string;
		order_id_hint?: string;
		details?: Record<string, string>;
	}
) {
	const priced = await quote(sql, {
		lines: payload.lines,
		destination: payload.destination,
		fulfillment_option_id: payload.fulfillment_option_id,
		details: payload.details
	});
	const order_id = payload.order_id_hint ?? `ord_${randomBytes(8).toString('hex')}`;
	const salt = randomBytes(16).toString('hex');

	await sql`INSERT INTO orders (order_id, cart_id, status, lines, quote, total_minor,
	                              destination, contact, details, agent_id, consumer_id, order_salt, receipt_id)
	          VALUES (${order_id}, ${payload.cart_id}, 'pending', ${sql.json(payload.lines)},
	                  ${sql.json(priced)}, ${priced.total_minor},
	                  ${sql.json(payload.destination)}, ${sql.json(payload.contact)},
	                  ${sql.json(payload.details ?? {})},
	                  ${payload.agent_id ?? ''}, ${payload.consumer_id ?? ''},
	                  ${salt}, ${`rcpt_${randomBytes(16).toString('hex')}`})`;

	return { order_id, order_salt_hex: salt, status: 'pending' };
}

/** Door 10 (optional): what this shop asks beyond address + contact. */
export const DELIVERY_SLOTS = ['morning', 'afternoon', 'evening'];

export async function requirementsRead() {
	return {
		fields: [
			{
				key: 'delivery_slot',
				type: 'enum',
				required: false,
				pii: false,
				label: 'Delivery slot',
				description: 'When should the rider arrive? Morning 8–12, afternoon 12–4, evening 4–8.',
				values: DELIVERY_SLOTS
			},
			{
				key: 'delivery_instructions',
				type: 'string',
				required: false,
				pii: false,
				label: 'Delivery instructions',
				description: 'Gate code, floor, landmark — anything the rider needs. 500 chars max.'
			}
		]
	};
}

/** Door 7 (read). The salt is never here. */
export async function ordersRead(sql: Sql, order_id: string) {
	const rows = await sql`SELECT order_id, status, lines, total_minor, refunded_minor,
	                              tracking_number, carrier, dispatched_at, invoice_number, expires_at
	                         FROM orders WHERE order_id = ${order_id}`;
	const row = rows[0];
	if (!row) throw new DoorError('not-found', 'no such order');
	return {
		...row,
		total_minor: Number(row.total_minor),
		refunded_minor: Number(row.refunded_minor),
		dispatched_at: row.dispatched_at ? new Date(row.dispatched_at).toISOString() : null,
		expires_at: row.expires_at ? new Date(row.expires_at).toISOString() : null
	};
}

/** Door 8 — the serialization point for tap vs expiry vs shop-reject. */
export async function ordersSetStatus(
	sql: Sql,
	{ order_id, status, reason }: { order_id: string; status: string; reason?: string }
) {
	const updated = await sql`UPDATE orders SET status = ${status},
	                                 cancel_reason = COALESCE(${reason ?? null}, cancel_reason)
	                           WHERE order_id = ${order_id} RETURNING order_id`;
	if (!updated.length) throw new DoorError('not-found', 'no such order');
	return ordersRead(sql, order_id);
}
