package main

import (
	"log"
	"sync"
    "context"
	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
    "github.com/aws/aws-sdk-go-v2/aws"
    "github.com/aws/aws-sdk-go-v2/service/dynamodb"
)

var syncProducts sync.Map

func main() {
	// Load .env file
    if err := godotenv.Load(); err != nil {
        log.Println("No .env file found, using system environment variables")
    }

	// Initialize DynamoDB connection
	log.Println("Initializing DynamoDB...")
	if err := InitDynamoDB(); err != nil {
		log.Fatalf("Failed to initialize DynamoDB: %v", err)
	}

	// Check if products table is already seeded before generating
    ctx := context.Background()
    result, err := dynamoClient.Scan(ctx, &dynamodb.ScanInput{
        TableName: aws.String(productsTable),
        Limit:     aws.Int32(1),
    })
    if err != nil {
        log.Fatalf("Failed to scan products table: %v", err)
    }

    var products map[int]Item
    if len(result.Items) == 0 {
        log.Println("Products table empty, generating and seeding...")
        products = GenerateProducts(5000)
        if err := SeedData(products); err != nil {
            log.Printf("Warning: failed to seed data: %v", err)
        }
        for k, v := range products {
            syncProducts.Store(k, v)
        }
        printSample(products, 10)
        log.Printf("Total products: %d", len(products))
    } else {
        log.Println("Products already seeded, loading into memory...")
        loaded, err := LoadProductsIntoMemory()
        if err != nil {
            log.Fatalf("Failed to load products into memory: %v", err)
        }
        for k, v := range loaded {
            syncProducts.Store(k, v)
        }
        log.Printf("Loaded %d products into memory", len(loaded))
    }

	// initialize Gin router using Default
	router := gin.Default()

	// Health endpoint - checks DynamoDB connection
	router.GET("/health", func(c *gin.Context) {
		c.JSON(200, gin.H{
			"status":   "healthy",
			"database": "dynamodb",
		})
	})

	// product service endpoints
	router.GET("/products/", getProductList)
	router.GET("/products/search", searchProducts)
	router.POST("/products/pricecheck", priceCheck)
	router.GET("/products/:id", getProductById)
	router.PUT("/products/:id", updateProductById)
	
	// "Run()" attaches router to an http server and start the server
	router.Run(":8080")
}
