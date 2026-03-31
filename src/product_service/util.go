package main

import (
	"fmt"
	"log"
	"math"
	"math/rand"
	"strings"
)


// item represents data about a product item.
// (item struct used to store product item data in memory)
// struct tag (e.g. `json:"artist"`) specify what a field's name
// should be when the struct's contents are serialized into JSON.
type Item struct {
	ID       int     `json:"product_id" dynamodbav:"product_id"`
	Price    float64 `json:"price"      dynamodbav:"price"`
	Name     string  `json:"name"       dynamodbav:"name"`
	Category string  `json:"category"   dynamodbav:"category"`
	Color    string  `json:"color"      dynamodbav:"color"`
	Brand    string  `json:"brand"      dynamodbav:"brand"`
	IsActive bool    `json:"is_active"  dynamodbav:"is_active"`
	ImageURL string  `json:"image_url"  dynamodbav:"image_url"`
}

var categoryToBrand = make(map[string][]string)


var colors = []string{
	"Red", "Pink", "Blue", "Slate", "Purple", "Silver", "Gold", "Rose Gold",
	"Black", "White", "Yellow", "Green", "Navy", "Teal", "Brown", "Grey",
	"Maroon", "Burgundy", "Orange",
}

var categories = make(map[string][]string)
var adjectives = make(map[string][]string)
var productSet = make(map[string]struct{})

// brandSpecificImages is the set of brand+subcategory combos that have dedicated images
var brandSpecificImages = map[string]struct{}{
	"nike_sneakers":          {},
	"adidas_sneakers":        {},
	"new_balance_sneakers":   {},
	"vans_sneakers":          {},
	"apple_phone":            {},
	"samsung_phone":          {},
	"nike_jacket":            {},
	"adidas_hoodie":          {},
	"the_north_face_jacket":  {},
}

func getImageKey(brand, subcategory string) string {
	key := strings.ToLower(strings.ReplaceAll(brand, " ", "_")) + "_" + strings.ToLower(strings.ReplaceAll(subcategory, " ", "_"))
	if _, exists := brandSpecificImages[key]; exists {
		return key
	}
	return strings.ToLower(strings.ReplaceAll(subcategory, " ", "_"))
}

func GenerateProducts(count int) map[int]Item {
	// rand.Seed(time.Now().UnixNano())

	categoryToBrand["Apparel"] = []string{"Lululemon", "Nike", "Adidas",
										"Patagonia", "The North Face", "New Balance", 
										"Vans"}
	categoryToBrand["Electronics"] = []string{"Apple", "Samsung"}
	categoryToBrand["Macbook"] = []string{"Apple"}
	categoryToBrand["Sneakers"] = []string{"Nike", "Adidas", "New Balance", "Vans"}
	categoryToBrand["Body Care"] = []string{"Jo Malone","Byredo", "Diptyque", "L'Occitane"}
	categoryToBrand["Sunglasses"] = []string{"Gentle Monster", "Ray Ban","Oakley"}
	categoryToBrand["Jewlery"] = []string{"Tiffany", "Cartier", "Bulgari", "Mejuri"}

	categories["Macbook"] = []string{"Laptop"}
	categories["Apparel"] = []string{"Jacket", "Sweater", "Beanie",
									"Hoodie", "T-Shirt", "Shirt", 
									"Shoes", "Hat", "Dress", "Backpack"}
	categories["Electronics"] = []string{"Headphones", "Phone", "Smart Watch"}
	categories["Body Care"] = []string{"Candle", "Body Oil", "Body Lotion", "Perfume"}
	categories["Jewlery"] = []string{"Ring", "Necklace", "Bracelet"}

	adjectives["Apparel"] = []string{"Studio", "Align", "Softreme",
									"Scuba", "Effortless", "Groove",
									"Everyday", "Define", "Wonder", "Wide Fit", "Petite Fit"}
	adjectives["Electronics"] = []string{"Pro", "Lite", "Mini", "Max", "New Generation"}
	adjectives["Macbook"] = []string{"Pro", "Air", "Neo"}
	adjectives["Sneakers"] = []string{"Training", "Running", "Everyday", "Trail", "High Performance"}
	adjectives["Body Care"] = []string{"Luxurious","Fragrant", "Stress Relief", "Rose", "Gardenia", 
	                                  "Lavender", "Citrus", "Jasmine"}
	adjectives["Sunglasses"] = []string{"Stylish", "Everyday","High Performance"}
	adjectives["Jewlery"] = []string{"3.5mm", "4mm", "2.5mm", "3mm"}

	
	products := make(map[int]Item)
	categoryKeys := make([]string, 0, len(categoryToBrand))
    for k := range categoryToBrand {
        categoryKeys = append(categoryKeys, k)
    }

	for i := 1; i <= count; i++ {
		// Generate a unique item, retrying with a different category if one is exhausted
		var brand, adj, subcategory, color, name, choosenCategory string
		found := false
		for attempt := 0; attempt < len(categoryKeys)*50 && !found; attempt++ {
			choosenCategory = categoryKeys[rand.Intn(len(categoryKeys))]
			brand, adj, subcategory = GenerateItem(choosenCategory)
			color = colors[rand.Intn(len(colors))]
			displayCategory := choosenCategory
			if subcategory != "" {
				displayCategory = subcategory
			}
			name = fmt.Sprintf("%s %s %s %s", brand, adj, color, displayCategory)
			if _, exists := productSet[name]; !exists {
				productSet[name] = struct{}{}
				found = true
			}
		}
		if !found {
			log.Printf("Warning: could not generate unique product after many attempts, skipping item %d", i)
			continue
		}

		var price float64
		if choosenCategory == "Macbook" {
			price = math.Round((rand.Float64()*500+500)*100) / 100
		} else {
			price = math.Round((rand.Float64()*80+20)*100) / 100
		}

		

		displayCategory := choosenCategory
		if subcategory != "" {
			displayCategory = subcategory
		}
		imageKey := getImageKey(brand, displayCategory)
		imageURL := ImageURL(imageKey)

		item := Item{
			ID:       i,
			Price:    price,
			Category: choosenCategory,
			Color:    color,
			Name:     name,
			Brand:    brand,
			IsActive: true,
			ImageURL: imageURL,
		}

		products[i] = item
	}
	
	return products
}

func GenerateItem(category string) (brand, adj, subcategory string) {
	subcategories, ok := categories[category]
	if ok {
		subcategory = subcategories[rand.Intn(len(subcategories))]
	}

	// pick a brand
	brands, exists := categoryToBrand[category]
	if exists && len(brands) > 0 {
		brand = brands[rand.Intn(len(brands))]
	}

	// pick an adjective
	adjs, exists := adjectives[category]
	if exists && len(adjs) > 0 {
		adj = adjs[rand.Intn(len(adjs))]
	}

	return
}


// Alternative: If you want to print just the first few items as a sample
func printSample(products map[int]Item, sampleSize int) {
	fmt.Printf("\n// Sample of first %d items:\n", sampleSize)

	count := 0
	for i := 1; i <= len(products) && count < sampleSize; i++ {
		if item, exists := products[i]; exists {
			fmt.Printf("\tID: %d, Name: \"%s\", Category: \"%s\", Brand: \"%s\", Color: \"%s\", Price: %.2f, IsActive: %v, ImageURL: \"%s\"\n",
				item.ID, item.Name, item.Category, item.Brand, item.Color, item.Price, item.IsActive, item.ImageURL)
			count++
		}
	}
}



