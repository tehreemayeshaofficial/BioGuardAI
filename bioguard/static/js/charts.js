/*
 * Dependency-free SVG charts.
 *
 * There is no charting library here on purpose: this tool is meant to run on an
 * isolated hospital network, where a CDN is unreachable at best and an
 * un-pinned third-party script is a liability. Everything below paints plain
 * <svg> nodes, and every page still renders its numbers server-side if this
 * file never loads.
 *
 * Contract: a page emits <script id="chart-data" type="application/json">
 * holding Insights.chart_payload, plus <figure><div class="chart-host"
 * data-chart="kind"></div></figure> placeholders. Each kind maps to a builder
 * that turns the payload into a declarative spec, and one of three painters
 * (line / bars / donut) draws it.
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var PALETTE = ["#0284c7", "#7c3aed", "#db2777", "#ea580c", "#059669",
                 "#4f46e5", "#b45309", "#0891b2", "#be123c", "#65a30d"];
  var RISK = { High: "#ef4444", Medium: "#f59e0b", Low: "#10b981", None: "#64748b" };

  /* ---------------------------------------------------------------- nodes */
  function el(name, attrs, text) {
    var n = document.createElementNS(NS, name), k;
    if (attrs) {
      for (k in attrs) {
        if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) {
          n.setAttribute(k, attrs[k]);
        }
      }
    }
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  /* ------------------------------------------------------------ arithmetic */
  function maxOf(series) {
    var m = 0;
    series.forEach(function (s) {
      (s.data || []).forEach(function (v) {
        if (typeof v === "number" && v > m) m = v;
      });
    });
    return m;
  }

  /* Round an axis maximum up to something a human can read off a gridline. */
  function niceMax(raw, isPercent) {
    if (isPercent) return 100;
    if (!(raw > 0)) return 4;
    var exp = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var f = raw / exp;
    var step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return step * exp;
  }

  function fmt(v, kind) {
    if (v === null || v === undefined) return "";
    if (kind === "pct") return (Math.round(v * 10) / 10) + "%";
    if (kind === "score") return String(Math.round(v * 10) / 10);
    return String(Math.round(v));
  }

  /* ---------------------------------------------------------------- paint */
  function emptyState(host, msg) {
    clear(host);
    var d = document.createElement("div");
    d.className = "chart-empty";
    d.textContent = msg || "No data in this scope.";
    host.appendChild(d);
  }

  /* Frame the plot; returns a box in pixel coordinates matching the host size. */
  function frame(host, W, H, pad) {
    clear(host);
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img" });
    host.appendChild(svg);
    return {
      svg: svg, W: W, H: H,
      x0: pad.left, x1: W - pad.right, y0: pad.top, y1: H - pad.bottom
    };
  }

  function gridAndYAxis(g, box, top, ticks, kind) {
    var i, y, t;
    for (i = 0; i <= ticks; i++) {
      y = Math.round(box.y1 - (box.y1 - box.y0) * (i / ticks)) + 0.5;
      g.appendChild(el("line", {
        x1: box.x0, x2: box.x1, y1: y, y2: y,
        "class": i === 0 ? "axis" : "gridline"
      }));
      t = el("text", { x: box.x0 - 6, y: y + 3, "text-anchor": "end",
                       "class": "tick-label" }, fmt(top * i / ticks, kind));
      g.appendChild(t);
    }
  }

  /* Thin the x labels so they never collide. */
  function labelStride(labels, span) {
    var room = Math.max(2, Math.floor(span / 58));
    return Math.max(1, Math.ceil(labels.length / room));
  }

  /* --------------------------------------------------------- line painter */
  function drawLine(host, spec) {
    var W = Math.max(240, host.clientWidth || 560);
    var H = Math.max(140, host.clientHeight || 220);
    var labels = spec.labels || [];
    var all = spec.series || [];
    var shown = all.filter(function (s) { return !s.hidden; });
    if (!labels.length || !shown.length) return emptyState(host, spec.empty);

    var box = frame(host, W, H, { top: 10, right: 12, bottom: 24, left: 38 });
    var top = niceMax(maxOf(shown), spec.percent);
    var kind = spec.percent ? "pct" : "num";
    var g = el("g", null);
    box.svg.appendChild(g);
    gridAndYAxis(g, box, top, 4, kind);

    var n = labels.length;
    var span = box.x1 - box.x0;
    var rise = box.y1 - box.y0;
    function px(i) { return n < 2 ? box.x0 + span / 2 : box.x0 + span * i / (n - 1); }
    function py(v) { return box.y1 - rise * (v / top); }

    var stride = labelStride(labels, span);
    labels.forEach(function (lab, i) {
      if (i % stride !== 0 && i !== n - 1) return;
      g.appendChild(el("text", {
        x: px(i), y: box.y1 + 15,
        "text-anchor": i === 0 ? "start" : (i === n - 1 ? "end" : "middle"),
        "class": "tick-label"
      }, lab));
    });

    (spec.thresholds || []).forEach(function (t) {
      var y = py(t.value);
      g.appendChild(el("line", { x1: box.x0, x2: box.x1, y1: y, y2: y,
                                "class": "gridline", stroke: t.color || "#94a3b8" }));
      g.appendChild(el("text", { x: box.x1, y: y - 3, "text-anchor": "end",
                                "class": "tick-label", fill: t.color || "#64748b" }, t.label));
    });

    shown.forEach(function (s, idx) {
      var color = s.color || PALETTE[idx % PALETTE.length];
      var pts = [];
      var paths = [];
      s.data.forEach(function (v, i) {
        if (v === null || v === undefined) {
          /* A month with no tests is a gap, not a zero: joining the line would
             invent a fall in resistance that was never measured. */
          if (pts.length > 1) paths.push("M" + pts.join(" L"));
          pts = [];
          return;
        }
        pts.push(px(i).toFixed(1) + "," + py(v).toFixed(1));
      });
      if (pts.length > 1) paths.push("M" + pts.join(" L"));
      if (!paths.length) return;

      if (s.area) {
        g.appendChild(el("path", {
          d: paths.join(" ") + " L" + box.x1 + "," + box.y1 + " L" + box.x0 + "," + box.y1 + " Z",
          "class": "series-area", fill: color, stroke: "none"
        }));
      }
      g.appendChild(el("path", { d: paths.join(" "), "class": "series-line",
                                stroke: color, "stroke-width": s.width || 2,
                                "stroke-dasharray": s.dash || null }));

      var last = -1, i;
      for (i = 0; i < s.data.length; i++) {
        if (s.data[i] !== null && s.data[i] !== undefined) last = i;
      }
      if (last >= 0) {
        var c = el("circle", { cx: px(last), cy: py(s.data[last]), r: 3, fill: color });
        c.appendChild(el("title", null, s.label + ": " + fmt(s.data[last], kind)));
        g.appendChild(c);
      }
    });

    /* One transparent band per bucket gives a native tooltip for free. */
    labels.forEach(function (lab, i) {
      var w = n < 2 ? span : span / (n - 1);
      var x = n < 2 ? box.x0 : Math.max(box.x0, Math.min(px(i) - w / 2, box.x1 - w));
      var rect = el("rect", { x: x, y: box.y0, width: w, height: rise, fill: "transparent" });
      var lines = [lab];
      shown.forEach(function (s) {
        var v = s.data[i];
        lines.push(s.label + ": " + (v === null || v === undefined
                   ? "no data" : fmt(v, kind)));
      });
      rect.appendChild(el("title", null, lines.join("\n")));
      g.appendChild(rect);
    });

    legend(host, all, spec);
  }

  /* Legend sits outside the <svg> so it wraps on narrow screens. Clicking a
     name hides that series and the axis re-scales. */
  function legend(host, series, spec) {
    var figure = host.parentNode;
    if (!figure) return;
    dropLegend(figure);
    if (series.length < 2) return;
    var ul = document.createElement("ul");
    ul.className = "chart-legend";
    series.forEach(function (s, idx) {
      var li = document.createElement("li");
      var sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = s.color || PALETTE[idx % PALETTE.length];
      var lab = document.createElement("button");
      lab.type = "button";
      lab.className = "legend-name";
      lab.textContent = s.label;
      lab.title = s.hidden ? "Show this series" : "Hide this series";
      if (s.hidden) li.className = "legend-off";
      lab.addEventListener("click", function () {
        s.hidden = !s.hidden;
        render(host, spec);
      });
      li.appendChild(sw);
      li.appendChild(lab);
      ul.appendChild(li);
    });
    figure.appendChild(ul);
  }

  function dropLegend(figure) {
    var old = figure.querySelector(":scope > .chart-legend");
    if (old) old.parentNode.removeChild(old);
  }

  /* --------------------------------------------------------- bar painter */
  function drawBars(host, spec) {
    var rows = spec.rows || [];
    if (!rows.length) return emptyState(host, spec.empty);
    var W = Math.max(240, host.clientWidth || 560);
    var H = Math.max(120, host.clientHeight || 220);
    var pad = {
      top: 8, right: 46, bottom: 20,
      left: Math.min(160, Math.max(74, Math.round(W * 0.26)))
    };
    var box = frame(host, W, H, pad);
    var g = el("g", null);
    box.svg.appendChild(g);

    var maxV = spec.max || rows.reduce(function (m, r) {
      return Math.max(m, r.value || 0, r.overlay || 0);
    }, 0);
    if (spec.percent) maxV = Math.max(maxV, 10);
    maxV = maxV > 0 ? maxV : 1;
    var span = box.x1 - box.x0;
    var rowH = (box.y1 - box.y0) / rows.length;
    var barH = Math.max(6, Math.min(spec.barH || 15, rowH - 5));
    var kind = spec.percent ? "pct" : (spec.score ? "score" : "num");
    var ticks = spec.percent ? 4 : 3;
    var t, v, x;

    for (t = 0; t <= ticks; t++) {
      v = maxV * t / ticks;
      x = Math.round(box.x0 + span * t / ticks) + 0.5;
      g.appendChild(el("line", { x1: x, x2: x, y1: box.y0, y2: box.y1,
                                "class": t === 0 ? "axis" : "gridline" }));
      g.appendChild(el("text", {
        x: x, y: box.y1 + 14, "text-anchor": t === 0 ? "start" : "middle",
        "class": "tick-label"
      }, fmt(v, kind)));
    }

    rows.forEach(function (r, i) {
      var cy = box.y0 + rowH * i + rowH / 2;
      var w = Math.max(span * (r.value || 0) / maxV, 1);
      var name = String(r.label || "");
      g.appendChild(el("text", { x: box.x0 - 8, y: cy + 4, "text-anchor": "end",
                                "class": "bar-label" },
                       name.length > 21 ? name.slice(0, 20) + "\u2026" : name));
      g.appendChild(el("rect", { x: box.x0, y: cy - barH / 2, width: w, height: barH,
                                rx: 3, fill: r.color || "#0284c7",
                                opacity: r.muted ? ".4" : "1" }));
      if (r.overlay !== undefined && r.overlay !== null) {
        g.appendChild(el("rect", {
          x: box.x0, y: cy - barH / 2, height: barH, rx: 3,
          width: Math.max(span * r.overlay / maxV, 1),
          fill: r.overlayColor || "#0c4a6e"
        }));
      }
      var label = el("text", { x: box.x0 + w + 6, y: cy + 4, "class": "value-label" },
                     r.text !== undefined ? r.text : fmt(r.value, kind));
      if (r.note) label.appendChild(el("title", null, r.label + " - " + r.note));
      g.appendChild(label);
    });

    (spec.markers || []).forEach(function (m) {
      var mx = box.x0 + span * m.value / maxV;
      g.appendChild(el("line", { x1: mx, x2: mx, y1: box.y0 - 4, y2: box.y1,
                                "class": "gridline", stroke: m.color || "#94a3b8" }));
      g.appendChild(el("text", { x: mx + 2, y: box.y0 + 7, "class": "tick-label",
                                fill: m.color || "#64748b" }, m.label));
    });
  }

  /* ------------------------------------------------------- donut painter */
  function drawDonut(host, spec) {
    var rows = (spec.rows || []).filter(function (r) { return r.value > 0; });
    var total = rows.reduce(function (m, r) { return m + r.value; }, 0);
    if (!total) return emptyState(host, spec.empty);
    var W = Math.max(150, host.clientWidth || 220);
    var H = Math.max(120, host.clientHeight || 190);
    clear(host);
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H,
                         role: "img", "aria-label": spec.aria || "risk mix" });
    host.appendChild(svg);
    var cx = W / 2, cy = H / 2;
    var ro = Math.max(26, Math.min(W, H) / 2 - 6);
    var ri = ro * 0.6;
    var ang = -Math.PI / 2;
    var g = el("g", null);
    svg.appendChild(g);

    rows.forEach(function (r) {
      var sweep = (r.value / total) * Math.PI * 2;
      if (rows.length === 1) {
        g.appendChild(el("circle", { cx: cx, cy: cy, r: (ro + ri) / 2, fill: "none",
                                    stroke: r.color, "stroke-width": ro - ri }));
      } else {
        g.appendChild(arcPath(cx, cy, ro, ri, ang, ang + sweep, r.color));
      }
      ang += sweep;
    });

    g.appendChild(el("circle", { cx: cx, cy: cy, r: ri - 1, fill: "#ffffff" }));
    g.appendChild(el("text", {
      x: cx, y: cy + 3, "text-anchor": "middle",
      style: "font-size:" + Math.round(ri * 0.7) + "px;font-weight:700;fill:#0f172a"
    }, String(total)));
    g.appendChild(el("text", { x: cx, y: cy + Math.round(ri * 0.7) + 5,
                              "text-anchor": "middle", "class": "tick-label" },
                     spec.caption || "organisms"));

    var figure = host.parentNode;
    if (figure) {
      dropLegend(figure);
      var ul = document.createElement("ul");
      ul.className = "chart-legend";
      rows.forEach(function (r) {
        var li = document.createElement("li");
        var sw = document.createElement("span");
        sw.className = "swatch";
        sw.style.background = r.color;
        li.appendChild(sw);
        li.appendChild(document.createTextNode(r.label + " "));
        var b = document.createElement("b");
        b.textContent = String(r.value);
        li.appendChild(b);
        ul.appendChild(li);
      });
      figure.appendChild(ul);
    }
  }

  function arcPath(cx, cy, ro, ri, a1, a2, color) {
    var big = (a2 - a1) > Math.PI ? 1 : 0;
    var d = "M" + (cx + ro * Math.cos(a1)) + "," + (cy + ro * Math.sin(a1)) +
            " A" + ro + "," + ro + " 0 " + big + " 1 " + (cx + ro * Math.cos(a2)) + "," + (cy + ro * Math.sin(a2)) +
            " L" + (cx + ri * Math.cos(a2)) + "," + (cy + ri * Math.sin(a2)) +
            " A" + ri + "," + ri + " 0 " + big + " 0 " + (cx + ri * Math.cos(a1)) + "," + (cy + ri * Math.sin(a1)) + " Z";
    return el("path", { d: d, fill: color, stroke: "#ffffff", "stroke-width": 1 });
  }

  function render(host, spec) {
    if (!spec || !(spec.series || spec.rows)) {
      return emptyState(host, (spec && spec.empty) || "Nothing to plot for this scope.");
    }
    host.__spec = spec;
    if (spec.paint === "bars") drawBars(host, spec);
    else if (spec.paint === "donut") drawDonut(host, spec);
    else drawLine(host, spec);
  }

  /* ------------------------------------------------------------ builders */
  /* Each one maps Insights.chart_payload onto a painter's spec. Keeping the
     mapping here - not in the template - means the JSON API and the page can
     never disagree about what a bar means. */
  var BUILDERS = {
    /* Weekly isolates, one line per organism. */
    timeline: function (p) {
      var tl = p.timeline || {};
      return {
        paint: "line", labels: tl.labels || [], empty: noData(p),
        series: (tl.series || []).map(function (s) {
          return { label: s.display, data: s.data, color: s.color };
        })
      };
    },

    /* Volume vs. distinct patients vs. MDR-or-worse: the three curves an IPC
       reader compares by eye. */
    cases: function (p) {
      var tl = p.timeline || {};
      return {
        paint: "line", labels: tl.labels || [], empty: noData(p),
        series: [
          { label: "Isolates", data: tl.total || [], color: "#0284c7", area: true, width: 2.5 },
          { label: "Patients", data: tl.patients || [], color: "#7c3aed", width: 2 },
          { label: "MDR or worse", data: tl.mdr || [], color: "#ef4444", dash: "5 3", width: 2 }
        ]
      };
    },

    /* Weighted outbreak score per organism, with the band edges drawn on. */
    risk: function (p) {
      var th = p.thresholds || {};
      var hi = th.high || 70, med = th.medium || 40;
      return {
        paint: "bars", score: true, max: 100, empty: noData(p),
        rows: (p.risk || []).map(function (r) {
          return {
            label: r.label, value: r.score, color: RISK[r.level] || RISK.None,
            muted: r.level === "Low",
            text: String(r.score),
            note: r.level + " risk (" + r.score + "/100)"
          };
        }),
        markers: [
          { value: med, label: "medium " + med, color: "#d97706" },
          { value: hi, label: "high " + hi, color: "#dc2626" }
        ]
      };
    },

    mix: function (p) {
      var c = p.risk_mix || {};
      return {
        paint: "donut", caption: "organisms", empty: noData(p),
        rows: [
          { label: "High", value: c.High || 0, color: RISK.High },
          { label: "Medium", value: c.Medium || 0, color: RISK.Medium },
          { label: "Low", value: c.Low || 0, color: RISK.Low }
        ]
      };
    },

    /* Monthly resistance rate per organism; nulls are untested months, drawn
       as gaps rather than a reassuring drop to zero. */
    amr: function (p) {
      var am = p.amr_monthly || {};
      var th = p.thresholds || {};
      return {
        paint: "line", percent: true, labels: am.labels || [],
        empty: "No antibiograms in this scope yet.",
        thresholds: [{ value: th.alert_rate || 20, label: "alert rate",
                       color: "#dc2626" }],
        series: (am.series || []).map(function (s, i) {
          return { label: s.label, data: s.data, color: s.color || PALETTE[i % PALETTE.length] };
        })
      };
    },

    mdr: function (p) {
      return {
        paint: "bars", percent: true, barH: 13, empty: "No susceptibility panels yet.",
        rows: (p.mdr_by_pathogen || []).map(function (r) {
          return {
            label: r.label, value: r.mdr, text: r.mdr + "%",
            color: r.mdr >= 50 ? RISK.High : (r.mdr >= 20 ? RISK.Medium : RISK.Low),
            note: r.mdr + "% MDR/XDR/PDR across " + r.isolates + " tested isolate(s)"
          };
        })
      };
    },

    /* Faint bar = whole dataset, dark bar = the recent window. */
    wards: function (p) {
      return {
        paint: "bars", barH: 12, empty: noData(p),
        rows: (p.wards || []).map(function (w) {
          return {
            label: w.label, value: w.isolates, overlay: w.recent,
            overlayColor: "#075985", color: "#bae6fd",
            note: w.recent + " of " + w.isolates + " isolates in the recent window; " +
                  w.mdr + "% MDR or worse"
          };
        })
      };
    }
  };

  function noData(p) {
    return (p && p.scope && p.scope !== "all data")
      ? "Nothing matched this filter."
      : "No isolates stored yet - upload a lab report.";
  }

  /* ----------------------------------------------------------- bootstrap */
  function readPayload() {
    var tag = document.getElementById("chart-data");
    if (!tag) return {};
    try { return JSON.parse(tag.textContent) || {}; }
    catch (err) { return {}; }
  }

  var DATA = readPayload();

  function renderAll() {
    var hosts = document.querySelectorAll("[data-chart]");
    Array.prototype.forEach.call(hosts, function (host) {
      var build = BUILDERS[host.getAttribute("data-chart")];
      if (!build) return;
      try { render(host, build(DATA, host)); }
      catch (err) { emptyState(host, "This chart could not be drawn."); }
    });
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAll, 180);
  });

  window.BioCharts = {
    renderAll: renderAll,
    /* Re-point every chart at a fresh payload after an upload, without a
       round trip through the server for the HTML. */
    setData: function (next) { DATA = next || {}; renderAll(); },
    reload: function () { DATA = readPayload(); renderAll(); },
    render: render
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderAll);
  } else {
    renderAll();
  }
})();
