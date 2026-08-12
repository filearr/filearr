package inventory

import (
	"bytes"
	"context"
	"os/exec"
	"strings"
	"time"
)

// swVersTimeout bounds the one sw_vers call. It is a tiny Apple binary that
// prints a line and exits; the ceiling exists only so a pathological host
// degrades to "unknown OS version" instead of stalling the caller.
const swVersTimeout = 3 * time.Second

// osVersion reads the macOS product version and build from sw_vers.
//
// macOS has no cheap in-process equivalent: the supported answer lives in
// /System/Library/CoreServices/SystemVersion.plist (a binary plist this agent
// has no parser for, and whose format is Apple's to change) or behind
// Objective-C runtime calls that would drag cgo into a deliberately pure-Go
// binary. So this is the one platform that shells out — acceptable precisely
// because OSVersion() caches for the life of the process and this therefore
// runs AT MOST ONCE per agent process, never per poll.
//
// Both values are asked for in one invocation: -productVersion gives "15.3" and
// -buildVersion gives "24D60", and the build is what distinguishes two hosts on
// the same point release (Apple ships RSR patches without changing the product
// version). A failure of either half is not fatal — whatever was obtained is
// returned, and "" only when nothing was.
func osVersion() string {
	product := swVers("-productVersion")
	if product == "" {
		return ""
	}
	if build := swVers("-buildVersion"); build != "" {
		return "macOS " + product + " (build " + build + ")"
	}
	return "macOS " + product
}

func swVers(arg string) string {
	ctx, cancel := context.WithTimeout(context.Background(), swVersTimeout)
	defer cancel()
	var out bytes.Buffer
	cmd := exec.CommandContext(ctx, "/usr/bin/sw_vers", arg)
	cmd.Stdout = &out
	if err := cmd.Run(); err != nil {
		return ""
	}
	return strings.TrimSpace(out.String())
}
