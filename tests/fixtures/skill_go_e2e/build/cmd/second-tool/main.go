package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Printf("second:%d\n", len(os.Args)-1)
}
