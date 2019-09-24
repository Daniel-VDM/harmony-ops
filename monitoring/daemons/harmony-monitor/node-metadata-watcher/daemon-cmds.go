package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

const (
	installD = "install the harmony-watchdog service"
	removeD  = "remove the harmony-watchdog service"
	startD   = "start the harmony-watchdog service"
	stopD    = "stop the harmony-watchdog service"
	statusD  = "check status of the harmony-watchdog service"
)

type cobraSrvWrapper struct {
	*Service
}

func (cw *cobraSrvWrapper) install(cmd *cobra.Command, args []string) error {
	r, err := cw.Install()
	if err != nil {
		return err
	}
	fmt.Println(r)
	return nil
}

func (cw *cobraSrvWrapper) remove(cmd *cobra.Command, args []string) error {
	r, err := cw.Remove()
	if err != nil {
		return err
	}
	fmt.Println(r)
	return nil
}

func (cw *cobraSrvWrapper) stop(cmd *cobra.Command, args []string) error {
	r, err := cw.Stop()
	if err != nil {
		return err
	}
	fmt.Println(r)
	return nil
}

func (cw *cobraSrvWrapper) status(cmd *cobra.Command, args []string) error {
	r, err := cw.Status()
	if err != nil {
		return err
	}
	fmt.Println(r)
	return nil
}

func daemonCmds() []*cobra.Command {
	start := &cobra.Command{
		Use:   "start",
		Short: startD,
		RunE:  w.start,
	}
	// descr := "yaml detailing what to watch"
	// start.Flags().StringVar(&monitorNodeYAML, "watch", "", descr)
	// start.MarkFlagRequired("watch")

	return []*cobra.Command{{
		Use:   "install",
		Short: installD,
		RunE:  w.install,
	}, {
		Use:   "remove",
		Short: removeD,
		RunE:  w.remove,
	}, start, {
		Use:   "stop",
		Short: stopD,
		RunE:  w.stop,
	}, {
		Use:   "status",
		Short: statusD,
		RunE:  w.status,
	},
	}
}
