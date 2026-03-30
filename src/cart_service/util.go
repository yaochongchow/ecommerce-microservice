package main

import "time"

type CartItem struct {
	ProductID int `json:"product_id"`
	Quantity  int `json:"quantity"`
}

type Cart struct {
	CartID     string     `json:"cart_id"`
	UserID     string     `json:"user_id"`
	Items      []CartItem `json:"items"`
	CreatedAt  time.Time  `json:"created_at"`
	UpdatedAt  time.Time  `json:"updated_at"`
	CheckedOut bool       `json:"checked_out"`
}
