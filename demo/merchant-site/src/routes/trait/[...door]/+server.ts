/**
 * The nine doors, mounted. Private network only — the sidecar calls these and
 * nothing public ever reaches them.
 *
 * HMAC on the raw body, idempotency on every mutating door, and a closed reason
 * code on every refusal. **Same key, same result** including a refusal: a retry
 * that succeeds where the first attempt refused is a second hold with extra
 * steps.
 */
import type { RequestHandler } from './$types';
import { json } from '@sveltejs/kit';
import { sql } from '$lib/db.ts';
import {
	IDEMPOTENCY_HEADER,
	NONCE_HEADER,
	SIGNATURE_HEADER,
	TIMESTAMP_HEADER,
	SignatureError,
	verify
} from '$lib/hmac.ts';
import * as doors from '$lib/trait/doors.ts';

const MUTATING = new Set([
	'reserve',
	'commit',
	'release',
	'restock',
	'orders.create',
	'orders.set-status'
]);

const HANDLERS: Record<string, (body: any) => Promise<unknown>> = {
	'catalog.read': () => doors.catalogRead(sql),
	'stock.read': (b) => doors.stockRead(sql, b.skus),
	reserve: (b) => doors.reserve(sql, b),
	commit: (b) => doors.commit(sql, b.order_id),
	release: (b) => doors.release(sql, b.order_id),
	restock: (b) => doors.restock(sql, b.order_id, b.lines),
	'orders.create': (b) => doors.ordersCreate(sql, b),
	'orders.read': (b) => doors.ordersRead(sql, b.order_id),
	'orders.set-status': (b) => doors.ordersSetStatus(sql, b),
	quote: (b) => doors.quote(sql, b),
	'requirements.read': () => doors.requirementsRead()
};

export const POST: RequestHandler = async ({ params, request }) => {
	const door = params.door ?? '';
	const handler = HANDLERS[door];
	if (!handler) {
		return json(new doors.DoorError('not-found', `no door ${door}`).toPayload(), { status: 404 });
	}

	const raw = await request.text();
	try {
		verify(process.env.TRAIT_HMAC_SECRET ?? 'conformance-secret', {
			body: raw,
			path: `/trait/${door}`,
			signature: request.headers.get(SIGNATURE_HEADER) ?? '',
			timestamp: request.headers.get(TIMESTAMP_HEADER) ?? '',
			nonce: request.headers.get(NONCE_HEADER) ?? ''
		});
	} catch (error) {
		const detail = error instanceof SignatureError ? error.message : 'signature does not verify';
		return json(new doors.DoorError('signature-invalid', detail).toPayload(), { status: 401 });
	}

	const key = request.headers.get(IDEMPOTENCY_HEADER);
	if (MUTATING.has(door)) {
		if (!key) {
			return json(
				new doors.DoorError(
					'signature-invalid',
					`${door} mutates and requires an Idempotency-Key`
				).toPayload(),
				{ status: 400 }
			);
		}
		const cached = await sql<{ status: number; body: unknown }[]>`
			SELECT status, body FROM idempotency WHERE key = ${`${door}:${key}`}`;
		const hit = cached[0];
		if (hit) return json(hit.body as Record<string, unknown>, { status: hit.status });
	}

	let status = 200;
	let body: unknown;
	try {
		body = await handler(raw ? JSON.parse(raw) : {});
	} catch (error) {
		if (error instanceof doors.DoorError) {
			status = error.status;
			body = error.toPayload();
		} else {
			throw error;
		}
	}

	if (MUTATING.has(door) && key) {
		// Cached before the response goes out, so a crash on the wire still
		// replays the same answer rather than re-running the mutation.
		// Serialized through JSON.parse/stringify so what is replayed is exactly
		// what went on the wire — a Date in the object would otherwise cache as a
		// different shape than the response the caller saw.
		await sql`INSERT INTO idempotency (key, door, status, body)
		          VALUES (${`${door}:${key}`}, ${door}, ${status}, ${sql.json(JSON.parse(JSON.stringify(body)))})
		          ON CONFLICT (key) DO NOTHING`;
	}
	return json(body as Record<string, unknown>, { status });
};
