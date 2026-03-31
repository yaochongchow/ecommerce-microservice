package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"os"
	"strconv"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
)

var sqsClient *sqs.Client

type sqsMessage struct {
	Source     string          `json:"source"`
	DetailType string          `json:"detail-type"`
	Detail     json.RawMessage `json:"detail"`
}

type lowStockDetail struct {
	ProductID string `json:"productId"`
	Available int    `json:"available"`
}

type outOfStockDetail struct {
	ProductID string `json:"productId"`
	Available int    `json:"available"`
}

type stockReplenishedDetail struct {
	ProductID string `json:"productId"`
	Quantity  int    `json:"quantity"`
}

type restockFailedDetail struct {
	ProductID string `json:"productId"`
	Reason    string `json:"reason"`
}

func InitSQS() error {
	cfg, err := config.LoadDefaultConfig(context.Background(),
		config.WithRegion(os.Getenv("AWS_REGION")),
	)
	if err != nil {
		return fmt.Errorf("failed to load config for SQS: %v", err)
	}
	sqsClient = sqs.NewFromConfig(cfg)
	log.Println("SQS client initialized")
	return nil
}

// restockQuantity returns a random restock quantity between 15 and 30
func restockQuantity() int {
	return rand.Intn(16) + 15
}

func StartSQSListener() {
	queueURL := os.Getenv("SQS_QUEUE_URL")
	if queueURL == "" {
		log.Println("SQS_QUEUE_URL not set, skipping SQS listener")
		return
	}

	go func() {
		log.Println("SQS listener started")
		for {
			output, err := sqsClient.ReceiveMessage(context.Background(), &sqs.ReceiveMessageInput{
				QueueUrl:            aws.String(queueURL),
				MaxNumberOfMessages: 10,
				WaitTimeSeconds:     20, // long polling
			})
			if err != nil {
				log.Printf("SQS receive error: %v", err)
				continue
			}

			for _, msg := range output.Messages {
				// SQS message body is an SNS/EventBridge envelope — parse it
				var envelope struct {
					Message string `json:"Message"`
				}
				// EventBridge → SQS sends the event directly as the body
				var event sqsMessage
				if err := json.Unmarshal([]byte(*msg.Body), &envelope); err == nil && envelope.Message != "" {
					// SNS envelope wrapping
					json.Unmarshal([]byte(envelope.Message), &event)
				} else {
					json.Unmarshal([]byte(*msg.Body), &event)
				}

				handleEvent(event)

				sqsClient.DeleteMessage(context.Background(), &sqs.DeleteMessageInput{
					QueueUrl:      aws.String(queueURL),
					ReceiptHandle: msg.ReceiptHandle,
				})
			}
		}
	}()
}

func handleEvent(event sqsMessage) {
	switch event.DetailType {
	case "LowStock":
		var d lowStockDetail
		if err := json.Unmarshal(event.Detail, &d); err != nil {
			log.Printf("Failed to parse LowStock detail: %v", err)
			return
		}
		handleLowStock(d)

	case "OutOfStock":
		var d outOfStockDetail
		if err := json.Unmarshal(event.Detail, &d); err != nil {
			log.Printf("Failed to parse OutOfStock detail: %v", err)
			return
		}
		handleOutOfStock(d)

	case "StockReplenished":
		var d stockReplenishedDetail
		if err := json.Unmarshal(event.Detail, &d); err != nil {
			log.Printf("Failed to parse ProductRestockedSuccess detail: %v", err)
			return
		}
		handleStockReplenished(d)

	case "InventoryInitialized":
		var d restockFailedDetail
		if err := json.Unmarshal(event.Detail, &d); err != nil {
			log.Printf("Failed to parse InventoryInitialized detail: %v", err)
			return
		}
		if d.Reason == "product already exists in inventory" {
			log.Printf("InventoryInitialized: product %s already exists, ignoring duplicate", d.ProductID)
			return
		}

	case "ProductRestockedFailed":
		var d restockFailedDetail
		if err := json.Unmarshal(event.Detail, &d); err != nil {
			log.Printf("Failed to parse ProductRestockedFailed detail: %v", err)
			return
		}
		handleRestockFailed(d)

	default:
		log.Printf("Unknown event type: %s", event.DetailType)
	}
}

func handleLowStock(d lowStockDetail) {
	log.Printf("LowStock: product %s has %d available, restocking", d.ProductID, d.Available)
	id, err := strconv.Atoi(d.ProductID)
	if err != nil {
		log.Printf("LowStock: invalid productId %s", d.ProductID)
		return
	}
	quantity := restockQuantity()
	EmitProductRestocked(id, quantity)
}

func handleOutOfStock(d outOfStockDetail) {
	log.Printf("OutOfStock: product %s, marking inactive and restocking", d.ProductID)
	id, err := strconv.Atoi(d.ProductID)
	if err != nil {
		log.Printf("OutOfStock: invalid productId %s", d.ProductID)
		return
	}

	// Mark product inactive in DynamoDB and syncProducts
	setProductActive(id, false)

	quantity := restockQuantity()
	EmitProductRestocked(id, quantity)
}

func handleStockReplenished(d stockReplenishedDetail) {
	log.Printf("StockReplenished: product %s restocked with quantity %d", d.ProductID, d.Quantity)
	id, err := strconv.Atoi(d.ProductID)
	if err != nil {
		log.Printf("ProductRestockedSuccess: invalid productId %s", d.ProductID)
		return
	}

	// Only update if currently inactive
	if val, ok := syncProducts.Load(id); ok {
		if !val.(Item).IsActive {
			setProductActive(id, true)
		}
	}
}

func handleRestockFailed(d restockFailedDetail) {
	log.Printf("ProductRestockedFailed: product %s, reason: %s", d.ProductID, d.Reason)
	if d.Reason != "product not found in inventory" {
		return
	}

	id, err := strconv.Atoi(d.ProductID)
	if err != nil {
		log.Printf("ProductRestockedFailed: invalid productId %s", d.ProductID)
		return
	}

	// Check if product ID is within our generated range
	if _, ok := syncProducts.Load(id); !ok {
		log.Printf("ProductRestockedFailed: product %d not in our range, ignoring", id)
		return
	}

	// Product exists — emit ProductCreated first, then retry restock
	val, _ := syncProducts.Load(id)
	item := val.(Item)
	stock := rand.Intn(71) + 10 // random 10-80 as per seeding
	log.Printf("ProductRestockedFailed: emitting ProductCreated for product %d then retrying restock", id)
	_ = item
	EmitProductCreated(id, stock)

	time.Sleep(30 * time.Second)

	quantity := restockQuantity()
	EmitProductRestocked(id, quantity)
}
