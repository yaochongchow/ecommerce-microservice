/**
 * User service Lambda
 * Handles:
 *   GET/PUT  /api/me              — user profile
 *   GET/POST /api/me/cart         — cart operations
 *   DELETE   /api/me/cart         — clear entire cart
 *   DELETE   /api/me/cart/{id}    — remove cart item
 *   GET      /api/me/orders       — order history
 *   EventBridge: order.created / order.confirmed / order.cancelled
 *     → writes order reference into order-ref table for fast history queries
 */
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  DeleteCommand,
  GetCommand,
  PutCommand,
  UpdateCommand,
  QueryCommand,
} from "@aws-sdk/lib-dynamodb";

// ── env ───────────────────────────────────────────────────────────────────────
const USERS_TABLE     = process.env.USERS_TABLE!;
const CARTS_TABLE     = process.env.CARTS_TABLE!;
const ORDER_REF_TABLE = process.env.ORDER_REF_TABLE!;
const CART_TTL_DAYS   = 7;

// ── clients ───────────────────────────────────────────────────────────────────
const dynamo = DynamoDBDocumentClient.from(new DynamoDBClient({}));

// ── types ─────────────────────────────────────────────────────────────────────
interface CartItem {
  productId: string;
  name:      string;
  price:     number;
  quantity:  number;
  imageUrl?: string;
  emoji?:    string;
}

interface Cart {
  userId:    string;
  items:     CartItem[];
  subtotal:  number;
  updatedAt: number;
  expiresAt: number;
}

interface OrderRef {
  userId:    string;
  orderId:   string;
  status:    string;
  total:     number;
  itemCount: number;
  createdAt: number;
  updatedAt: number;
}

interface ApiEvent {
  routeKey:       string;
  pathParameters?: Record<string, string>;
  body?:           string;
  headers?:        Record<string, string>;
  requestContext?: {
    authorizer?: {
      jwt?: { claims?: Record<string, string> };
    };
  };
}

interface EBEvent {
  "detail-type": string;
  detail:        Record<string, unknown>;
}

// ── helpers ───────────────────────────────────────────────────────────────────
const response = (status: number, body: unknown, correlationId = "") => ({
  statusCode: status,
  headers: {
    "Content-Type": "application/json",
    "X-Correlation-Id": correlationId,
  },
  body: JSON.stringify(body),
});

const getUserId = (event: ApiEvent): string | undefined =>
  event.requestContext?.authorizer?.jwt?.claims?.["sub"];

const getCorrelationId = (event: ApiEvent): string =>
  event.headers?.["x-correlation-id"] ??
  event.headers?.["X-Correlation-Id"] ??
  crypto.randomUUID();

const cartTtl = () => Math.floor(Date.now() / 1000) + CART_TTL_DAYS * 86400;
const now     = () => Math.floor(Date.now() / 1000);

// ── route handlers ────────────────────────────────────────────────────────────
async function getProfile(userId: string, cid: string) {
  const res = await dynamo.send(new GetCommand({ TableName: USERS_TABLE, Key: { userId } }));
  if (!res.Item) return response(404, { message: "User not found" }, cid);

  const ts = now();
  await dynamo.send(new UpdateCommand({
    TableName: USERS_TABLE,
    Key: { userId },
    UpdateExpression: "SET lastLogin = :ts",
    ExpressionAttributeValues: { ":ts": ts },
  }));

  return response(200, { ...res.Item, lastLogin: ts }, cid);
}

async function updateProfile(userId: string, body: Record<string, unknown>, cid: string) {
  const allowed = ["firstName", "lastName", "phone", "address"] as const;
  type AllowedKey = typeof allowed[number];
  const updates = Object.fromEntries(
    Object.entries(body).filter(([k]) => (allowed as readonly string[]).includes(k))
  ) as Partial<Record<AllowedKey, unknown>>;

  if (!Object.keys(updates).length) {
    return response(400, { message: "No updatable fields provided" }, cid);
  }

  const exprParts  = ["updatedAt = :ts"];
  const exprValues: Record<string, unknown> = { ":ts": now() };

  for (const [k, v] of Object.entries(updates)) {
    exprParts.push(`${k} = :${k}`);
    exprValues[`:${k}`] = v;
  }

  const res = await dynamo.send(new UpdateCommand({
    TableName: USERS_TABLE,
    Key: { userId },
    UpdateExpression: "SET " + exprParts.join(", "),
    ExpressionAttributeValues: exprValues,
    ReturnValues: "ALL_NEW",
  }));

  return response(200, res.Attributes, cid);
}

async function getCart(userId: string, cid: string) {
  const res = await dynamo.send(new GetCommand({ TableName: CARTS_TABLE, Key: { userId } }));
  return response(200, res.Item ?? { userId, items: [], subtotal: 0 }, cid);
}

async function upsertCart(userId: string, body: Record<string, unknown>, cid: string) {
  const required = ["productId", "name", "price", "quantity"];
  if (!required.every(k => k in body)) {
    return response(400, { message: `Required fields: ${required.join(", ")}` }, cid);
  }

  const res  = await dynamo.send(new GetCommand({ TableName: CARTS_TABLE, Key: { userId } }));
  const cart = (res.Item as Cart | undefined) ?? { userId, items: [], subtotal: 0, updatedAt: 0, expiresAt: 0 };
  const items: CartItem[] = cart.items ?? [];

  const productId = body.productId as string;
  const existing  = items.find(i => i.productId === productId);

  if (existing) {
    existing.quantity += Number(body.quantity);
  } else {
    items.push({
      productId,
      name:     body.name as string,
      price:    Number(body.price),
      quantity: Number(body.quantity),
      imageUrl: body.imageUrl as string | undefined,
    });
  }

  const subtotal = items.reduce((s, i) => s + i.price * i.quantity, 0);

  await dynamo.send(new PutCommand({
    TableName: CARTS_TABLE,
    Item: { userId, items, subtotal, updatedAt: now(), expiresAt: cartTtl() },
  }));

  return response(200, { message: "Cart updated", itemCount: items.length, subtotal }, cid);
}

async function clearCart(userId: string, cid: string) {
  await dynamo.send(new DeleteCommand({ TableName: CARTS_TABLE, Key: { userId } }));
  return response(200, { message: "Cart cleared" }, cid);
}

async function deleteCartItem(userId: string, itemId: string, cid: string) {
  const res = await dynamo.send(new GetCommand({ TableName: CARTS_TABLE, Key: { userId } }));
  if (!res.Item) return response(404, { message: "Cart not found" }, cid);

  const cart  = res.Item as Cart;
  const items = cart.items.filter(i => i.productId !== itemId);
  const subtotal = items.reduce((s, i) => s + i.price * i.quantity, 0);

  await dynamo.send(new PutCommand({
    TableName: CARTS_TABLE,
    Item: { ...cart, items, subtotal, updatedAt: now(), expiresAt: cartTtl() },
  }));

  return response(200, { message: "Item removed", itemCount: items.length }, cid);
}

async function getOrderHistory(userId: string, cid: string) {
  const res = await dynamo.send(new QueryCommand({
    TableName: ORDER_REF_TABLE,
    KeyConditionExpression: "userId = :uid",
    ExpressionAttributeValues: { ":uid": userId },
    ScanIndexForward: false,
    Limit: 50,
  }));
  return response(200, { orders: res.Items ?? [] }, cid);
}

// ── EventBridge handler ───────────────────────────────────────────────────────
async function handleOrderEvent(detail: Record<string, unknown>, detailType: string) {
  const userId  = detail.userId  as string | undefined;
  const orderId = detail.orderId as string | undefined;
  if (!userId || !orderId) return;

  const statusMap: Record<string, string> = {
    "OrderCreated":   "PENDING",
    "OrderConfirmed": "CONFIRMED",
    "OrderCanceled":  "CANCELLED",
  };

  const items = detail.items as unknown[] | undefined;
  const item: OrderRef = {
    userId,
    orderId,
    status:    statusMap[detailType] ?? "UNKNOWN",
    total:     Number(detail.totalAmount ?? detail.total ?? 0),
    itemCount: items?.length ?? Number(detail.itemCount ?? 0),
    createdAt: now(),
    updatedAt: now(),
  };

  await dynamo.send(new PutCommand({ TableName: ORDER_REF_TABLE, Item: item }));

  // On order creation, save the shipping address to the user's profile
  if (detailType === "OrderCreated") {
    const addr = detail.shippingAddress as string | Record<string, unknown> | undefined;
    const addrStr = typeof addr === "string" ? addr : (addr && Object.keys(addr).length ? JSON.stringify(addr) : null);
    if (addrStr) {
      await dynamo.send(new UpdateCommand({
        TableName: USERS_TABLE,
        Key: { userId },
        UpdateExpression: "SET address = :addr, updatedAt = :ts",
        ExpressionAttributeValues: { ":addr": addrStr, ":ts": now() },
      }));
    }
  }
}

// ── main handler ──────────────────────────────────────────────────────────────
export const handler = async (event: ApiEvent | EBEvent): Promise<unknown> => {
  // EventBridge invocation
  if ("detail-type" in event) {
    await handleOrderEvent(
      event.detail as Record<string, unknown>,
      event["detail-type"],
    );
    return { statusCode: 200 };
  }

  // HTTP API invocation
  const cid    = getCorrelationId(event);
  const userId = getUserId(event);
  let body: Record<string, unknown> = {};

  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    return response(400, { message: "Invalid JSON body" }, cid);
  }

  if (!userId && event.routeKey !== "GET /health") {
    return response(401, { message: "Unauthorized" }, cid);
  }

  const pathParams = event.pathParameters ?? {};

  switch (event.routeKey) {
    case "GET /health":
      return response(200, { status: "ok", service: "user-service" }, cid);
    case "GET /api/me":
      return getProfile(userId!, cid);
    case "PUT /api/me":
      return updateProfile(userId!, body, cid);
    case "GET /api/me/cart":
      return getCart(userId!, cid);
    case "POST /api/me/cart":
      return upsertCart(userId!, body, cid);
    case "DELETE /api/me/cart":
      return clearCart(userId!, cid);
    case "DELETE /api/me/cart/{itemId}":
      return deleteCartItem(userId!, pathParams.itemId ?? "", cid);
    case "GET /api/me/orders":
      return getOrderHistory(userId!, cid);
    default:
      return response(404, { message: "Route not found" }, cid);
  }
};