package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

var s3Client *s3.Client
var imageBucket string

func InitS3() error {
	imageBucket = os.Getenv("IMAGE_BUCKET")
	if imageBucket == "" {
		return fmt.Errorf("IMAGE_BUCKET not set")
	}
	cfg, err := config.LoadDefaultConfig(context.Background(),
		config.WithRegion(os.Getenv("AWS_REGION")),
	)
	if err != nil {
		return err
	}
	s3Client = s3.NewFromConfig(cfg)
	log.Println("S3 client initialized")
	return nil
}

func UploadImages() error {
	entries, err := os.ReadDir("product_images")
	if err != nil {
		return fmt.Errorf("failed to read product_images dir: %v", err)
	}
	for _, entry := range entries {
		if entry.IsDir() || strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		path := filepath.Join("product_images", entry.Name())
		f, err := os.Open(path)
		if err != nil {
			log.Printf("skip %s: %v", path, err)
			continue
		}
		_, err = s3Client.PutObject(context.Background(), &s3.PutObjectInput{
			Bucket:      aws.String(imageBucket),
			Key:         aws.String("products/" + entry.Name()),
			Body:        f,
			ContentType: aws.String("image/jpeg"),
		})
		f.Close()
		if err != nil {
			log.Printf("failed to upload %s: %v", entry.Name(), err)
		} else {
			log.Printf("uploaded %s", entry.Name())
		}
	}
	return nil
}

func ImageURL(key string) string {
	return fmt.Sprintf("https://%s.s3.%s.amazonaws.com/products/%s.jpg",
		imageBucket, os.Getenv("AWS_REGION"), key)
}
