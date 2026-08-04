package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"

	"example.test/fixturedep"
)

func main() {
	args := os.Args[1:]
	exitCode := 0
	if len(args) >= 2 && args[0] == "--exit" {
		parsed, err := strconv.Atoi(args[1])
		if err != nil {
			fmt.Fprintln(os.Stderr, "invalid exit code")
			os.Exit(64)
		}
		exitCode = parsed
		args = args[2:]
	}
	payload, err := json.Marshal(args)
	if err != nil {
		panic(err)
	}
	fmt.Printf("%s:%s\n", fixturedep.Prefix(), payload)
	fmt.Fprintf(os.Stderr, "stderr:%d\n", len(args))
	if sentinel := os.Getenv("CSK_GO_E2E_LAUNCH_SENTINEL"); sentinel != "" {
		if err := os.WriteFile(sentinel, []byte("launched\n"), 0o600); err != nil {
			panic(err)
		}
	}
	os.Exit(exitCode)
}
