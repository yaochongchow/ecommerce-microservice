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
import { DynamoDBDocumentClient, ScanCommand, UpdateCommand, PutCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { EventBridgeClient, PutEventsCommand } from '@aws-sdk/client-eventbridge';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { CloudWatchClient, GetMetricDataCommand, ListMetricsCommand } from '@aws-sdk/client-cloudwatch';

const EVENT_BUS_NAME = process.env.EVENT_BUS_NAME!;
const ORDER_API_FN_NAME = process.env.ORDER_API_FN_NAME;
const PRODUCT_SERVICE_URL = process.env.PRODUCT_SERVICE_URL!;
const INVENTORY_TABLE     = process.env.INVENTORY_TABLE!;
const ORDERS_TABLE        = process.env.ORDERS_TABLE || 'OrdersTable';
const SAGA_STATE_TABLE    = process.env.SAGA_STATE_TABLE || 'SagaStateTable';
const PAYMENTS_TABLE      = process.env.PAYMENTS_TABLE || 'PaymentsTable';
const SHIPMENTS_TABLE     = process.env.SHIPMENTS_TABLE || 'ShipmentsTable';
const RESERVATIONS_TABLE  = process.env.RESERVATIONS_TABLE || 'ReservationsTable';
const eb          = new EventBridgeClient({});
const lambdaClient = new LambdaClient({});
const dynamo       = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const cloudWatch   = new CloudWatchClient({});

const EVENTBRIDGE_METRICS = [
  { key: 'putSuccess',        metricName: 'PutEventsApproximateSuccessCount', label: 'Published (Success)', color: '#16a34a' },
  { key: 'putFailed',         metricName: 'PutEventsApproximateFailedCount',  label: 'Published (Failed)',  color: '#dc2626' },
  { key: 'putCalls',          metricName: 'PutEventsApproximateCallCount',    label: 'Publish API Calls',   color: '#2563eb' },
  { key: 'matchedEvents',     metricName: 'MatchedEvents',                     label: 'Matched Events',      color: '#8b5cf6' },
  { key: 'invocations',       metricName: 'Invocations',                       label: 'Target Invocations',  color: '#f59e0b' },
  { key: 'failedInvocations', metricName: 'FailedInvocations',                 label: 'Failed Invocations',  color: '#ef4444' },
  { key: 'triggeredRules',    metricName: 'TriggeredRules',                    label: 'Triggered Rules',     color: '#06b6d4' },
] as const;

type EventBridgeMetricKey = (typeof EVENTBRIDGE_METRICS)[number]['key'];

interface MetricPoint {
  timestamp: string;
  value: number;
}

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
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
const parsePositiveInt = (value: string | undefined, fallback: number) => {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

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

async function cancelOrder(id: string, correlationId: string) {
  if (!ORDER_API_FN_NAME) return err(501, 'Order service not configured', correlationId);
  const result = await lambdaClient.send(new InvokeCommand({
    FunctionName: ORDER_API_FN_NAME,
    InvocationType: 'RequestResponse',
    Payload: Buffer.from(JSON.stringify({
      httpMethod: 'PUT',
      path: `/orders/${id}/cancel`,
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

// ── Admin helpers ────────────────────────────────────────────────────────────

async function scanTable(tableName: string, limit = 200) {
  const items: Record<string, unknown>[] = [];
  let lastKey: Record<string, any> | undefined;
  do {
    const res = await dynamo.send(new ScanCommand({
      TableName: tableName,
      Limit: limit - items.length,
      ExclusiveStartKey: lastKey,
    }));
    items.push(...(res.Items ?? []));
    lastKey = res.LastEvaluatedKey as Record<string, any> | undefined;
  } while (lastKey && items.length < limit);
  return items;
}

async function adminListOrders(correlationId: string) {
  if (ORDER_API_FN_NAME) {
    const result = await lambdaClient.send(new InvokeCommand({
      FunctionName: ORDER_API_FN_NAME,
      InvocationType: 'RequestResponse',
      Payload: Buffer.from(JSON.stringify({
        httpMethod: 'GET', path: '/orders', headers: { 'X-Correlation-Id': correlationId },
      })),
    }));
    const resp = JSON.parse(new TextDecoder().decode(result.Payload));
    return { statusCode: resp.statusCode || 200, headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': correlationId }, body: resp.body || JSON.stringify(resp) };
  }
  return ok({ orders: [] }, correlationId);
}

async function adminScanTable(tableName: string, correlationId: string) {
  const tableMap: Record<string, string> = {
    orders: ORDERS_TABLE, saga: SAGA_STATE_TABLE, payments: PAYMENTS_TABLE,
    shipments: SHIPMENTS_TABLE, inventory: INVENTORY_TABLE, reservations: RESERVATIONS_TABLE,
  };
  const resolved = tableMap[tableName] || tableName;
  const items = await scanTable(resolved);
  return ok({ table: resolved, count: items.length, items }, correlationId);
}

async function adminRestock(payload: Record<string, unknown>, correlationId: string) {
  const productId = payload.productId as string;
  const quantity = payload.quantity as number;
  if (!productId || !quantity) return err(400, 'productId and quantity required', correlationId);

  await eb.send(new PutEventsCommand({
    Entries: [{
      Source: 'admin-service',
      DetailType: 'ProductRestocked',
      Detail: JSON.stringify({ productId, quantity, correlationId, timestamp: new Date().toISOString() }),
      EventBusName: EVENT_BUS_NAME,
    }],
  }));
  return ok({ message: `Restock event sent: ${quantity} units for product ${productId}` }, correlationId);
}

async function adminUpdateInventory(payload: Record<string, unknown>, correlationId: string) {
  const productId = payload.productId as string;
  const available = payload.available as number;
  if (!productId || available === undefined) return err(400, 'productId and available required', correlationId);

  await dynamo.send(new UpdateCommand({
    TableName: INVENTORY_TABLE,
    Key: { productId },
    UpdateExpression: 'SET available = :a',
    ExpressionAttributeValues: { ':a': available },
  }));
  return ok({ message: `Inventory for ${productId} set to ${available}` }, correlationId);
}

async function adminStats(correlationId: string) {
  const [orders, inventory, payments, shipments] = await Promise.all([
    scanTable(ORDERS_TABLE), scanTable(INVENTORY_TABLE),
    scanTable(PAYMENTS_TABLE), scanTable(SHIPMENTS_TABLE),
  ]);

  const statusCounts: Record<string, number> = {};
  let totalRevenue = 0;
  for (const o of orders) {
    const s = (o as any).status || 'UNKNOWN';
    statusCounts[s] = (statusCounts[s] || 0) + 1;
    if (s === 'CONFIRMED' || s === 'REFUNDED') totalRevenue += parseFloat((o as any).total_amount || '0');
  }

  const lowStock = inventory.filter((i: any) => (Number(i.available) || 0) <= 10);
  const outOfStock = inventory.filter((i: any) => (Number(i.available) || 0) === 0);

  return ok({
    orders: { total: orders.length, byStatus: statusCounts, totalRevenue },
    inventory: { total: inventory.length, lowStock: lowStock.length, outOfStock: outOfStock.length },
    payments: { total: payments.length },
    shipments: { total: shipments.length },
  }, correlationId);
}

async function listEventBridgeMetricNames(): Promise<string[]> {
  const metricNames = new Set<string>();
  let nextToken: string | undefined;
  do {
    const res = await cloudWatch.send(new ListMetricsCommand({
      Namespace: 'AWS/Events',
      Dimensions: [{ Name: 'EventBusName', Value: EVENT_BUS_NAME }],
      NextToken: nextToken,
    }));
    for (const metric of res.Metrics ?? []) {
      if (metric.MetricName) metricNames.add(metric.MetricName);
    }
    nextToken = res.NextToken;
  } while (nextToken);
  return [...metricNames].sort();
}

function summarizePoints(points: MetricPoint[]) {
  const total = points.reduce((sum, point) => sum + point.value, 0);
  const max = points.reduce((peak, point) => Math.max(peak, point.value), 0);
  const latest = points.length ? points[points.length - 1].value : 0;
  return { total, max, latest };
}

async function adminObservability(qs: Record<string, string>, correlationId: string) {
  const windowMinutes = clamp(parsePositiveInt(qs.windowMinutes, 180), 30, 1440);
  const periodSeconds = clamp(parsePositiveInt(qs.periodSeconds, windowMinutes <= 240 ? 60 : 300), 60, 3600);
  const endTime = new Date();
  const startTime = new Date(endTime.getTime() - windowMinutes * 60 * 1000);

  let availableMetricNames: string[] = [];
  try {
    availableMetricNames = await listEventBridgeMetricNames();
  } catch {
    // Metrics can still be queried even when ListMetrics has no recent datapoints.
  }

  const metricQueries = EVENTBRIDGE_METRICS.map((def, idx) => ({
    Id: `m${idx + 1}`,
    Label: def.label,
    ReturnData: true,
    MetricStat: {
      Metric: {
        Namespace: 'AWS/Events',
        MetricName: def.metricName,
        Dimensions: [{ Name: 'EventBusName', Value: EVENT_BUS_NAME }],
      },
      Period: periodSeconds,
      Stat: 'Sum',
    },
  }));

  const rawResults: Record<string, { timestamps: Date[]; values: number[] }> = {};
  for (const query of metricQueries) rawResults[query.Id] = { timestamps: [], values: [] };

  let nextToken: string | undefined;
  do {
    const res = await cloudWatch.send(new GetMetricDataCommand({
      StartTime: startTime,
      EndTime: endTime,
      MetricDataQueries: metricQueries,
      ScanBy: 'TimestampAscending',
      NextToken: nextToken,
    }));

    for (const item of res.MetricDataResults ?? []) {
      if (!item.Id || !rawResults[item.Id]) continue;
      rawResults[item.Id].timestamps.push(...(item.Timestamps ?? []));
      rawResults[item.Id].values.push(...(item.Values ?? []));
    }
    nextToken = res.NextToken;
  } while (nextToken);

  const series: Record<EventBridgeMetricKey, {
    key: EventBridgeMetricKey;
    metricName: string;
    label: string;
    color: string;
    points: MetricPoint[];
    total: number;
    max: number;
    latest: number;
  }> = {} as Record<EventBridgeMetricKey, {
    key: EventBridgeMetricKey;
    metricName: string;
    label: string;
    color: string;
    points: MetricPoint[];
    total: number;
    max: number;
    latest: number;
  }>;

  for (let idx = 0; idx < EVENTBRIDGE_METRICS.length; idx++) {
    const def = EVENTBRIDGE_METRICS[idx];
    const queryId = `m${idx + 1}`;
    const { timestamps, values } = rawResults[queryId];
    const points: MetricPoint[] = timestamps
      .map((timestamp, i) => ({
        timestamp: timestamp.toISOString(),
        value: Number(values[i] ?? 0),
      }))
      .filter((p) => Number.isFinite(p.value))
      .sort((a, b) => a.timestamp.localeCompare(b.timestamp));

    const summary = summarizePoints(points);
    series[def.key] = {
      key: def.key,
      metricName: def.metricName,
      label: def.label,
      color: def.color,
      points,
      ...summary,
    };
  }

  const publishSuccess = series.putSuccess.total;
  const publishFailed = series.putFailed.total;
  const publishCalls = series.putCalls.total || (publishSuccess + publishFailed);
  const invocationCount = series.invocations.total;
  const failedInvocations = series.failedInvocations.total;

  return ok({
    eventBusName: EVENT_BUS_NAME,
    generatedAt: endTime.toISOString(),
    windowMinutes,
    periodSeconds,
    availableMetricNames,
    summary: {
      publishSuccess,
      publishFailed,
      publishCalls,
      matchedEvents: series.matchedEvents.total,
      triggeredRules: series.triggeredRules.total,
      invocations: invocationCount,
      failedInvocations,
      publishFailureRatePct: publishCalls > 0 ? Number(((publishFailed / publishCalls) * 100).toFixed(2)) : 0,
      invocationFailureRatePct: invocationCount > 0 ? Number(((failedInvocations / invocationCount) * 100).toFixed(2)) : 0,
    },
    series,
  }, correlationId);
}

// ── Shipment management ──────────────────────────────────────────────────────

async function adminCreateShipment(payload: Record<string, unknown>, correlationId: string) {
  const orderId = payload.orderId as string;
  const trackingNumber = payload.trackingNumber as string;
  const carrier = (payload.carrier as string) || 'MANUAL';
  if (!orderId || !trackingNumber) return err(400, 'orderId and trackingNumber required', correlationId);

  // Check if shipment already exists for this order
  const existing = await dynamo.send(new QueryCommand({
    TableName: SHIPMENTS_TABLE,
    IndexName: 'orderId-index',
    KeyConditionExpression: 'orderId = :oid',
    ExpressionAttributeValues: { ':oid': orderId },
    Limit: 1,
  }));
  if (existing.Items?.length) {
    return err(409, `Shipment already exists for order ${orderId}: ${existing.Items[0].shipmentId}`, correlationId);
  }

  // Fetch order to get items for the event
  let orderItems: unknown[] = [];
  let email = 'customer@example.com';
  if (ORDER_API_FN_NAME) {
    try {
      const res = await lambdaClient.send(new InvokeCommand({
        FunctionName: ORDER_API_FN_NAME, InvocationType: 'RequestResponse',
        Payload: Buffer.from(JSON.stringify({ httpMethod: 'GET', path: `/orders/${orderId}`, pathParameters: { id: orderId }, headers: {} })),
      }));
      const resp = JSON.parse(new TextDecoder().decode(res.Payload));
      const body = JSON.parse(resp.body || '{}');
      orderItems = body.order?.items || [];
    } catch { /* proceed without items */ }
  }

  const shipmentId = `shp_${crypto.randomUUID().replace(/-/g, '').slice(0, 8)}`;
  const now = new Date().toISOString();

  const shipment = {
    shipmentId, orderId, email, carrier, trackingNumber,
    status: 'SHIPPED',
    shippingAddress: {},
    items: orderItems,
    createdAt: now,
  };

  // Write to ShipmentsTable
  await dynamo.send(new PutCommand({ TableName: SHIPMENTS_TABLE, Item: shipment }));

  // Publish ShipmentCreated event → triggers inventory fulfillment automatically
  await eb.send(new PutEventsCommand({
    Entries: [{
      Source: 'shipping-service',
      DetailType: 'ShipmentCreated',
      Detail: JSON.stringify({
        shipmentId, orderId, email, carrier, trackingNumber,
        status: 'SHIPPED', items: orderItems, correlationId, timestamp: now,
      }),
      EventBusName: EVENT_BUS_NAME,
    }],
  }));

  return ok({ message: `Shipment created and fulfillment triggered`, shipment }, correlationId);
}

async function adminUpdateShipment(payload: Record<string, unknown>, correlationId: string) {
  const shipmentId = payload.shipmentId as string;
  const status = payload.status as string;
  const trackingNumber = payload.trackingNumber as string;
  if (!shipmentId) return err(400, 'shipmentId required', correlationId);

  const updates: string[] = [];
  const values: Record<string, unknown> = {};
  const names: Record<string, string> = {};

  if (status) {
    updates.push('#s = :s');
    names['#s'] = 'status';
    values[':s'] = status;
  }
  if (trackingNumber) {
    updates.push('trackingNumber = :tn');
    values[':tn'] = trackingNumber;
  }
  if (!updates.length) return err(400, 'Nothing to update', correlationId);

  updates.push('updatedAt = :now');
  values[':now'] = new Date().toISOString();

  await dynamo.send(new UpdateCommand({
    TableName: SHIPMENTS_TABLE,
    Key: { shipmentId },
    UpdateExpression: `SET ${updates.join(', ')}`,
    ExpressionAttributeValues: values,
    ...(Object.keys(names).length ? { ExpressionAttributeNames: names } : {}),
  }));

  return ok({ message: `Shipment ${shipmentId} updated` }, correlationId);
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
    case 'PUT /api/orders/{id}/cancel':
      if (!userId) return err(401, 'Unauthorized', correlationId);
      return cancelOrder(params.id ?? '', correlationId);
    // Admin routes
    case 'GET /api/admin/orders':
      return adminListOrders(correlationId);
    case 'GET /api/admin/stats':
      return adminStats(correlationId);
    case 'GET /api/admin/observability':
      return adminObservability(qs, correlationId);
    case 'GET /api/admin/table/{name}':
      return adminScanTable(params.name ?? '', correlationId);
    case 'POST /api/admin/restock':
      return adminRestock(payload as Record<string, unknown>, correlationId);
    case 'PUT /api/admin/inventory':
      return adminUpdateInventory(payload as Record<string, unknown>, correlationId);
    case 'POST /api/admin/ship':
      return adminCreateShipment(payload as Record<string, unknown>, correlationId);
    case 'PUT /api/admin/shipment':
      return adminUpdateShipment(payload as Record<string, unknown>, correlationId);
    case 'PUT /api/admin/orders/{id}/cancel':
      return cancelOrder(params.id ?? '', correlationId);
    default:
      return err(404, 'Route not found', correlationId);
  }
};
