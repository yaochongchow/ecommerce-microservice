package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/eventbridge"
	"github.com/aws/aws-sdk-go-v2/service/eventbridge/types"
)

var ebClient *eventbridge.Client

func InitEventBridge() error {
	cfg, err := config.LoadDefaultConfig(context.Background(),
		config.WithRegion(os.Getenv("AWS_REGION")),
	)
	if err != nil {
		return fmt.Errorf("failed to load config for EventBridge: %v", err)
	}
	ebClient = eventbridge.NewFromConfig(cfg)
	log.Println("EventBridge client initialized")
	return nil
}

func emitEvent(detailType string, detail any) error {
	detailJSON, err := json.Marshal(detail)
	if err != nil {
		return fmt.Errorf("failed to marshal event detail: %v", err)
	}

	_, err = ebClient.PutEvents(context.Background(), &eventbridge.PutEventsInput{
		Entries: []types.PutEventsRequestEntry{
			{
				Source:     aws.String("product-service"),
				DetailType: aws.String(detailType),
				Detail:     aws.String(string(detailJSON)),
				EventBusName: aws.String("default"),
			},
		},
	})
	return err
}

func EmitProductCreated(productID int, stock int) {
	err := emitEvent("ProductCreated", map[string]any{
		"productId": fmt.Sprintf("%d", productID),
		"stock":     stock,
	})
	if err != nil {
		log.Printf("Failed to emit ProductCreated for product %d: %v", productID, err)
	}
}

// EmitProductCreatedBatch sends ProductCreated events in batches of 10 (EventBridge max per call).
func EmitProductCreatedBatch(products map[int]Item, stocks map[int]int) {
	entries := make([]types.PutEventsRequestEntry, 0, 10)

	flush := func() {
		if len(entries) == 0 {
			return
		}
		_, err := ebClient.PutEvents(context.Background(), &eventbridge.PutEventsInput{
			Entries: entries,
		})
		if err != nil {
			log.Printf("Failed to batch emit ProductCreated: %v", err)
		}
		entries = entries[:0]
	}

	for id, stock := range stocks {
		detail, err := json.Marshal(map[string]any{
			"productId": fmt.Sprintf("%d", id),
			"stock":     stock,
		})
		if err != nil {
			log.Printf("Failed to marshal ProductCreated for product %d: %v", id, err)
			continue
		}
		entries = append(entries, types.PutEventsRequestEntry{
			Source:       aws.String("product-service"),
			DetailType:   aws.String("ProductCreated"),
			Detail:       aws.String(string(detail)),
			EventBusName: aws.String("default"),
		})
		if len(entries) == 10 {
			flush()
		}
	}
	flush()
	log.Printf("Emitted ProductCreated events for %d products", len(stocks))
}

func EmitProductRestocked(productID int, quantity int) {
	err := emitEvent("ProductRestocked", map[string]any{
		"productId": fmt.Sprintf("%d", productID),
		"quantity":  quantity,
	})
	if err != nil {
		log.Printf("Failed to emit ProductRestocked for product %d: %v", productID, err)
	}
}
