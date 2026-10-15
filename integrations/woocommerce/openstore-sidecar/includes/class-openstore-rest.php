<?php
/**
 * The ten doors, mounted.
 *
 * `POST /wp-json/openstore/v1/trait/<door>`, JSON in and out, HMAC over the raw
 * body, idempotency on every mutating door, and a closed reason code on every
 * refusal — **never a 500 and never a coerced default**.
 *
 * Private network only. WordPress will happily serve these to the internet, so
 * the readme says to firewall them and the admin screen says it again: door 2
 * hands back exact stock integers and door 7 hands back the `order_salt`.
 */

declare( strict_types=1 );

defined( 'ABSPATH' ) || exit;

final class OpenStore_REST {

	public const NAMESPACE = 'openstore/v1';

	/** Doors that change something, and therefore must carry an idempotency key. */
	private const MUTATING = array(
		'reserve',
		'commit',
		'release',
		'restock',
		'orders.create',
		'orders.set-status',
	);

	public function register(): void {
		add_action( 'rest_api_init', array( $this, 'routes' ) );
	}

	public function routes(): void {
		register_rest_route(
			self::NAMESPACE,
			'/trait/(?P<door>[a-z.\-]+)',
			array(
				'methods'  => 'POST',
				'callback' => array( $this, 'handle' ),
				// Authentication is the HMAC, not a WordPress capability: the
				// caller is a sidecar holding the Merchant's shared secret, and
				// it has no WordPress user to be.
				'permission_callback' => '__return_true',
				'args'                => array(
					'door' => array( 'required' => true ),
				),
			)
		);
	}

	public function handle( WP_REST_Request $request ): WP_REST_Response {
		$door = (string) $request->get_param( 'door' );

		try {
			$handlers = $this->handlers();
			if ( ! isset( $handlers[ $door ] ) ) {
				throw new OpenStore_Door_Error( 'not-found', sprintf( 'no door %s', $door ) );
			}

			// **The raw bytes, not the parsed body.** A signature computed over
			// a re-serialization covers a round trip and not the request.
			$raw     = (string) $request->get_body();
			$headers = OpenStore_HMAC::headers_from( $request );
			OpenStore_HMAC::verify(
				OpenStore_Settings::secret(),
				$raw,
				'/trait/' . $door,
				$headers
			);

			$key = (string) ( $headers[ OpenStore_HMAC::IDEMPOTENCY_HEADER ] ?? '' );
			if ( in_array( $door, self::MUTATING, true ) ) {
				if ( '' === $key ) {
					throw new OpenStore_Door_Error(
						'signature-invalid',
						sprintf( '%s mutates and requires an Idempotency-Key', $door )
					);
				}
				$replay = OpenStore_Idempotency::replay( $door, $key );
				if ( null !== $replay ) {
					return new WP_REST_Response( $replay['body'], $replay['status'] );
				}
			}

			$body   = $raw ? json_decode( $raw, true ) : array();
			$body   = is_array( $body ) ? $body : array();
			$status = 200;

			try {
				$payload = $handlers[ $door ]( $body );
			} catch ( OpenStore_Door_Error $refusal ) {
				$status  = $refusal->status();
				$payload = $refusal->to_payload();
			}

			if ( in_array( $door, self::MUTATING, true ) && '' !== $key ) {
				// Stored before the response goes out, so a crash on the wire
				// still replays the same answer rather than re-running the
				// mutation — and a refusal is stored too, because a retry that
				// succeeds where the first attempt refused is a second hold with
				// extra steps.
				OpenStore_Idempotency::remember( $door, $key, $status, $payload );
			}

			return new WP_REST_Response( $payload, $status );
		} catch ( OpenStore_Door_Error $refusal ) {
			return new WP_REST_Response( $refusal->to_payload(), $refusal->status() );
		} catch ( Throwable $error ) {
			// An unexpected failure is still a refusal with a code. A 500 with a
			// stack trace tells an agent nothing it can act on, and tells
			// everyone else too much.
			error_log( '[openstore] ' . $door . ': ' . $error->getMessage() );
			$envelope = new OpenStore_Door_Error(
				'not-found',
				'This store could not answer that door. Its log has the reason.'
			);
			return new WP_REST_Response( $envelope->to_payload(), 500 );
		}
	}

	/** @return array<string, callable(array<string, mixed>): array<string, mixed>> */
	private function handlers(): array {
		$catalogue = new OpenStore_Catalogue();
		$quote     = new OpenStore_Quote( $catalogue );
		$stock     = new OpenStore_Stock( $catalogue );
		$orders    = new OpenStore_Orders( $catalogue, $quote );

		return array(
			'catalog.read'      => static fn( array $b ): array => $catalogue->catalog_read(),
			'stock.read'        => static fn( array $b ): array => $catalogue->stock_read( (array) ( $b['skus'] ?? array() ) ),
			'reserve'           => static fn( array $b ): array => $stock->reserve(
				(string) ( $b['order_id'] ?? '' ),
				(array) ( $b['lines'] ?? array() ),
				isset( $b['discount_code'] ) ? (string) $b['discount_code'] : null
			),
			'commit'            => static fn( array $b ): array => $stock->commit( (string) ( $b['order_id'] ?? '' ) ),
			'release'           => static fn( array $b ): array => $stock->release( (string) ( $b['order_id'] ?? '' ) ),
			'restock'           => static fn( array $b ): array => $stock->restock(
				(string) ( $b['order_id'] ?? '' ),
				(array) ( $b['lines'] ?? array() )
			),
			'orders.create'     => static fn( array $b ): array => $orders->create( $b ),
			'orders.read'       => static fn( array $b ): array => $orders->read( (string) ( $b['order_id'] ?? '' ) ),
			'orders.set-status' => static fn( array $b ): array => $orders->set_status(
				(string) ( $b['order_id'] ?? '' ),
				(string) ( $b['status'] ?? '' ),
				(string) ( $b['reason'] ?? '' )
			),
			'quote'             => static fn( array $b ): array => $quote->quote(
				(array) ( $b['lines'] ?? array() ),
				(array) ( $b['destination'] ?? array() ),
				isset( $b['fulfillment_option_id'] ) ? (string) $b['fulfillment_option_id'] : null,
				isset( $b['discount_code'] ) ? (string) $b['discount_code'] : null
			),
			// Door 10, optional: this adapter declares no extras, so buyers
			// use the fixed shape. A store that needs more implements this
			// door with its fields instead of removing the stub.
			'requirements.read' => static fn( array $b ): array => array( 'fields' => array() ),
		);
	}
}
