/**
 * BFF Lambda — aggregation layer
 * Currently handles:
 *   GET  /health           — liveness check
 *   GET  /api/products     — demo product catalog (replace with real DB call)
 *   GET  /api/products/:id — single product
 *   GET  /api/search       — simple in-memory search over demo catalog
 *   POST /api/orders       — creates order stub + emits order.created event
 *   GET  /api/orders/:id   — order lookup stub
 *
 * When M2 (Order/Payment) and M3 (Product/Search) services deploy, swap
 * the demo data and stubs for real downstream Lambda/API calls.
 */
import { EventBridgeClient, PutEventsCommand } from '@aws-sdk/client-eventbridge';

const EVENT_BUS_NAME = process.env.EVENT_BUS_NAME!;
const eb = new EventBridgeClient({});

// ── Demo catalog ─────────────────────────────────────────────────────────────
const PRODUCTS = [
  { id: 'p1', name: 'Wireless Headphones',  desc: 'Premium noise-cancelling over-ear headphones', price: 89.99,  emoji: '🎧', stock: 14, category: 'Electronics' },
  { id: 'p2', name: 'Mechanical Keyboard',  desc: 'TKL layout with tactile switches and RGB',     price: 129.99, emoji: '⌨️', stock: 7,  category: 'Electronics' },
  { id: 'p3', name: 'Running Shoes',        desc: 'Lightweight trail runners for all terrains',    price: 74.99,  emoji: '👟', stock: 22, category: 'Apparel'     },
  { id: 'p4', name: 'Coffee Grinder',       desc: 'Burr grinder with 15 grind settings',           price: 49.99,  emoji: '☕', stock: 9,  category: 'Kitchen'     },
  { id: 'p5', name: 'Yoga Mat',             desc: 'Extra thick non-slip mat with carry strap',     price: 34.99,  emoji: '🧘', stock: 31, category: 'Fitness'     },
  { id: 'p6', name: 'Smart Watch',          desc: 'Health tracking with 7-day battery life',       price: 199.99, emoji: '⌚', stock: 5,  category: 'Electronics' },
  { id: 'p7', name: 'Backpack',             desc: '30L waterproof daypack with laptop sleeve',     price: 59.99,  emoji: '🎒', stock: 18, category: 'Accessories' },
  { id: 'p8', name: 'Desk Lamp',            desc: 'LED lamp with wireless charging base',           price: 44.99,  emoji: '💡', stock: 12, category: 'Home'        },
];

// ── Types ─────────────────────────────────────────────────────────────────────
interface ApiEvent {
  routeKey:        string;
  pathParameters?: Record<string, string>;
  queryStringParameters?: Record<string, string>;
  body?:           string;
  headers?:        Record<string, string>;
  requestContext?: {
    authorizer?: { jwt?: { claims?: Record<string, string> } };
  };
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const ok    = (body: unknown, cid = '') => ({ statusCode: 200, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': cid }, body: JSON.stringify(body) });
const err   = (status: number, msg: string, cid = '') => ({ statusCode: status, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': cid }, body: JSON.stringify({ message: msg }) });
const cid   = (e: ApiEvent) => e.headers?.['x-correlation-id'] ?? e.headers?.['X-Correlation-Id'] ?? crypto.randomUUID();
const uid   = (e: ApiEvent) => e.requestContext?.authorizer?.jwt?.claims?.['sub'];
const body  = (e: ApiEvent) => { try { return e.body ? JSON.parse(e.body) : {}; } catch { return null; } };

// ── Route handlers ────────────────────────────────────────────────────────────
function getProducts(correlationId: string) {
  return ok({ products: PRODUCTS, count: PRODUCTS.length }, correlationId);
}

function getProduct(id: string, correlationId: string) {
  const p = PRODUCTS.find(x => x.id === id);
  return p ? ok(p, correlationId) : err(404, 'Product not found', correlationId);
}

function searchProducts(q: string, correlationId: string) {
  const lower = q.toLowerCase();
  const results = PRODUCTS.filter(p =>
    p.name.toLowerCase().includes(lower) ||
    p.category.toLowerCase().includes(lower) ||
    p.desc.toLowerCase().includes(lower),
  );
  return ok({ results, count: results.length, query: q }, correlationId);
}

async function createOrder(userId: string, payload: Record<string, unknown>, correlationId: string) {
  if (!payload.items || !Array.isArray(payload.items) || !payload.items.length) {
    return err(400, 'items array required', correlationId);
  }
  const orderId = crypto.randomUUID();
  const order = {
    orderId,
    userId,
    items:     payload.items,
    total:     payload.total ?? 0,
    itemCount: (payload.items as unknown[]).length,
    status:    'PENDING',
    createdAt: Math.floor(Date.now() / 1000),
  };

  await eb.send(new PutEventsCommand({
    Entries: [{
      Source:       'ecomm.order',
      DetailType:   'order.created',
      Detail:       JSON.stringify(order),
      EventBusName: EVENT_BUS_NAME,
    }],
  }));

  return { statusCode: 201, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': correlationId }, body: JSON.stringify(order) };
}

function getOrder(id: string, correlationId: string) {
  // Stub — M2 (Order service) will own this route once deployed
  return ok({ orderId: id, status: 'PENDING', message: 'Order service coming in M2' }, correlationId);
}

// ── Main handler ──────────────────────────────────────────────────────────────
export const handler = async (event: ApiEvent): Promise<unknown> => {
  const correlationId = cid(event);
  const params = event.pathParameters ?? {};
  const qs = event.queryStringParameters ?? {};
  const userId = uid(event);
  const payload = body(event);

  if (payload === null) return err(400, 'Invalid JSON body', correlationId);

  switch (event.routeKey) {
    case 'GET /health':
      return ok({ status: 'ok', service: 'bff', ts: Date.now() }, correlationId);

    case 'GET /api/products':
      return getProducts(correlationId);

    case 'GET /api/products/{id}':
      return getProduct(params.id ?? '', correlationId);

    case 'GET /api/search':
      return qs.q
        ? searchProducts(qs.q, correlationId)
        : err(400, 'Query param ?q= required', correlationId);

    case 'POST /api/orders':
      if (!userId) return err(401, 'Unauthorized', correlationId);
      return createOrder(userId, payload as Record<string, unknown>, correlationId);

    case 'GET /api/orders/{id}':
      return getOrder(params.id ?? '', correlationId);

    default:
      return err(404, 'Route not found', correlationId);
  }
};