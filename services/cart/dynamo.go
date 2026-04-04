package main

import (
	"context"
	"log"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

const cartTTLDays = 7

var ddbClient *dynamodb.Client
var cartsTableName string

func InitDynamo() {
	cfg, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		log.Fatalf("failed to load AWS config: %v", err)
	}
	ddbClient = dynamodb.NewFromConfig(cfg)
	cartsTableName = os.Getenv("CARTS_TABLE")
	if cartsTableName == "" {
		log.Fatal("CARTS_TABLE env var not set")
	}
}

func getCartFromDB(userID string) (*Cart, error) {
	out, err := ddbClient.GetItem(context.Background(), &dynamodb.GetItemInput{
		TableName: aws.String(cartsTableName),
		Key: map[string]types.AttributeValue{
			"userId": &types.AttributeValueMemberS{Value: userID},
		},
	})
	if err != nil {
		return nil, err
	}
	if out.Item == nil {
		return nil, nil
	}
	var cart Cart
	if err := attributevalue.UnmarshalMap(out.Item, &cart); err != nil {
		return nil, err
	}
	return &cart, nil
}

func saveCartToDB(cart *Cart) error {
	cart.UpdatedAt = time.Now().Unix()
	cart.ExpiresAt = time.Now().Add(cartTTLDays * 24 * time.Hour).Unix()
	item, err := attributevalue.MarshalMap(cart)
	if err != nil {
		return err
	}
	_, err = ddbClient.PutItem(context.Background(), &dynamodb.PutItemInput{
		TableName: aws.String(cartsTableName),
		Item:      item,
	})
	return err
}

func deleteCartFromDB(userID string) error {
	_, err := ddbClient.DeleteItem(context.Background(), &dynamodb.DeleteItemInput{
		TableName: aws.String(cartsTableName),
		Key: map[string]types.AttributeValue{
			"userId": &types.AttributeValueMemberS{Value: userID},
		},
	})
	return err
}
