package main

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/extract"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/scan"
)

// hostExtractCapabilities snapshots what this host can actually extract, in the
// shape the ignored-setting rules and the status page consume.
func hostExtractCapabilities() agentcfg.ExtractCapabilities {
	return agentcfg.ExtractCapabilities{
		Extract:   true, // this build carries the pass (see inventory.Capabilities)
		FFprobe:   inventory.HasFFprobe(),
		Tesseract: inventory.HasTesseract(),
		Exiftool:  inventory.HasExiftool(),
		PDFInfo:   inventory.HasPDFInfo(),
		PDFToText: inventory.HasPDFToText(),
		PDFToPPM:  inventory.HasPDFToPPM(),
	}
}

// extractOptions maps the central policy onto the extraction pass's Options,
// resolving the host tools the policy's intent depends on. A tool the host does
// not have resolves to "" and the dependent extractor is simply skipped — the
// per-capability degradation the parity design is built around. The mismatch is
// reported separately (IgnoredSettings), never silently.
func extractOptions(pol agentcfg.Policy) extract.Options {
	opts := extract.Options{
		BodyText:    pol.ExtractBodyTextValue(),
		OCR:         pol.ExtractOCRValue(),
		MaxBytes:    pol.ExtractMaxBytesOr(agentcfg.DefaultExtractMaxBytes),
		FFprobePath: inventory.FFprobePath(),
		// PDF properties ride extract_enabled — they are cheap curated metadata,
		// the same posture central takes. pdftotext is resolved regardless of
		// extract_body_text because the scanned-PDF OCR gate needs to know how
		// much native text a PDF already has before deciding to rasterise it.
		PDFInfoPath:   inventory.PDFInfoPath(),
		PDFToTextPath: inventory.PDFToTextPath(),
	}
	// EXIF is its own opt-in (see Policy.ExtractEXIF): one subprocess per image
	// inside the scan, and GPS coordinates leaving the host.
	if pol.ExtractEXIFValue() {
		opts.ExiftoolPath = inventory.ExiftoolPath()
	}
	if opts.OCR {
		opts.TesseractPath = inventory.TesseractPath()
		opts.PDFToPPMPath = inventory.PDFToPPMPath()
	}
	return opts
}

// newExtractFn builds the scan's extraction seam from a policy. It returns nil
// — meaning "no extraction pass at all" — when the policy has not enabled it,
// so the scan's hot path pays literally nothing in the default configuration.
//
// The returned func is best-effort by contract: a framing failure (unreadable
// file, cancelled ctx) yields nil and is logged at debug, because a file the
// agent cannot parse must never fail its scan. Per-extractor failures ride the
// result's errors map to central instead.
func newExtractFn(pol agentcfg.Policy, log *slog.Logger) scan.ExtractFn {
	if !pol.ExtractEnabledValue() {
		return nil
	}
	opts := extractOptions(pol)
	return func(ctx context.Context, absPath, category string) *outbox.Extracted {
		res, err := extract.Extract(ctx, absPath, category, opts)
		if err != nil {
			if log != nil {
				log.Debug("extract skipped", "path", absPath, "err", err)
			}
			return nil
		}
		if res.Empty() {
			return nil
		}
		meta := res.Meta
		if len(res.Errors) > 0 {
			// Surface per-extractor failures in central's own error vocabulary
			// (_extract_error / _extract_error_kind are what tasks/extract.py
			// stores), joined because the agent may have run several extractors
			// over one file. The successful keys still ship alongside.
			if meta == nil {
				meta = map[string]any{}
			}
			meta["_extract_error"] = joinErrors(res.Errors)
			meta["_extract_error_kind"] = "agent"
		}
		return &outbox.Extracted{
			Schema:            extract.Schema,
			Meta:              meta,
			BodyText:          res.BodyText,
			BodyTextTruncated: res.BodyTextTruncated,
		}
	}
}

// scanExtractFn is the `scan` command's entry point: read the cached policy,
// report any setting this host cannot honour, and build the seam. A missing or
// unreadable policy cache yields nil (no extraction), matching the rest of the
// scan path's offline-first posture — a broken policy never fails a scan.
//
// The ignored-setting logging here is unconditional because `scan` is a
// short-lived process: "once per distinct change" is satisfied by the process
// boundary itself, and an operator reading a scan's log wants the reason in
// THAT log, not only in the daemon's.
func scanExtractFn(dataDir string) scan.ExtractFn {
	pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
	if err != nil || !ok {
		return nil
	}
	log := newLogger()
	agentcfg.NewIgnoredLogger(log).Log(agentcfg.IgnoredSettings(pol, hostExtractCapabilities()))
	return newExtractFn(pol, log)
}

// extractSnapshot renders the local status page's extraction section: the
// EFFECTIVE value of each of the four policy keys, where each value came from,
// and the ignored-settings list with reasons.
//
// "source" answers the question an operator actually has when a setting looks
// wrong — did central set this, or am I looking at a default? A key present in
// the cached policy body reports the policy's scope/version; anything else
// reports "default".
func extractSnapshot(dataDir string) map[string]any {
	caps := hostExtractCapabilities()
	doc, ok, err := agentcfg.NewETagCache(dataDir).Load()
	if err != nil || !ok {
		// Never contacted (or an unreadable cache): every value is the
		// never-contacted default, which is exactly what a zero Policy yields.
		return extractSnapshotFor(agentcfg.Policy{}, caps, nil, "default")
	}
	pol, perr := doc.Parsed()
	if perr != nil {
		return extractSnapshotFor(agentcfg.Policy{}, caps, nil, "default (policy cache unparseable)")
	}
	present := map[string]bool{}
	for _, k := range doc.PolicyKeys() {
		present[k] = true
	}
	source := fmt.Sprintf("central policy %s v%d", doc.Scope, doc.Version)
	return extractSnapshotFor(pol, caps, present, source)
}

func extractSnapshotFor(
	pol agentcfg.Policy, caps agentcfg.ExtractCapabilities,
	present map[string]bool, source string,
) map[string]any {
	from := func(key string) string {
		if present[key] {
			return source
		}
		return "default"
	}
	ignored := agentcfg.IgnoredSettings(pol, caps)
	// A non-nil empty slice so the JSON is [] rather than null — the page
	// renders "none" from an empty list, and null would read as "unknown".
	list := make([]map[string]string, 0, len(ignored))
	for _, it := range ignored {
		list = append(list, map[string]string{"key": it.Key, "reason": it.Reason})
	}
	return map[string]any{
		"effective": map[string]any{
			"extract_enabled":   pol.ExtractEnabledValue(),
			"extract_body_text": pol.ExtractBodyTextValue(),
			"extract_ocr":       pol.ExtractOCRValue(),
			"extract_exif":      pol.ExtractEXIFValue(),
			"extract_max_bytes": pol.ExtractMaxBytesOr(agentcfg.DefaultExtractMaxBytes),
		},
		"source": map[string]any{
			"extract_enabled":   from("extract_enabled"),
			"extract_body_text": from("extract_body_text"),
			"extract_ocr":       from("extract_ocr"),
			"extract_exif":      from("extract_exif"),
			"extract_max_bytes": from("extract_max_bytes"),
		},
		"ignored_settings": list,
	}
}

// joinErrors renders the per-extractor error map as one stable, sorted line.
func joinErrors(errs map[string]string) string {
	keys := make([]string, 0, len(errs))
	for k := range errs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+": "+errs[k])
	}
	return strings.Join(parts, "; ")
}
