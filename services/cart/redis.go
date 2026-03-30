package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"

	"github.com/go-redis/redis/v8"
)

var (
	rdb *redis.Client
	ctx = context.Background()
)

const (
	ACTIVE_CART_KEY   = "cart:active"
	INACTIVE_CART_KEY = "cart:history"
	CART_DATA_KEY     = "cart:data"
	SHADOW_KEY        = "cart:shadow"
)

func activeCartKey(userID string) string {
	return fmt.Sprintf("%s:%s", ACTIVE_CART_KEY, userID)
}

func historyKey(userID string) string {
	return fmt.Sprintf("%s:%s", INACTIVE_CART_KEY, userID)
}

func cartDataKey(cartID string) string {
	return fmt.Sprintf("%s:%s", CART_DATA_KEY, cartID)
}

func shadowKey(userID string) string {
	return fmt.Sprintf("%s:%s", SHADOW_KEY, userID)
}

func InitRedis() {
	redisAddr := os.Getenv("REDIS_ADDR")
	if redisAddr == "" {
		redisAddr = "localhost:6379"
	}

	rdb = redis.NewClient(&redis.Options{
		Addr:     redisAddr,
		Password: "",
		DB:       0,
	})

	_, err := rdb.Ping(ctx).Result()
	if err != nil {
		log.Fatal("Failed to connect to Redis:", err)
	}
	log.Println("Connected to Redis at", redisAddr)
}

func CloseRedis() {
	if rdb != nil {
		rdb.Close()
		log.Println("Closed Redis connection")
	}
}

// getActiveCartID returns the active cart ID for a user, or "" if none exists
func getActiveCartID(userID string) (string, error) {
	cartID, err := rdb.Get(ctx, activeCartKey(userID)).Result()
	if err == redis.Nil {
		return "", nil
	}
	return cartID, err
}

// getCartData fetches and deserializes a cart by cart ID
func getCartData(cartID string) (*Cart, error) {
	data, err := rdb.Get(ctx, cartDataKey(cartID)).Result()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var cart Cart
	if err := json.Unmarshal([]byte(data), &cart); err != nil {
		return nil, err
	}
	return &cart, nil
}

// saveCartData serializes and saves a cart (no TTL — cart data is permanent)
func saveCartData(cart *Cart) error {
	data, err := json.Marshal(cart)
	if err != nil {
		return err
	}
	return rdb.Set(ctx, cartDataKey(cart.CartID), data, 0).Err()
}

// getActiveCart is a convenience helper combining getActiveCartID + getCartData
func getActiveCart(userID string) (*Cart, error) {
	cartID, err := getActiveCartID(userID)
	if err != nil || cartID == "" {
		return nil, err
	}
	return getCartData(cartID)
}
