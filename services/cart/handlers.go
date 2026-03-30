package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type AddItemRequest struct {
	ProductID int `json:"product_id"`
	Quantity  int `json:"quantity"`
}

type DeleteItemRequest struct {
	ProductID int `json:"product_id"`
}

type UpdateItemRequest struct {
	ProductID int `json:"product_id"`
	Quantity  int `json:"quantity"`
}

// getCart returns the user's active cart
func getCart(c *gin.Context) {
	userID := c.Param("userId")

	cart, err := getActiveCart(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "INTERNAL_SERVER_ERROR",
			"message": fmt.Sprintf("failed to get cart: %v", err),
		})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{
			"error":   "NOT_FOUND",
			"message": fmt.Sprintf("no active cart for user %s", userID),
		})
		return
	}

	c.IndentedJSON(http.StatusOK, cart)
}

// createCart creates a new active cart for the user
func createCart(c *gin.Context) {
	userID := c.Param("userId")

	existingID, err := getActiveCartID(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "INTERNAL_SERVER_ERROR",
			"message": fmt.Sprintf("failed to check existing cart: %v", err),
		})
		return
	}
	if existingID != "" {
		c.JSON(http.StatusConflict, gin.H{
			"error":   "CONFLICT",
			"message": fmt.Sprintf("user %s already has an active cart", userID),
		})
		return
	}

	now := time.Now()
	cart := Cart{
		CartID:     uuid.New().String(),
		UserID:     userID,
		Items:      []CartItem{},
		CreatedAt:  now,
		UpdatedAt:  now,
		CheckedOut: false,
	}

	if err := saveCartData(&cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "INTERNAL_SERVER_ERROR",
			"message": fmt.Sprintf("failed to save cart: %v", err),
		})
		return
	}

	// Active cart key expires after 2 days; cart data itself has no TTL
	if err := rdb.Set(ctx, activeCartKey(userID), cart.CartID, 48*time.Hour).Err(); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "INTERNAL_SERVER_ERROR",
			"message": fmt.Sprintf("failed to set active cart: %v", err),
		})
		return
	}

	// Shadow key holds the cartID permanently so the expiry listener can archive it
	if err := rdb.Set(ctx, shadowKey(userID), cart.CartID, 0).Err(); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "INTERNAL_SERVER_ERROR",
			"message": fmt.Sprintf("failed to set shadow key: %v", err),
		})
		return
	}

	c.IndentedJSON(http.StatusCreated, cart)
}

// addItem adds an item to the user's active cart
// If the product is already in the cart, its quantity is incremented
func addItem(c *gin.Context) {
	userID := c.Param("userId")

	var body AddItemRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "BAD_REQUEST",
			"message": fmt.Sprintf("invalid request body: %v", err),
		})
		return
	}

	cart, err := getActiveCart(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	found := false
	for i, item := range cart.Items {
		if item.ProductID == body.ProductID {
			cart.Items[i].Quantity += body.Quantity
			found = true
			break
		}
	}
	if !found {
		cart.Items = append(cart.Items, CartItem{ProductID: body.ProductID, Quantity: body.Quantity})
	}
	cart.UpdatedAt = time.Now()

	if err := saveCartData(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	c.IndentedJSON(http.StatusOK, cart)
}

// deleteItem removes an item from the user's active cart
func deleteItem(c *gin.Context) {
	userID := c.Param("userId")

	var body DeleteItemRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "BAD_REQUEST",
			"message": fmt.Sprintf("invalid request body: %v", err),
		})
		return
	}

	cart, err := getActiveCart(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	filtered := []CartItem{}
	for _, item := range cart.Items {
		if item.ProductID != body.ProductID {
			filtered = append(filtered, item)
		}
	}
	cart.Items = filtered
	cart.UpdatedAt = time.Now()

	if err := saveCartData(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	c.IndentedJSON(http.StatusOK, cart)
}

// updateItem updates the quantity of an item in the user's active cart
func updateItem(c *gin.Context) {
	userID := c.Param("userId")

	var body UpdateItemRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error":   "BAD_REQUEST",
			"message": fmt.Sprintf("invalid request body: %v", err),
		})
		return
	}

	cart, err := getActiveCart(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	found := false
	for i, item := range cart.Items {
		if item.ProductID == body.ProductID {
			cart.Items[i].Quantity = body.Quantity
			found = true
			break
		}
	}
	if !found {
		c.JSON(http.StatusNotFound, gin.H{
			"error":   "NOT_FOUND",
			"message": fmt.Sprintf("product %d not in cart", body.ProductID),
		})
		return
	}
	cart.UpdatedAt = time.Now()

	if err := saveCartData(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	c.IndentedJSON(http.StatusOK, cart)
}

// deactivateCart marks the active cart as checked out and moves it to history
func deactivateCart(c *gin.Context) {
	userID := c.Param("userId")

	cartID, err := getActiveCartID(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cartID == "" {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	cart, err := getCartData(cartID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	cart.CheckedOut = true
	cart.UpdatedAt = time.Now()

	if err := saveCartData(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	if err := rdb.RPush(ctx, historyKey(userID), cartID).Err(); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	if err := rdb.Del(ctx, activeCartKey(userID)).Err(); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	// Clean up shadow key on normal checkout (expiry listener won't need it)
	rdb.Del(ctx, shadowKey(userID))

	c.IndentedJSON(http.StatusOK, gin.H{"message": fmt.Sprintf("cart %s deactivated", cartID)})
}

// priceCheck fetches prices from the product service and calculates the cart total
func priceCheck(c *gin.Context) {
	userID := c.Param("userId")

	cart, err := getActiveCart(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}
	if len(cart.Items) == 0 {
		c.IndentedJSON(http.StatusOK, gin.H{"prices": map[string]float64{}, "total": 0})
		return
	}

	productIDs := make([]int, 0, len(cart.Items))
	for _, item := range cart.Items {
		productIDs = append(productIDs, item.ProductID)
	}

	reqBody, err := json.Marshal(map[string][]int{"product_ids": productIDs})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": "failed to build request"})
		return
	}

	productServiceURL := os.Getenv("PRODUCT_SERVICE_URL")
	resp, err := http.Post(productServiceURL+"/products/pricecheck", "application/json", bytes.NewBuffer(reqBody))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":   "INTERNAL_SERVER_ERROR",
			"message": fmt.Sprintf("failed to call product service: %v", err),
		})
		return
	}
	defer resp.Body.Close()

	var priceResp struct {
		Prices map[string]float64 `json:"prices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&priceResp); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": "failed to parse price response"})
		return
	}

	total := 0.0
	for _, item := range cart.Items {
		if price, ok := priceResp.Prices[strconv.Itoa(item.ProductID)]; ok {
			total += price * float64(item.Quantity)
		}
	}

	c.IndentedJSON(http.StatusOK, gin.H{
		"cart":   cart,
		"prices": priceResp.Prices,
		"total":  total,
	})
}
