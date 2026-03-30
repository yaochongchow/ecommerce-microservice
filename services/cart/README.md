# Cart Service

Shopping cart management API built in **Go** with **Redis** for fast session-based storage. Carts auto-expire after 48 hours and are archived to history for order reference.

Deployed as an **ECS Fargate** container with a Redis sidecar.

## Redis Data Model

| Key Pattern | Value | TTL | Purpose |
|-------------|-------|-----|---------|
| `cart:active:<userId>` | cartId (string) | 48 hours | Tracks which cart is active for a user |
| `cart:data:<cartId>` | Cart JSON | None | Full cart data (items, timestamps) |
| `cart:history:<userId>` | List of cartIds | None | Archive of expired/checked-out carts |
| `cart:shadow:<userId>` | cartId (string) | None | Backup reference for expiry listener |

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (`{ status: "healthy", cache: "redis" }`) |
| GET | `/cart/:userId` | Get user's active cart |
| POST | `/cart/create/:userId` | Create new cart (409 if one already exists) |
| POST | `/cart/add/:userId` | Add item to cart (increments qty if exists) |
| PUT | `/cart/delete/:userId` | Remove item from cart |
| PUT | `/cart/update/:userId` | Update item quantity |
| PUT | `/cart/deactivate/:userId` | Checkout: move cart to history |
| GET | `/cart/:userId/pricecheck` | Get cart with product prices and total |

### Add Item Request

```json
{ "product_id": 42, "quantity": 2 }
```

### Price Check Response

```json
{
  "cart": { "cart_id": "...", "items": [...] },
  "prices": { "42": 89.99, "15": 34.99 },
  "total": 214.97
}
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `REDIS_ADDR` | Redis server address | `"localhost:6379"` |
| `PRODUCT_SERVICE_URL` | Product service base URL (for price lookups) | — |
| `PORT` | HTTP server port | `8080` |

## Key Design Patterns

### Cart Lifecycle

```
1. POST /cart/create/:userId
   -> Sets cart:active:<userId> with 48h TTL
   -> Sets cart:shadow:<userId> (permanent)
   -> Creates cart:data:<cartId>

2. POST /cart/add/:userId  (add/modify items)

3a. PUT /cart/deactivate/:userId  (checkout)
    -> Deletes active + shadow keys
    -> Pushes cartId to cart:history:<userId>
    -> Sets checked_out = true

3b. Active key expires after 48h  (abandoned cart)
    -> Expiry listener detects via keyspace notification
    -> Reads shadow key to find cartId
    -> Archives cart to history
    -> Cleans up shadow key
```

### Redis Keyspace Expiry Listener

A background goroutine subscribes to Redis keyspace notifications (`__keyevent@0__:expired`). When the `cart:active:*` key expires:
1. Reads the shadow key to find the associated cartId
2. Archives the cart data to the user's history list
3. Cleans up the shadow key

This prevents loss of cart data when users abandon their carts.

### Cross-Service Price Lookup

The `/cart/:userId/pricecheck` endpoint calls the product service's `/products/pricecheck` API to get current prices, then calculates the total. This ensures cart totals always reflect live pricing.

## Files

| File | Description |
|------|-------------|
| `main.go` | Entry point: Redis init, expiry listener, Gin router setup |
| `handlers.go` | Route handlers: CRUD operations, checkout, price check |
| `redis.go` | Redis operations: key builders, cart get/save, active cart management |
| `expiry.go` | Expiry listener: keyspace subscription, cart archival on timeout |
| `util.go` | Data structures: `Cart`, `CartItem` structs |
| `Dockerfile` | Multi-stage Go build for minimal container image |
