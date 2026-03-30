package main

import (
	"log"

	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
)

func main() {
	if err := godotenv.Load(); err != nil {
		log.Println("No .env file found, using system environment variables")
	}

	InitRedis()
	defer CloseRedis()
	StartExpiryListener()

	router := gin.Default()

	router.GET("/health", func(c *gin.Context) {
		c.JSON(200, gin.H{"status": "healthy", "cache": "redis"})
	})

	router.GET("/cart/:userId", getCart)
	router.POST("/cart/create/:userId", createCart)
	router.POST("/cart/add/:userId", addItem)
	router.PUT("/cart/delete/:userId", deleteItem)
	router.PUT("/cart/update/:userId", updateItem)
	router.PUT("/cart/deactivate/:userId", deactivateCart)
	router.GET("/cart/:userId/pricecheck", priceCheck)

	router.Run(":8080")
}
