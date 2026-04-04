package main

type CartItem struct {
	ProductID string `json:"product_id" dynamodbav:"productId"`
	Quantity  int    `json:"quantity"   dynamodbav:"quantity"`
}

type Cart struct {
	UserID     string     `json:"user_id"     dynamodbav:"userId"`
	CartID     string     `json:"cart_id"     dynamodbav:"cart_id"`
	Items      []CartItem `json:"items"       dynamodbav:"items"`
	CreatedAt  int64      `json:"created_at"  dynamodbav:"createdAt"`
	UpdatedAt  int64      `json:"updated_at"  dynamodbav:"updatedAt"`
	CheckedOut bool       `json:"checked_out" dynamodbav:"checkedOut"`
	ExpiresAt  int64      `json:"-"           dynamodbav:"expiresAt"`
}
