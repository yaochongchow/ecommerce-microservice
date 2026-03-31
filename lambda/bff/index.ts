/**
 * BFF Lambda — aggregation layer
 *
 * Routes:
 *   GET  /health           — liveness
 *   GET  /api/products     — demo product catalog (TODO: route to M3 product service)
 *   GET  /api/products/:id — single product
 *   GET  /api/search       — in-memory search over demo catalog
 *   POST /api/orders       — creates order via M2 order service
 *   GET  /api/orders/:id   — order lookup via M2
 */
import { EventBridgeClient, PutEventsCommand } from '@aws-sdk/client-eventbridge';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';

const EVENT_BUS_NAME = process.env.EVENT_BUS_NAME!;
const ORDER_API_FN_NAME = process.env.ORDER_API_FN_NAME;
const eb = new EventBridgeClient({});
const lambdaClient = new LambdaClient({});

// Demo catalog — will be replaced by M3 product service (ECS)
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

interface ApiEvent {
  routeKey: string;
  pathParameters?: Record<string, string>;
  queryStringParameters?: Record<string, string>;
  body?: string;
  headers?: Record<string, string>;
  requestContext?: { authorizer?: { jwt?: { claims?: Record<string, string> } } };
}

const ok  = (body: unknown, cid = '') => ({ statusCode: 200, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': cid }, body: JSON.stringify(body) });
const err = (status: number, msg: string, cid = '') => ({ statusCode: status, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': cid }, body: JSON.stringify({ message: msg }) });
const getCid = (e: ApiEvent) => e.headers?.['x-correlation-id'] ?? e.headers?.['X-Correlation-Id'] ?? crypto.randomUUID();
const getUid = (e: ApiEvent) => e.requestContext?.authorizer?.jwt?.claims?.['sub'];
const getBody = (e: ApiEvent) => { try { return e.body ? JSON.parse(e.body) : {}; } catch { return null; } };

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

  // If M2 order service is deployed, invoke it directly
  if (ORDER_API_FN_NAME) {
    const orderBody = {
      user_id: userId,
      items: (payload.items as any[]).map(i => ({
        product_id: i.productId || i.id || i.product_id,
        quantity: i.quantity || 1,
        unit_price: i.price || i.unit_price || 0,
      })),
      shipping_address: payload.shippingAddress || payload.shipping_address || {},
    };

    const result = await lambdaClient.send(new InvokeCommand({
      FunctionName: ORDER_API_FN_NAME,
      InvocationType: 'RequestResponse',
      Payload: Buffer.from(JSON.stringify({
        httpMethod: 'POST',
        path: '/orders',
        headers: { 'X-Correlation-Id': correlationId },
        body: JSON.stringify(orderBody),
      })),
    }));

    const responsePayload = JSON.parse(new TextDecoder().decode(result.Payload));
    return {
      statusCode: responsePayload.statusCode || 201,
      headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': correlationId },
      body: responsePayload.body || JSON.stringify(responsePayload),
    };
  }

  // Fallback: emit event directly (for when M2 is not deployed)
  const orderId = crypto.randomUUID();
  const order = {
    orderId, userId,
    items: payload.items,
    total: payload.total ?? 0,
    itemCount: (payload.items as unknown[]).length,
    status: 'PENDING',
    createdAt: Math.floor(Date.now() / 1000),
  };

  await eb.send(new PutEventsCommand({
    Entries: [{
      Source: 'order-service',
      DetailType: 'OrderCreated',
      Detail: JSON.stringify(order),
      EventBusName: EVENT_BUS_NAME,
    }],
  }));

  return { statusCode: 201, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': correlationId }, body: JSON.stringify(order) };
}

async function getOrder(id: string, correlationId: string) {
  if (ORDER_API_FN_NAME) {
    const result = await lambdaClient.send(new InvokeCommand({
      FunctionName: ORDER_API_FN_NAME,
      InvocationType: 'RequestResponse',
      Payload: Buffer.from(JSON.stringify({
        httpMethod: 'GET',
        path: `/orders/${id}`,
        pathParameters: { id },
        headers: { 'X-Correlation-Id': correlationId },
      })),
    }));
    const responsePayload = JSON.parse(new TextDecoder().decode(result.Payload));
    return {
      statusCode: responsePayload.statusCode || 200,
      headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': correlationId },
      body: responsePayload.body || JSON.stringify(responsePayload),
    };
  }

  return ok({ orderId: id, status: 'PENDING', message: 'Order service not configured' }, correlationId);
}

export const handler = async (event: ApiEvent): Promise<unknown> => {
  const correlationId = getCid(event);
  const params = event.pathParameters ?? {};
  const qs = event.queryStringParameters ?? {};
  const userId = getUid(event);
  const payload = getBody(event);

  if (payload === null) return err(400, 'Invalid JSON body', correlationId);

  switch (event.routeKey) {
    case 'GET /health':
      return ok({ status: 'ok', service: 'bff', ts: Date.now() }, correlationId);
    case 'GET /api/products':
      return getProducts(correlationId);
    case 'GET /api/products/{id}':
      return getProduct(params.id ?? '', correlationId);
    case 'GET /api/search':
      return qs.q ? searchProducts(qs.q, correlationId) : err(400, 'Query param ?q= required', correlationId);
    case 'POST /api/orders':
      if (!userId) return err(401, 'Unauthorized', correlationId);
      return createOrder(userId, payload as Record<string, unknown>, correlationId);
    case 'GET /api/orders/{id}':
      return getOrder(params.id ?? '', correlationId);
    default:
      return err(404, 'Route not found', correlationId);
  }
};
