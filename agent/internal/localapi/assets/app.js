"use strict";
// Filearr local web UI — read-only. This script issues ONLY GET requests to the
// agent's local query API (never a mutating verb). It relies on the same-origin
// session cookie (set by the bootstrap-token exchange) for auth.

(function () {
  var qInput = document.getElementById("q");
  var form = document.getElementById("search-form");
  var results = document.getElementById("results");
  var body = document.getElementById("results-body");
  var meta = document.getElementById("meta");
  var errBox = document.getElementById("error");
  var empty = document.getElementById("empty");
  var bannerScope = document.getElementById("banner-scope");
  var bannerStale = document.getElementById("banner-stale");
  var scopePreds = document.getElementById("scope-preds");
  var indexStatus = document.getElementById("index-status");

  var LIMIT = 200;
  var debounceTimer = null;
  var inflight = null; // AbortController for the current request
  var lastRows = [];   // last rendered result rows (export + client sort)
  var selectedKind = ""; // active category chip

  // ---- shared helpers -------------------------------------------------------

  function fmtSize(n) {
    if (n === null || n === undefined || n === "") return "";
    n = Number(n);
    if (isNaN(n)) return "";
    if (n < 1024) return n + " B";
    var units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
    var v = n, i = -1;
    do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
    return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
  }

  function fmtMtime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit"
    });
  }

  function splitPath(rel) {
    var i = rel.lastIndexOf("/");
    if (i < 0) return { dir: "", name: rel };
    return { dir: rel.slice(0, i + 1), name: rel.slice(i + 1) };
  }

  function fullPath(row) {
    var rel = row.rel_path || "";
    if (row.root) {
      var sep = row.root.indexOf("\\") >= 0 ? "\\" : "/";
      return row.root.replace(/[\/\\]+$/, "") + sep + rel;
    }
    return rel;
  }

  function clearError() { errBox.hidden = true; errBox.textContent = ""; }
  function showError(msg) {
    errBox.hidden = false;
    errBox.textContent = msg;
    results.hidden = true;
    empty.hidden = true;
  }

  function renderScope(scope, stale) {
    var active = scope && scope.active;
    bannerScope.hidden = !active;
    if (active) {
      var preds = (scope.predicates || []);
      scopePreds.textContent = preds.length ? preds.join("  ·  ") : "";
    }
    bannerStale.hidden = !stale;
  }

  function copyToClipboard(text, btn) {
    function ok() {
      var prev = btn.textContent;
      btn.textContent = "Copied";
      btn.classList.add("copied");
      setTimeout(function () { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, function () { fallbackCopy(text, ok); });
    } else {
      fallbackCopy(text, ok);
    }
  }
  function fallbackCopy(text, ok) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); ok(); } catch (e) { /* no-op */ }
    document.body.removeChild(ta);
  }

  function downloadText(name, text, mime) {
    var blob = new Blob([text], { type: mime || "text/plain" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  }

  function csvEscape(v) {
    var s = v === null || v === undefined ? "" : String(v);
    if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }
  function toCSV(header, rows) {
    var out = [header.map(csvEscape).join(",")];
    rows.forEach(function (r) { out.push(r.map(csvEscape).join(",")); });
    return out.join("\r\n") + "\r\n";
  }

  function stamp() {
    var d = new Date();
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return "" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate());
  }

  function kindBadge(kind) {
    var b = document.createElement("span");
    b.className = "badge";
    b.textContent = kind || "—";
    return b;
  }

  // ---- search tab -----------------------------------------------------------

  function effectiveQuery() {
    var q = qInput.value.trim();
    if (selectedKind) q = (q ? q + " " : "") + "kind:" + selectedKind;
    return q;
  }

  function sortRows(rows) {
    var mode = document.getElementById("sort").value;
    if (!mode) return rows;
    var copy = rows.slice();
    if (mode === "name") {
      copy.sort(function (a, b) { return (a.filename || "").localeCompare(b.filename || ""); });
    } else if (mode === "size") {
      copy.sort(function (a, b) { return (b.size || 0) - (a.size || 0); });
    } else if (mode === "newest") {
      copy.sort(function (a, b) { return (b.mtime || "").localeCompare(a.mtime || ""); });
    }
    return copy;
  }

  function render(data) {
    clearError();
    renderScope(data.scope, data.scope && data.scope.stale);
    body.textContent = "";
    lastRows = data.rows || [];
    setExportEnabled(lastRows.length > 0);

    var rows = sortRows(lastRows);
    if (rows.length === 0) {
      results.hidden = true;
      empty.hidden = false;
      meta.textContent = describeMeta(data, 0);
      return;
    }
    empty.hidden = true;
    results.hidden = false;

    var frag = document.createDocumentFragment();
    rows.forEach(function (row) {
      var tr = document.createElement("tr");

      var tdKind = document.createElement("td");
      tdKind.className = "col-kind";
      tdKind.appendChild(kindBadge(row.kind));
      tr.appendChild(tdKind);

      var tdPath = document.createElement("td");
      tdPath.className = "path";
      var parts = splitPath(row.rel_path || "");
      if (parts.dir) {
        var dir = document.createElement("span");
        dir.className = "dir";
        dir.textContent = parts.dir;
        tdPath.appendChild(dir);
      }
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = parts.name;
      tdPath.appendChild(name);
      if (row.fuzzy_matched) {
        var tag = document.createElement("span");
        tag.className = "fuzzy-tag";
        tag.textContent = "fuzzy";
        tdPath.appendChild(tag);
      }
      tr.appendChild(tdPath);

      var tdSize = document.createElement("td");
      tdSize.className = "col-size";
      tdSize.textContent = fmtSize(row.size);
      tr.appendChild(tdSize);

      var tdMod = document.createElement("td");
      tdMod.className = "col-mod";
      tdMod.textContent = fmtMtime(row.mtime);
      tr.appendChild(tdMod);

      var tdCopy = document.createElement("td");
      tdCopy.className = "col-copy";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-btn";
      btn.textContent = "Copy path";
      btn.addEventListener("click", function () { copyToClipboard(fullPath(row), btn); });
      tdCopy.appendChild(btn);
      tr.appendChild(tdCopy);

      frag.appendChild(tr);
    });
    body.appendChild(frag);
    meta.textContent = describeMeta(data, rows.length);
  }

  function describeMeta(data, shown) {
    var bits = [];
    bits.push(shown + (data.truncated ? "+" : "") + " result" + (shown === 1 ? "" : "s"));
    if (typeof data.elapsed_ms === "number") bits.push(data.elapsed_ms + " ms");
    if (data.fuzzy) bits.push("includes fuzzy matches");
    if (data.truncated) bits.push("result window truncated");
    return bits.join(" · ");
  }

  function search(q) {
    if (inflight) { inflight.abort(); }
    if (!q) {
      results.hidden = true;
      empty.hidden = true;
      meta.textContent = "";
      lastRows = [];
      setExportEnabled(false);
      clearError();
      return;
    }
    var ctrl = new AbortController();
    inflight = ctrl;
    meta.textContent = "Searching…";
    var url = "api/query?q=" + encodeURIComponent(q) + "&limit=" + LIMIT;
    fetch(url, { method: "GET", credentials: "same-origin", signal: ctrl.signal, headers: { "Accept": "application/json" } })
      .then(function (resp) {
        return resp.json().then(function (data) { return { status: resp.status, data: data }; });
      })
      .then(function (r) {
        if (ctrl !== inflight) return; // superseded
        if (r.status === 200) {
          render(r.data);
        } else {
          var msg = r.data && (r.data.reason || r.data.error) ? (r.data.error + (r.data.reason ? ": " + r.data.reason : "")) : ("query failed (HTTP " + r.status + ")");
          showError(msg);
          if (r.data && r.data.scope) renderScope(r.data.scope, r.data.scope.stale);
        }
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") return;
        showError("network error: " + (e && e.message ? e.message : e));
      });
  }

  function runSearch() { search(effectiveQuery()); }

  function setExportEnabled(on) {
    document.getElementById("export-csv").disabled = !on;
    document.getElementById("export-json").disabled = !on;
  }

  document.getElementById("export-csv").addEventListener("click", function () {
    var rows = sortRows(lastRows).map(function (r) {
      return [fullPath(r), r.filename, r.extension || "", r.kind || "", r.group || "", r.size, r.mtime, r.quick_hash || "", r.content_hash || ""];
    });
    downloadText("filearr-agent-search-" + stamp() + ".csv",
      toCSV(["path", "filename", "extension", "kind", "group", "size", "mtime", "quick_hash", "content_hash"], rows),
      "text/csv");
  });
  document.getElementById("export-json").addEventListener("click", function () {
    downloadText("filearr-agent-search-" + stamp() + ".json",
      JSON.stringify(sortRows(lastRows), null, 2), "application/json");
  });
  document.getElementById("sort").addEventListener("change", function () {
    if (lastRows.length) render({ rows: lastRows, scope: null });
  });

  // Category chips: fed by the local categories report; each chip toggles a
  // kind:<category> term composed into the query (console-parity pills).
  function loadChips() {
    fetch("api/reports/categories?limit=12", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (page) {
        if (!page || !page.rows || !page.rows.length) return;
        var wrap = document.getElementById("chips");
        wrap.textContent = "";
        page.rows.forEach(function (row) {
          var cat = row[0], count = row[1];
          if (!cat || cat === "(unclassified)") return;
          var chip = document.createElement("button");
          chip.type = "button";
          chip.className = "chip";
          chip.textContent = cat + " ";
          var n = document.createElement("span");
          n.className = "chip-count";
          n.textContent = Number(count).toLocaleString();
          chip.appendChild(n);
          chip.addEventListener("click", function () {
            selectedKind = selectedKind === cat ? "" : cat;
            Array.prototype.forEach.call(wrap.children, function (c) {
              c.classList.toggle("active", c === chip && selectedKind !== "");
            });
            runSearch();
          });
          wrap.appendChild(chip);
        });
      })
      .catch(function () { /* chips are progressive enhancement */ });
  }

  function loadStatus() {
    fetch("api/status", { method: "GET", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        if (!s) return;
        if (typeof s.item_count === "number") {
          indexStatus.textContent = s.item_count.toLocaleString() + " items indexed" +
            (s.index_ready ? "" : " (index not ready)");
        }
        renderScope(s.scope, s.policy_stale);
      })
      .catch(function () { /* status is best-effort */ });
  }

  qInput.addEventListener("input", function () {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runSearch, 180);
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    clearTimeout(debounceTimer);
    runSearch();
  });

  loadStatus();
  loadChips();

  // ---- tabs -----------------------------------------------------------------

  var panels = {
    search: document.getElementById("panel-search"),
    filters: document.getElementById("panel-filters"),
    reports: document.getElementById("panel-reports"),
    status: document.getElementById("panel-status"),
    logs: document.getElementById("panel-logs")
  };
  var tabs = {
    search: document.getElementById("tab-search"),
    filters: document.getElementById("tab-filters"),
    reports: document.getElementById("tab-reports"),
    status: document.getElementById("tab-status"),
    logs: document.getElementById("tab-logs")
  };
  var logsTimer = null;

  function showTab(name) {
    Object.keys(panels).forEach(function (k) {
      panels[k].hidden = k !== name;
      tabs[k].classList.toggle("active", k === name);
      tabs[k].setAttribute("aria-selected", k === name ? "true" : "false");
    });
    if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
    if (name === "status") loadStatusPanel();
    if (name === "reports") initReports();
    if (name === "filters") initBuilder();
    if (name === "logs") {
      loadLogs();
      logsTimer = setInterval(loadLogs, 5000);
    }
  }
  Object.keys(tabs).forEach(function (k) {
    tabs[k].addEventListener("click", function () { showTab(k); });
  });

  // ---- status tab -----------------------------------------------------------

  function kvRow(dl, key, value) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    dl.appendChild(dt); dl.appendChild(dd);
  }
  function kvSection(dl, title) {
    var div = document.createElement("div");
    div.className = "kv-section";
    div.textContent = title;
    dl.appendChild(div);
  }

  function renderRoots(roots) {
    var wrap = document.getElementById("roots-wrap");
    var tbody = document.getElementById("roots-body");
    tbody.textContent = "";
    if (!roots || !roots.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    roots.forEach(function (r) {
      var tr = document.createElement("tr");
      var ls = r.last_scan || {};
      function td(v, cls) {
        var t = document.createElement("td");
        if (cls) t.className = cls;
        t.textContent = v === null || v === undefined || v === "" ? "—" : String(v);
        tr.appendChild(t);
        return t;
      }
      td(r.path, "path-cell");
      td(Number(r.items || 0).toLocaleString(), "num");
      td(fmtSize(r.bytes), "num");
      td(ls.finished_at ? fmtMtime(ls.finished_at) : (ls.status === "running" ? "running…" : "never"));
      var st = td(ls.status || "—");
      if (ls.status) st.firstChild && (st.textContent = "");
      if (ls.status) {
        var b = document.createElement("span");
        b.className = "badge badge-" + ls.status;
        b.textContent = ls.status;
        st.textContent = "";
        st.appendChild(b);
      }
      td(ls.seen !== undefined ? Number(ls.seen).toLocaleString() : "—", "num");
      td(ls.new !== undefined ? Number(ls.new).toLocaleString() : "—", "num");
      td(ls.changed !== undefined ? Number(ls.changed).toLocaleString() : "—", "num");
      td(ls.duration_seconds !== undefined && ls.duration_seconds !== null && ls.status !== "running"
        ? ls.duration_seconds + " s" : "—", "num");
      tbody.appendChild(tr);
    });
  }

  function loadStatusPanel() {
    var errBox2 = document.getElementById("status-error");
    fetch("/api/settings", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (s) {
        errBox2.hidden = true;
        renderRoots(s.roots);
        var dl = document.getElementById("status-list");
        dl.textContent = "";
        kvSection(dl, "Activity");
        var act = s.activity || {};
        var scanSt = act.scan;
        if (scanSt) {
          var scanLine = (scanSt.running ? "RUNNING" : (scanSt.status || "idle")) +
            " — " + (scanSt.root || "") +
            "  seen=" + (scanSt.seen || 0) +
            " new=" + (scanSt.new || 0) +
            " changed=" + (scanSt.changed || 0) +
            (scanSt.updated_at ? "  (as of " + fmtMtime(scanSt.updated_at) + ")" : "");
          kvRow(dl, scanSt.running ? "Scan (running)" : "Last scan", scanLine);
        } else {
          kvRow(dl, "Last scan", "no scan recorded yet");
        }
        kvRow(dl, "Replication backlog",
          act.outbox_pending === undefined ? "—"
            : act.outbox_pending === 0 ? "0 (fully replicated)"
            : act.outbox_pending + " change(s) waiting to reach central");
        kvSection(dl, "Identity");
        kvRow(dl, "Agent id", s.agent_id);
        kvRow(dl, "Agent version", s.agent_version);
        kvRow(dl, "Central", s.central_url);
        kvRow(dl, "Rollout group", s.rollout_group);
        kvRow(dl, "Data dir", s.data_dir);
        kvSection(dl, "Scanning");
        var sc = s.scan || {};
        kvRow(dl, "Roots", (sc.roots || []).join(", ") || s.scan_roots_env);
        kvRow(dl, "Presets", (sc.presets || []).join(", "));
        kvRow(dl, "Exclude globs", (sc.exclude_globs || []).join(", "));
        kvRow(dl, "Categories", (sc.enabled_categories || []).join(", "));
        kvRow(dl, "Share map", s.share_map);
        kvRow(dl, "ffmpeg available", s.ffmpeg ? "yes" : "no (video thumbs skipped)");
        kvSection(dl, "Policy (from central)");
        var pv = s.policy || {};
        kvRow(dl, "Web UI enabled", pv.web_ui_enabled ? "yes" : "no");
        kvRow(dl, "Auth required", pv.auth_required ? "yes" : "no");
        kvRow(dl, "Local query API", pv.local_access_enabled ? "yes" : "no");
        kvRow(dl, "Policy version", pv.version);
        kvRow(dl, "Policy stale", pv.stale ? "YES (past offline grace)" : "no");
        kvRow(dl, "Path scope", (pv.path_scope || []).join(" OR ") || "unrestricted");
        kvSection(dl, "This process");
        kvRow(dl, "Web bind", s.web_addr + (s.web_remote ? " (remote access enabled)" : " (loopback only)"));
        kvRow(dl, "Self-update", s.self_update ? "enabled" : "off (image pulls are the update path)");
        kvRow(dl, "Log level", s.log_level || "info");
      })
      .catch(function (e) {
        errBox2.hidden = false;
        errBox2.textContent = "Could not load settings: " + e.message;
      });
  }

  // ---- logs tab -------------------------------------------------------------

  var lastLogLines = [];

  // parseLogLine splits one rendered line into {time, level, msg, details}.
  // slog text: time=… level=… msg=… k=v…  (values may be double-quoted with \" escapes)
  // entrypoint: 2026-07-27T05:00:00Z [entrypoint] message…
  function parseLogLine(line) {
    if (line.slice(0, 5) === "time=") {
      var re = /([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)/g;
      var out = { time: "", level: "", msg: "", details: "" };
      var extras = [];
      var m;
      while ((m = re.exec(line)) !== null) {
        var key = m[1], val = m[2];
        if (val.charAt(0) === '"') {
          try { val = JSON.parse(val); } catch (e) { val = val.slice(1, -1); }
        }
        if (key === "time") out.time = val;
        else if (key === "level") out.level = val;
        else if (key === "msg") out.msg = val;
        else extras.push(key + "=" + val);
      }
      out.details = extras.join("  ");
      return out;
    }
    var em = line.match(/^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+(\[[^\]]+\])?\s*(.*)$/);
    if (em) {
      return { time: em[1], level: "INFO", msg: em[3] || "", details: em[2] || "" };
    }
    return { time: "", level: "", msg: line, details: "" };
  }

  function renderLogs(lines) {
    var tbody = document.getElementById("log-body");
    var wrap = document.querySelector(".loglines-wrap");
    var pre = document.getElementById("log-lines");
    var raw = document.getElementById("log-raw").checked;
    pre.hidden = !raw;
    wrap.hidden = raw;
    if (raw) {
      var stickR = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
      pre.textContent = lines.join("\n");
      if (stickR) pre.scrollTop = pre.scrollHeight;
      return;
    }
    var stick = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 8;
    tbody.textContent = "";
    var frag = document.createDocumentFragment();
    lines.forEach(function (line) {
      var p = parseLogLine(line);
      var tr = document.createElement("tr");
      var tdT = document.createElement("td");
      tdT.className = "log-time";
      tdT.textContent = p.time ? fmtMtime(p.time) : "";
      tr.appendChild(tdT);
      var tdL = document.createElement("td");
      var lv = (p.level || "").toUpperCase();
      tdL.className = "log-level level-" + lv.toLowerCase().replace("+", "");
      tdL.textContent = lv;
      tr.appendChild(tdL);
      var tdM = document.createElement("td");
      tdM.className = "log-msg";
      tdM.textContent = p.msg;
      tr.appendChild(tdM);
      var tdD = document.createElement("td");
      tdD.className = "log-details";
      tdD.textContent = p.details;
      tdD.title = p.details;
      tr.appendChild(tdD);
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
    if (stick) wrap.scrollTop = wrap.scrollHeight;
  }

  function loadLogs() {
    var errBox3 = document.getElementById("logs-error");
    var sel = document.getElementById("log-limit");
    var limit = sel ? sel.value : "500";
    fetch("/api/logs?limit=" + encodeURIComponent(limit), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        errBox3.hidden = true;
        lastLogLines = d.lines || [];
        document.getElementById("log-count").textContent = String(lastLogLines.length);
        renderLogs(lastLogLines);
      })
      .catch(function (e) {
        errBox3.hidden = false;
        errBox3.textContent = "Could not load logs: " + e.message;
      });
  }
  document.getElementById("log-limit").addEventListener("change", loadLogs);
  document.getElementById("log-raw").addEventListener("change", function () { renderLogs(lastLogLines); });
  document.getElementById("log-export").addEventListener("click", function () {
    downloadText("filearr-agent-" + stamp() + ".log", lastLogLines.join("\n") + "\n", "text/plain");
  });
  document.getElementById("log-export-csv").addEventListener("click", function () {
    var rows = lastLogLines.map(function (l) {
      var p = parseLogLine(l);
      return [p.time, p.level, p.msg, p.details];
    });
    downloadText("filearr-agent-logs-" + stamp() + ".csv",
      toCSV(["time", "level", "message", "details"], rows), "text/csv");
  });

  // ---- reports tab ----------------------------------------------------------

  var rptSpecs = null;
  var rptState = { id: "", offset: 0, limit: 100, total: 0 };

  function initReports() {
    if (rptSpecs) { loadReport(); return; }
    fetch("api/reports", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        rptSpecs = d.reports || [];
        var sel = document.getElementById("rpt-select");
        sel.textContent = "";
        rptSpecs.forEach(function (s) {
          var o = document.createElement("option");
          o.value = s.id;
          o.textContent = s.title;
          sel.appendChild(o);
        });
        if (rptSpecs.length) {
          rptState.id = rptSpecs[0].id;
          loadReport();
        }
      })
      .catch(function (e) { rptError("Could not load reports: " + e.message); });
  }

  function rptError(msg) {
    var eb = document.getElementById("rpt-error");
    eb.hidden = false;
    eb.textContent = msg;
  }

  function specFor(id) {
    for (var i = 0; i < (rptSpecs || []).length; i++) {
      if (rptSpecs[i].id === id) return rptSpecs[i];
    }
    return null;
  }

  function loadReport() {
    var eb = document.getElementById("rpt-error");
    eb.hidden = true;
    var spec = specFor(rptState.id);
    document.getElementById("rpt-desc").textContent = spec ? spec.description : "";
    document.getElementById("rpt-page").textContent =
      "Computing… first load on a large index can take a while; later pages are instant.";
    fetch("api/reports/" + encodeURIComponent(rptState.id) +
      "?limit=" + rptState.limit + "&offset=" + rptState.offset, { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(renderReport)
      .catch(function (e) {
        document.getElementById("rpt-page").textContent = "";
        rptError("Report failed: " + e.message);
      });
  }

  function renderReport(page) {
    rptState.total = page.total;
    var spec = page.spec;
    var head = document.getElementById("rpt-head");
    var tbody = document.getElementById("rpt-body");
    var table = document.getElementById("rpt-table");
    var byteCols = {};
    (spec.byte_cols || []).forEach(function (c) { byteCols[c] = true; });

    head.textContent = "";
    var hr = document.createElement("tr");
    spec.columns.forEach(function (c) {
      var th = document.createElement("th");
      th.textContent = c.replace(/_/g, " ");
      if (byteCols[c] || c === "files" || c === "copies" || c === "size") th.className = "num";
      hr.appendChild(th);
    });
    head.appendChild(hr);

    tbody.textContent = "";
    var emptyEl = document.getElementById("rpt-empty");
    if (!page.rows.length) {
      table.hidden = true;
      emptyEl.hidden = false;
    } else {
      table.hidden = false;
      emptyEl.hidden = true;
      var frag = document.createDocumentFragment();
      page.rows.forEach(function (row) {
        var tr = document.createElement("tr");
        row.forEach(function (v, i) {
          var td = document.createElement("td");
          var col = spec.columns[i];
          if (byteCols[col]) {
            td.textContent = fmtSize(v);
            td.className = "num";
          } else if (typeof v === "number") {
            td.textContent = Number(v).toLocaleString();
            td.className = "num";
          } else {
            td.textContent = v === null || v === undefined ? "" : String(v);
            if (col === "path" || col === "sample_path") td.className = "path-cell";
          }
          tr.appendChild(td);
        });
        frag.appendChild(tr);
      });
      tbody.appendChild(frag);
    }

    var lo = rptState.offset + 1;
    var hi = rptState.offset + page.rows.length;
    var label = page.total === 0 ? "0 rows" : lo + "–" + hi + " of " + Number(page.total).toLocaleString();
    if (page.capped) label += "+ (capped)";
    if (page.computed_at) label += " · as of " + fmtMtime(page.computed_at);
    document.getElementById("rpt-page").textContent = label;
    document.getElementById("rpt-prev").disabled = rptState.offset <= 0;
    document.getElementById("rpt-next").disabled = hi >= page.total;
  }

  document.getElementById("rpt-select").addEventListener("change", function (e) {
    rptState.id = e.target.value;
    rptState.offset = 0;
    loadReport();
  });
  document.getElementById("rpt-prev").addEventListener("click", function () {
    rptState.offset = Math.max(0, rptState.offset - rptState.limit);
    loadReport();
  });
  document.getElementById("rpt-next").addEventListener("click", function () {
    rptState.offset += rptState.limit;
    loadReport();
  });
  document.getElementById("rpt-csv").addEventListener("click", function () {
    var spec = specFor(rptState.id);
    if (!spec) return;
    fetch("api/reports/" + encodeURIComponent(rptState.id) + "?limit=10000", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (page) {
        downloadText("filearr-agent-" + rptState.id + "-" + stamp() + ".csv",
          toCSV(spec.columns, page.rows), "text/csv");
      })
      .catch(function (e) { rptError("Export failed: " + e.message); });
  });

  // ---- filter-builder tab ---------------------------------------------------
  // Console-parity rows compiled to the shared query grammar (AND-joined).

  var FIELDS = [
    { id: "text", label: "Text", ops: ["contains"] },
    { id: "kind", label: "Kind", ops: ["is", "is not"] },
    { id: "group", label: "Group", ops: ["is", "is not"] },
    { id: "ext", label: "Extension", ops: ["is", "is not"], hint: "mp4;mkv for multiple" },
    { id: "size", label: "Size", ops: ["=", ">", ">=", "<", "<=", "range"], hint: "e.g. 500M or 1G" },
    { id: "modified", label: "Modified", ops: ["within last", "older than", "after date", "before date", "range"], time: true },
    { id: "created", label: "Created", ops: ["within last", "older than", "after date", "before date", "range"], time: true },
    { id: "path", label: "Path", ops: ["matches"], hint: "glob, e.g. */backups/*" },
    { id: "tag", label: "Tag", ops: ["is", "is not"] },
    { id: "hash", label: "Hash", ops: ["is"], hint: "hex digest (quick or content)" },
    { id: "meta", label: "Metadata", ops: ["=", ">", ">=", "<", "<=", "range"], key: true, hint: "key e.g. height" },
    { id: "cf", label: "Custom field", ops: ["=", ">", ">=", "<", "<=", "range"], key: true, hint: "field name" }
  ];
  var fbConds = [];
  var fbInited = false;
  var fbTimer = null;
  var fbInflight = null;

  function fieldDef(id) {
    for (var i = 0; i < FIELDS.length; i++) if (FIELDS[i].id === id) return FIELDS[i];
    return FIELDS[0];
  }

  function newCond() {
    return { field: "kind", op: "is", key: "", value: "", value2: "" };
  }

  function quoteVal(v) {
    return /\s/.test(v) ? '"' + v + '"' : v;
  }

  // condToken compiles one row to a DSL token, or "" while incomplete.
  function condToken(c) {
    var f = fieldDef(c.field);
    var v = (c.value || "").trim();
    if (!v) return "";
    if (f.id === "text") return quoteVal(v);
    var key = f.id;
    if (f.key) {
      var k = (c.key || "").trim();
      if (!k) return "";
      key = f.id + "." + k;
    }
    var neg = c.op === "is not" ? "-" : "";
    var val;
    switch (c.op) {
      case "is": case "is not": case "matches": val = v; break;
      case "=": val = v; break;
      case ">": case ">=": case "<": case "<=": val = c.op + v; break;
      case "range":
        var v2 = (c.value2 || "").trim();
        if (!v2) return "";
        val = v + ".." + v2;
        break;
      case "within last": val = ">" + v; break;
      case "older than": val = "<" + v; break;
      case "after date": val = ">" + v; break;
      case "before date": val = "<" + v; break;
      default: val = v;
    }
    return neg + key + ":" + quoteVal(val);
  }

  function compileDsl() {
    var parts = [];
    fbConds.forEach(function (c) {
      var t = condToken(c);
      if (t) parts.push(t);
    });
    return parts.join(" ");
  }

  function fbRefresh() {
    var dsl = compileDsl();
    document.getElementById("fb-compiled").textContent = dsl || "(no complete conditions yet)";
    clearTimeout(fbTimer);
    fbTimer = setTimeout(function () { fbPreview(dsl); }, 300);
  }

  function fbPreview(dsl) {
    var table = document.getElementById("fb-table");
    var emptyEl = document.getElementById("fb-empty");
    var count = document.getElementById("fb-count");
    var eb = document.getElementById("fb-error");
    if (fbInflight) fbInflight.abort();
    if (!dsl) {
      table.hidden = true;
      emptyEl.hidden = true;
      count.textContent = "";
      eb.hidden = true;
      return;
    }
    var ctrl = new AbortController();
    fbInflight = ctrl;
    count.textContent = "searching…";
    fetch("api/query?q=" + encodeURIComponent(dsl) + "&limit=50",
      { credentials: "same-origin", signal: ctrl.signal })
      .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
      .then(function (r) {
        if (ctrl !== fbInflight) return;
        if (r.status !== 200) {
          eb.hidden = false;
          eb.textContent = r.data && (r.data.reason || r.data.error)
            ? (r.data.error + (r.data.reason ? ": " + r.data.reason : ""))
            : "query failed (HTTP " + r.status + ")";
          table.hidden = true;
          emptyEl.hidden = true;
          count.textContent = "";
          return;
        }
        eb.hidden = true;
        var rows = r.data.rows || [];
        count.textContent = rows.length + (r.data.truncated ? "+" : "") + " match" + (rows.length === 1 ? "" : "es");
        var tbody = document.getElementById("fb-body");
        tbody.textContent = "";
        if (!rows.length) {
          table.hidden = true;
          emptyEl.hidden = false;
          return;
        }
        table.hidden = false;
        emptyEl.hidden = true;
        var frag = document.createDocumentFragment();
        rows.forEach(function (row) {
          var tr = document.createElement("tr");
          var tdK = document.createElement("td");
          tdK.appendChild(kindBadge(row.kind));
          tr.appendChild(tdK);
          var tdN = document.createElement("td");
          tdN.className = "path-cell";
          tdN.textContent = row.filename;
          tdN.title = fullPath(row);
          tr.appendChild(tdN);
          var tdS = document.createElement("td");
          tdS.className = "num";
          tdS.textContent = fmtSize(row.size);
          tr.appendChild(tdS);
          frag.appendChild(tr);
        });
        tbody.appendChild(frag);
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") return;
        eb.hidden = false;
        eb.textContent = "network error: " + (e && e.message ? e.message : e);
      });
  }

  function renderBuilder() {
    var wrap = document.getElementById("fb-rows");
    wrap.textContent = "";
    fbConds.forEach(function (c, idx) {
      var row = document.createElement("div");
      row.className = "fb-row";

      var f = fieldDef(c.field);

      var selF = document.createElement("select");
      FIELDS.forEach(function (fd) {
        var o = document.createElement("option");
        o.value = fd.id;
        o.textContent = fd.label;
        if (fd.id === c.field) o.selected = true;
        selF.appendChild(o);
      });
      selF.addEventListener("change", function () {
        c.field = selF.value;
        var nf = fieldDef(c.field);
        c.op = nf.ops[0];
        c.value = "";
        c.value2 = "";
        renderBuilder();
        fbRefresh();
      });
      row.appendChild(selF);

      if (f.key) {
        var keyIn = document.createElement("input");
        keyIn.type = "text";
        keyIn.className = "fb-key";
        keyIn.placeholder = f.hint || "key";
        keyIn.value = c.key;
        keyIn.addEventListener("input", function () { c.key = keyIn.value; fbRefresh(); });
        row.appendChild(keyIn);
      }

      var selO = document.createElement("select");
      f.ops.forEach(function (op) {
        var o = document.createElement("option");
        o.value = op;
        o.textContent = op;
        if (op === c.op) o.selected = true;
        selO.appendChild(o);
      });
      selO.addEventListener("change", function () { c.op = selO.value; renderBuilder(); fbRefresh(); });
      row.appendChild(selO);

      function valueInput(prop, ph) {
        var inp = document.createElement("input");
        if (f.time && (c.op === "after date" || c.op === "before date" || c.op === "range")) {
          inp.type = "date";
        } else {
          inp.type = "text";
          inp.placeholder = ph;
        }
        inp.className = "fb-value";
        inp.value = c[prop];
        inp.addEventListener("input", function () { c[prop] = inp.value; fbRefresh(); });
        return inp;
      }
      var ph = f.hint || "value";
      if (f.time && (c.op === "within last" || c.op === "older than")) ph = "e.g. 7d (s/m/h/d/w)";
      row.appendChild(valueInput("value", ph));
      if (c.op === "range") {
        var toLbl = document.createElement("span");
        toLbl.className = "fb-to";
        toLbl.textContent = "to";
        row.appendChild(toLbl);
        row.appendChild(valueInput("value2", ph));
      }

      var del = document.createElement("button");
      del.type = "button";
      del.className = "btn fb-del";
      del.textContent = "✕";
      del.setAttribute("aria-label", "Remove condition");
      del.addEventListener("click", function () {
        fbConds.splice(idx, 1);
        renderBuilder();
        fbRefresh();
      });
      row.appendChild(del);

      wrap.appendChild(row);
    });
  }

  function initBuilder() {
    if (fbInited) return;
    fbInited = true;
    fbConds = [newCond()];
    renderBuilder();
    fbRefresh();
    document.getElementById("fb-add").addEventListener("click", function () {
      fbConds.push(newCond());
      renderBuilder();
      fbRefresh();
    });
    document.getElementById("fb-copy").addEventListener("click", function () {
      copyToClipboard(compileDsl(), document.getElementById("fb-copy"));
    });
    document.getElementById("fb-reset").addEventListener("click", function () {
      fbConds = [newCond()];
      renderBuilder();
      fbRefresh();
    });
    document.getElementById("fb-open-search").addEventListener("click", function () {
      var dsl = compileDsl();
      if (!dsl) return;
      selectedKind = "";
      Array.prototype.forEach.call(document.getElementById("chips").children, function (c) {
        c.classList.remove("active");
      });
      qInput.value = dsl;
      showTab("search");
      runSearch();
    });
  }
})();
