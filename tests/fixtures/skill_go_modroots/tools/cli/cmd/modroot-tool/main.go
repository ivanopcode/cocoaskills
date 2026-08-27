package main

import (
	"fmt"
	"os"

	"example.test/board"
)

func main() {
	fmt.Println(board.Label(), os.Args[1:])
}
