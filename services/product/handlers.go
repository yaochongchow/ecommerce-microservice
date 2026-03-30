package main

import (
    "errors"
    "net/http"
    "strconv"
    "strings"
    "fmt"
    "github.com/gin-gonic/gin"
    "github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"
    "github.com/aws/aws-sdk-go-v2/service/dynamodb"
    "github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
    "github.com/aws/aws-sdk-go-v2/aws"
)

type PriceCheckRequest struct {
    ProductIDs []int `json:"product_ids"`
}

type UpdateProductRequest struct {
    Price    *float64 `json:"price"`
    Name     *string  `json:"name"`
    Category *string  `json:"category"`
    Color    *string  `json:"color"`
    Brand    *string  `json:"brand"`
    IsActive *bool    `json:"is_active"`
    ImageURL *string  `json:"image_url"`
}

// getProductList returns a paginated list of products
// Query params: limit (default 20), cursor (optional, from previous response's next_cursor)
func getProductList(c *gin.Context) {

    defer func() {
        if r := recover(); r != nil {
            c.JSON(http.StatusInternalServerError, gin.H{
                "error":   "INTERNAL_SERVER_ERROR",
                "message": "something went wrong",
                "details": fmt.Sprintf("%v", r),
            })
        }
    }()

    // Parse limit
    limit := 20
    if l := c.Query("limit"); l != "" {
        if parsed, err := strconv.Atoi(l); err == nil && parsed > 0 {
            limit = parsed
        }
    }

    // Build Scan input — only return active products
    scanInput := &dynamodb.ScanInput{
        TableName:        aws.String(productsTable),
        Limit:            aws.Int32(int32(limit)),
        FilterExpression: aws.String("is_active = :active"),
        ExpressionAttributeValues: map[string]types.AttributeValue{
            ":active": &types.AttributeValueMemberBOOL{Value: true},
        },
    }

    // If cursor is provided, use it as ExclusiveStartKey
    if cursor := c.Query("cursor"); cursor != "" {
        scanInput.ExclusiveStartKey = map[string]types.AttributeValue{
            "product_id": &types.AttributeValueMemberN{Value: cursor},
        }
    }

    result, err := dynamoClient.Scan(c.Request.Context(), scanInput)
    if err != nil {
        c.JSON(http.StatusInternalServerError, gin.H{
            "error":   "INTERNAL_SERVER_ERROR",
            "message": fmt.Sprintf("failed to scan products: %v", err),
        })
        return
    }

    // Unmarshal results
    var products []Item
    if err := attributevalue.UnmarshalListOfMaps(result.Items, &products); err != nil {
        c.JSON(http.StatusInternalServerError, gin.H{
            "error":   "INTERNAL_SERVER_ERROR",
            "message": fmt.Sprintf("failed to unmarshal products: %v", err),
        })
        return
    }

    // Build next cursor from LastEvaluatedKey
    var nextCursor string
    if result.LastEvaluatedKey != nil {
        if v, ok := result.LastEvaluatedKey["product_id"].(*types.AttributeValueMemberN); ok {
            nextCursor = v.Value
        }
    }

    c.IndentedJSON(http.StatusOK, gin.H{
        "items":       products,
        "next_cursor": nextCursor,
    })
}


// searchProducts searches products in memory by keyword against name, brand, and category
// Query params: q (required, search keyword)
func searchProducts(c *gin.Context) {
    keyword := strings.ToLower(c.Query("q"))
    if keyword == "" {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": "query parameter 'q' is required",
        })
        return
    }

    var results []Item
    syncProducts.Range(func(_, value any) bool {
        item := value.(Item)
        if strings.Contains(strings.ToLower(item.Name), keyword) ||
            strings.Contains(strings.ToLower(item.Brand), keyword) ||
            strings.Contains(strings.ToLower(item.Category), keyword) ||
            strings.Contains(strings.ToLower(item.Color), keyword) {
            results = append(results, item)
        }
        return true
    })

    c.IndentedJSON(http.StatusOK, gin.H{
        "items": results,
        "count": len(results),
    })
}

// getProductById retrieves a single product by its ID
func getProductById(c *gin.Context) {
    idStr := c.Param("id")
    id, err := strconv.Atoi(idStr)
    if err != nil {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": "product id must be a number",
        })
        return
    }

    result, err := dynamoClient.GetItem(c.Request.Context(), &dynamodb.GetItemInput{
        TableName: aws.String(productsTable),
        Key: map[string]types.AttributeValue{
            "product_id": &types.AttributeValueMemberN{Value: strconv.Itoa(id)},
        },
    })
    if err != nil {
        c.JSON(http.StatusInternalServerError, gin.H{
            "error":   "INTERNAL_SERVER_ERROR",
            "message": fmt.Sprintf("failed to get product: %v", err),
        })
        return
    }

    if result.Item == nil {
        c.JSON(http.StatusNotFound, gin.H{
            "error":   "NOT_FOUND",
            "message": fmt.Sprintf("product %d not found", id),
        })
        return
    }

    var product Item
    if err := attributevalue.UnmarshalMap(result.Item, &product); err != nil {
        c.JSON(http.StatusInternalServerError, gin.H{
            "error":   "INTERNAL_SERVER_ERROR",
            "message": fmt.Sprintf("failed to unmarshal product: %v", err),
        })
        return
    }

    c.IndentedJSON(http.StatusOK, product)
}

// updateProductById updates a product's attributes by its ID
// Accepts a JSON body with any subset of product fields — only provided fields are updated
func updateProductById(c *gin.Context) {
    idStr := c.Param("id")
    id, err := strconv.Atoi(idStr)
    if err != nil {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": "product id must be a number",
        })
        return
    }

    var body UpdateProductRequest
    if err := c.ShouldBindJSON(&body); err != nil {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": fmt.Sprintf("invalid request body: %v", err),
        })
        return
    }

    // Build update expression dynamically from non-nil fields
    setClauses := []string{}
    exprValues := map[string]types.AttributeValue{}
    exprNames := map[string]string{}

    if body.Name != nil {
        setClauses = append(setClauses, "#name = :name")
        exprNames["#name"] = "name" // "name" is a reserved word in DynamoDB
        exprValues[":name"] = &types.AttributeValueMemberS{Value: *body.Name}
    }
    if body.Price != nil {
        setClauses = append(setClauses, "price = :price")
        exprValues[":price"] = &types.AttributeValueMemberN{Value: fmt.Sprintf("%f", *body.Price)}
    }
    if body.Category != nil {
        setClauses = append(setClauses, "category = :category")
        exprValues[":category"] = &types.AttributeValueMemberS{Value: *body.Category}
    }
    if body.Color != nil {
        setClauses = append(setClauses, "color = :color")
        exprValues[":color"] = &types.AttributeValueMemberS{Value: *body.Color}
    }
    if body.Brand != nil {
        setClauses = append(setClauses, "brand = :brand")
        exprValues[":brand"] = &types.AttributeValueMemberS{Value: *body.Brand}
    }
    if body.IsActive != nil {
        setClauses = append(setClauses, "is_active = :is_active")
        exprValues[":is_active"] = &types.AttributeValueMemberBOOL{Value: *body.IsActive}
    }
    if body.ImageURL != nil {
        setClauses = append(setClauses, "image_url = :image_url")
        exprValues[":image_url"] = &types.AttributeValueMemberS{Value: *body.ImageURL}
    }

    if len(setClauses) == 0 {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": "no fields provided to update",
        })
        return
    }

    updateInput := &dynamodb.UpdateItemInput{
        TableName: aws.String(productsTable),
        Key: map[string]types.AttributeValue{
            "product_id": &types.AttributeValueMemberN{Value: strconv.Itoa(id)},
        },
        UpdateExpression:          aws.String("SET " + strings.Join(setClauses, ", ")),
        ExpressionAttributeValues: exprValues,
        ConditionExpression:       aws.String("attribute_exists(product_id)"),
    }
    if len(exprNames) > 0 {
        updateInput.ExpressionAttributeNames = exprNames
    }

    _, err = dynamoClient.UpdateItem(c.Request.Context(), updateInput)
    if err != nil {
        var ccf *types.ConditionalCheckFailedException
        if errors.As(err, &ccf) {
            c.JSON(http.StatusNotFound, gin.H{
                "error":   "NOT_FOUND",
                "message": fmt.Sprintf("product %d not found", id),
            })
        } else {
            c.JSON(http.StatusInternalServerError, gin.H{
                "error":   "INTERNAL_SERVER_ERROR",
                "message": fmt.Sprintf("failed to update product: %v", err),
            })
        }
        return
    }

    // Update syncProducts to keep in-memory search consistent
    if val, ok := syncProducts.Load(id); ok {
        existing := val.(Item)
        if body.Name != nil     { existing.Name = *body.Name }
        if body.Price != nil    { existing.Price = *body.Price }
        if body.Category != nil { existing.Category = *body.Category }
        if body.Color != nil    { existing.Color = *body.Color }
        if body.Brand != nil    { existing.Brand = *body.Brand }
        if body.IsActive != nil { existing.IsActive = *body.IsActive }
        if body.ImageURL != nil { existing.ImageURL = *body.ImageURL }
        syncProducts.Store(id, existing)
    }

    c.IndentedJSON(http.StatusOK, gin.H{
        "message": fmt.Sprintf("product %d updated successfully", id),
    })
}

// priceCheck accepts a list of product IDs and returns their current prices
func priceCheck(c *gin.Context) {
    var body PriceCheckRequest
    if err := c.ShouldBindJSON(&body); err != nil {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": fmt.Sprintf("invalid request body: %v", err),
        })
        return
    }

    if len(body.ProductIDs) == 0 {
        c.JSON(http.StatusBadRequest, gin.H{
            "error":   "BAD_REQUEST",
            "message": "product_ids must not be empty",
        })
        return
    }

    // Build keys for BatchGetItem
    keys := make([]map[string]types.AttributeValue, 0, len(body.ProductIDs))
    for _, id := range body.ProductIDs {
        keys = append(keys, map[string]types.AttributeValue{
            "product_id": &types.AttributeValueMemberN{Value: strconv.Itoa(id)},
        })
    }

    result, err := dynamoClient.BatchGetItem(c.Request.Context(), &dynamodb.BatchGetItemInput{
        RequestItems: map[string]types.KeysAndAttributes{
            productsTable: {
                Keys:                 keys,
                ProjectionExpression: aws.String("product_id, price"),
            },
        },
    })
    if err != nil {
        c.JSON(http.StatusInternalServerError, gin.H{
            "error":   "INTERNAL_SERVER_ERROR",
            "message": fmt.Sprintf("failed to get prices: %v", err),
        })
        return
    }

    // Build response map of product_id -> price
    prices := map[string]float64{}
    for _, item := range result.Responses[productsTable] {
        var product Item
        if err := attributevalue.UnmarshalMap(item, &product); err != nil {
            continue
        }
        prices[strconv.Itoa(product.ID)] = product.Price
    }

    c.IndentedJSON(http.StatusOK, gin.H{
        "prices": prices,
    })
}




