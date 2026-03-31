package main

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"os"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

var (
	dynamoClient    *dynamodb.Client
	productsTable   string
)

type ProductItem struct {
	ID       int     `dynamodbav:"product_id"`
	Price    float64 `dynamodbav:"price"`
	Name     string  `dynamodbav:"name"`
	Category string  `dynamodbav:"category"`
	Color    string  `dynamodbav:"color"`
	Brand    string  `dynamodbav:"brand"`
	IsActive bool    `dynamodbav:"is_active"`
	ImageURL string  `dynamodbav:"image_url"`
}


// type CustomerItem struct {
// 	CustomerID int    `dynamodbav:"customer_id"`
// 	Name       string `dynamodbav:"name"`
// 	Email      string `dynamodbav:"email"`
// 	CreatedAt  string `dynamodbav:"created_at"`
// }

// InitDynamoDB initializes the DynamoDB client and table names
func InitDynamoDB() error {
	ctx := context.Background()

	// Load AWS configuration
	cfg, err := config.LoadDefaultConfig(ctx,
		config.WithRegion(os.Getenv("AWS_REGION")),
	)
	if err != nil {
		return fmt.Errorf("unable to load SDK config: %v", err)
	}

	dynamoClient = dynamodb.NewFromConfig(cfg)

	// Get table names from environment
	productsTable = os.Getenv("PRODUCTS_TABLE")

	if productsTable == "" {
		return fmt.Errorf("table names not set in environment variables")
	}

	log.Printf("DynamoDB initialized with tables: %s", 
		productsTable)

	return nil
}

// SeedData populates DynamoDB with sample data using your existing GenerateProducts function
func SeedData(productsMap map[int]Item) error {
	ctx := context.Background()

	log.Println("Seeding DynamoDB tables...")

	
	log.Printf("Starting batch write to DynamoDB...")

	// Convert map to slice and batch write (max 25 items per batch)
	batchCount := 0
	writeRequests := make([]types.WriteRequest, 0, 25)
	
	for _, product := range productsMap {
		// Convert Item struct to DynamoDB ProductItem format (same structure, just with dynamodb tags)
		dynamoProduct := ProductItem{
			ID:       product.ID,
			Price:    product.Price,
			Name:     product.Name,
			Category: product.Category,
			Color:    product.Color,
			Brand:    product.Brand,
			IsActive: product.IsActive,
			ImageURL: product.ImageURL,
		}
		
		item, err := attributevalue.MarshalMap(dynamoProduct)
		if err != nil {
			log.Printf("Warning: failed to marshal product %d: %v", product.ID, err)
			continue
		}

		writeRequests = append(writeRequests, types.WriteRequest{
			PutRequest: &types.PutRequest{
				Item: item,
			},
		})

		// When we have 25 items, write the batch
		if len(writeRequests) == 25 {
			_, err := dynamoClient.BatchWriteItem(ctx, &dynamodb.BatchWriteItemInput{
				RequestItems: map[string][]types.WriteRequest{
					productsTable: writeRequests,
				},
			})
			if err != nil {
				log.Printf("Warning: failed to batch write products: %v", err)
			}
			
			batchCount++
			if batchCount%100 == 0 {
				log.Printf("Seeded %d products...", batchCount*25)
			}
			
			// Reset for next batch
			writeRequests = make([]types.WriteRequest, 0, 25)
		}
	}
	
	// Write any remaining items
	if len(writeRequests) > 0 {
		_, err := dynamoClient.BatchWriteItem(ctx, &dynamodb.BatchWriteItemInput{
			RequestItems: map[string][]types.WriteRequest{
				productsTable: writeRequests,
			},
		})
		if err != nil {
			log.Printf("Warning: failed to batch write final products: %v", err)
		}
		batchCount++
	}

	log.Printf("Database seeding completed! Seeded %d products in %d batches", len(productsMap), batchCount)

	// Emit ProductCreated for each seeded product in batches of 10
	log.Println("Emitting ProductCreated events...")
	stocks := make(map[int]int, len(productsMap))
	for _, product := range productsMap {
		stocks[product.ID] = rand.Intn(71) + 10 // random 10-80
	}
	EmitProductCreatedBatch(productsMap, stocks)

	return nil
}

// setProductActive updates is_active in DynamoDB and syncProducts
func setProductActive(id int, active bool) {
	ctx := context.Background()
	val, ok := syncProducts.Load(id)
	if !ok {
		log.Printf("setProductActive: product %d not in memory, cannot update", id)
		return
	}
	category := val.(Item).Category

	_, err := dynamoClient.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName: aws.String(productsTable),
		Key: map[string]types.AttributeValue{
			"product_id": &types.AttributeValueMemberN{Value: fmt.Sprintf("%d", id)},
			"category":   &types.AttributeValueMemberS{Value: category},
		},
		UpdateExpression: aws.String("SET is_active = :active"),
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":active": &types.AttributeValueMemberBOOL{Value: active},
		},
	})
	if err != nil {
		log.Printf("setProductActive: failed to update product %d: %v", id, err)
		return
	}
	if val, ok := syncProducts.Load(id); ok {
		item := val.(Item)
		item.IsActive = active
		syncProducts.Store(id, item)
	}
}

// LoadProductsIntoMemory scans all products from DynamoDB and returns them as a map
func LoadProductsIntoMemory() (map[int]Item, error) {
	ctx := context.Background()
	products := make(map[int]Item)
	var lastKey map[string]types.AttributeValue

	for {
		input := &dynamodb.ScanInput{
			TableName:         aws.String(productsTable),
			ExclusiveStartKey: lastKey,
		}
		result, err := dynamoClient.Scan(ctx, input)
		if err != nil {
			return nil, err
		}
		var items []Item
		if err := attributevalue.UnmarshalListOfMaps(result.Items, &items); err != nil {
			return nil, err
		}
		for _, item := range items {
			products[item.ID] = item
		}
		lastKey = result.LastEvaluatedKey
		if lastKey == nil {
			break
		}
	}
	return products, nil
}