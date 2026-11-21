(function () {
  "use strict";

  var root = document.documentElement;
  var STORAGE_KEY = "regol-theme";
  var REGOL_REPO = "https://github.com/regol-gis/regol";

  /* Theme toggle */
  var themeBtn = document.getElementById("theme-toggle");
  function updateThemeButton() {
    if (!themeBtn) return;
    var mode = root.getAttribute("data-theme") || "auto";
    var labels = {
      auto: "Color theme: System (click to switch to Light)",
      light: "Color theme: Light (click to switch to Dark)",
      dark: "Color theme: Dark (click to switch to System)"
    };
    themeBtn.setAttribute("aria-label", labels[mode]);
    themeBtn.setAttribute("title", labels[mode]);
  }
  if (themeBtn) {
    updateThemeButton();
    themeBtn.addEventListener("click", function () {
      var cur = root.getAttribute("data-theme") || "auto";
      var next = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto";
      if (next === "auto") {
        root.removeAttribute("data-theme");
        try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      } else {
        root.setAttribute("data-theme", next);
        try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
      }
      updateThemeButton();
      if (state.history) renderHistory();
    });
  }

  /* Mobile nav */
  var navToggle = document.getElementById("nav-toggle");
  var siteNav = document.getElementById("site-nav");
  if (navToggle && siteNav) {
    navToggle.addEventListener("click", function () {
      var open = !siteNav.classList.contains("is-open");
      siteNav.classList.toggle("is-open", open);
      navToggle.setAttribute("aria-expanded", String(open));
    });
    siteNav.addEventListener("click", function (ev) {
      if (ev.target.closest("a")) {
        siteNav.classList.remove("is-open");
        navToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  var state = {
    manifest: null,
    history: null,
    latest: null,
    activeSuite: "writers",
    plot: null
  };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMs(v) {
    if (v == null || !isFinite(v)) return "—";
    if (v >= 10000) return (v / 1000).toFixed(2) + " s";
    if (v >= 1000) return (v / 1000).toFixed(3) + " s";
    if (v >= 100) return v.toFixed(1) + " ms";
    return v.toFixed(2) + " ms";
  }

  function fmtRatio(regolMs, refMs) {
    if (!regolMs || !refMs) return { text: "—", cls: "" };
    var speedup = refMs / regolMs;
    if (speedup >= 1.02) return { text: speedup.toFixed(2) + "× faster", cls: "ratio-good" };
    if (speedup <= 0.98) return { text: (1 / speedup).toFixed(2) + "× slower", cls: "ratio-bad" };
    return { text: "≈ parity", cls: "" };
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function suiteRowsFromLatest(suite) {
    if (!state.latest) return [];
    if (suite === "writers") {
      return (state.latest.writers || []).map(function (r) {
        var reg = r.engines.regol || {};
        var ref = r.engines["geodesk-current"] || {};
        return {
          key: r.key,
          label: r.dataset + " (" + r.cores + " CPU" + (r.cores > 1 ? "s" : "") + ")",
          sub: "PBF → GOL build",
          regolMs: reg.wall_ms_median,
          refMs: ref.wall_ms_median,
          regolRounds: reg.wall_ms_rounds || [],
          refRounds: ref.wall_ms_rounds || [],
          refLabel: "GOL Tool 2.5.3",
          extra: [
            reg.peak_rss_mib != null ? reg.peak_rss_mib.toFixed(0) + " / " + (ref.peak_rss_mib != null ? ref.peak_rss_mib.toFixed(0) : "—") + " MiB RSS" : "—",
            reg.output_mib != null ? reg.output_mib.toFixed(1) + " / " + (ref.output_mib != null ? ref.output_mib.toFixed(1) : "—") + " MiB GOL" : "—"
          ]
        };
      });
    }
    if (suite === "scale") {
      return (state.latest.scale || []).map(function (r) {
        var reg = r.engines.regol || {};
        var ref = r.engines.python || {};
        return {
          key: r.key,
          label: r.dataset,
          sub: r.gol_mib + " MiB GOL · 1 CPU · warm",
          regolMs: reg.median_ms,
          refMs: ref.median_ms,
          regolRounds: reg.rounds_ms || [],
          refRounds: ref.rounds_ms || [],
          refLabel: "GeoDesk Python",
          extra: [r.gol_mib + " MiB"]
        };
      });
    }
    if (suite === "cli") {
      return (state.latest.cli || []).map(function (r) {
        var reg = r.engines.Regol || {};
        var ref = r.engines["GOL Tool"] || {};
        return {
          key: r.key,
          label: r.format + " · " + r.query,
          sub: r.dataset + (r.selected != null ? " · " + r.selected.toLocaleString() + " features" : ""),
          regolMs: reg.median_ms,
          refMs: ref.median_ms,
          regolRounds: reg.rounds_ms || [],
          refRounds: ref.rounds_ms || [],
          refLabel: "GOL Tool 2.5.3",
          extra: [r.selected != null ? r.selected.toLocaleString() : "—"]
        };
      });
    }
    return (state.latest.applications || []).map(function (r) {
      var isCli = r.kind === "cli";
      var reg = isCli ? (r.engines["regol-cli"] || {}) : (r.engines.regol || {});
      var ref = isCli ? (r.engines["gol-tool"] || {}) : (r.engines.python || {});
      return {
        key: r.key,
        label: r.label,
        sub: r.interface + " · 1 CPU",
        regolMs: reg.median_ms,
        refMs: ref.median_ms,
        regolRounds: reg.rounds_ms || [],
        refRounds: ref.rounds_ms || [],
        refLabel: isCli ? "GOL Tool 2.5.3" : "GeoDesk Python",
        extra: [isCli ? "CLI export" : "Reader"]
      };
    });
  }

  function renderLatest() {
    var rows = suiteRowsFromLatest(state.activeSuite);
    var chartEl = document.getElementById("latest-chart");
    var legendEl = document.getElementById("latest-legend");
    var tableEl = document.getElementById("latest-table");

    if (!rows.length) {
      legendEl.innerHTML = "";
      chartEl.innerHTML = "<p>No results recorded for this suite yet.</p>";
      tableEl.innerHTML = "";
      return;
    }

    var refName = rows[0].refLabel;
    legendEl.innerHTML =
      '<span class="legend-item"><span class="swatch swatch-regol"></span>regol</span>' +
      '<span class="legend-item"><span class="swatch swatch-ref"></span>' + esc(refName) + '</span>' +
      '<span class="legend-item">Dots show individual rounds; bars show medians (lower is faster)</span>';

    var maxVal = 0;
    rows.forEach(function (r) {
      var vals = [r.regolMs || 0, r.refMs || 0].concat(r.regolRounds, r.refRounds);
      vals.forEach(function (v) { if (v > maxVal) maxVal = v; });
    });
    if (maxVal <= 0) maxVal = 1;

    chartEl.innerHTML = rows.map(function (r) {
      var pReg = r.regolMs ? Math.max(0.5, (r.regolMs / maxVal) * 100) : 0;
      var pRef = r.refMs ? Math.max(0.5, (r.refMs / maxVal) * 100) : 0;
      var ratio = fmtRatio(r.regolMs, r.refMs);
      var regDots = (r.regolRounds || []).map(function (v) {
        return '<span class="bar-dot" style="left:' + ((v / maxVal) * 100).toFixed(2) + '%" title="regol round: ' + fmtMs(v) + '"></span>';
      }).join("");
      var refDots = (r.refRounds || []).map(function (v) {
        return '<span class="bar-dot" style="left:' + ((v / maxVal) * 100).toFixed(2) + '%" title="' + esc(r.refLabel) + ' round: ' + fmtMs(v) + '"></span>';
      }).join("");
      return (
        '<div class="bar-row">' +
          '<div class="bar-label">' + esc(r.label) + '<small>' + esc(r.sub) + '</small></div>' +
          '<div class="bar-tracks">' +
            '<div class="bar-track"><div class="bar-fill bar-fill-regol" style="width:' + pReg.toFixed(2) + '%"></div>' + regDots + '</div>' +
            '<div class="bar-track"><div class="bar-fill bar-fill-ref" style="width:' + pRef.toFixed(2) + '%"></div>' + refDots + '</div>' +
          '</div>' +
          '<div class="bar-values">' +
            '<span class="val-regol">' + fmtMs(r.regolMs) + '</span>' +
            '<span class="val-ref">' + fmtMs(r.refMs) + '</span>' +
            '<span class="' + ratio.cls + '">' + esc(ratio.text) + '</span>' +
          '</div>' +
        '</div>'
      );
    }).join("");

    var extraCols = state.activeSuite === "writers"
      ? '<th class="num">Peak RSS (regol / ref)</th><th class="num">Output size</th>'
      : state.activeSuite === "scale"
        ? '<th class="num">GOL size</th>'
        : state.activeSuite === "cli"
          ? '<th class="num">Features</th>'
          : '<th>Stage</th>';

    var body = rows.map(function (r) {
      var ratio = fmtRatio(r.regolMs, r.refMs);
      var extras = r.extra.map(function (c) {
        return '<td class="' + (state.activeSuite === "applications" ? "" : "num") + '">' + esc(c) + '</td>';
      }).join("");
      return (
        '<tr>' +
          '<td><strong>' + esc(r.label) + '</strong></td>' +
          '<td class="num">' + fmtMs(r.regolMs) + '</td>' +
          '<td class="num">' + fmtMs(r.refMs) + '</td>' +
          '<td class="num ' + ratio.cls + '">' + esc(ratio.text) + '</td>' +
          extras +
        '</tr>'
      );
    }).join("");

    tableEl.innerHTML =
      '<thead><tr><th>Workload</th><th class="num">regol</th><th class="num">' + esc(refName) + '</th><th class="num">Relative</th>' + extraCols + '</tr></thead>' +
      '<tbody>' + body + '</tbody>';
  }

  /* History chart (uPlot) */
  function populateCaseSelect() {
    var suite = document.getElementById("hist-suite").value;
    var caseSel = document.getElementById("hist-case");
    var seriesMap = (state.history && state.history[suite]) || {};
    var keys = Object.keys(seriesMap);
    caseSel.innerHTML = keys.map(function (k) {
      return '<option value="' + esc(k) + '">' + esc(k) + '</option>';
    }).join("");
  }

  function renderHistory() {
    var suite = document.getElementById("hist-suite").value;
    var key = document.getElementById("hist-case").value;
    var metric = document.getElementById("hist-metric").value;
    var scaleMode = document.getElementById("hist-scale").value;
    var host = document.getElementById("hist-plot");
    var note = document.getElementById("hist-note");
    var inspector = document.getElementById("hist-inspector");

    if (state.plot) {
      state.plot.destroy();
      state.plot = null;
    }
    host.innerHTML = "";

    var points = (state.history && state.history[suite] && state.history[suite][key]) || [];
    if (!points.length) {
      inspector.textContent = "No historical data recorded for this workload yet.";
      note.textContent = "";
      return;
    }

    var xs = points.map(function (p, i) { return i; });
    var firstRegol = points[0].regol_ms || 1;
    var regSeries = points.map(function (p) {
      if (p.regol_ms == null) return null;
      if (metric === "pct") return (p.regol_ms / firstRegol) * 100;
      if (metric === "ratio") return p.ref_ms ? (p.ref_ms / p.regol_ms) : null;
      return p.regol_ms;
    });
    var refSeries = points.map(function (p) {
      if (metric === "ratio") return 1.0;
      if (p.ref_ms == null) return null;
      if (metric === "pct") return (p.ref_ms / (points[0].ref_ms || 1)) * 100;
      return p.ref_ms;
    });

    var accent = cssVar("--accent");
    var refColor = cssVar("--ref");
    var ink = cssVar("--ink-soft");
    var grid = cssVar("--border");
    var refLabel = points[0].ref_label || "Reference";

    var yLabel = metric === "ms" ? "ms" : metric === "pct" ? "% of first" : "× vs ref";

    function showPoint(idx) {
      var p = points[idx];
      if (!p) return;
      var tag = p.tag ? ' (' + esc(p.tag) + ')' : '';
      var ratio = fmtRatio(p.regol_ms, p.ref_ms);
      inspector.innerHTML =
        '<strong><a href="' + REGOL_REPO + '/commit/' + esc(p.commit) + '" rel="noopener">' + esc(p.short_commit) + '</a>' + tag + '</strong> · ' +
        esc(p.timestamp.slice(0, 10)) + ' · ' +
        'regol: <strong>' + fmtMs(p.regol_ms) + '</strong> · ' +
        esc(refLabel) + ': <strong>' + fmtMs(p.ref_ms) + '</strong> ' +
        '(<span class="' + ratio.cls + '">' + esc(ratio.text) + '</span>) · ' +
        esc(p.rustc || "");
    }
    showPoint(points.length - 1);

    // Epoch markers where machine_id or rustc changes
    var epochIdx = [];
    for (var i = 1; i < points.length; i++) {
      if (points[i].machine_id !== points[i - 1].machine_id || points[i].rustc !== points[i - 1].rustc) {
        epochIdx.push(i);
      }
    }

    var opts = {
      width: Math.max(300, host.clientWidth || 720),
      height: 320,
      scales: {
        x: { time: false, min: -0.3, max: Math.max(0.3, points.length - 0.7) },
        y: { distr: scaleMode === "log" ? 3 : 1 }
      },
      axes: [
        {
          stroke: ink,
          grid: { stroke: grid, width: 1 },
          splits: function () { return xs; },
          values: function (_, vals) {
            return vals.map(function (v) {
              var p = points[Math.round(v)];
              return p ? (p.tag || p.short_commit) : "";
            });
          }
        },
        {
          stroke: ink,
          grid: { stroke: grid, width: 1 },
          label: yLabel,
          values: function (_, vals) {
            return vals.map(function (v) {
              if (metric === "ms") return v >= 1000 ? (v / 1000).toFixed(1) + "s" : v.toFixed(0);
              if (metric === "pct") return v.toFixed(0) + "%";
              return v.toFixed(2) + "×";
            });
          }
        }
      ],
      series: [
        { label: "Commit" },
        {
          label: metric === "ratio" ? "regol speedup" : "regol",
          stroke: accent,
          width: 2.5,
          points: { show: true, size: 7, fill: accent }
        },
        {
          label: metric === "ratio" ? "Parity (1.0×)" : refLabel,
          stroke: refColor,
          width: 2,
          dash: [6, 4],
          points: { show: metric !== "ratio", size: 6, fill: refColor }
        }
      ],
      hooks: {
        setCursor: [function (u) {
          var idx = u.cursor.idx;
          if (idx != null && points[idx]) showPoint(idx);
        }],
        draw: [function (u) {
          if (!epochIdx.length) return;
          var ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = cssVar("--border-strong");
          ctx.setLineDash([4, 4]);
          ctx.lineWidth = 1.2;
          epochIdx.forEach(function (i) {
            var cx = u.valToPos(i - 0.5, "x", true);
            ctx.beginPath();
            ctx.moveTo(cx, u.bbox.top);
            ctx.lineTo(cx, u.bbox.top + u.bbox.height);
            ctx.stroke();
          });
          ctx.restore();
        }]
      }
    };

    state.plot = new uPlot(opts, [xs, regSeries, refSeries], host);
    note.textContent = points.length === 1
      ? "Initial baseline recorded. Subsequent commits on regol main and new release tags will extend this timeline automatically."
      : points.length + " recorded runs across this workload. Dashed vertical lines (if any) mark host or rustc changes.";
  }

  /* Compare two runs */
  function setupCompare() {
    var runs = (state.manifest && state.manifest.runs) || [];
    var baseSel = document.getElementById("cmp-base");
    var targetSel = document.getElementById("cmp-target");
    var opts = runs.map(function (r, idx) {
      var label = (r.tag || r.short_commit) + " (" + r.timestamp.slice(0, 10) + ")";
      return '<option value="' + idx + '">' + esc(label) + '</option>';
    }).join("");
    baseSel.innerHTML = opts;
    targetSel.innerHTML = opts;
    if (runs.length >= 2) {
      baseSel.value = String(runs.length - 2);
      targetSel.value = String(runs.length - 1);
    }
    baseSel.addEventListener("change", renderCompare);
    targetSel.addEventListener("change", renderCompare);
    renderCompare();
  }

  function renderCompare() {
    var table = document.getElementById("compare-table");
    var runs = (state.manifest && state.manifest.runs) || [];
    if (!runs.length) {
      table.innerHTML = '<tbody><tr><td>No benchmark runs recorded yet.</td></tr></tbody>';
      return;
    }
    var baseRun = runs[Number(document.getElementById("cmp-base").value)] || runs[0];
    var targetRun = runs[Number(document.getElementById("cmp-target").value)] || runs[runs.length - 1];

    var rows = [];
    ["writers", "scale", "cli", "applications"].forEach(function (suite) {
      var map = (state.history && state.history[suite]) || {};
      Object.keys(map).forEach(function (key) {
        var pts = map[key];
        var b = pts.find(function (p) { return p.run_id === baseRun.id; });
        var t = pts.find(function (p) { return p.run_id === targetRun.id; });
        if (!b || !t || b.regol_ms == null || t.regol_ms == null) return;
        var deltaPct = ((t.regol_ms - b.regol_ms) / b.regol_ms) * 100;
        var cls = deltaPct <= -2 ? "ratio-good" : deltaPct >= 2 ? "ratio-bad" : "";
        var sign = deltaPct > 0 ? "+" : "";
        rows.push(
          '<tr>' +
            '<td><code>' + esc(suite) + '</code></td>' +
            '<td><strong>' + esc(key) + '</strong></td>' +
            '<td class="num">' + fmtMs(b.regol_ms) + '</td>' +
            '<td class="num">' + fmtMs(t.regol_ms) + '</td>' +
            '<td class="num ' + cls + '">' + sign + deltaPct.toFixed(1) + '%</td>' +
          '</tr>'
        );
      });
    });

    table.innerHTML =
      '<thead><tr><th>Suite</th><th>Workload</th><th class="num">Base (' + esc(baseRun.tag || baseRun.short_commit) + ')</th><th class="num">Target (' + esc(targetRun.tag || targetRun.short_commit) + ')</th><th class="num">Delta</th></tr></thead>' +
      '<tbody>' + (rows.join("") || '<tr><td colspan="5">No overlapping workloads between these runs.</td></tr>') + '</tbody>';
  }

  function renderMeta() {
    var bar = document.getElementById("status-bar");
    var envList = document.getElementById("env-list");
    var runs = (state.manifest && state.manifest.runs) || [];
    if (!runs.length) {
      bar.textContent = "Awaiting first automated benchmark run from the runner host.";
      return;
    }
    var latest = runs[runs.length - 1];
    var machine = (state.manifest.machines && state.manifest.machines[latest.machine_id]) || {};
    bar.innerHTML =
      '<span><strong>Latest run:</strong> <a href="' + REGOL_REPO + '/commit/' + esc(latest.commit) + '" rel="noopener">' + esc(latest.tag || latest.short_commit) + '</a> (' + esc(latest.timestamp.slice(0, 10)) + ')</span>' +
      '<span><strong>Runs recorded:</strong> ' + runs.length + '</span>' +
      '<span><strong>Host:</strong> ' + esc(machine.label || latest.machine_id) + '</span>';

    var comps = latest.competitors || {};
    var items = [
      ["Hardware", machine.label || latest.machine_id],
      ["Operating system", machine.os || "Linux x86_64"],
      ["Rust compiler", latest.rustc || "—"],
      ["Latest regol", (latest.regol_version || "—") + " (" + latest.short_commit + ")"],
      ["GeoDesk Python", comps.geodesk_python ? "geodesk " + comps.geodesk_python + (comps.shapely ? " / shapely " + comps.shapely : "") : "—"],
      ["GOL Tool", comps.gol_tool ? "v" + comps.gol_tool : "—"]
    ];
    envList.innerHTML = items.map(function (pair) {
      return "<dt>" + esc(pair[0]) + "</dt><dd>" + esc(pair[1]) + "</dd>";
    }).join("");
  }

  /* Bind controls */
  document.querySelectorAll("#latest-tabs .suite-tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.activeSuite = btn.getAttribute("data-suite");
      document.querySelectorAll("#latest-tabs .suite-tab").forEach(function (b) {
        var active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-selected", String(active));
      });
      renderLatest();
    });
  });

  document.getElementById("hist-suite").addEventListener("change", function () {
    populateCaseSelect();
    renderHistory();
  });
  ["hist-case", "hist-metric", "hist-scale"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", renderHistory);
  });
  window.addEventListener("resize", function () {
    if (state.plot) {
      var host = document.getElementById("hist-plot");
      state.plot.setSize({ width: Math.max(300, host.clientWidth || 720), height: 320 });
    }
  });

  /* Load data */
  Promise.all([
    fetch("data/manifest.json").then(function (r) { return r.json(); }),
    fetch("data/history.json").then(function (r) { return r.json(); }),
    fetch("data/latest.json").then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
  ]).then(function (res) {
    state.manifest = res[0];
    state.history = res[1];
    state.latest = res[2];
    renderMeta();
    renderLatest();
    populateCaseSelect();
    renderHistory();
    setupCompare();
  }).catch(function (err) {
    document.getElementById("status-bar").textContent = "Failed to load benchmark data: " + err;
  });
})();
