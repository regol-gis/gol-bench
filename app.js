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

  function fmtSaving(v) {
    if (v == null) return { text: "—", cls: "" };
    return { text: (v > 0 ? "+" : "") + v.toFixed(1) + "%",
             cls: v >= 2 ? "ratio-good" : v <= -2 ? "ratio-bad" : "" };
  }

  function readerName(reader) {
    return ({regol: "Regol", python: "GeoDesk Python", java: "GeoDesk Java",
             "regol-cli": "Regol CLI", "gol-tool": "GOL Tool CLI"})[reader] || reader;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var SUITE_DESCRIPTIONS = {
    generators: "Same reader, same UK PBF, two file generators. Positive savings mean less time on the Regol-built file. The table reports the median saving across paired rounds. Tagged releases also cover cold opens and CLI exports.",
    writers: "Compiles public Geofabrik .osm.pbf extracts into GOL 2.0 files (1 & 4 CPUs, warm input) and verifies output features. Lower is faster.",
    scale: "Replays an identical 5-step map-editor viewport session per city (streets w[highway], amenities n[amenity], cafés *[amenity=cafe], pan east, and zoom to crossings n[highway=crossing]) across databases from 1 MiB (Monaco) to 3.6 GiB (UK), showing how viewport query latency scales with total file size (1 CPU, warm cache; fresh-process first pass, so GeoDesk Java includes JVM warmup).",
    cli: "Fresh-process command-line counts and exports (csv, geojson, geojsonl) over a Leeds bounding box on the 3.63 GiB UK GOL (2 CPUs), verifying decoded output equivalence.",
    applications: "Ten realistic multi-step OpenStreetMap application workflows replayed against the 3.63 GiB UK GOL (1 CPU), verifying identical feature digests across engines.",
    corpus: "524-query UK corpus grouped into three verified matched cohorts (identity streams, tag decoding, and geometry assembly) plus their combined total (1 CPU, warm cache)."
  };

  function suiteRowsFromLatest(suite) {
    if (!state.latest) return [];
    var golLabel = "GOL Tool " + ((state.latest.competitors && state.latest.competitors.gol_tool) || "2.0.0");
    if (suite === "generators") {
      return (state.latest.generators || []).map(function (r) {
        var workload = r.label + " · " + readerName(r.reader);
        var unit = r.unit || (r.key.indexOf("uk-") === 0 ? "queries" : "steps");
        var coverage = r.verified ? r.cases + " " + (r.cases === 1 ? ({queries: "query", steps: "step", selections: "selection"})[unit] : unit) :
          (r.differences.length ? "Mismatch in " + r.differences.join(", ") : "No matched queries");
        return {label: workload, sub: (r.cores || 1) + ((r.cores || 1) === 1 ? " CPU · " : " CPUs · ") + (r.cache || "warm"), refLabel: "GeoDesk-built",
                regolMs: r.regol_ms, refMs: r.geodesk_ms, saving: r.saved_pct,
                regolRounds: r.verified ? r.regol_rounds_ms : [],
                refRounds: r.verified ? r.geodesk_rounds_ms : [], extra: [coverage]};
      });
    }
    if (suite === "writers") {
      return (state.latest.writers || []).map(function (r) {
        var reg = r.engines.regol || {};
        var ref = r.engines[r.ref_engine || "geodesk-current"] || r.engines.geodesk || {};
        return {
          key: r.key,
          label: r.dataset + " (" + r.cores + " CPU" + (r.cores > 1 ? "s" : "") + ")",
          sub: "PBF → GOL build",
          regolMs: reg.wall_ms_median,
          refMs: ref.wall_ms_median,
          regolRounds: reg.wall_ms_rounds || [],
          refRounds: ref.wall_ms_rounds || [],
          refLabel: r.ref_label || golLabel,
          extra: [
            reg.peak_rss_mib != null ? reg.peak_rss_mib.toFixed(0) + " / " + (ref.peak_rss_mib != null ? ref.peak_rss_mib.toFixed(0) : "—") + " MiB RSS" : "—",
            reg.output_mib != null ? reg.output_mib.toFixed(1) + " / " + (ref.output_mib != null ? ref.output_mib.toFixed(1) : "—") + " MiB GOL" : "—"
          ]
        };
      });
    }
    if (suite === "scale") {
      var cityNote = {
        monaco: "Monaco (5 queries)",
        liechtenstein: "Vaduz (5 queries)",
        bremen: "Bremen (5 queries)",
        ghana: "Accra + Kumasi (10 queries)",
        uk: "Bristol + Belfast (10 queries)"
      };
      var javaVer = (state.latest.competitors && state.latest.competitors.geodesk_java) || "2.1.0";
      return (state.latest.scale || []).map(function (r) {
        var reg = r.engines.regol || {};
        var ref = r.engines.python || {};
        var jv = r.engines.java || {};
        return {
          key: r.key,
          label: r.dataset,
          sub: r.gol_mib + " MiB GOL · " + (cityNote[r.dataset] || "viewport history") + " · 1 CPU warm",
          regolMs: reg.median_ms,
          refMs: ref.median_ms,
          javaMs: jv.median_ms != null ? jv.median_ms : null,
          regolRounds: reg.rounds_ms || [],
          refRounds: ref.rounds_ms || [],
          javaRounds: jv.rounds_ms || [],
          refLabel: "GeoDesk Python",
          javaLabel: jv.median_ms != null ? "GeoDesk Java " + javaVer : null,
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
          refLabel: golLabel,
          extra: [r.selected != null ? r.selected.toLocaleString() : "—"]
        };
      });
    }
    if (suite === "corpus") {
      var corpusJavaVer = (state.latest.competitors && state.latest.competitors.geodesk_java) || "2.1.0";
      return (state.latest.corpus || []).map(function (r) {
        var reg = r.engines.regol || {};
        var ref = r.engines.python || {};
        var jv = r.engines.java || {};
        return {
          key: r.key,
          label: r.label,
          sub: r.cases + " verified matched cases (from 524-query UK spec) · 1 CPU warm",
          regolMs: reg.median_ms,
          refMs: ref.median_ms,
          javaMs: jv.median_ms != null ? jv.median_ms : null,
          regolRounds: reg.rounds_ms || [],
          refRounds: ref.rounds_ms || [],
          javaRounds: jv.rounds_ms || [],
          refLabel: "GeoDesk Python",
          javaLabel: jv.median_ms != null ? "GeoDesk Java " + corpusJavaVer : null,
          extra: [r.cases + " cases"]
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
        refLabel: isCli ? golLabel : "GeoDesk Python",
        extra: [isCli ? "CLI export" : "Reader"]
      };
    });
  }

  function renderLatest() {
    var rows = suiteRowsFromLatest(state.activeSuite);
    var chartEl = document.getElementById("latest-chart");
    var legendEl = document.getElementById("latest-legend");
    var tableEl = document.getElementById("latest-table");
    var subtitleEl = document.getElementById("latest-subtitle");
    if (subtitleEl && SUITE_DESCRIPTIONS[state.activeSuite]) {
      subtitleEl.textContent = SUITE_DESCRIPTIONS[state.activeSuite];
    }

    if (!rows.length) {
      legendEl.innerHTML = "";
      chartEl.innerHTML = "<p>No results recorded for this suite yet.</p>";
      tableEl.innerHTML = "";
      return;
    }

    var generators = state.activeSuite === "generators";
    var measuredLabel = generators ? "Regol-built" : "regol";
    var refName = rows[0].refLabel;
    var hasJava = rows.some(function (r) { return r.javaMs != null; });
    var javaRow = rows.find(function (r) { return r.javaLabel; });
    var javaName = (javaRow && javaRow.javaLabel) || "GeoDesk Java";

    legendEl.innerHTML =
      '<span class="legend-item"><span class="swatch swatch-regol"></span>' + measuredLabel + '</span>' +
      '<span class="legend-item"><span class="swatch swatch-ref"></span>' + esc(refName) + '</span>' +
      (hasJava ? '<span class="legend-item"><span class="swatch swatch-java"></span>' + esc(javaName) + '</span>' : '') +
      '<span class="legend-item">Dots show individual rounds; bars show medians (lower is faster)</span>';

    var maxVal = 0;
    rows.forEach(function (r) {
      var vals = [r.regolMs || 0, r.refMs || 0, r.javaMs || 0].concat(r.regolRounds || [], r.refRounds || [], r.javaRounds || []);
      vals.forEach(function (v) { if (v > maxVal) maxVal = v; });
    });
    if (maxVal <= 0) maxVal = 1;

    chartEl.innerHTML = rows.map(function (r) {
      var pReg = r.regolMs ? Math.max(0.5, (r.regolMs / maxVal) * 100) : 0;
      var pRef = r.refMs ? Math.max(0.5, (r.refMs / maxVal) * 100) : 0;
      var pJava = r.javaMs ? Math.max(0.5, (r.javaMs / maxVal) * 100) : 0;
      var ratio = generators ? fmtSaving(r.saving) : fmtRatio(r.regolMs, r.refMs);
      var regDots = (r.regolRounds || []).map(function (v) {
        return '<span class="bar-dot" style="left:' + ((v / maxVal) * 100).toFixed(2) + '%" title="' + measuredLabel + ' round: ' + fmtMs(v) + '"></span>';
      }).join("");
      var refDots = (r.refRounds || []).map(function (v) {
        return '<span class="bar-dot" style="left:' + ((v / maxVal) * 100).toFixed(2) + '%" title="' + esc(r.refLabel) + ' round: ' + fmtMs(v) + '"></span>';
      }).join("");
      var javaDots = (r.javaRounds || []).map(function (v) {
        return '<span class="bar-dot" style="left:' + ((v / maxVal) * 100).toFixed(2) + '%" title="' + esc(r.javaLabel || javaName) + ' round: ' + fmtMs(v) + '"></span>';
      }).join("");
      var javaTrack = r.javaMs != null
        ? '<div class="bar-track"><div class="bar-fill bar-fill-java" style="width:' + pJava.toFixed(2) + '%"></div>' + javaDots + '</div>'
        : '';
      return (
        '<div class="bar-row">' +
          '<div class="bar-label">' + esc(r.label) + '<small>' + esc(r.sub) + '</small></div>' +
          '<div class="bar-tracks">' +
            '<div class="bar-track"><div class="bar-fill bar-fill-regol" style="width:' + pReg.toFixed(2) + '%"></div>' + regDots + '</div>' +
            '<div class="bar-track"><div class="bar-fill bar-fill-ref" style="width:' + pRef.toFixed(2) + '%"></div>' + refDots + '</div>' +
            javaTrack +
          '</div>' +
          '<div class="bar-values">' +
            '<span class="val-regol">' + fmtMs(r.regolMs) + '</span>' +
            '<span class="val-ref">' + fmtMs(r.refMs) + '</span>' +
            (r.javaMs != null ? '<span class="val-java">' + fmtMs(r.javaMs) + '</span>' : '') +
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
          : state.activeSuite === "corpus"
            ? '<th class="num">Matched cases</th>'
            : '<th>Stage</th>';

    if (generators) extraCols = '<th>Verified coverage</th>';
    var body = rows.map(function (r) {
      var ratio = generators ? fmtSaving(r.saving) : fmtRatio(r.regolMs, r.refMs);
      var javaRatio = hasJava ? fmtRatio(r.regolMs, r.javaMs) : null;
      var extras = r.extra.map(function (c) {
        return '<td class="' + (state.activeSuite === "applications" ? "" : "num") + '">' + esc(c) + '</td>';
      }).join("");
      return (
        '<tr>' +
          '<td><strong>' + esc(r.label) + '</strong></td>' +
          '<td class="num">' + fmtMs(r.regolMs) + '</td>' +
          '<td class="num">' + fmtMs(r.refMs) + '</td>' +
          (hasJava ? '<td class="num">' + fmtMs(r.javaMs) + '</td>' : '') +
          '<td class="num ' + ratio.cls + '">' + esc(ratio.text) + '</td>' +
          (hasJava ? '<td class="num ' + javaRatio.cls + '">' + esc(javaRatio.text) + '</td>' : '') +
          extras +
        '</tr>'
      );
    }).join("");

    tableEl.innerHTML =
      '<thead><tr><th>Workload</th><th class="num">' + measuredLabel + '</th><th class="num">' + esc(refName) + '</th>' +
      (hasJava ? '<th class="num">' + esc(javaName) + '</th><th class="num">vs Python</th><th class="num">vs Java</th>' : '<th class="num">' + (generators ? 'Paired time saved' : 'Relative') + '</th>') +
      extraCols + '</tr></thead>' +
      '<tbody>' + body + '</tbody>';
  }

  /* History chart (uPlot) */
  function populateCaseSelect() {
    var suite = document.getElementById("hist-suite").value;
    var caseSel = document.getElementById("hist-case");
    var metric = document.getElementById("hist-metric");
    var generatorMode = suite === "generators";
    metric.querySelector('[value="saved"]').hidden = !generatorMode;
    metric.querySelector('[value="ratio"]').hidden = generatorMode;
    if (metric.dataset.suite !== suite && generatorMode) metric.value = "saved";
    if (!generatorMode && metric.value === "saved") metric.value = "ms";
    metric.dataset.suite = suite;
    var seriesMap = (state.history && state.history[suite]) || {};
    var keys = Object.keys(seriesMap);
    caseSel.innerHTML = keys.map(function (k) {
      return '<option value="' + esc(k) + '">' + esc(generatorMode ? seriesMap[k][0].label + " · " + readerName(seriesMap[k][0].reader) : k) + '</option>';
    }).join("");
  }

  function renderHistory() {
    var suite = document.getElementById("hist-suite").value;
    var key = document.getElementById("hist-case").value;
    var metric = document.getElementById("hist-metric").value;
    var scaleSelect = document.getElementById("hist-scale");
    scaleSelect.disabled = metric === "saved";
    var scaleMode = scaleSelect.disabled ? "linear" : scaleSelect.value;
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

    var generators = suite === "generators";
    var measuredLabel = generators ? "Regol-built" : "regol";
    if (generators) {
      points = points.map(function (p) { return Object.assign({ref_ms: p.geodesk_ms}, p); });
    }
    var xs = points.map(function (p, i) { return i; });
    var hasJava = points.some(function (p) { return p.java_ms != null; });
    var firstPoint = points.find(function (p) { return p.regol_ms != null; }) || {};
    var firstRegol = firstPoint.regol_ms || 1;
    var firstJavaPt = points.find(function (p) { return p.java_ms != null; });
    var firstJava = (firstJavaPt && firstJavaPt.java_ms) || 1;
    var regSeries = points.map(function (p) {
      if (metric === "saved") return p.saved_pct;
      if (p.regol_ms == null) return null;
      if (metric === "pct") return (p.regol_ms / firstRegol) * 100;
      if (metric === "ratio") return p.ref_ms ? (p.ref_ms / p.regol_ms) : null;
      return p.regol_ms;
    });
    var refSeries = points.map(function (p) {
      if (metric === "saved") return p.verified ? 0 : null;
      if (metric === "ratio") return 1.0;
      if (p.ref_ms == null) return null;
      if (metric === "pct") return (p.ref_ms / (firstPoint.ref_ms || 1)) * 100;
      return p.ref_ms;
    });
    var javaSeries = hasJava ? points.map(function (p) {
      if (p.java_ms == null) return null;
      if (metric === "pct") return (p.java_ms / firstJava) * 100;
      if (metric === "ratio") return p.regol_ms ? (p.java_ms / p.regol_ms) : null;
      return p.java_ms;
    }) : null;

    var accent = cssVar("--accent");
    var refColor = cssVar("--ref");
    var javaColor = cssVar("--java");
    var ink = cssVar("--ink-soft");
    var grid = cssVar("--border");
    var refLabel = generators ? "GeoDesk-built" : (points[0].ref_label || "Reference");

    var yLabel = metric === "saved" ? "% saved" : metric === "ms" ? "ms" : metric === "pct" ? "% of first" : "× vs ref";

    function showPoint(idx) {
      var p = points[idx];
      if (!p) return;
      var tag = p.tag ? ' (' + esc(p.tag) + ')' : '';
      var ratio = generators ? fmtSaving(p.saved_pct) : fmtRatio(p.regol_ms, p.ref_ms);
      var javaPart = "";
      if (p.java_ms != null) {
        var jRatio = fmtRatio(p.regol_ms, p.java_ms);
        javaPart = ' · GeoDesk Java: <strong>' + fmtMs(p.java_ms) + '</strong> (<span class="' + jRatio.cls + '">' + esc(jRatio.text) + '</span>)';
      }
      inspector.innerHTML =
        '<strong><a href="' + REGOL_REPO + '/commit/' + esc(p.commit) + '" rel="noopener">' + esc(p.short_commit) + '</a>' + tag + '</strong> · ' +
        esc(p.timestamp.slice(0, 10)) + ' · ' +
        esc(measuredLabel) + ' <strong>' + fmtMs(p.regol_ms) + '</strong> · ' +
        esc(refLabel) + ': <strong>' + fmtMs(p.ref_ms) + '</strong> ' +
        '(<span class="' + ratio.cls + '">' + esc(ratio.text) + '</span>)' +
        javaPart + ' · ' +
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

    var plotSeries = [
      { label: "Commit" },
      {
        label: metric === "saved" ? "Paired time saved" : metric === "ratio" ? "regol vs " + refLabel : measuredLabel,
        stroke: accent,
        width: 2.5,
        points: { show: true, size: 7, fill: accent }
      },
      {
        label: metric === "saved" ? "Parity (0%)" : metric === "ratio" ? "Parity (1.0×)" : refLabel,
        stroke: refColor,
        width: 2,
        dash: [6, 4],
        points: { show: metric !== "ratio", size: 6, fill: refColor }
      }
    ];
    var plotData = [xs, regSeries, refSeries];
    if (hasJava) {
      plotSeries.push({
        label: metric === "ratio" ? "regol vs GeoDesk Java" : "GeoDesk Java",
        stroke: javaColor,
        width: 2,
        dash: metric === "ratio" ? [] : [2, 3],
        points: { show: true, size: 6, fill: javaColor }
      });
      plotData.push(javaSeries);
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
              if (metric === "pct" || metric === "saved") return v.toFixed(1) + "%";
              return v.toFixed(2) + "×";
            });
          }
        }
      ],
      series: plotSeries,
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

    state.plot = new uPlot(opts, plotData, host);
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
    ["writers", "scale", "cli", "applications", "corpus"].forEach(function (suite) {
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
      ["GeoDesk Java", comps.geodesk_java ? "geodesk " + comps.geodesk_java + (comps.java_runtime ? " (" + comps.java_runtime + ")" : "") : "—"],
      ["GOL Tool", comps.gol_tool ? "v" + comps.gol_tool : "—"]
    ];
    var files = state.latest && state.latest.datasets && state.latest.datasets.uk && state.latest.datasets.uk.generators;
    if (files) {
      items.push(["UK GeoDesk-built file", "GOL Tool " + files.geodesk.revision + " · " + files.geodesk.bytes.toLocaleString() + " bytes"],
                 ["UK Regol-built file", files.regol.revision.slice(0, 7) + " · " + files.regol.bytes.toLocaleString() + " bytes"]);
    }
    envList.innerHTML = items.map(function (pair) {
      return "<dt>" + esc(pair[0]) + "</dt><dd>" + esc(pair[1]) + "</dd>";
    }).join("");
  }

  /* Bind controls */
  function setActiveSuite(suite) {
    var valid = ["writers", "scale", "cli", "applications", "corpus", "generators"];
    if (valid.indexOf(suite) === -1) return;
    state.activeSuite = suite;
    document.querySelectorAll("#latest-tabs .suite-tab").forEach(function (b) {
      var active = b.getAttribute("data-suite") === suite;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-selected", String(active));
    });
    var histSel = document.getElementById("hist-suite");
    if (histSel && histSel.value !== suite) {
      histSel.value = suite;
      if (state.history) {
        populateCaseSelect();
        renderHistory();
      }
    }
    if (state.latest) renderLatest();
  }

  document.querySelectorAll("#latest-tabs .suite-tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setActiveSuite(btn.getAttribute("data-suite"));
    });
  });

  try {
    var initSuite = new URLSearchParams(window.location.search).get("suite");
    if (initSuite) setActiveSuite(initSuite);
  } catch (e) {}

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
