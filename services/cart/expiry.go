package main

import (
	"context"
	"log"
	"strings"
	"time"

	"github.com/go-redis/redis/v8"
)

// StartExpiryListener enables Redis keyspace notifications and subscribes to
// expired-key events. When a cart:active:<userID> key expires, the cart is
// archived to the user's history automatically.
func StartExpiryListener() {
	// Enable keyspace notifications: E = keyevent events, x = expired events
	if err := rdb.ConfigSet(ctx, "notify-keyspace-events", "Ex").Err(); err != nil {
		log.Printf("Warning: could not configure keyspace notifications: %v", err)
	}

	go func() {
		subClient := redis.NewClient(rdb.Options())
		defer subClient.Close()

		pubsub := subClient.Subscribe(context.Background(), "__keyevent@0__:expired")
		defer pubsub.Close()

		log.Println("Expiry listener started")
		for msg := range pubsub.Channel() {
			key := msg.Payload
			if strings.HasPrefix(key, ACTIVE_CART_KEY+":") {
				userID := strings.TrimPrefix(key, ACTIVE_CART_KEY+":")
				archiveExpiredCart(userID)
			}
		}
	}()
}

func archiveExpiredCart(userID string) {
	// Active key is gone; use shadow key to find the cart ID
	cartID, err := rdb.Get(ctx, shadowKey(userID)).Result()
	if err == redis.Nil {
		log.Printf("expiry: no shadow key for user %s, nothing to archive", userID)
		return
	}
	if err != nil {
		log.Printf("expiry: error reading shadow key for user %s: %v", userID, err)
		return
	}

	cart, err := getCartData(cartID)
	if err != nil || cart == nil {
		log.Printf("expiry: cart data not found for cartID %s, cleaning up shadow key", cartID)
		rdb.Del(ctx, shadowKey(userID))
		return
	}

	cart.UpdatedAt = time.Now()
	if err := saveCartData(cart); err != nil {
		log.Printf("expiry: failed to update cart %s timestamp: %v", cartID, err)
		return
	}

	if err := rdb.RPush(ctx, historyKey(userID), cartID).Err(); err != nil {
		log.Printf("expiry: failed to push cart %s to history for user %s: %v", cartID, userID, err)
		return
	}

	rdb.Del(ctx, shadowKey(userID))
	log.Printf("expiry: archived cart %s for user %s", cartID, userID)
}
