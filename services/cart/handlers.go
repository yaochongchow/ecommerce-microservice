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

func getCart(c *gin.Context) {
	userID := c.Param("userId")
	cart, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("failed to get cart: %v", err)})
		return
	}
	if cart == nil {
		cart = &Cart{
			CartID:    uuid.New().String(),
			UserID:    userID,
			Items:     []CartItem{},
			CreatedAt: time.Now().Unix(),
		}
		if err := saveCartToDB(cart); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("failed to create cart: %v", err)})
			return
		}
	}
	c.IndentedJSON(http.StatusOK, cart)
}

func createCart(c *gin.Context) {
	userID := c.Param("userId")
	existing, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("failed to check existing cart: %v", err)})
		return
	}
	if existing != nil {
		c.JSON(http.StatusConflict, gin.H{"error": "CONFLICT", "message": fmt.Sprintf("user %s already has an active cart", userID)})
		return
	}

	cart := Cart{
		CartID:    uuid.New().String(),
		UserID:    userID,
		Items:     []CartItem{},
		CreatedAt: time.Now().Unix(),
	}
	if err := saveCartToDB(&cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("failed to save cart: %v", err)})
		return
	}
	c.IndentedJSON(http.StatusCreated, cart)
}

func addItem(c *gin.Context) {
	userID := c.Param("userId")
	var body AddItemRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "BAD_REQUEST", "message": fmt.Sprintf("invalid request body: %v", err)})
		return
	}

	cart, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		cart = &Cart{
			CartID:    uuid.New().String(),
			UserID:    userID,
			Items:     []CartItem{},
			CreatedAt: time.Now().Unix(),
		}
	}

	pid := strconv.Itoa(body.ProductID)
	found := false
	for i, item := range cart.Items {
		if item.ProductID == pid {
			cart.Items[i].Quantity += body.Quantity
			found = true
			break
		}
	}
	if !found {
		cart.Items = append(cart.Items, CartItem{ProductID: pid, Quantity: body.Quantity})
	}

	if err := saveCartToDB(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	c.IndentedJSON(http.StatusOK, cart)
}

func deleteItem(c *gin.Context) {
	userID := c.Param("userId")
	var body DeleteItemRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "BAD_REQUEST", "message": fmt.Sprintf("invalid request body: %v", err)})
		return
	}

	cart, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	pid := strconv.Itoa(body.ProductID)
	filtered := []CartItem{}
	for _, item := range cart.Items {
		if item.ProductID != pid {
			filtered = append(filtered, item)
		}
	}
	cart.Items = filtered

	if err := saveCartToDB(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	c.IndentedJSON(http.StatusOK, cart)
}

func updateItem(c *gin.Context) {
	userID := c.Param("userId")
	var body UpdateItemRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "BAD_REQUEST", "message": fmt.Sprintf("invalid request body: %v", err)})
		return
	}

	cart, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	pid := strconv.Itoa(body.ProductID)
	found := false
	for i, item := range cart.Items {
		if item.ProductID == pid {
			cart.Items[i].Quantity = body.Quantity
			found = true
			break
		}
	}
	if !found {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("product %d not in cart", body.ProductID)})
		return
	}

	if err := saveCartToDB(cart); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	c.IndentedJSON(http.StatusOK, cart)
}

func deactivateCart(c *gin.Context) {
	userID := c.Param("userId")
	cart, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}

	cartID := cart.CartID
	if err := deleteCartFromDB(userID); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}

	c.IndentedJSON(http.StatusOK, gin.H{
		"message": fmt.Sprintf("cart %s deactivated", cartID),
		"user_id": userID,
		"cart_id": cartID,
	})
}

func priceCheck(c *gin.Context) {
	userID := c.Param("userId")
	cart, err := getCartFromDB(userID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("%v", err)})
		return
	}
	if cart == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "NOT_FOUND", "message": fmt.Sprintf("no active cart for user %s", userID)})
		return
	}
	if len(cart.Items) == 0 {
		c.IndentedJSON(http.StatusOK, gin.H{"user_id": userID, "cart_id": cart.CartID, "items": []interface{}{}, "total": 0})
		return
	}

	productIDs := make([]int, 0, len(cart.Items))
	for _, item := range cart.Items {
		pid, _ := strconv.Atoi(item.ProductID)
		productIDs = append(productIDs, pid)
	}

	reqBody, err := json.Marshal(map[string][]int{"product_ids": productIDs})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": "failed to build request"})
		return
	}

	productServiceURL := os.Getenv("PRODUCT_SERVICE_URL")
	resp, err := http.Post(productServiceURL+"/products/pricecheck", "application/json", bytes.NewBuffer(reqBody))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "INTERNAL_SERVER_ERROR", "message": fmt.Sprintf("failed to call product service: %v", err)})
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

	type PricedItem struct {
		ProductID string  `json:"product_id"`
		Quantity  int     `json:"quantity"`
		Price     float64 `json:"price"`
	}

	total := 0.0
	pricedItems := make([]PricedItem, 0, len(cart.Items))
	for _, item := range cart.Items {
		price := priceResp.Prices[item.ProductID]
		total += price * float64(item.Quantity)
		pricedItems = append(pricedItems, PricedItem{ProductID: item.ProductID, Quantity: item.Quantity, Price: price})
	}

	c.IndentedJSON(http.StatusOK, gin.H{
		"user_id": cart.UserID,
		"cart_id": cart.CartID,
		"items":   pricedItems,
		"total":   total,
	})
}
