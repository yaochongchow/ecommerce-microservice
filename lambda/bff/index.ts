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
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, ScanCommand } from '@aws-sdk/lib-dynamodb';
import { EventBridgeClient, PutEventsCommand } from '@aws-sdk/client-eventbridge';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';

const EVENT_BUS_NAME = process.env.EVENT_BUS_NAME!;
const ORDER_API_FN_NAME = process.env.ORDER_API_FN_NAME;
const PRODUCT_SERVICE_URL = process.env.PRODUCT_SERVICE_URL!;
const INVENTORY_TABLE     = process.env.INVENTORY_TABLE!;
const eb          = new EventBridgeClient({});
const lambdaClient = new LambdaClient({});
const dynamo       = DynamoDBDocumentClient.from(new DynamoDBClient({}));

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

async function getInventory(correlationId: string) {
  const stock: Record<string, number> = {};
  let lastKey: Record<string, any> | undefined;
  do {
    const res = await dynamo.send(new ScanCommand({
      TableName: INVENTORY_TABLE,
      ProjectionExpression: 'productId, available',
      ExclusiveStartKey: lastKey,
    }));
    for (const item of res.Items ?? []) {
      stock[item.productId] = Number(item.available ?? 0);
    }
    lastKey = res.LastEvaluatedKey as Record<string, any> | undefined;
  } while (lastKey);
  return ok({ stock }, correlationId);
}

async function getProducts(qs: Record<string, string>, correlationId: string) {
  const params = new URLSearchParams();
  if (qs.limit)  params.set('limit',  qs.limit);
  if (qs.cursor) params.set('cursor', qs.cursor);
  const res = await fetch(`${PRODUCT_SERVICE_URL}/products/?${params}`);
  if (!res.ok) return err(res.status, 'Product service unavailable', correlationId);
  return ok(await res.json(), correlationId);
}

async function getProduct(id: string, correlationId: string) {
  const res = await fetch(`${PRODUCT_SERVICE_URL}/products/${id}`);
  if (!res.ok) return err(res.status, res.status === 404 ? 'Product not found' : 'Product service unavailable', correlationId);
  return ok(await res.json(), correlationId);
}

async function searchProducts(q: string, correlationId: string) {
  const res = await fetch(`${PRODUCT_SERVICE_URL}/products/search?q=${encodeURIComponent(q)}`);
  if (!res.ok) return err(res.status, 'Product service unavailable', correlationId);
  return ok(await res.json(), correlationId);
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
      return getProducts(qs, correlationId);
    case 'GET /api/products/{id}':
      return getProduct(params.id ?? '', correlationId);
    case 'GET /api/inventory':
      return getInventory(correlationId);
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
