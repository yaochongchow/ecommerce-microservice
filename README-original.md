# E-Commerce Service

## Product Service

**Product generation:** up to 500 products seeded on first boot. (5000 products max can be generated)

### API Endpoints

#### `GET /products/`
Returns a paginated list of active products.

Query params:
- `limit` — number of items to return (default 20)
- `cursor` — opaque pagination token from a previous response's `next_cursor`

Example:
`GET "/products?limit=20"` and `"/products?limit=20&cursor={cursor}"`

> **Note:** `limit` is not guaranteed — DynamoDB Scan returns items in partition order. If some items in a page are inactive they are filtered out, so fewer than `limit` items may be returned.

```json
{
  "items": [
    {
      "product_id": 42,
      "price": 119.35,
      "name": "Apple Pro Black Macbook",
      "category": "Macbook",
      "color": "Black",
      "brand": "Apple",
      "is_active": true,
      "image_url": "https://..."
    }
  ],
  "next_cursor": "<opaque token>"
}
```

#### `GET /products/:id`
Returns product info based on provided id.

Response `200`:
```json
{
  "product_id": 42,
  "price": 119.35,
  "name": "Apple Pro Black Macbook",
  "category": "Macbook",
  "color": "Black",
  "brand": "Apple",
  "is_active": true,
  "image_url": "https://..."
}
```

Response `404`:
```json
{ "error": "NOT_FOUND", "message": "product 42 not found" }
```

#### `PUT /products/:id`
Updates product info. Only provided fields are updated.

Request body:
```json
{
  "price": 999.99,
  "category": "Macbook",
  "name": "Macbook",
  "brand": "Apple"
}
```

Response `200`:
```json
{ "message": "product 42 updated successfully" }
```

#### `POST /products/pricecheck`
Accepts a list of product IDs and returns their current prices.

Request body:
```json
{ "product_ids": [1, 42, 87] }
```

Response `200`:
```json
{
  "prices": {
    "1":  54.36,
    "42": 119.35,
    "87": 43.52
  }
}
```

#### `GET /products/search?q=<keyword>`
Searches products in memory by keyword against name, brand, category, and color.

Response `200`:
```json
    {
        "count": 2,
        "items": [
            {
                "product_id": 12,
                "price": 67.45,
                "name": "Nike Training Black Sneakers",
                "category": "Sneakers",
                "color": "Black",
                "brand": "Nike",
                "is_active": true,
                "image_url": "https://your-bucket.s3.us-east-1.amazonaws.com/products/nike_sneakers.jpg"
            },
            {
                "product_id": 87,
                "price": 43.20,
                "name": "Nike Everyday Blue Sneakers",
                "category": "Sneakers",
                "color": "Blue",
                "brand": "Nike",
                "is_active": true,
                "image_url": "https://your-bucket.s3.us-east-1.amazonaws.com/products/nike_sneakers.jpg"
            },
            
        ]
    }

```

All error response has format:
```json
{
  "error": "{error}",
  "message": "{error message}"
}
```

---

### DynamoDB Table Schema

| Field        | Type   |
|--------------|--------|
| `product_id` | number |
| `name`       | string |
| `price`      | number |
| `category`   | string |
| `color`      | string |
| `brand`      | string |
| `is_active`  | bool   |
| `image_url`  | string |

- **Partition key:** `product_id`
- **Sort key:** `category`

---

## Cart Service

### Functionality
- Storing and serving cart data
- Cart expires after 48 hours if not checked out (abandoned)
- Cart history is preserved after checkout or expiry

### API Endpoints

#### `GET /cart/:userId`
Returns the user's current active cart.

Response `200`:
```json
{
  "cart_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "user1",
  "items": [
    { "product_id": 1, "quantity": 2 }
  ],
  "created_at": "2026-03-30T00:00:00Z",
  "updated_at": "2026-03-30T01:00:00Z",
  "checked_out": false
}
```

#### `POST /cart/create/:userId`
Creates a new cart for the user. Returns `409 CONFLICT` if an active cart already exists.

#### `POST /cart/add/:userId`
Adds an item to the user's active cart. If the product is already in the cart, its quantity is incremented.

Request body:
```json
{ "product_id": 1, "quantity": 2 }
```

#### `DELETE /cart/delete/:userId`
Removes an item from the user's active cart.

Request body:
```json
{ "product_id": 1 }
```

#### `PUT /cart/update/:userId`
Updates the quantity of an item in the user's active cart.

Request body:
```json
{ "product_id": 1, "quantity": 5 }
```

#### `POST /cart/deactivate/:userId`
Checks out the active cart, moves it to cart history.

Response `200`:
```json
{
  "message": "cart 550e8400-... deactivated",
  "user_id": "user1",
  "cart_id": "550e8400-..."
}
```

#### `GET /cart/:userId/pricecheck`
Calls the product service for current prices and calculates the cart total.

Request body:
```json
{ "product_ids": [1,42,87] }
```

Response `200`:
```json
{
  "user_id": "user1",
  "cart_id": "550e8400-...",
  "items": [
    { "product_id": 1, "quantity": 2, "price": 54.36 }
  ],
  "total": 108.72
}
```

All error response has format:
```json
{
  "error": "{error}",
  "message": "{error message}"
}
```

---

### Redis Design

```
cart:active:<userId>   → cartID        (TTL: 48h — expires = abandoned cart)
cart:shadow:<userId>   → cartID        (no TTL — lets expiry listener recover cartID)
cart:data:<cartID>     → cart JSON     (no TTL — data outlives active key)
cart:history:<userId>  → list of cartIDs
```

When `cart:active:<userId>` expires, a Redis keyspace notification triggers an expiry listener that reads the shadow key to find the cartID, marks the cart as abandoned, and appends it to cart history.

---

## Deployment

A CDK stack, each deployed independently:

- **NetworkStack** — shared VPC and a single ALB with two port-based listeners:
  - Port `80` → Product Service
  - Port `8081` → Cart Service
- **DynamoStack** — DynamoDB `products` table
- **ProductServiceStack** — ECS Fargate service + S3 image bucket + SQS + EventBridge rules
- **CartServiceStack** — ECS Fargate service with a Redis sidecar container


---

