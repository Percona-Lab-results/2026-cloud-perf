#!/usr/bin/env python3
"""
html_report.py - renders the aggregated benchmark data as a self-contained HTML page.

The page draws its own inline SVG rather than embedding the PNGs: an HTML chart is
interactive by default, so the data-set size is a control above the charts rather
than a row of small multiples, the line chart carries a snapping crosshair with a
readout for every provider, the ranked bars carry a per-row tooltip, and every
figure has a table view beside it.  There is no CDN dependency - the file works
offline and can be copied anywhere.

Called by analyze_results.py; the data contract is build_payload() below.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# The categorical slots are the same documented palette the PNGs use, in the same
# fixed order, so a provider keeps its hue across both outputs.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"]
MARKERS = ["circle", "square", "triangle", "diamond", "triangleDown", "plus"]


def build_payload(aggs: dict, sessions: list, root: Path, specs: dict,
                  pretty, label_dataset, cost_usd, cost_native, fx: dict) -> dict:
    """Shape the Agg objects into the JSON the page's renderer consumes."""
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "root": str(root),
        # Dated and attributed: cost efficiency is only as reproducible as the
        # rate table it was divided by.
        "fx": fx,
        "sessions": [{
            "provider": s.provider, "name": pretty(s.provider), "stamp": s.stamp,
            "bench": s.bench, "rows": len(s.rows), "dropped": s.dropped,
            "dir": s.path.name,
        } for s in sessions],
        "benches": [],
    }
    for bench, agg in aggs.items():
        spec = specs[bench]
        cells = {}
        for (provider, dataset, conc), (mean, lo, hi, runs) in agg.stats.items():
            if provider not in agg.providers:
                continue
            cells[f"{provider}|{dataset}|{conc}"] = {
                "mean": round(mean, 2), "min": round(lo, 2),
                "max": round(hi, 2), "runs": runs,
                # The individual runs behind the mean, in run order.
                "vals": [[r, round(v, 2)] for r, v in
                         agg.runs.get((provider, dataset, conc), [])],
            }
        peaks = {}
        for provider in agg.providers:
            for dataset in agg.datasets:
                value, at = agg.peak(provider, dataset)
                if value is not None:
                    peaks[f"{provider}|{dataset}"] = {"value": round(value, 2), "at": at}
        payload["benches"].append({
            "key": bench,
            "title": spec["title"].split(" - ")[0],
            "metric": spec["metric"].split()[0],
            "metricLong": spec["metric"],
            "concLabel": spec["conc_label"],
            "concShort": spec["conc_short"],
            "scalingTitle": spec["title"],
            "scalingSub": spec["sub"],
            "peakTitle": spec["peak_title"],
            "peakSub": spec["peak_sub"],
            "datasetCol": spec["dataset_col"],
            "concCol": spec["conc_col"],
            "effTitle": spec["eff_title"],
            "effSub": spec["eff_sub"],
            "providers": [dict({
                "key": p, "name": pretty(p),
                "light": SERIES_LIGHT[i], "dark": SERIES_DARK[i],
                "marker": MARKERS[i],
            }, **_price(p, cost_usd, cost_native)) for i, p in enumerate(agg.providers)],
            "datasets": [{"value": d, "label": label_dataset(bench, d)}
                         for d in agg.datasets],
            "concurrency": agg.concurrency,
            "cells": cells,
            "peaks": peaks,
            "incomplete": [{"provider": p, "name": pretty(p), "dataset": d,
                            "datasetLabel": label_dataset(bench, d), "have": have}
                           for p, d, have in agg.incomplete()],
        })
    return payload


def _price(provider: str, cost_usd, cost_native) -> dict:
    usd, native = cost_usd(provider), cost_native(provider)
    return {
        "costUsd": None if usd is None else round(usd, 2),
        "costNative": None if native is None else native[0],
        "costCurrency": None if native is None else native[1],
    }


def write(payload: dict, out: Path) -> Path:
    out.write_text(TEMPLATE.replace("/*__DATA__*/null", json.dumps(payload)),
                   encoding="utf-8")
    return out


# ----------------------------------------------------------------- template ---
# Dark values are declared under both the media query and the data-theme scope:
# the query covers the OS setting, the scope covers the page's own toggle, and
# the toggle has to win both ways.
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Managed MySQL cloud performance - TPS / NOPM</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11, 11, 11, 0.10);
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --series-4: #eda100;
    --series-5: #e87ba4;
  --series-6: #008300;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --plane: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --border: rgba(255, 255, 255, 0.10);
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --series-4: #c98500;
      --series-5: #d55181;
      --series-6: #008300;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --series-5: #d55181;
  --series-6: #008300;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0 0 64px;
    background: var(--plane);
    color: var(--text-primary);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1240px; margin: 0 auto; padding: 0 24px; }

  header.page { padding: 40px 0 8px; display: flex; gap: 24px; align-items: flex-start; }
  header.page .grow { flex: 1 1 auto; min-width: 0; }
  h1 { margin: 0 0 6px; font-size: 26px; font-weight: 650; letter-spacing: -0.01em; }
  .sub { margin: 0; color: var(--text-secondary); font-size: 14px; }
  .meta { margin: 10px 0 0; color: var(--text-muted); font-size: 12.5px; }

  button {
    font: inherit; font-size: 13px; color: var(--text-secondary);
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 12px; cursor: pointer;
  }
  button:hover { color: var(--text-primary); }
  button:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
  button[aria-pressed="true"] { color: var(--text-primary); border-color: var(--axis); }

  section.bench { margin-top: 44px; }
  h2 {
    margin: 0 0 4px; font-size: 19px; font-weight: 650;
    padding-bottom: 10px; border-bottom: 1px solid var(--border);
  }

  figure.card {
    margin: 24px 0 0; padding: 20px 20px 16px;
    background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 12px;
    position: relative;
  }
  figure.card figcaption { margin-bottom: 18px; display: flex; gap: 16px; align-items: flex-start; }
  figcaption .grow { flex: 1 1 auto; min-width: 0; }
  .fig-title { font-size: 16px; font-weight: 600; color: var(--text-primary); }
  .fig-sub { margin-top: 3px; font-size: 13px; color: var(--text-secondary); }

  /* One filter row above everything it scopes - never inside a chart card. */
  .filters {
    display: flex; flex-wrap: wrap; align-items: center;
    gap: 10px 14px; margin-top: 18px;
  }
  .filters .filter-group { display: inline-flex; align-items: center; gap: 10px; }
  .filters .filter-label { font-size: 13px; color: var(--text-secondary); }
  .segmented {
    display: inline-flex; gap: 4px; padding: 4px;
    border: 1px solid var(--border); border-radius: 10px;
  }
  .segmented button {
    background: none; border: 1px solid transparent; border-radius: 7px;
    padding: 6px 14px;
  }
  .segmented button[aria-checked="true"] {
    background: var(--surface-1); border-color: var(--axis);
    color: var(--text-primary); font-weight: 600;
  }

  /* Scoped to the plot: a blanket `svg { width: 100% }` would also stretch the
     legend's key swatch across its button and draw it over the label. */
  .plot { min-width: 0; }
  .plot > svg { display: block; width: 100%; height: auto; overflow: visible; }
  .plot > svg:focus-visible { outline: 2px solid var(--series-1); outline-offset: 3px; border-radius: 4px; }

  .legend { display: flex; flex-wrap: wrap; gap: 6px 4px; justify-content: center; margin-top: 16px; }
  .legend button {
    display: inline-flex; align-items: center; gap: 7px;
    background: none; border: 1px solid transparent; padding: 4px 9px; border-radius: 999px;
    color: var(--text-secondary); font-size: 13px;
  }
  .legend button:hover { background: var(--plane); }
  .legend button[aria-pressed="false"] { opacity: 0.4; }
  .legend .key { flex: 0 0 auto; display: block; width: 22px; height: 12px; overflow: visible; }

  .note { margin-top: 14px; font-size: 12.5px; color: var(--text-muted); }

  .tooltip {
    position: absolute; z-index: 10; pointer-events: none; opacity: 0;
    transition: opacity .09s linear;
    min-width: 150px; padding: 10px 12px;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 9px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, .14);
    font-size: 13px;
  }
  .tooltip[data-open="1"] { opacity: 1; }
  .tt-head { font-size: 12px; color: var(--text-secondary); margin-bottom: 7px; }
  .tt-row { display: flex; align-items: baseline; gap: 8px; margin-top: 4px; }
  .tt-row .stroke { flex: 0 0 12px; height: 2px; border-radius: 1px; align-self: center; }
  .tt-row .val { font-weight: 650; font-variant-numeric: tabular-nums; }
  .tt-row .name { color: var(--text-secondary); font-size: 12.5px; }
  .tt-row .range { color: var(--text-muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }

  .table-wrap { display: none; margin-top: 18px; overflow-x: auto; }
  .table-wrap[data-open="1"] { display: block; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  caption { text-align: left; color: var(--text-secondary); font-size: 12.5px; padding-bottom: 8px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  thead th { color: var(--text-secondary); font-weight: 600; }
  tbody td { font-variant-numeric: tabular-nums; color: var(--text-primary); }
  tbody td .range { color: var(--text-muted); font-size: 11.5px; }
  .swatch { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 7px; vertical-align: baseline; }

  .inventory { margin-top: 28px; }
  .inventory table { max-width: 760px; }
  .warn-dot { color: var(--text-secondary); }

  @media print { body { background: #fff; } figure.card { break-inside: avoid; } }
</style>
</head>
<body>
<div class="wrap">
  <header class="page">
    <div class="grow">
      <h1>Managed MySQL cloud performance</h1>
      <p class="sub">sysbench <code>oltp_read_write</code> throughput (TPS) and HammerDB TPC-C throughput (NOPM) across cloud providers, and what each provider's throughput costs per dollar.</p>
      <p class="meta" id="meta"></p>
    </div>
    <button id="theme-toggle" type="button" aria-live="polite">Theme</button>
  </header>

  <div id="benches"></div>

  <section class="inventory">
    <h2>Instance cost</h2>
    <div id="pricing"></div>
  </section>

  <section class="inventory">
    <h2>Benchmark sessions</h2>
    <div id="inventory"></div>
  </section>
</div>

<script id="payload" type="application/json">/*__DATA__*/null</script>
<script>
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("payload").textContent);
  var root = document.documentElement;

  // ------------------------------------------------------------- helpers ----
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function svgEl(tag, attrs) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) n.setAttribute(k, attrs[k]);
    }
    return n;
  }
  function cssVar(name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  }
  function fmt(v) {
    if (v === null || v === undefined) return "n/a";
    return Math.round(v).toLocaleString("en-US");
  }
  // Throughput per dollar lands in the tens for sysbench, so rounding to a
  // whole number there would throw away a digit that matters.
  function fmtp(v) {
    if (v === null || v === undefined) return "n/a";
    // A decimal only where it carries information: axis ticks land on whole
    // numbers and "20.0" is just noise there.
    if (v >= 100 || Math.abs(v - Math.round(v)) < 0.05) return Math.round(v).toLocaleString("en-US");
    return v.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }
  function usd(v) {
    if (v === null || v === undefined) return "n/a";
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function isDark() {
    var t = root.getAttribute("data-theme");
    if (t === "dark") return true;
    if (t === "light") return false;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  // Color follows the provider, never its rank, so isolating a series in the
  // legend cannot repaint the survivors.
  function colorOf(p) { return isDark() ? p.dark : p.light; }

  // Nice round axis ticks, so the labels carry the values that are not
  // directly labelled. The axis always rounds UP to a whole step - otherwise
  // the top gridline lands under the maximum and the data runs into the frame -
  // and among the round candidates it takes the one closest to the wanted tick
  // count, breaking ties towards the tighter axis so the plot is not left with
  // a band of empty space above the data.
  function ticks(max, target) {
    max = Math.max(max, 1);
    var mag = Math.pow(10, Math.floor(Math.log10(max / target)));
    var best = null;
    [mag / 10, mag, mag * 10].forEach(function (m) {
      [1, 2, 2.5, 5].forEach(function (f) {
        var step = m * f;
        if (!(step > 0)) return;
        var niceMax = Math.ceil(max / step - 0.0000001) * step;
        var n = Math.round(niceMax / step);
        if (n < 3 || n > 12) return;
        var score = [Math.abs(n - target), niceMax, -n];
        if (!best || score[0] < best.score[0] ||
            (score[0] === best.score[0] && (score[1] < best.score[1] ||
            (score[1] === best.score[1] && score[2] < best.score[2])))) {
          best = { step: step, niceMax: niceMax, n: n, score: score };
        }
      });
    });
    if (!best) best = { step: max, niceMax: max, n: 1 };
    var out = [];
    for (var i = 0; i <= best.n; i++) out.push(i * best.step);
    return { ticks: out, top: best.niceMax };
  }

  var MARKER_R = 4.2;   // >= 8px mark
  // The individual-runs view draws three times as many marks, so it asks for a
  // smaller r; the hit target there is the column crosshair, not the mark.
  function marker(shape, x, y, color, ring, r, ringW) {
    var g = svgEl("g", {});
    r = r || MARKER_R;
    var node;
    if (shape === "square") {
      node = svgEl("rect", { x: x - r, y: y - r, width: r * 2, height: r * 2, rx: 0.8 });
    } else if (shape === "triangle") {
      node = svgEl("polygon", { points: [x, y - r * 1.2, x + r * 1.15, y + r * 0.85, x - r * 1.15, y + r * 0.85].join(" ") });
    } else if (shape === "triangleDown") {
      node = svgEl("polygon", { points: [x, y + r * 1.2, x + r * 1.15, y - r * 0.85, x - r * 1.15, y - r * 0.85].join(" ") });
    } else if (shape === "plus") {
      var a = r * 0.42, b = r * 1.2;
      node = svgEl("polygon", { points: [
        x - a, y - b, x + a, y - b, x + a, y - a, x + b, y - a,
        x + b, y + a, x + a, y + a, x + a, y + b, x - a, y + b,
        x - a, y + a, x - b, y + a, x - b, y - a, x - a, y - a].join(" ") });
    } else if (shape === "diamond") {
      node = svgEl("polygon", { points: [x, y - r * 1.3, x + r * 1.1, y, x, y + r * 1.3, x - r * 1.1, y].join(" ") });
    } else {
      node = svgEl("circle", { cx: x, cy: y, r: r });
    }
    node.setAttribute("fill", color);
    // 2px surface ring keeps the mark legible where it crosses another line.
    node.setAttribute("stroke", ring);
    node.setAttribute("stroke-width", ringW || 2);
    g.appendChild(node);
    return g;
  }
  function legendKey(p, color, forBar) {
    var s = svgEl("svg", {
      width: 22, height: 12, viewBox: "0 0 22 12", class: "key",
      style: "width:22px;height:12px;flex:0 0 auto",
    });
    if (forBar) {
      var rect = svgEl("rect", { x: 5, y: 1, width: 12, height: 10, rx: 2, fill: color });
      s.appendChild(rect);
    } else {
      var line = svgEl("line", { x1: 0, y1: 6, x2: 22, y2: 6, stroke: color, "stroke-width": 2, "stroke-linecap": "round" });
      s.appendChild(line);
      s.appendChild(marker(p.marker, 11, 6, color, cssVar("--surface-1")));
    }
    return s;
  }

  // 4px rounded data-end, square at the baseline.
  function barPath(x, w, yTop, yBase, r) {
    r = Math.min(r, w / 2, Math.max(0, yBase - yTop));
    var L = x, R = x + w;
    return "M" + L + "," + yBase +
           " L" + L + "," + (yTop + r) +
           " Q" + L + "," + yTop + " " + (L + r) + "," + yTop +
           " L" + (R - r) + "," + yTop +
           " Q" + R + "," + yTop + " " + R + "," + (yTop + r) +
           " L" + R + "," + yBase + " Z";
  }

  // The same rounding, laid on its side: rounded data-end, square at the baseline.
  function barPathH(y, h, xBase, xEnd, r) {
    r = Math.min(r, h / 2, Math.max(0, xEnd - xBase));
    var T = y, B = y + h;
    return "M" + xBase + "," + T +
           " L" + (xEnd - r) + "," + T +
           " Q" + xEnd + "," + T + " " + xEnd + "," + (T + r) +
           " L" + xEnd + "," + (B - r) +
           " Q" + xEnd + "," + B + " " + (xEnd - r) + "," + B +
           " L" + xBase + "," + B + " Z";
  }

  // One labelled segmented control; single-select, arrow-key navigable.
  function segmented(host, label, options, onPick) {
    var group = el("div", "filter-group");
    group.appendChild(el("span", "filter-label", label));
    var seg = el("div", "segmented");
    seg.setAttribute("role", "radiogroup");
    seg.setAttribute("aria-label", label);
    var btns = options.map(function (o, i) {
      var b = el("button", null, o.label);
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", i === 0 ? "true" : "false");
      b.tabIndex = i === 0 ? 0 : -1;
      seg.appendChild(b);
      return b;
    });
    function select(i, focus) {
      if (i < 0 || i >= btns.length) return;
      btns.forEach(function (b, j) {
        b.setAttribute("aria-checked", j === i ? "true" : "false");
        b.tabIndex = j === i ? 0 : -1;
      });
      if (focus) btns[i].focus();
      onPick(options[i].value);
    }
    btns.forEach(function (b, i) {
      b.addEventListener("click", function () { select(i, false); });
      b.addEventListener("keydown", function (ev) {
        if (ev.key === "ArrowRight" || ev.key === "ArrowDown") { select((i + 1) % btns.length, true); ev.preventDefault(); }
        else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") { select((i - 1 + btns.length) % btns.length, true); ev.preventDefault(); }
      });
    });
    group.appendChild(seg);
    host.appendChild(group);
  }

  function datasetOf(bench, value) {
    for (var i = 0; i < bench.datasets.length; i++) {
      if (bench.datasets[i].value === value) return bench.datasets[i];
    }
    return bench.datasets[0];
  }

  // -------------------------------------------------------------- tooltip ---
  function Tooltip(host) {
    var node = el("div", "tooltip");
    node.setAttribute("role", "status");
    host.appendChild(node);
    return {
      node: node,
      // Values lead, labels follow - the reader has the series and wants the number.
      show: function (head, rows, anchorX, anchorY) {
        node.textContent = "";
        node.appendChild(el("div", "tt-head", head));
        rows.forEach(function (r) {
          var row = el("div", "tt-row");
          var stroke = el("span", "stroke");
          stroke.style.background = r.color;
          row.appendChild(stroke);
          row.appendChild(el("span", "val", r.value));
          row.appendChild(el("span", "name", r.name));
          if (r.range) row.appendChild(el("span", "range", r.range));
          node.appendChild(row);
        });
        node.setAttribute("data-open", "1");
        var hb = host.getBoundingClientRect(), tb = node.getBoundingClientRect();
        var x = anchorX - hb.left + 14, y = anchorY - hb.top - tb.height / 2;
        if (x + tb.width > hb.width - 4) x = anchorX - hb.left - tb.width - 14;
        node.style.left = Math.max(4, x) + "px";
        node.style.top = Math.max(4, Math.min(y, hb.height - tb.height - 4)) + "px";
      },
      hide: function () { node.removeAttribute("data-open"); },
    };
  }

  // ----------------------------------------------------------- line chart ---
  // Throughput against the concurrency sweep for the selected data-set size.
  function scalingFigure(bench, state) {
    var fig = el("figure", "card");
    var cap = el("figcaption");
    var grow = el("div", "grow");
    var titleNode = el("div", "fig-title", bench.scalingTitle);
    grow.appendChild(titleNode);
    var subNode = el("div", "fig-sub", bench.scalingSub);
    grow.appendChild(subNode);
    cap.appendChild(grow);
    var tableBtn = el("button", null, "Table view");
    tableBtn.type = "button";
    tableBtn.setAttribute("aria-pressed", "false");
    cap.appendChild(tableBtn);
    fig.appendChild(cap);

    var plot = el("div", "plot");
    fig.appendChild(plot);
    var legend = el("div", "legend");
    fig.appendChild(legend);
    var tableWrap = el("div", "table-wrap");
    fig.appendChild(tableWrap);
    var tip = Tooltip(fig);

    var hidden = {};
    tableBtn.addEventListener("click", function () {
      var open = tableWrap.getAttribute("data-open") === "1";
      tableWrap.setAttribute("data-open", open ? "0" : "1");
      tableBtn.setAttribute("aria-pressed", open ? "false" : "true");
    });
    function cell(p, d, c) { return bench.cells[p + "|" + d + "|" + c] || null; }

    function render() {
      plot.textContent = "";
      legend.textContent = "";
      tip.hide();

      var d = datasetOf(bench, state.dataset);
      var individual = state.mode === "individual";
      titleNode.textContent = bench.scalingTitle + " - " + d.label;
      subNode.textContent = individual
        ? "One line per run over the measured 20 min window - not averaged."
        : bench.scalingSub;

      var surface = cssVar("--surface-1"), grid = cssVar("--grid"),
          axis = cssVar("--axis"), muted = cssVar("--text-muted"),
          secondary = cssVar("--text-secondary"),
          primary = cssVar("--text-primary"), border = cssVar("--border");

      // The y-scale spans every data set, not just the selected one, so
      // switching the control does not silently rescale the comparison.
      var max = 0;
      bench.providers.forEach(function (p) {
        if (hidden[p.key]) return;
        bench.datasets.forEach(function (ds) {
          bench.concurrency.forEach(function (c) {
            var v = cell(p.key, ds.value, c);
            if (v) max = Math.max(max, v.max);
          });
        });
      });
      var scale0 = ticks(max, 6);
      var tk = scale0.ticks, top = scale0.top;

      var W = 1100, H = 520, M = { t: 22, r: 132, b: 58, l: 86 };
      var pw = W - M.l - M.r, ph = H - M.t - M.b;
      var xs = bench.concurrency.map(function (_, i) {
        return M.l + (bench.concurrency.length === 1 ? pw / 2 : (pw * i) / (bench.concurrency.length - 1));
      });
      function yOf(v) { return M.t + ph - (v / top) * ph; }

      var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img", tabindex: "0" });
      svg.setAttribute("aria-label", bench.scalingTitle + ", " + d.label +
        ". Use the arrow keys to read each " + bench.concLabel.toLowerCase() + " level.");

      // Recessive chrome: hairline solid gridlines, no top/right frame.
      tk.forEach(function (t) {
        svg.appendChild(svgEl("line", {
          x1: M.l, y1: yOf(t), x2: M.l + pw, y2: yOf(t),
          stroke: grid, "stroke-width": 1,
        }));
        var lab = svgEl("text", {
          x: M.l - 11, y: yOf(t) + 4, "text-anchor": "end", fill: muted, "font-size": 12.5,
        });
        lab.textContent = fmt(t);
        svg.appendChild(lab);
      });
      svg.appendChild(svgEl("line", { x1: M.l, y1: M.t, x2: M.l, y2: M.t + ph, stroke: axis, "stroke-width": 1 }));
      svg.appendChild(svgEl("line", { x1: M.l, y1: M.t + ph, x2: M.l + pw, y2: M.t + ph, stroke: axis, "stroke-width": 1 }));

      bench.concurrency.forEach(function (c, i) {
        var t = svgEl("text", { x: xs[i], y: M.t + ph + 21, "text-anchor": "middle", fill: muted, "font-size": 12.5 });
        t.textContent = c;
        svg.appendChild(t);
      });
      var xlab = svgEl("text", { x: M.l + pw / 2, y: H - 10, "text-anchor": "middle", fill: secondary, "font-size": 13 });
      xlab.textContent = bench.concLabel;
      svg.appendChild(xlab);
      var ylab = svgEl("text", {
        fill: secondary, "font-size": 13, "text-anchor": "middle",
        transform: "translate(17," + (M.t + ph / 2) + ") rotate(-90)",
      });
      ylab.textContent = bench.metricLong;
      svg.appendChild(ylab);

      var crosshair = svgEl("line", {
        x1: 0, y1: M.t, x2: 0, y2: M.t + ph,
        stroke: axis, "stroke-width": 1, opacity: 0,
      });
      svg.appendChild(crosshair);

      var ends = [];
      bench.providers.forEach(function (p) {
        if (hidden[p.key]) return;
        var color = colorOf(p);
        var pts = [];
        bench.concurrency.forEach(function (c, i) {
          var v = cell(p.key, d.value, c);
          if (v) pts.push({ i: i, x: xs[i], v: v });
        });
        if (!pts.length) return;
        if (individual) {
          // One polyline per run. No min-max band: the runs *are* the spread,
          // and a wash behind them would only restate it.
          var byRun = {};
          pts.forEach(function (q) {
            (q.v.vals || []).forEach(function (rv) {
              (byRun[rv[0]] = byRun[rv[0]] || []).push({ i: q.i, x: q.x, value: rv[1] });
            });
          });
          Object.keys(byRun).sort(function (a, b) { return a - b; }).forEach(function (rk) {
            var rp = byRun[rk];
            if (rp.length > 1) {
              svg.appendChild(svgEl("polyline", {
                points: rp.map(function (q) { return q.x + "," + yOf(q.value); }).join(" "),
                fill: "none", stroke: color, "stroke-width": 1.6, opacity: 0.85,
                "stroke-linecap": "round", "stroke-linejoin": "round",
              }));
            }
            rp.forEach(function (q) {
              svg.appendChild(marker(p.marker, q.x, yOf(q.value), color, surface, 3.6, 1.5));
            });
          });
        } else {
          // min-max band as a 10% wash, never a saturated block.
          if (pts.length > 1) {
            var up = pts.map(function (q) { return q.x + "," + yOf(q.v.max); });
            var down = pts.slice().reverse().map(function (q) { return q.x + "," + yOf(q.v.min); });
            svg.appendChild(svgEl("polygon", {
              points: up.concat(down).join(" "), fill: color, opacity: 0.10,
            }));
          }
          svg.appendChild(svgEl("polyline", {
            points: pts.map(function (q) { return q.x + "," + yOf(q.v.mean); }).join(" "),
            fill: "none", stroke: color, "stroke-width": 2,
            "stroke-linecap": "round", "stroke-linejoin": "round",
          }));
          pts.forEach(function (q) {
            svg.appendChild(marker(p.marker, q.x, yOf(q.v.mean), color, surface));
          });
        }
        var last = pts[pts.length - 1];
        // Only a series that reaches the end of the sweep gets an end label; a
        // truncated one would drop its label mid-plot, on top of the others.
        if (last.i === bench.concurrency.length - 1) {
          ends.push({ p: p, color: color, y: yOf(last.v.mean), value: last.v.mean });
        }
      });

      // Lines -> one value at the end of each, in the margin. Labels that would
      // collide are stacked to a minimum pitch and tied back to their own line
      // with a leader, rather than dropped - two providers finishing a few
      // pixels apart is exactly when the reader wants both numbers. In
      // individual mode the label is the run mean, keyed by a full-size mark;
      // the run ends bracket it.
      var LPITCH = 17, lx = M.l + pw + 12;
      ends.sort(function (a, b) { return a.y - b.y; });
      var prevL = -Infinity;
      ends.forEach(function (e) {
        e.ly = Math.max(e.y, prevL + LPITCH);
        prevL = e.ly;
      });
      var spill = prevL - (M.t + ph);
      if (spill > 0 && ends.length) {
        var lift = Math.min(spill, Math.max(0, ends[0].ly - M.t));
        ends.forEach(function (e) { e.ly -= lift; });
      }
      ends.forEach(function (e) {
        if (Math.abs(e.ly - e.y) > 3) {
          svg.appendChild(svgEl("polyline", {
            points: [M.l + pw + 3, e.y, lx - 4, e.ly, lx + 1, e.ly].join(" "),
            fill: "none", stroke: e.color, "stroke-width": 1, opacity: 0.55,
          }));
        }
        svg.appendChild(marker(e.p.marker, lx + 5, e.ly, e.color, surface));
        var t = svgEl("text", { x: lx + 16, y: e.ly + 4.5, fill: secondary, "font-size": 12.5 });
        t.textContent = fmt(e.value);
        svg.appendChild(t);
      });

      // The crosshair finds the X: readers aim at a thread level, not a line.
      var active = -1;
      function focusAt(idx, clientX, clientY) {
        if (idx < 0 || idx >= bench.concurrency.length) return;
        active = idx;
        var box = svg.getBoundingClientRect();
        var scale = box.width / W;
        crosshair.setAttribute("x1", xs[idx]);
        crosshair.setAttribute("x2", xs[idx]);
        crosshair.setAttribute("opacity", 1);
        var rows = [];
        bench.providers.forEach(function (p) {
          if (hidden[p.key]) return;
          var v = cell(p.key, d.value, bench.concurrency[idx]);
          rows.push({
            color: colorOf(p), name: p.name,
            value: v ? fmt(v.mean) : "no data",
            range: !v ? "" : individual
              ? "runs: " + (v.vals || []).map(function (rv) { return fmt(rv[1]); }).join(", ")
              : fmt(v.min) + "\u2013" + fmt(v.max) + " over " + v.runs + " run" + (v.runs === 1 ? "" : "s"),
          });
        });
        rows.sort(function (a, b) {
          return (parseFloat(b.value.replace(/,/g, "")) || -1) - (parseFloat(a.value.replace(/,/g, "")) || -1);
        });
        var ax = clientX === undefined ? box.left + xs[idx] * scale : clientX;
        var ay = clientY === undefined ? box.top + box.height / 2 : clientY;
        tip.show(d.label + " - " + bench.concLabel.toLowerCase() + " " + bench.concurrency[idx], rows, ax, ay);
      }
      function clear() {
        active = -1;
        crosshair.setAttribute("opacity", 0);
        tip.hide();
      }
      svg.addEventListener("pointermove", function (ev) {
        var box = svg.getBoundingClientRect();
        var ux = ((ev.clientX - box.left) / box.width) * W;
        var best = 0, bd = Infinity;
        xs.forEach(function (x, i) {
          var dd = Math.abs(x - ux);
          if (dd < bd) { bd = dd; best = i; }
        });
        focusAt(best, ev.clientX, ev.clientY);
      });
      svg.addEventListener("pointerleave", clear);
      // Same details on keyboard focus as on hover.
      svg.addEventListener("focus", function () { focusAt(active < 0 ? 0 : active); });
      svg.addEventListener("blur", clear);
      svg.addEventListener("keydown", function (ev) {
        if (ev.key === "ArrowRight") { focusAt(Math.min((active < 0 ? -1 : active) + 1, bench.concurrency.length - 1)); ev.preventDefault(); }
        else if (ev.key === "ArrowLeft") { focusAt(Math.max((active < 0 ? 1 : active) - 1, 0)); ev.preventDefault(); }
        else if (ev.key === "Escape") { clear(); }
      });

      plot.appendChild(svg);
      buildLegend(legend, bench, hidden, false, render);
      buildSweepTable(tableWrap, bench, individual);
    }

    render();
    fig.addEventListener("viz:rerender", render);
    return fig;
  }

  // ------------------------------------------------------ ranked bar chart --
  // With the data set chosen by the control this is a ranking of five named
  // categories, so it is bars with the names on the axis - no legend needed,
  // and the value rides the tip of each bar.
  // Same ranked-bar figure for two measures: throughput, and throughput per
  // dollar. One renderer, because the only difference is the divisor - a second
  // copy would drift.
  function peakFigure(bench, state, eff) {
    var fig = el("figure", "card");
    var cap = el("figcaption");
    var grow = el("div", "grow");
    var titleNode = el("div", "fig-title", eff ? bench.effTitle : bench.peakTitle);
    grow.appendChild(titleNode);
    var subNode = el("div", "fig-sub", eff ? bench.effSub : bench.peakSub);
    grow.appendChild(subNode);
    cap.appendChild(grow);
    var tableBtn = el("button", null, "Table view");
    tableBtn.type = "button";
    tableBtn.setAttribute("aria-pressed", "false");
    cap.appendChild(tableBtn);
    fig.appendChild(cap);

    var plot = el("div", "plot");
    fig.appendChild(plot);
    var tableWrap = el("div", "table-wrap");
    fig.appendChild(tableWrap);
    var tip = Tooltip(fig);

    tableBtn.addEventListener("click", function () {
      var open = tableWrap.getAttribute("data-open") === "1";
      tableWrap.setAttribute("data-open", open ? "0" : "1");
      tableBtn.setAttribute("aria-pressed", open ? "false" : "true");
    });

    function peak(p, d) { return bench.peaks[p + "|" + d] || null; }

    function render() {
      plot.textContent = "";
      tip.hide();

      var d = datasetOf(bench, state.dataset);
      var individual = state.mode === "individual";
      var nf = eff ? fmtp : fmt;
      titleNode.textContent = (eff ? bench.effTitle : bench.peakTitle) + " - " + d.label;
      subNode.textContent = (eff ? bench.effSub : bench.peakSub) + (individual
        ? " Each run marked on the bar." : "");

      var surface = cssVar("--surface-1"), grid = cssVar("--grid"),
          axis = cssVar("--axis"), muted = cssVar("--text-muted"),
          secondary = cssVar("--text-secondary"), primary = cssVar("--text-primary");

      // In cost mode every value is divided by what the instance cost. An
      // unpriced provider is dropped rather than drawn at zero, which would
      // read as "no throughput per dollar" instead of "price unknown".
      function div(p) { return eff ? (p.costUsd || 0) : 1; }
      function peakOf(p, dsv) {
        var q = peak(p.key, dsv), c = div(p);
        return (!q || !c) ? null : { value: q.value / c, at: q.at, raw: q.value };
      }
      function detailOf(p, dsv, at) {
        var c0 = bench.cells[p.key + "|" + dsv + "|" + at], c = div(p);
        return (!c0 || !c) ? null : {
          min: c0.min / c, max: c0.max / c, runs: c0.runs,
          vals: c0.vals.map(function (rv) { return rv[1] / c; }),
        };
      }

      // One shared scale across the data sets, so the bars do not re-stretch
      // when the control changes.
      var max = 0;
      bench.providers.forEach(function (p) {
        bench.datasets.forEach(function (ds) {
          var q = peakOf(p, ds.value);
          if (!q) return;
          max = Math.max(max, q.value);
          // A single run can sit above the mean it fed, so in individual mode
          // the axis has to reach the marks, not just the bar ends.
          var c0 = individual ? detailOf(p, ds.value, q.at) : null;
          if (c0) c0.vals.forEach(function (rv) { max = Math.max(max, rv); });
        });
      });
      var scale0 = ticks(max, 6);
      var tk = scale0.ticks, top = scale0.top;

      // Rows are ordered by value - the chart's job is the ranking - but the
      // hue still follows the provider, so reordering never repaints anyone.
      var rows = bench.providers.map(function (p) {
        return { p: p, q: peakOf(p, d.value) };
      });
      rows.sort(function (a, b) {
        return (b.q ? b.q.value : -1) - (a.q ? a.q.value : -1);
      });

      var PITCH = 56, BAR = 24;                     // bars capped at 24px
      var W = 1100, M = { t: 16, r: 268, b: 50, l: 146 };
      var ph = PITCH * rows.length;
      var H = M.t + ph + M.b;
      var pw = W - M.l - M.r;
      function xOf(v) { return M.l + (v / top) * pw; }

      var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });
      svg.setAttribute("aria-label", (eff ? bench.effTitle : bench.peakTitle) + ", "
        + d.label + ". " + (eff ? bench.effSub : bench.peakSub) + ".");

      tk.forEach(function (t) {
        svg.appendChild(svgEl("line", {
          x1: xOf(t), y1: M.t, x2: xOf(t), y2: M.t + ph, stroke: grid, "stroke-width": 1,
        }));
        var lab = svgEl("text", { x: xOf(t), y: M.t + ph + 22, "text-anchor": "middle", fill: muted, "font-size": 12.5 });
        lab.textContent = nf(t);
        svg.appendChild(lab);
      });
      svg.appendChild(svgEl("line", { x1: M.l, y1: M.t, x2: M.l, y2: M.t + ph, stroke: axis, "stroke-width": 1 }));
      var xlab = svgEl("text", { x: M.l + pw / 2, y: H - 8, "text-anchor": "middle", fill: secondary, "font-size": 13 });
      xlab.textContent = eff ? bench.metric + " per USD of spend" : bench.metricLong;
      svg.appendChild(xlab);

      rows.forEach(function (r, i) {
        var yTop = M.t + i * PITCH, yc = yTop + PITCH / 2;
        var g = svgEl("g", { tabindex: "0", role: "img" });
        g.setAttribute("aria-label", r.p.name + ", " + d.label + ": " + (r.q
          ? nf(r.q.value) + " " + bench.metric + (eff ? " per USD" : "")
            + " at " + bench.concShort + "=" + r.q.at
          : eff && !r.p.costUsd ? "not priced" : "no data"));

        var name = svgEl("text", { x: M.l - 14, y: yc + 4.5, "text-anchor": "end", fill: secondary, "font-size": 13 });
        name.textContent = r.p.name;
        g.appendChild(name);
        // Identity comes from the colored mark beside the text, never from
        // coloring the text itself.
        g.appendChild(svgEl("rect", {
          x: M.l - 9, y: yc - 4.5, width: 9, height: 9, rx: 2, fill: colorOf(r.p),
        }));

        if (!r.q) {
          var none = svgEl("text", { x: M.l + 12, y: yc + 4.5, fill: muted, "font-size": 12.5 });
          none.textContent = eff && !r.p.costUsd ? "not priced" : "no data";
          g.appendChild(none);
          svg.appendChild(g);
          return;
        }

        var color = colorOf(r.p);
        var bar = svgEl("path", {
          d: barPathH(yc - BAR / 2, BAR, M.l, xOf(r.q.value), 4), fill: color,
        });
        g.appendChild(bar);
        // Bars -> value at the tip, outside the bar end so it is never clipped.
        // The mean leads; the level that reached it and the min-max spread of
        // the runs behind it follow underneath.
        var at = detailOf(r.p, d.value, r.q.at);
        var tipFrom = r.q.value;
        if (individual && at) {
          at.vals.forEach(function (rv) { tipFrom = Math.max(tipFrom, rv); });
        }
        var tipX = xOf(tipFrom) + 12 + (individual ? 4 : 0);
        var v1 = svgEl("text", { x: tipX, y: yc - 1, fill: primary, "font-size": 13, "font-weight": 600 });
        v1.textContent = nf(r.q.value);
        g.appendChild(v1);
        var v2 = svgEl("text", { x: tipX, y: yc + 12, fill: muted, "font-size": 10.5 });
        // In cost mode the second line carries the two numbers the ratio came
        // from, so the bar is never an unexplained quotient.
        v2.textContent = eff
          ? fmt(r.q.raw) + " / " + usd(r.p.costUsd)
          : bench.concShort + "=" + r.q.at + (!at ? "" : individual
            ? "  \u00b7  " + at.vals.map(function (rv) { return fmt(rv); }).join("  \u00b7  ")
            : "  \u00b7  " + fmt(at.min) + "\u2013" + fmt(at.max) + " / n=" + at.runs);
        g.appendChild(v2);

        // The bar is the mean; in individual mode each run that went into it is
        // marked where it actually landed, so the average never hides a flyer.
        if (individual && at) {
          at.vals.forEach(function (rv) {
            g.appendChild(marker("circle", xOf(rv), yc, color, surface, 3.6, 2));
          });
        }

        // The hit target is the whole row, far bigger than the mark.
        var hit = svgEl("rect", { x: M.l, y: yTop, width: pw, height: PITCH, fill: "transparent" });
        g.appendChild(hit);

        function show(ev) {
          bar.setAttribute("stroke", surface);
          bar.setAttribute("stroke-width", 2);
          var box = svg.getBoundingClientRect(), scale = box.width / W;
          var c0 = at;
          tip.show(r.p.name + " - " + d.label, [{
            color: color,
            name: bench.metric + (eff ? " per USD" : "") + " at " + bench.concShort + "=" + r.q.at,
            value: nf(r.q.value),
            range: eff
              ? fmt(r.q.raw) + " " + bench.metric + " / " + usd(r.p.costUsd) + " ("
                + r.p.costNative + " " + r.p.costCurrency + ")"
              : !c0 ? "" : individual
                ? "runs: " + c0.vals.map(function (rv) { return fmt(rv); }).join(", ")
                : fmt(c0.min) + "\u2013" + fmt(c0.max) + " over " + c0.runs +
                  " run" + (c0.runs === 1 ? "" : "s"),
          }], ev && ev.clientX !== undefined ? ev.clientX : box.left + xOf(r.q.value) * scale,
             ev && ev.clientY !== undefined ? ev.clientY : box.top + yc * scale);
        }
        function hide() {
          bar.removeAttribute("stroke");
          bar.removeAttribute("stroke-width");
          tip.hide();
        }
        g.addEventListener("pointermove", show);
        g.addEventListener("pointerleave", hide);
        g.addEventListener("focus", show);
        g.addEventListener("blur", hide);
        svg.appendChild(g);
      });

      plot.appendChild(svg);
      buildPeakTable(tableWrap, bench, eff);
    }

    render();
    fig.addEventListener("viz:rerender", render);
    return fig;
  }

  // --------------------------------------------------------------- legend ---
  function buildLegend(host, bench, hidden, forBar, rerender) {
    bench.providers.forEach(function (p) {
      var b = el("button", null, null);
      b.type = "button";
      b.setAttribute("aria-pressed", hidden[p.key] ? "false" : "true");
      b.appendChild(legendKey(p, colorOf(p), forBar));
      b.appendChild(el("span", null, p.name));
      b.title = "Show or hide " + p.name;
      b.addEventListener("click", function () {
        var next = !hidden[p.key];
        var visible = bench.providers.filter(function (q) { return !hidden[q.key]; });
        if (next && visible.length <= 1) return;   // never empty the chart
        hidden[p.key] = next;
        rerender();
      });
      host.appendChild(b);
    });
  }

  // ----------------------------------------------------------- table views --
  function tableShell(host, caption) {
    host.textContent = "";
    var t = el("table");
    var cap = el("caption", null, caption);
    t.appendChild(cap);
    host.appendChild(t);
    return t;
  }
  function providerCell(p) {
    var td = el("td");
    var sw = el("span", "swatch");
    sw.style.background = colorOf(p);
    td.appendChild(sw);
    td.appendChild(document.createTextNode(p.name));
    return td;
  }

  function buildSweepTable(host, bench, individual) {
    var t = tableShell(host, bench.metricLong + " - mean across runs, with "
      + (individual ? "each run's own value" : "the min-max range") + ". "
      + "Every data set, not just the selected one, so the controls never hide a value.");
    var thead = el("thead"), hr = el("tr");
    hr.appendChild(el("th", null, "Provider"));
    hr.appendChild(el("th", null, "Data set"));
    bench.concurrency.forEach(function (c) {
      hr.appendChild(el("th", null, bench.concLabel + " " + c));
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tb = el("tbody");
    bench.providers.forEach(function (p) {
      bench.datasets.forEach(function (d) {
        var tr = el("tr");
        tr.appendChild(providerCell(p));
        tr.appendChild(el("td", null, d.label));
        bench.concurrency.forEach(function (c) {
          var v = bench.cells[p.key + "|" + d.value + "|" + c];
          var td = el("td");
          if (!v) {
            td.appendChild(document.createTextNode("-"));
          } else {
            td.appendChild(document.createTextNode(fmt(v.mean)));
            td.appendChild(el("div", "range", individual
              ? (v.vals || []).map(function (rv) { return fmt(rv[1]); }).join(" \u00b7 ")
              : fmt(v.min) + "\u2013" + fmt(v.max) + " / n=" + v.runs));
          }
          tr.appendChild(td);
        });
        tb.appendChild(tr);
      });
    });
    t.appendChild(tb);
  }

  function buildPeakTable(host, bench, eff) {
    var t = tableShell(host, eff
      ? "Peak " + bench.metric + " per USD of instance spend, with the peak and the price "
        + "it was divided by. FX as of " + DATA.fx.asof + " (" + DATA.fx.source + ")."
      : "Peak " + bench.metric + " per provider and data set, with the "
        + bench.concLabel.toLowerCase() + " level that reached it. Every data set, not just "
        + "the selected one.");
    var thead = el("thead"), hr = el("tr");
    hr.appendChild(el("th", null, "Provider"));
    if (eff) hr.appendChild(el("th", null, "Cost"));
    bench.datasets.forEach(function (d) { hr.appendChild(el("th", null, d.label)); });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tb = el("tbody");
    bench.providers.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(providerCell(p));
      if (eff) {
        var ct = el("td");
        ct.appendChild(document.createTextNode(usd(p.costUsd)));
        if (p.costCurrency && p.costCurrency !== "USD") {
          ct.appendChild(el("div", "range", p.costNative + " " + p.costCurrency));
        }
        tr.appendChild(ct);
      }
      bench.datasets.forEach(function (d) {
        var q = bench.peaks[p.key + "|" + d.value];
        var td = el("td");
        if (!q || (eff && !p.costUsd)) {
          td.appendChild(document.createTextNode("-"));
        } else if (eff) {
          td.appendChild(document.createTextNode(fmtp(q.value / p.costUsd)));
          td.appendChild(el("div", "range", fmt(q.value) + " / " + usd(p.costUsd)));
        } else {
          td.appendChild(document.createTextNode(fmt(q.value)));
          td.appendChild(el("div", "range", bench.concShort + "=" + q.at));
        }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
  }

  // --------------------------------------------------------------- pricing ---
  function buildCostTable(host) {
    var t = tableShell(host, "What the benchmarked instance cost at each provider, as billed, "
      + "converted at one dated rate table so the per-dollar figures are comparable. "
      + "Rates are USD per unit of the currency, as of " + DATA.fx.asof
      + " (" + DATA.fx.source + "); they are pinned in the report, not fetched at view time.");
    var thead = el("thead"), hr = el("tr");
    ["Provider", "As billed", "USD per unit", "Cost (USD)"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    // Every provider the report knows about, once - the same entity appears in
    // both benchmark sections but is billed once.
    var seen = {}, list = [];
    DATA.benches.forEach(function (b) {
      b.providers.forEach(function (p) {
        if (seen[p.key]) return;
        seen[p.key] = 1;
        list.push(p);
      });
    });
    list.sort(function (a, b) { return (b.costUsd || 0) - (a.costUsd || 0); });
    var tb = el("tbody");
    list.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(providerCell(p));
      tr.appendChild(el("td", null, p.costNative === null || p.costNative === undefined
        ? "not priced" : p.costNative.toLocaleString("en-US") + " " + p.costCurrency));
      tr.appendChild(el("td", null, p.costCurrency
        ? (DATA.fx.usdPer[p.costCurrency] || 1).toFixed(6) : "-"));
      tr.appendChild(el("td", null, usd(p.costUsd)));
      tb.appendChild(tr);
    });
    t.appendChild(tb);
  }

  // ------------------------------------------------------------ inventory ---
  function buildInventory(host) {
    var t = tableShell(host, "One directory per session under " + DATA.root +
      ". A session is classified by the result CSV it holds, so each provider's "
      + "sysbench and HammerDB runs stay separate.");
    var thead = el("thead"), hr = el("tr");
    ["Provider", "Benchmark", "Started (UTC)", "Measurements", "Dropped"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tb = el("tbody");
    DATA.sessions.forEach(function (s) {
      var tr = el("tr");
      tr.appendChild(el("td", null, s.name));
      tr.appendChild(el("td", null, s.bench === "sysbench" ? "sysbench" : "HammerDB TPC-C"));
      tr.appendChild(el("td", null, s.stamp || "-"));
      tr.appendChild(el("td", null, s.rows));
      var td = el("td");
      if (s.dropped) {
        td.className = "warn-dot";
        td.textContent = s.dropped + " without a result";
      } else {
        td.textContent = "-";
      }
      tr.appendChild(td);
      tb.appendChild(tr);
    });
    t.appendChild(tb);
  }

  // ----------------------------------------------------------------- boot ---
  document.getElementById("meta").textContent =
    "Generated " + DATA.generated + " from " + DATA.root +
    " - mean of the measured window across runs, 5 min ramp-up discarded. " +
    "Costs converted to USD at rates as of " + DATA.fx.asof + " (" + DATA.fx.source + ").";

  var host = document.getElementById("benches");
  DATA.benches.forEach(function (bench) {
    var sec = el("section", "bench");
    sec.appendChild(el("h2", null, bench.title));

    // One control row above both charts in the section; they re-render against
    // the same slice. The two benchmarks size their data differently (rows per
    // table vs warehouses), so each section scopes its own.
    var state = {
      dataset: bench.datasets.length ? bench.datasets[0].value : null,
      mode: "average",
    };
    var filters = el("div", "filters");
    function rerender() {
      sec.querySelectorAll("figure.card").forEach(function (f) {
        f.dispatchEvent(new Event("viz:rerender"));
      });
    }
    segmented(filters, "Data set", bench.datasets.map(function (dd) {
      return { label: dd.label, value: dd.value };
    }), function (v) { state.dataset = v; rerender(); });
    segmented(filters, "Runs", [
      { label: "Average", value: "average" },
      { label: "Individual", value: "individual" },
    ], function (v) { state.mode = v; rerender(); });
    sec.appendChild(filters);

    // The incomplete-sweep caveat stays visible whichever slice is selected -
    // it qualifies the peaks, so it must not be hidden behind the control.
    if (bench.incomplete.length) {
      var txt = bench.incomplete.map(function (g) {
        return g.name + " " + g.datasetLabel + " (" + bench.concShort + "=" + g.have.join(", ") + " only)";
      }).join("; ");
      sec.appendChild(el("div", "note", "Incomplete sweep, peak taken over fewer levels: " + txt));
    }

    sec.appendChild(scalingFigure(bench, state));
    sec.appendChild(peakFigure(bench, state, false));
    sec.appendChild(peakFigure(bench, state, true));
    host.appendChild(sec);
  });
  buildCostTable(document.getElementById("pricing"));
  buildInventory(document.getElementById("inventory"));

  // Dark mode is a selected set of steps, not an automatic flip; re-render so
  // the SVG picks up the other mode's palette.
  var toggle = document.getElementById("theme-toggle");
  var ORDER = ["auto", "light", "dark"];
  var LABEL = { auto: "Theme: auto", light: "Theme: light", dark: "Theme: dark" };
  function applyTheme(mode) {
    root.setAttribute("data-theme", mode);
    toggle.textContent = LABEL[mode];
    try { localStorage.setItem("viz-theme", mode); } catch (e) { /* private mode */ }
    requestAnimationFrame(function () {
      document.querySelectorAll("figure.card").forEach(function (f) {
        f.dispatchEvent(new Event("viz:rerender"));
      });
    });
  }
  var stored = null;
  try { stored = localStorage.getItem("viz-theme"); } catch (e) { stored = null; }
  applyTheme(ORDER.indexOf(stored) >= 0 ? stored : "auto");
  toggle.addEventListener("click", function () {
    applyTheme(ORDER[(ORDER.indexOf(root.getAttribute("data-theme")) + 1) % ORDER.length]);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    if (root.getAttribute("data-theme") === "auto") applyTheme("auto");
  });
})();
</script>
</body>
</html>
"""
