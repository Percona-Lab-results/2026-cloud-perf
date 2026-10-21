#!/usr/bin/env python3
"""
html_report.py - renders the aggregated benchmark data as a self-contained HTML page.

The page draws its own inline SVG rather than embedding the PNGs: an HTML chart is
interactive by default, so the data-set size is a control above the charts rather
than a row of small multiples, the line chart carries a snapping crosshair with a
readout for every provider, the ranked bars carry a per-row tooltip, and every
figure has a table view beside it.  There is no CDN dependency - the file works
offline and can be copied anywhere.

Each benchmark section also reads its data-set size from the URL, one query key
per benchmark - index.html?sysbench=40 or index.html?hammerdb=320 - and rewrites
it when the size is picked, naming just the section that was picked and scrolling
to it, so a link can point at the slice being discussed instead of describing
which buttons to press.  Both keys are still honoured if a URL carries them.

Which table views are open travels in the URL too, as one "tables" key listing
them - index.html?hammerdb=640&tables=hammerdb-peak,cost, or tables=all for every
one - so a link can hand someone the numbers instead of the picture.

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


def build_payload(aggs: dict, lats: dict, sessions: list, work, root: Path,
                  specs: dict, cost_spec: dict, pretty, label_dataset,
                  cost_usd, cost_native, fx: dict) -> dict:
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
        # Cost efficiency is one measure for the whole host, so it sits beside
        # the benchmark sections rather than inside either of them.
        "cost": {
            "title": cost_spec["title"],
            "sub": cost_spec["sub"],
            "providers": [dict({
                "key": p, "name": pretty(p),
                "light": SERIES_LIGHT[i % len(SERIES_LIGHT)],
                "dark": SERIES_DARK[i % len(SERIES_DARK)],
                "marker": MARKERS[i % len(MARKERS)],
                "perUsd": work.per_usd(p),
            }, **_campaign(work.total(p)), **_price(p, cost_usd, cost_native))
                for i, p in enumerate(work.providers)],
            "skipped": [{"name": r["name"], "why": r["why"]} for r in work.skipped],
        },
        "benches": [],
    }
    for bench, agg in aggs.items():
        spec = specs[bench]
        cells = _cells(agg, agg.providers)
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
            # The latency view of the same sweep: its own cells and axis, but
            # the section's data-set and average/individual controls drive both.
            "p95": _latency_view(lats.get(bench), agg, spec),
        })
    return payload


def _cells(agg, providers: list) -> dict:
    """The (provider, dataset, concurrency) grid the renderer indexes into."""
    cells = {}
    for (provider, dataset, conc), (mean, lo, hi, runs) in agg.stats.items():
        if provider not in providers:
            continue
        cells[f"{provider}|{dataset}|{conc}"] = {
            "mean": round(mean, 2), "min": round(lo, 2),
            "max": round(hi, 2), "runs": runs,
            # The individual runs behind the mean, in run order.
            "vals": [[r, round(v, 2)] for r, v in
                     agg.runs.get((provider, dataset, conc), [])],
        }
    return cells


def _latency_view(lat, agg, spec: dict) -> dict | None:
    """p95 for the same providers as the throughput chart, or None if a
    benchmark has no latency at all - a missing figure beats an empty one."""
    if lat is None or not lat.stats:
        return None
    cells = _cells(lat, agg.providers)
    if not cells:
        return None
    return {
        "metric": "p95",
        "metricLong": spec["p95_metric"],
        "title": spec["p95_title"],
        "sub": spec["p95_sub"],
        "table": spec["p95_table"],
        "concurrency": lat.concurrency,
        "cells": cells,
        # What this percentile does and does not cover; the two benchmarks
        # differ on that, so it is stated next to each chart.
        "note": spec["p95_note"],
        # Rows that have throughput but no percentile; said out loud rather
        # than silently thinning the lines.
        "missing": lat.missing,
    }


def _campaign(total: dict) -> dict:
    """One provider's whole campaign, named the way the page reads it."""
    return {
        "txn": total["txn"], "inRuns": total["in_run"], "setup": total["setup"],
        "runs": total["runs"], "sessionCount": total["sessions"],
        "hours": round(total["hours"], 1), "queries": total["queries"],
        "rows": total["rows"], "benches": total["benches"],
    }


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
  button[aria-pressed="true"],
  button[aria-expanded="true"] { color: var(--text-primary); border-color: var(--axis); }

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
      <p class="sub">sysbench <code>oltp_read_write</code> throughput (TPS) and HammerDB TPC-C throughput (NOPM) across cloud providers, the 95th-percentile latency each sustained while delivering it, and how many transactions each provider's database host committed per dollar billed for running it.</p>
      <p class="meta" id="meta"></p>
    </div>
    <button id="theme-toggle" type="button" aria-live="polite">Theme</button>
  </header>

  <div id="benches"></div>

  <!-- Cost efficiency is one measure for the whole host, not one per
       benchmark, so it sits on its own rather than inside either section. -->
  <div id="cost"></div>

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
  // Transaction counts run into the tens of millions. Full digits on every
  // tick would be a wall of zeroes, so they are scaled and suffixed; the exact
  // figure stays in the table view and the CSV.
  function fmtc(v) {
    if (v === null || v === undefined) return "n/a";
    var u = [[1e9, "B"], [1e6, "M"], [1e3, "k"]];
    for (var i = 0; i < u.length; i++) {
      if (Math.abs(v) >= u[i][0]) {
        var q = v / u[i][0];
        return (Math.abs(q) < 100
          ? q.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })
          : Math.round(q).toLocaleString("en-US")) + u[i][1];
      }
    }
    return Math.round(v).toLocaleString("en-US");
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

  // ------------------------------------------------------- table heat ramp ---
  // Ranking wash for the table cells: best green, worst red, through a neutral
  // grey rather than a third hue, so the middle of the field reads as "middle"
  // instead of as its own category.
  //
  // These are status steps, deliberately not the categorical hues - those belong
  // to providers, and a provider must not look like a verdict. The wash is never
  // the only encoding either: every cell it tints prints its own number, carries
  // its rank in the title, and the shading is explained in the caption, so a
  // reader who cannot separate red from green loses nothing but a scanning aid.
  var HEAT = {
    light: { good: [22, 122, 73], bad: [176, 36, 40], mid: [140, 138, 130], gain: 0.22 },
    dark: { good: [46, 166, 105], bad: [206, 74, 66], mid: [140, 138, 130], gain: 0.34 },
  };

  // t runs 0 (worst) to 1 (best); null leaves the cell alone.
  function heat(t) {
    if (t === null) return "";
    var h = HEAT[isDark() ? "dark" : "light"];
    var pole = t < 0.5 ? h.bad : h.good;
    var w = Math.abs(t - 0.5) * 2;
    var rgb = [0, 1, 2].map(function (i) {
      return Math.round(h.mid[i] + (pole[i] - h.mid[i]) * w);
    });
    return "rgba(" + rgb.join(",") + "," + (0.05 + h.gain * w).toFixed(3) + ")";
  }

  // Where one value sits in the spread of its column. By value, not by rank, so
  // a field with one runaway winner looks like one - four near-equal providers
  // do not get spaced out into four distinct verdicts.
  function heatT(vals, v, higherIsBetter) {
    if (v === null || v === undefined || vals.length < 2) return null;
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (!(hi > lo)) return null;
    var t = (v - lo) / (hi - lo);
    return higherIsBetter ? t : 1 - t;
  }

  // "1 of 6" for the best, counting from the good end whichever end that is.
  function heatRank(vals, v, higherIsBetter) {
    var better = vals.filter(function (o) {
      return higherIsBetter ? o > v : o < v;
    }).length;
    return (better + 1) + " of " + vals.length;
  }

  // The wash and the title in one place, so no table can tint without saying
  // what the tint means.
  function shade(td, vals, v, higherIsBetter, what) {
    var t = heatT(vals, v, higherIsBetter);
    if (t === null) return;
    td.style.background = heat(t);
    td.title = "Rank " + heatRank(vals, v, higherIsBetter) + " " + what;
  }

  var HEAT_CAPTION = " Cells are shaded by where each provider lands in the "
    + "column, green best to red worst; the value is printed in every cell.";

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

  // Latency spans two orders of magnitude across these providers, so its axis
  // is logarithmic - on a linear one the fast providers collapse onto the
  // baseline and only the slowest is readable. 1-2-5 per decade, with the frame
  // snapped out to the next step past the data so no point sits on the edge.
  function logTicks(lo, hi) {
    lo = Math.max(lo, 1e-6);
    hi = Math.max(hi, lo * 1.001);
    var all = [];
    for (var e = Math.floor(Math.log10(lo)) - 1; e <= Math.ceil(Math.log10(hi)); e++) {
      [1, 2, 5].forEach(function (f) { all.push(f * Math.pow(10, e)); });
    }
    var bottom = all[0], top = all[all.length - 1];
    all.forEach(function (t) {
      if (t <= lo) bottom = Math.max(bottom, t);
    });
    for (var i = all.length - 1; i >= 0; i--) if (all[i] >= hi) top = all[i];
    return {
      ticks: all.filter(function (t) { return t >= bottom && t <= top; }),
      bottom: bottom, top: top,
    };
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
  // `initial` is the index that starts selected - the caller has already put the
  // matching value in its own state, so this only sets the chip, and onPick is
  // not fired at boot.
  function segmented(host, label, options, onPick, initial) {
    var start = initial > 0 && initial < options.length ? initial : 0;
    var group = el("div", "filter-group");
    group.appendChild(el("span", "filter-label", label));
    var seg = el("div", "segmented");
    seg.setAttribute("role", "radiogroup");
    seg.setAttribute("aria-label", label);
    var btns = options.map(function (o, i) {
      var b = el("button", null, o.label);
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", i === start ? "true" : "false");
      b.tabIndex = i === start ? 0 : -1;
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

  // A data-set size can be pinned in the URL, one key per benchmark, so a
  // particular slice is linkable rather than something the reader has to be
  // told to click:
  //
  //   index.html?sysbench=40&hammerdb=320
  //
  // Read once at boot, then the controls own the state.
  var QUERY = (function () {
    var out = {};
    try {
      new URLSearchParams(window.location.search).forEach(function (v, k) {
        out[k.trim().toLowerCase()] = v;
      });
    } catch (e) { /* no URL API: the defaults stand */ }
    return out;
  })();

  // Which of a benchmark's data sets a URL value names, or null for none. The
  // short form each benchmark is spoken in is accepted alongside the raw value,
  // because the number in a query string is typed by a person: for sysbench
  // 40, 40m, 40M and 40000000 all name the same data set, and for HammerDB the
  // warehouse count is already the short form.
  function datasetFromToken(bench, token) {
    if (token === null || token === undefined) return null;
    var want = String(token).trim().toLowerCase().replace(/[\s,_]/g, "");
    if (!want) return null;
    for (var i = 0; i < bench.datasets.length; i++) {
      var d = bench.datasets[i];
      var label = d.label.toLowerCase().replace(/[\s,_]/g, "");
      var cands = [String(d.value), label];
      if (d.value >= 1e6 && d.value % 1e6 === 0) {
        cands.push(String(d.value / 1e6));
        cands.push(String(d.value / 1e6) + "m");
      }
      for (var j = 0; j < cands.length; j++) {
        if (cands[j] === want) return d.value;
      }
      // "40mrows" against "40mrows/table". Only a token carrying a letter may
      // match on a prefix: a bare number must be exact, or ?hammerdb=32 would
      // quietly land on 320 warehouses instead of saying it found nothing.
      if (/[a-z]/.test(want) && label.indexOf(want) === 0) return d.value;
    }
    return null;
  }

  // The form a person types: 40 for 40,000,000 rows a table, 640 for 640
  // warehouses. The parser takes the raw value too, but there is no reason to
  // put eight digits in a shared link.
  function shortSize(value) {
    if (value === null || value === undefined) return "";
    if (value >= 1e6 && value % 1e6 === 0) return String(value / 1e6);
    return String(value);
  }

  // Picking a size rewrites the address bar, so whatever the reader is looking
  // at is the thing they copy out of it. replaceState rather than pushState:
  // the chips are a lens on one page, and filling the back button with a step
  // per click would make Back mean something the reader did not ask for.
  //
  // One section per link: the URL names the benchmark that was just picked and
  // drops the other benchmark's key, so a shared link reads as "this chart at
  // this size" rather than as a snapshot of every control on the page.
  // Anything else in the query string is left alone.
  function pinSize(bench, value) {
    DATA.benches.forEach(function (b) { delete QUERY[b.key]; });
    QUERY[bench.key] = shortSize(value);
    pushQuery();
  }

  // QUERY is the single source of truth for the address bar; every control
  // edits it and then asks for one rewrite, so two controls can never disagree
  // about what the link says.
  function pushQuery() {
    try {
      var url = new URL(window.location.href);
      url.search = "";
      Object.keys(QUERY).forEach(function (k) {
        if (QUERY[k] !== null && QUERY[k] !== undefined && QUERY[k] !== "") {
          url.searchParams.set(k, QUERY[k]);
        }
      });
      // A comma is legal unencoded in a query string, and the list of open
      // tables is meant to be read and edited by hand, so %2C is put back.
      history.replaceState(null, "",
        url.pathname + url.search.replace(/%2C/g, ",") + url.hash);
    } catch (e) {
      // Opened straight off disk, where the origin is opaque and the browser
      // refuses to rewrite the URL. The controls still work; only the link does
      // not follow.
    }
  }

  // The link names one section, so picking a size takes the reader to that
  // section rather than leaving them to find it. Animated unless the reader has
  // asked for less motion.
  function reveal(sec) {
    try {
      var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      sec.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
    } catch (e) {
      sec.scrollIntoView(true);
    }
  }

  function initialDataset(bench) {
    var fallback = bench.datasets.length ? bench.datasets[0].value : null;
    if (!(bench.key in QUERY)) return fallback;
    var hit = datasetFromToken(bench, QUERY[bench.key]);
    if (hit !== null) return hit;
    // Said out loud rather than silently ignored: a link naming a size this
    // report does not hold is a broken link, not a preference.
    if (window.console && console.warn) {
      console.warn("no " + bench.key + " data set matches "
        + JSON.stringify(String(QUERY[bench.key])) + "; showing "
        + bench.datasets.map(function (d) { return d.label; }).join(", ")
        + " - defaulted to the first");
    }
    return fallback;
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
  // Two readings of the same sweep: how much work got through, and what the
  // slow tail cost while it did. The geometry, the crosshair and the table twin
  // are identical, so the figure takes a view that supplies the grid it reads,
  // its axis scale and its number format - one axis each, never two on one
  // chart. Both views answer to the section's data-set and averaging controls.
  function throughputView(bench) {
    return {
      slug: "throughput",
      cells: bench.cells, concurrency: bench.concurrency,
      metric: bench.metric, metricLong: bench.metricLong,
      title: bench.scalingTitle, sub: bench.scalingSub,
      indivSub: "One line per run over the measured 20 min window - not averaged.",
      log: false, nf: fmt, higherIsBetter: true, note: "",
    };
  }
  function latencyView(bench) {
    var p = bench.p95;
    if (!p) return null;
    return {
      slug: "p95",
      cells: p.cells, concurrency: p.concurrency,
      metric: "p95", metricLong: p.metricLong,
      title: p.title, sub: p.sub,
      indivSub: "One run's own p95 per line - not averaged. " + p.sub,
      log: true, nf: fmtp, higherIsBetter: false,
      // What the percentile covers belongs beside it, and a thinned line
      // should say why it is thin rather than look like a provider that
      // simply stopped scaling.
      note: p.note + (p.missing
        ? " " + p.missing + " measurement" + (p.missing === 1 ? "" : "s")
          + " reported throughput but no percentile and " + (p.missing === 1 ? "is" : "are")
          + " not plotted here."
        : ""),
    };
  }

  // Throughput against the concurrency sweep for the selected data-set size.
  function scalingFigure(bench, state, view) {
    var fig = el("figure", "card");
    var cap = el("figcaption");
    var grow = el("div", "grow");
    var titleNode = el("div", "fig-title", view.title);
    grow.appendChild(titleNode);
    var subNode = el("div", "fig-sub", view.sub);
    grow.appendChild(subNode);
    cap.appendChild(grow);
    var tableBtn = el("button", null, "Table view");
    tableBtn.type = "button";
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
    tableToggle(tableBtn, tableWrap, bench.key + "-" + view.slug);
    var levels = view.concurrency;
    function cell(p, d, c) { return view.cells[p + "|" + d + "|" + c] || null; }

    function render() {
      plot.textContent = "";
      legend.textContent = "";
      tip.hide();

      var d = datasetOf(bench, state.dataset);
      var individual = state.mode === "individual";
      titleNode.textContent = view.title + " - " + d.label;
      subNode.textContent = individual ? view.indivSub : view.sub;

      var surface = cssVar("--surface-1"), grid = cssVar("--grid"),
          axis = cssVar("--axis"), muted = cssVar("--text-muted"),
          secondary = cssVar("--text-secondary"),
          primary = cssVar("--text-primary"), border = cssVar("--border");

      // The y-scale spans every data set, not just the selected one, so
      // switching the control does not silently rescale the comparison.
      var max = 0, min = Infinity;
      bench.providers.forEach(function (p) {
        if (hidden[p.key]) return;
        bench.datasets.forEach(function (ds) {
          levels.forEach(function (c) {
            var v = cell(p.key, ds.value, c);
            if (!v) return;
            max = Math.max(max, v.max);
            if (v.min > 0) min = Math.min(min, v.min);
          });
        });
      });
      var scale0 = view.log ? logTicks(min === Infinity ? 1 : min, max) : ticks(max, 6);
      var tk = scale0.ticks, top = scale0.top, bottom = scale0.bottom || 0;

      var W = 1100, H = 520, M = { t: 22, r: 132, b: 58, l: 86 };
      var pw = W - M.l - M.r, ph = H - M.t - M.b;
      var xs = levels.map(function (_, i) {
        return M.l + (levels.length === 1 ? pw / 2 : (pw * i) / (levels.length - 1));
      });
      var lspan = view.log ? Math.log10(top) - Math.log10(bottom) : 0;
      function yOf(v) {
        if (!view.log) return M.t + ph - (v / top) * ph;
        var q = (Math.log10(Math.max(v, bottom)) - Math.log10(bottom)) / lspan;
        return M.t + ph - q * ph;
      }

      var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img", tabindex: "0" });
      svg.setAttribute("aria-label", view.title + ", " + d.label +
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
        lab.textContent = view.nf(t);
        svg.appendChild(lab);
      });
      svg.appendChild(svgEl("line", { x1: M.l, y1: M.t, x2: M.l, y2: M.t + ph, stroke: axis, "stroke-width": 1 }));
      svg.appendChild(svgEl("line", { x1: M.l, y1: M.t + ph, x2: M.l + pw, y2: M.t + ph, stroke: axis, "stroke-width": 1 }));

      levels.forEach(function (c, i) {
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
      ylab.textContent = view.metricLong;
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
        levels.forEach(function (c, i) {
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
        if (last.i === levels.length - 1) {
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
        t.textContent = view.nf(e.value);
        svg.appendChild(t);
      });

      // The crosshair finds the X: readers aim at a thread level, not a line.
      var active = -1;
      function focusAt(idx, clientX, clientY) {
        if (idx < 0 || idx >= levels.length) return;
        active = idx;
        var box = svg.getBoundingClientRect();
        var scale = box.width / W;
        crosshair.setAttribute("x1", xs[idx]);
        crosshair.setAttribute("x2", xs[idx]);
        crosshair.setAttribute("opacity", 1);
        var rows = [];
        bench.providers.forEach(function (p) {
          if (hidden[p.key]) return;
          var v = cell(p.key, d.value, levels[idx]);
          rows.push({
            color: colorOf(p), name: p.name,
            value: v ? view.nf(v.mean) : "no data",
            range: !v ? "" : individual
              ? "runs: " + (v.vals || []).map(function (rv) { return view.nf(rv[1]); }).join(", ")
              : view.nf(v.min) + "\u2013" + view.nf(v.max) + " over " + v.runs + " run" + (v.runs === 1 ? "" : "s"),
          });
        });
        // Best first, which on a latency axis means smallest.
        rows.sort(function (a, b) {
          var av = parseFloat(a.value.replace(/,/g, "")), bv = parseFloat(b.value.replace(/,/g, ""));
          if (isNaN(av)) return 1;
          if (isNaN(bv)) return -1;
          return view.higherIsBetter ? bv - av : av - bv;
        });
        var ax = clientX === undefined ? box.left + xs[idx] * scale : clientX;
        var ay = clientY === undefined ? box.top + box.height / 2 : clientY;
        tip.show(d.label + " - " + bench.concLabel.toLowerCase() + " " + levels[idx], rows, ax, ay);
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
        if (ev.key === "ArrowRight") { focusAt(Math.min((active < 0 ? -1 : active) + 1, levels.length - 1)); ev.preventDefault(); }
        else if (ev.key === "ArrowLeft") { focusAt(Math.max((active < 0 ? 1 : active) - 1, 0)); ev.preventDefault(); }
        else if (ev.key === "Escape") { clear(); }
      });

      plot.appendChild(svg);
      if (view.note) plot.appendChild(el("div", "note", view.note));
      buildLegend(legend, bench, hidden, false, render);
      buildSweepTable(tableWrap, bench, d, individual, view);
    }

    render();
    fig.addEventListener("viz:rerender", render);
    return fig;
  }

  // ------------------------------------------------------ ranked bar chart --
  // With the data set chosen by the control this is a ranking of five named
  // categories, so it is bars with the names on the axis - no legend needed,
  // and the value rides the tip of each bar.
  function peakFigure(bench, state) {
    var fig = el("figure", "card");
    var cap = el("figcaption");
    var grow = el("div", "grow");
    var titleNode = el("div", "fig-title", bench.peakTitle);
    grow.appendChild(titleNode);
    var subNode = el("div", "fig-sub", bench.peakSub);
    grow.appendChild(subNode);
    cap.appendChild(grow);
    var tableBtn = el("button", null, "Table view");
    tableBtn.type = "button";
    cap.appendChild(tableBtn);
    fig.appendChild(cap);

    var plot = el("div", "plot");
    fig.appendChild(plot);
    var tableWrap = el("div", "table-wrap");
    fig.appendChild(tableWrap);
    var tip = Tooltip(fig);

    tableToggle(tableBtn, tableWrap, bench.key + "-peak");

    function peak(p, d) { return bench.peaks[p + "|" + d] || null; }

    function render() {
      plot.textContent = "";
      tip.hide();

      var d = datasetOf(bench, state.dataset);
      var individual = state.mode === "individual";
      var nf = fmt;
      titleNode.textContent = bench.peakTitle + " - " + d.label;
      subNode.textContent = bench.peakSub + (individual ? " Each run marked on the bar." : "");

      var surface = cssVar("--surface-1"), grid = cssVar("--grid"),
          axis = cssVar("--axis"), muted = cssVar("--text-muted"),
          secondary = cssVar("--text-secondary"), primary = cssVar("--text-primary");

      function peakOf(p, dsv) {
        var q = peak(p.key, dsv);
        return q ? { value: q.value, at: q.at } : null;
      }
      function detailOf(p, dsv, at) {
        if (at === null) return null;
        var c0 = bench.cells[p.key + "|" + dsv + "|" + at];
        return !c0 ? null : {
          min: c0.min, max: c0.max, runs: c0.runs,
          vals: c0.vals.map(function (rv) { return rv[1]; }),
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
      svg.setAttribute("aria-label", bench.peakTitle + ", " + d.label + ". "
        + bench.peakSub + ".");

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
      xlab.textContent = bench.metricLong;
      svg.appendChild(xlab);

      rows.forEach(function (r, i) {
        var yTop = M.t + i * PITCH, yc = yTop + PITCH / 2;
        var g = svgEl("g", { tabindex: "0", role: "img" });
        g.setAttribute("aria-label", r.p.name + ", " + d.label + ": " + (r.q
          ? nf(r.q.value) + " " + bench.metric + " at " + bench.concShort + "=" + r.q.at
          : "no data"));

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
          none.textContent = "no data";
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
        v2.textContent = bench.concShort + "=" + r.q.at + (!at ? "" : individual
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
            name: bench.metric + " at " + bench.concShort + "=" + r.q.at,
            value: nf(r.q.value),
            range: !c0 ? "" : individual
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
      buildPeakTable(tableWrap, bench, d);
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
  // One disclosure for all three kinds of card. The table view is the chart's
  // accessibility twin, so every card carries the same control - and it is
  // written once rather than copied per figure, where one copy can drift.
  //
  // Visibility is set inline as well as through the attribute, so no stylesheet
  // rule can out-specify it; the label flips to "Hide table" so a click that
  // registered is visible even if the table itself were empty; and opening
  // scrolls the table into view, because it appears below a chart that is
  // usually taller than the viewport.
  //
  // Which tables are open is part of the link as well - one "tables" key
  // carrying the open ones, or the word "all" - so a URL can hand someone the
  // numbers rather than the picture.
  var OPEN = (function () {
    var want = String(QUERY.tables === undefined ? "" : QUERY.tables).toLowerCase();
    var out = { all: /(^|,)\s*all\s*(,|$)/.test(want), set: {} };
    want.split(",").forEach(function (s) {
      s = s.trim().replace(/[\s_]/g, "");
      if (s && s !== "all") out.set[s] = true;
    });
    return out;
  })();

  function pinTables() {
    var open = Object.keys(OPEN.set).filter(function (k) { return OPEN.set[k]; }).sort();
    if (open.length) QUERY.tables = open.join(",");
    else delete QUERY.tables;
    pushQuery();
  }

  function tableToggle(btn, wrap, slug) {
    wrap.id = "tableview-" + slug;
    btn.setAttribute("aria-controls", wrap.id);
    function set(open) {
      wrap.setAttribute("data-open", open ? "1" : "0");
      wrap.style.display = open ? "block" : "none";
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "Hide table" : "Table view";
      OPEN.set[slug] = open;
    }
    // ?tables=all opens every one; otherwise only the ones the link names.
    set(OPEN.all || !!OPEN.set[slug]);
    btn.addEventListener("click", function () {
      var open = btn.getAttribute("aria-expanded") !== "true";
      set(open);
      pinTables();
      // The table unfolds under a chart that is often taller than the viewport,
      // so opening it changes nothing the reader can see. Bring it to them.
      if (open) reveal(wrap);
    });
  }

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

  // The table is the chart's accessibility twin, so it shows the same data set
  // the chart is drawing - the control governs both, and the selected size is
  // named in the caption rather than repeated down a column.
  function buildSweepTable(host, bench, d, individual, view) {
    var nf = view.nf, levels = view.concurrency;
    var t = tableShell(host, view.metricLong + " at " + d.label
      + " - mean across runs, with "
      + (individual ? "each run's own value" : "the min-max range") + "."
      + HEAT_CAPTION);
    var thead = el("thead"), hr = el("tr");
    hr.appendChild(el("th", null, "Provider"));
    levels.forEach(function (c) {
      hr.appendChild(el("th", null, bench.concLabel + " " + c));
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    // Providers are compared against each other at the same concurrency level,
    // so the wash runs down each column and never across a row - a row spans
    // the sweep, where rising or falling is the point, not better or worse.
    var column = {};
    levels.forEach(function (c) {
      column[c] = bench.providers.map(function (p) {
        var v = view.cells[p.key + "|" + d.value + "|" + c];
        return v ? v.mean : null;
      }).filter(function (v) { return v !== null; });
    });
    var tb = el("tbody");
    bench.providers.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(providerCell(p));
      levels.forEach(function (c) {
        var v = view.cells[p.key + "|" + d.value + "|" + c];
        var td = el("td");
        if (!v) {
          td.appendChild(document.createTextNode("-"));
        } else {
          td.appendChild(document.createTextNode(nf(v.mean)));
          td.appendChild(el("div", "range", individual
            ? (v.vals || []).map(function (rv) { return nf(rv[1]); }).join(" \u00b7 ")
            : nf(v.min) + "\u2013" + nf(v.max) + " / n=" + v.runs));
          shade(td, column[c], v.mean, view.higherIsBetter,
                "at " + bench.concLabel.toLowerCase() + " " + c);
        }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
  }

  // Same data set as the bars above it, in the same ranked order, so reading
  // down the table and down the chart give the same answer.
  function buildPeakTable(host, bench, d) {
    var t = tableShell(host, "Peak " + bench.metric + " at " + d.label
      + ", with the " + bench.concLabel.toLowerCase() + " level that reached it "
      + "and the min-max range of the runs behind it." + HEAT_CAPTION);
    var thead = el("thead"), hr = el("tr");
    hr.appendChild(el("th", null, "Provider"));
    hr.appendChild(el("th", null, "Peak " + bench.metric));
    hr.appendChild(el("th", null, bench.concLabel));
    thead.appendChild(hr);
    t.appendChild(thead);
    var tb = el("tbody");
    var rows = bench.providers.map(function (p) {
      return { p: p, q: bench.peaks[p.key + "|" + d.value] || null };
    }).sort(function (a, b) {
      return (b.q ? b.q.value : -1) - (a.q ? a.q.value : -1);
    });
    var peaks = rows.filter(function (r) { return r.q; })
      .map(function (r) { return r.q.value; });
    rows.forEach(function (r) {
      var tr = el("tr");
      tr.appendChild(providerCell(r.p));
      var td = el("td");
      if (!r.q) {
        td.appendChild(document.createTextNode("-"));
        tr.appendChild(td);
        tr.appendChild(el("td", null, "-"));
      } else {
        td.appendChild(document.createTextNode(fmt(r.q.value)));
        var c0 = bench.cells[r.p.key + "|" + d.value + "|" + r.q.at];
        if (c0) {
          td.appendChild(el("div", "range",
            fmt(c0.min) + "\u2013" + fmt(c0.max) + " / n=" + c0.runs));
        }
        shade(td, peaks, r.q.value, true, "on peak " + bench.metric);
        tr.appendChild(td);
        tr.appendChild(el("td", null, bench.concShort + "=" + r.q.at));
      }
      tb.appendChild(tr);
    });
    t.appendChild(tb);
  }

  // --------------------------------------------------- cost efficiency ------
  // One figure for the whole report rather than one per benchmark: the bill
  // covers every hour the host ran, whatever it was doing, so the measure
  // counts every transaction the database host committed - measured runs,
  // ramp-ups, schema builds, data loads, idle time - against what the provider
  // has charged for running it. Five named categories ranked by value, so bars
  // with the names on the axis, and no legend: the axis already carries identity.
  function costFigure() {
    var cost = DATA.cost;
    var fig = el("figure", "card");
    var cap = el("figcaption");
    var grow = el("div", "grow");
    grow.appendChild(el("div", "fig-title", cost.title));
    grow.appendChild(el("div", "fig-sub", cost.sub));
    cap.appendChild(grow);
    var tableBtn = el("button", null, "Table view");
    tableBtn.type = "button";
    cap.appendChild(tableBtn);
    fig.appendChild(cap);

    var plot = el("div", "plot");
    fig.appendChild(plot);
    var tableWrap = el("div", "table-wrap");
    fig.appendChild(tableWrap);
    var tip = Tooltip(fig);

    tableToggle(tableBtn, tableWrap, "cost");

    function render() {
      plot.textContent = "";
      tip.hide();

      var surface = cssVar("--surface-1"), grid = cssVar("--grid"),
          axis = cssVar("--axis"), muted = cssVar("--text-muted"),
          secondary = cssVar("--text-secondary"), primary = cssVar("--text-primary");

      // Priced hosts with counters first, in rank order; anything that cannot
      // be placed on the scale is listed after them rather than dropped, so a
      // missing provider is visible as missing.
      var rows = cost.providers.slice().sort(function (a, b) {
        return (b.perUsd || -1) - (a.perUsd || -1);
      });

      var max = 0;
      rows.forEach(function (p) { if (p.perUsd) max = Math.max(max, p.perUsd); });
      var scale0 = ticks(max, 6);
      var tk = scale0.ticks, top = scale0.top;

      var PITCH = 56, BAR = 24;                     // bars capped at 24px
      var W = 1100, M = { t: 16, r: 300, b: 50, l: 146 };
      var ph = PITCH * rows.length;
      var H = M.t + ph + M.b;
      var pw = W - M.l - M.r;
      function xOf(v) { return M.l + (v / top) * pw; }

      var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });
      svg.setAttribute("aria-label", cost.title + ". " + cost.sub + ".");

      tk.forEach(function (t) {
        svg.appendChild(svgEl("line", {
          x1: xOf(t), y1: M.t, x2: xOf(t), y2: M.t + ph, stroke: grid, "stroke-width": 1,
        }));
        var lab = svgEl("text", { x: xOf(t), y: M.t + ph + 22, "text-anchor": "middle", fill: muted, "font-size": 12.5 });
        lab.textContent = fmtc(t);
        svg.appendChild(lab);
      });
      svg.appendChild(svgEl("line", { x1: M.l, y1: M.t, x2: M.l, y2: M.t + ph, stroke: axis, "stroke-width": 1 }));
      var xlab = svgEl("text", { x: M.l + pw / 2, y: H - 8, "text-anchor": "middle", fill: secondary, "font-size": 13 });
      xlab.textContent = "Transactions committed on the database host per USD billed";
      svg.appendChild(xlab);

      rows.forEach(function (p, i) {
        var yTop = M.t + i * PITCH, yc = yTop + PITCH / 2;
        var g = svgEl("g", { tabindex: "0", role: "img" });
        g.setAttribute("aria-label", p.name + ": " + (p.perUsd
          ? fmtc(p.perUsd) + " transactions per USD, " + fmt(p.txn)
            + " transactions over " + fmt(p.hours) + " host hours for " + usd(p.costUsd)
          : p.costUsd ? "no counter data" : "not priced"));

        var name = svgEl("text", { x: M.l - 14, y: yc + 4.5, "text-anchor": "end", fill: secondary, "font-size": 13 });
        name.textContent = p.name;
        g.appendChild(name);
        // Identity comes from the colored mark beside the text, never from
        // coloring the text itself.
        g.appendChild(svgEl("rect", {
          x: M.l - 9, y: yc - 4.5, width: 9, height: 9, rx: 2, fill: colorOf(p),
        }));

        if (!p.perUsd) {
          var none = svgEl("text", { x: M.l + 12, y: yc + 4.5, fill: muted, "font-size": 12.5 });
          none.textContent = p.costUsd ? "no counter data" : "not priced";
          g.appendChild(none);
          svg.appendChild(g);
          return;
        }

        var color = colorOf(p);
        var bar = svgEl("path", {
          d: barPathH(yc - BAR / 2, BAR, M.l, xOf(p.perUsd), 4), fill: color,
        });
        g.appendChild(bar);
        // Value at the tip, outside the bar end so it is never clipped; the
        // numerator, the price it was divided by and how long the host was
        // billed for sit underneath, because the ratio alone hides all three.
        var tipX = xOf(p.perUsd) + 12;
        // The labels sit out in the gridded field, so each is painted over a
        // 2px surface halo - otherwise a gridline runs through the digits.
        var halo = { stroke: surface, "stroke-width": 2.5, "paint-order": "stroke" };
        var v1 = svgEl("text", { x: tipX, y: yc - 1, fill: primary, "font-size": 13, "font-weight": 600 });
        for (var hk in halo) v1.setAttribute(hk, halo[hk]);
        v1.textContent = fmtc(p.perUsd);
        g.appendChild(v1);
        var v2 = svgEl("text", { x: tipX, y: yc + 12, fill: muted, "font-size": 10.5 });
        for (var hk2 in halo) v2.setAttribute(hk2, halo[hk2]);
        v2.textContent = fmtc(p.txn) + " / " + usd(p.costUsd) + "  \u00b7  "
          + fmt(p.hours) + " h";
        g.appendChild(v2);

        // The hit target is the whole row, far bigger than the mark.
        g.appendChild(svgEl("rect", { x: M.l, y: yTop, width: pw, height: PITCH, fill: "transparent" }));

        function show(ev) {
          bar.setAttribute("stroke", surface);
          bar.setAttribute("stroke-width", 2);
          var box = svg.getBoundingClientRect(), scale = box.width / W;
          tip.show(p.name, [{
            color: color,
            name: "Transactions per USD billed",
            value: fmtc(p.perUsd),
            range: fmt(p.txn) + " transactions / " + usd(p.costUsd)
              + (p.costCurrency && p.costCurrency !== "USD"
                 ? " (" + p.costNative + " " + p.costCurrency + ")" : ""),
          }, {
            color: color,
            name: "Of those, outside the measured runs",
            value: fmtc(p.setup),
            range: fmt(p.hours) + " host hours billed, " + p.sessionCount + " session"
              + (p.sessionCount === 1 ? "" : "s") + " of "
              + p.benches.join(" and ") + ", "
              + p.runs + " measured runs",
          }], ev && ev.clientX !== undefined ? ev.clientX : box.left + xOf(p.perUsd) * scale,
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
      plot.appendChild(el("div", "note", costNote(rows)));
      buildCostEffTable(tableWrap);
    }

    render();
    fig.addEventListener("viz:rerender", render);
    return fig;
  }

  // What belongs next to the number: what the counter counts, what the price
  // covers, and the one place the count undershoots the work the host did.
  function costNote(rows) {
    var cheap = null, dear = null;
    rows.forEach(function (p) {
      if (!p.perUsd || !p.hours) return;
      var r = p.costUsd / p.hours;
      if (cheap === null || r < cheap) cheap = r;
      if (dear === null || r > dear) dear = r;
    });
    // Only quotable when at least one provider has both a bill and an uptime.
    var span = cheap === null ? "" : ", at " + cheap.toFixed(2) + " to "
      + dear.toFixed(2) + " USD per host-hour";
    return "Counted as Com_commit + Com_rollback on the database server itself, "
      + "from the first capture to the last, so schema builds, data loads, ramp-ups "
      + "and idle time count alongside the measured runs. The price is what each "
      + "provider has billed for running the host up to now - usage accrued to date, "
      + "not a flat rate - so the hours each campaign took are already in the "
      + "denominator" + span + "; idle hours cost money and commit nothing, which is the point. "
      + "sysbench loads its tables with autocommitted bulk inserts, which do not "
      + "increment the commit counter, so that work shows up as rows inserted in the "
      + "table view rather than here. Prices converted at one dated rate table, as of "
      + DATA.fx.asof + " (" + DATA.fx.source + ").";
  }

  function buildCostEffTable(host) {
    var cost = DATA.cost;
    var t = tableShell(host, "Every transaction the database host committed over the "
      + "whole campaign, per USD billed for running it, split between the measured runs and "
      + "everything else the host did. InnoDB rows inserted is carried because "
      + "sysbench's autocommitted data load moves that counter and not the commit one."
      + " The Per USD column is shaded by rank, green best to red worst; the other "
      + "columns are inputs to it, not verdicts, so they are left plain.");
    var thead = el("thead"), hr = el("tr");
    ["Provider", "Cost", "Sessions", "Host hours", "Transactions", "In runs",
     "Builds and idle", "InnoDB rows inserted", "Per USD"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tb = el("tbody");
    var perUsd = cost.providers.filter(function (p) { return p.perUsd; })
      .map(function (p) { return p.perUsd; });
    cost.providers.slice().sort(function (a, b) {
      return (b.perUsd || -1) - (a.perUsd || -1);
    }).forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(providerCell(p));
      var ct = el("td");
      ct.appendChild(document.createTextNode(usd(p.costUsd)));
      if (p.costCurrency && p.costCurrency !== "USD") {
        ct.appendChild(el("div", "range", p.costNative + " " + p.costCurrency));
      }
      tr.appendChild(ct);
      var sc = el("td");
      sc.appendChild(document.createTextNode(String(p.sessionCount)));
      if (p.benches && p.benches.length) {
        sc.appendChild(el("div", "range", p.benches.join(" + ")));
      }
      tr.appendChild(sc);
      tr.appendChild(el("td", null, fmt(p.hours)));
      tr.appendChild(el("td", null, fmt(p.txn)));
      var ir = el("td");
      ir.appendChild(document.createTextNode(fmt(p.inRuns)));
      ir.appendChild(el("div", "range", p.runs + " measured runs"));
      tr.appendChild(ir);
      tr.appendChild(el("td", null, fmt(p.setup)));
      tr.appendChild(el("td", null, fmt(p.rows)));
      var pu = el("td", null, p.perUsd ? fmtc(p.perUsd) : "-");
      if (p.perUsd) shade(pu, perUsd, p.perUsd, true, "on transactions per USD");
      tr.appendChild(pu);
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    if (cost.skipped && cost.skipped.length) {
      host.appendChild(el("div", "note", "Sessions left out of the count: "
        + cost.skipped.map(function (s) { return s.name + " (" + s.why + ")"; }).join("; ")
        + "."));
    }
  }

  // --------------------------------------------------------------- pricing ---
  function buildCostTable(host) {
    var t = tableShell(host, "What each provider has billed for the benchmarked instance "
      + "up to now - usage accrued to date, not a flat rate - converted at one dated "
      + "rate table so the per-dollar figures are comparable. "
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
    "Cost efficiency counts every transaction the database host committed, " +
    "benchmark runs and database initialization alike, from the server's own " +
    "counters; costs converted to USD at rates as of " + DATA.fx.asof +
    " (" + DATA.fx.source + ").";

  var host = document.getElementById("benches");
  DATA.benches.forEach(function (bench) {
    var sec = el("section", "bench");
    sec.appendChild(el("h2", null, bench.title));

    // One control row above both charts in the section; they re-render against
    // the same slice. The two benchmarks size their data differently (rows per
    // table vs warehouses), so each section scopes its own - and each reads its
    // own URL key, so ?sysbench=40&hammerdb=320 opens both at a chosen size.
    var state = {
      dataset: initialDataset(bench),
      mode: "average",
    };
    var filters = el("div", "filters");
    function rerender() {
      sec.querySelectorAll("figure.card").forEach(function (f) {
        f.dispatchEvent(new Event("viz:rerender"));
      });
    }
    var sizes = bench.datasets.map(function (dd) {
      return { label: dd.label, value: dd.value };
    });
    var picked = 0;
    sizes.forEach(function (o, i) { if (o.value === state.dataset) picked = i; });
    segmented(filters, "Data set", sizes, function (v) {
      state.dataset = v;
      pinSize(bench, v);
      rerender();
      reveal(sec);
    }, picked);
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

    sec.appendChild(scalingFigure(bench, state, throughputView(bench)));
    sec.appendChild(peakFigure(bench, state));
    // Latency last, under the throughput it explains, and driven by the same
    // controls: a provider that holds its TPS by making every statement wait
    // should be read against the chart that shows it holding.
    var lat = latencyView(bench);
    if (lat) sec.appendChild(scalingFigure(bench, state, lat));
    host.appendChild(sec);
  });

  var costHost = document.getElementById("cost");
  var costSec = el("section", "bench");
  costSec.appendChild(el("h2", null, "Cost efficiency"));
  costSec.appendChild(costFigure());
  costHost.appendChild(costSec);

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
