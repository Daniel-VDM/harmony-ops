package main

import (
	"fmt"
	"os"

	"github.com/pkg/profile"
)

var (
	version string
	commit  string
	builtAt string
	builtBy string
)

func main() {
	// Use profile.NoShutdownHook to ignore system interrupt

	// defer profile.Start().Stop()

	defer profile.Start(profile.CPUProfile).Stop()

	if err := rootCmd.Execute(); err != nil {
		fmt.Println(err)
		os.Exit(-1)
	}
}
