# Product Service

RESTful product catalog API built in **Go**. Serves product listings, search, and price lookups. Caches 5,000 products in memory for fast search while using DynamoDB as the source of truth.

Deployed as an **ECS Fargate** container behind an Application Load Balancer.

## DynamoDB Tables

| Table | Partition Key | Description |
|-------|--------------|-------------|
| `products` | `product_id` (Number) | Product catalog with 5,000 auto-seeded items |

### Product Schema

```json
{
  "product_id": 1,
  "name": "Nike Running Jacket",
  "price": 89.99,
  "category": "Apparel",
  "brand": "Nike",
  "color": "Black",
  "is_active": true,
  "image_url": "https://..."
}
```

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (`{ status: "healthy", database: "dynamodb" }`) |
| GET | `/products/` | Paginated product list (`?limit=20&cursor=...`) |
| GET | `/products/search?q=` | Keyword search across name, brand, category, color |
| GET | `/products/:id` | Get single product by ID |
| PUT | `/products/:id` | Update product fields |
| POST | `/products/pricecheck` | Batch price lookup for multiple product IDs |

### Pagination

```bash
# First page
curl /products/?limit=20

# Next page (use next_cursor from previous response)
curl /products/?limit=20&cursor=eyJwcm9kdWN0X2lkIjoyMH0=
```

### Batch Price Check

```bash
curl -X POST /products/pricecheck \
  -d '{ "product_ids": [1, 2, 3] }'
# Returns: { "prices": { "1": 89.99, "2": 129.99, "3": 34.99 } }
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region | — |
| `PRODUCTS_TABLE` | DynamoDB table name | `"products"` |
| `PORT` | HTTP server port | `8080` |

## Key Design Patterns

### In-Memory Cache

- All 5,000 products loaded into a `sync.Map` at startup
- Search queries (`/products/search`) hit the in-memory cache (no DB call)
- Product updates sync both DynamoDB and the in-memory map
- Single-product lookups and price checks query DynamoDB directly for freshness

### Auto-Seeding

On first startup, if the DynamoDB table is empty, generates and inserts 5,000 sample products with realistic data:
- Categories: Apparel, Electronics, Body Care, Jewelry
- Real brand names (Nike, Adidas, Apple, Samsung, etc.)
- 17 color options, category-specific subcategories
- Random pricing by category (electronics: $500-1000, others: $20-100)

### Docker Deployment

```dockerfile
FROM golang:1.23 AS builder
# Multi-stage build produces a minimal Alpine image
```

## Files

| File | Description |
|------|-------------|
| `main.go` | Entry point: DynamoDB init, seed/load products, Gin router setup |
| `handlers.go` | Route handlers: list, search, get, update, pricecheck |
| `dynamo.go` | DynamoDB operations: init, seed (batch write), load into memory |
| `util.go` | Product generation: brands, categories, colors, image URLs |
| `Dockerfile` | Multi-stage Go build for minimal container image |
