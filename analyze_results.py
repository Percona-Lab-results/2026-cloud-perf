#!/usr/bin/env python3
"""
analyze_results.py - build TPS / NOPM graphs from a cloud-mysql-perf.sh results tree.

The results root holds one directory per benchmark session, named
<provider>-<UTC stamp>[-<label>].  A session is classified by the result CSV it
contains, so the sysbench and the HammerDB session of a provider are kept apart
regardless of the order they were run in:

    <provider>_sysbench_results.csv  -> sysbench oltp_read_write, metric TPS
    <provider>_hammerdb_results.csv  -> HammerDB TPC-C,           metric NOPM

One graph per image file: a scaling chart and a peak chart for each data-set
size, each emitted once per theme (light / dark), plus the table view of the
same aggregates as CSV.  Charts of the same benchmark share a y-scale across the
sizes, so separate files stay comparable.  Rows whose metric is empty or NA are
dropped and reported - an aborted run leaves those behind.

Cost efficiency is deliberately *not* one of those per-benchmark charts.  The
bill covers every hour the host ran, whatever it was doing, so the measure is
one number per provider taken from the database server's own counters: every
transaction the host committed across the whole campaign - benchmark runs,
ramp-ups, schema builds and data loads, idle time - per USD of what the provider
has charged for running it.

Needs only numpy + matplotlib (no pandas).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.path import Path as MPath
from matplotlib.patches import PathPatch, Rectangle

import html_report

# --------------------------------------------------------------------- theme --
# Values are the documented default data-viz palette.  Categorical slots are
# assigned in fixed order and never cycled; a provider keeps its hue whatever
# else is on the chart.  Validated on the adjacent pairlist (lines/bars) in both
# modes: worst adjacent CVD delta-E 9.1 light / 8.4 dark (target >= 8), worst
# adjacent normal-vision delta-E 19.6 / 19.3 (floor >= 15).
SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
}
THEME = {
    "light": dict(
        surface="#fcfcfb", plane="#f9f9f7", primary="#0b0b0b",
        secondary="#52514e", muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
    ),
    "dark": dict(
        surface="#1a1a19", plane="#0d0d0d", primary="#ffffff",
        secondary="#c3c2b7", muted="#898781", grid="#2c2c2a", axis="#383835",
    ),
}
# Secondary encoding, so identity never rests on hue alone (two light-mode
# slots sit below 3:1 on the light surface; the table view is the other relief).
MARKERS = ["o", "s", "^", "D", "v", "P"]

FONT = ["DejaVu Sans", "system-ui", "sans-serif"]
MAXSERIES = len(MARKERS)

DIRNAME = re.compile(r"^(?P<provider>[A-Za-z0-9]+)-(?P<stamp>\d{8}-\d{6})(?:-(?P<label>.+))?$")

# ----------------------------------------------------------------- pricing --
# What each provider has billed for the benchmarked instance up to now - usage
# accrued to date, not a flat monthly rate, so the hours the campaign took are
# already in it.  Kept in the provider's own currency, because that is the
# number on the invoice; cost efficiency is throughput per USD of that spend, so
# every price converts through one dated rate table rather than a mix of
# eyeballed conversions.
COSTS = {
    "exoscale": (75, "CHF"),
    "scaleway": (49, "EUR"),
    "vultr": (51, "USD"),
    "upcloud": (40, "USD"),
    "digitalocean": (39, "USD"),
    "ovhcloud": (79, "AUD"),
}
# USD per one unit of the currency.  Pinned, not fetched at render time: a
# report that silently re-prices itself is not reproducible.
FX_USD_PER = {"USD": 1.0, "CHF": 1.210038, "EUR": 1.121736, "AUD": 0.698058}
FX_ASOF = "2026-10-20"
FX_SOURCE = "exchangerate-api.com"


def cost_usd(provider: str) -> float | None:
    """The provider's price converted to USD, or None if it is not priced."""
    hit = COSTS.get(provider.lower())
    if not hit:
        return None
    amount, currency = hit
    rate = FX_USD_PER.get(currency)
    return None if rate is None else amount * rate


def cost_native(provider: str) -> tuple[float, str] | None:
    return COSTS.get(provider.lower())


# ---------------------------------------------------------------- discovery --
class Session:
    """One results directory: a provider, a timestamp and one benchmark's rows."""

    def __init__(self, path: Path, provider: str, stamp: str, bench: str, csv_path: Path):
        self.path = path
        self.provider = provider
        self.stamp = stamp
        self.bench = bench          # "sysbench" | "hammerdb"
        self.csv_path = csv_path
        self.rows: list[dict] = []
        self.dropped = 0

    def __repr__(self) -> str:
        return f"<{self.provider} {self.stamp} {self.bench} n={len(self.rows)}>"


def dir_identity(d: Path) -> tuple[str, str]:
    """(provider, UTC stamp) from a results directory name."""
    m = DIRNAME.match(d.name)
    if not m:
        return d.name.split("-")[0].lower(), ""
    return m.group("provider").lower(), m.group("stamp")


def discover(root: Path) -> list[Session]:
    sessions = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        provider, stamp = dir_identity(d)
        for bench, pattern in (("sysbench", "*_sysbench_results.csv"),
                               ("hammerdb", "*_hammerdb_results.csv")):
            for csv_path in sorted(d.glob(pattern)):
                sessions.append(Session(d, provider, stamp, bench, csv_path))
    return sessions


def _num(value: str) -> float | None:
    value = (value or "").strip()
    if value in ("", "NA", "N/A", "nan"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- latency ----
# sysbench reports its own p95 and the harness asks for it (--percentile=95),
# so the CSV column is taken as given.  It covers the measured window only:
# --report-checkpoints resets the counters at the end of the ramp-up and the
# harness parses the last statistics block.
#
# HammerDB keeps its response times in the xtprof profile, copied next to the
# run log.  Each file ends with a SUMMARY block that HammerDB merges across the
# active virtual users, so the p95 there is a real percentile over every call -
# not an average of per-user percentiles, which cannot be combined.  NEWORD is
# the new-order transaction, the one TPC-C response time that is quoted.
# Unlike NOPM it is *not* windowed: xtprof is never reset, so the figure covers
# the ramp-up as well as the measured 20 minutes.
XTPROF_NAME = re.compile(r"_run(?P<run>\d+)_w(?P<dataset>\d+)_vu(?P<conc>\d+)"
                         r"_hdbxtprofile\.log$")
# Non-greedy and barred from crossing the next ">>>>>" marker, so a NEWORD
# block without a percentile line cannot borrow the next procedure's.
XTPROF_P95 = re.compile(r"PROC: NEWORD\n(?:(?!>>>>>).)*?P95: ([\d.]+)ms", re.S)


def hammerdb_p95(session: Session) -> dict[tuple[int, int, int], float]:
    """New-Order p95 in ms per (run, warehouses, virtual users)."""
    out: dict[tuple[int, int, int], float] = {}
    for log in sorted((session.path / "hammerdb").glob("*_hdbxtprofile.log")):
        name = XTPROF_NAME.search(log.name)
        if not name:
            continue
        # The last SUMMARY block is the one merged over all the virtual users;
        # everything before it is a single user's own profile.
        tail = log.read_text(errors="replace").rpartition(">>>>> SUMMARY OF ")[2]
        hit = XTPROF_P95.search(tail)
        if hit:
            out[tuple(int(name[k]) for k in ("run", "dataset", "conc"))] = float(hit[1])
    return out


def load(session: Session) -> None:
    """Read the session CSV into rows of {dataset, conc, metric, p95, run}."""
    # sysbench: provider,benchmark,run,tables,rows_per_table,threads,
    #           elapsed_s,transactions,tps,queries,qps,lat_*,errors
    # hammerdb: provider,benchmark,run,warehouses,vu,nopm,tpm,status,log_errors
    key = ("rows_per_table", "threads", "tps") if session.bench == "sysbench" \
        else ("warehouses", "vu", "nopm")
    lat = {} if session.bench == "sysbench" else hammerdb_p95(session)
    with session.csv_path.open(newline="") as fh:
        for rec in csv.DictReader(fh):
            dataset, conc, metric = (_num(rec.get(k, "")) for k in key)
            if None in (dataset, conc, metric) or metric <= 0:
                session.dropped += 1
                continue
            dataset, conc = int(dataset), int(conc)
            run = int(_num(rec.get("run", "0")) or 0)
            # A row can have throughput and no latency (an older CSV, a missing
            # profile).  That is not a reason to drop the throughput, so the
            # latency is carried as None and counted where it is aggregated.
            p95 = _num(rec.get("lat_p95_ms", "")) if session.bench == "sysbench" \
                else lat.get((run, dataset, conc))
            session.rows.append(dict(
                dataset=dataset, conc=conc, metric=metric,
                p95=p95 if (p95 or 0) > 0 else None, run=run,
                provider=rec.get("provider", session.provider).strip() or session.provider,
            ))


# ------------------------------------------------------------- host work ----
# Cost efficiency is not a property of a benchmark: the bill covers every hour
# the host ran, whatever it was doing.  MySQL's own counters are the
# only record that covers all of it, so the total comes from SHOW GLOBAL STATUS
# as the harness captured it, not from the result CSVs:
#
#   <provider>_status_baseline.tsv    counters when the session started
#   <provider>_status_final.tsv       counters when it finished
#   <provider>_status_delta_*.tsv     before / after / delta of one measured run
#
# A transaction here is a commit or a rollback.  What that includes is worth
# being precise about: HammerDB's schema build commits in batches and shows up,
# while sysbench's data load inserts its rows in autocommit, which MySQL does
# not count as an explicit transaction - so the row counters are carried
# alongside, where the loads are plainly visible.
TXN_KEYS = ("Com_commit", "Com_rollback")
STATUS_KEYS = TXN_KEYS + ("Com_insert", "Queries", "Innodb_rows_inserted", "Uptime")


def read_status(path: Path, column: int = 0) -> dict[str, int]:
    """One SHOW GLOBAL STATUS capture as {variable: value}.  The delta files
    carry three value columns (before, after, delta); column picks one."""
    out: dict[str, int] = {}
    for line in path.read_text(errors="replace").splitlines():
        f = line.split("\t")
        if len(f) < 2 + column:
            continue
        try:
            out[f[0]] = int(f[1 + column])
        except ValueError:
            continue
    return out


def host_session(path: Path) -> dict | None:
    """What the host did over one results directory, or None if it kept no
    counters (an aborted session leaves a baseline and nothing to subtract)."""
    provider, stamp = dir_identity(path)
    srv = path / "server"
    base = sorted(srv.glob("*_status_baseline.tsv")) if srv.is_dir() else []
    final = sorted(srv.glob("*_status_final.tsv")) if srv.is_dir() else []
    head = dict(path=path, name=path.name, provider=provider, stamp=stamp)
    if not base or not final:
        return dict(head, ok=False, why="no start/end counter capture")
    first, last = read_status(base[0]), read_status(final[0])
    delta = {k: last.get(k, 0) - first.get(k, 0) for k in STATUS_KEYS}
    # Status counters are cumulative since server start, so a restart mid-session
    # resets them and the subtraction goes negative.  Drop the session rather
    # than report a total that is short by an unknown amount.
    if min(delta[k] for k in TXN_KEYS) < 0 or delta["Uptime"] <= 0:
        return dict(head, ok=False, why="counters reset mid-session (restart)")
    in_run = 0
    runs = sorted(srv.glob("*_status_delta_*.tsv"))
    for d in runs:
        per = read_status(d, column=2)
        in_run += sum(per.get(k, 0) for k in TXN_KEYS)
    txn = sum(delta[k] for k in TXN_KEYS)
    benches = sorted(b for b, pat in (("sysbench", "*_sysbench_results.csv"),
                                      ("hammerdb", "*_hammerdb_results.csv"))
                     if any(path.glob(pat)))
    return dict(head, ok=True, why="", benches=benches, runs=len(runs),
                txn=txn,
                # Everything the counters saw that was not inside a measured
                # run: the schema builds and data loads, plus idle time.
                in_run=min(in_run, txn), setup=max(txn - in_run, 0),
                hours=delta["Uptime"] / 3600.0, queries=delta["Queries"],
                inserts=delta["Com_insert"], rows=delta["Innodb_rows_inserted"])


class HostWork:
    """Every transaction the billed hosts committed, per provider."""

    def __init__(self, root: Path):
        self.sessions: list[dict] = []
        self.skipped: list[dict] = []
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            rec = host_session(d)
            if rec is None:
                continue
            (self.sessions if rec["ok"] else self.skipped).append(rec)
        self.providers = sorted({r["provider"] for r in self.sessions})

    def of(self, provider: str) -> list[dict]:
        return [r for r in self.sessions if r["provider"] == provider]

    def total(self, provider: str) -> dict:
        """The provider's whole campaign as one record."""
        rows = self.of(provider)
        out = {k: sum(r[k] for r in rows) for k in
               ("txn", "in_run", "setup", "runs", "hours", "queries", "inserts", "rows")}
        out["sessions"] = len(rows)
        out["benches"] = sorted({b for r in rows for b in r["benches"]})
        return out

    def per_usd(self, provider: str) -> float | None:
        """Transactions per USD of the instance price, or None if either side
        is missing - a provider without a price gets no bar, not a zero."""
        txn = self.total(provider)["txn"]
        usd = cost_usd(provider)
        return None if not txn or not usd else txn / usd


# --------------------------------------------------------------- aggregation --
class Agg:
    """mean / min / max of a metric per (provider, dataset, concurrency), plus
    the individual run values behind each of those means."""

    def __init__(self, bench: str, sessions: list[Session], field: str = "metric"):
        self.bench = bench
        self.sessions = sessions
        self.field = field
        self.missing = 0            # rows that carried no value for this field
        bucket: dict[tuple[str, int, int], list[float]] = defaultdict(list)
        runs: dict[tuple[str, int, int], list[tuple[int, float]]] = defaultdict(list)
        for s in sessions:
            for r in s.rows:
                value = r.get(field)
                if value is None:
                    self.missing += 1
                    continue
                bucket[(s.provider, r["dataset"], r["conc"])].append(value)
                runs[(s.provider, r["dataset"], r["conc"])].append((r["run"], value))
        self.stats = {k: (float(np.mean(v)), float(np.min(v)), float(np.max(v)), len(v))
                      for k, v in bucket.items()}
        # Kept in run order so "individual runs" is a stable series, not a set.
        self.runs = {k: sorted(v) for k, v in runs.items()}
        self.providers = sorted({k[0] for k in self.stats})
        self.datasets = sorted({k[1] for k in self.stats})
        self.concurrency = sorted({k[2] for k in self.stats})

    def mean(self, provider: str, dataset: int, conc: int) -> float | None:
        hit = self.stats.get((provider, dataset, conc))
        return hit[0] if hit else None

    def band(self, provider: str, dataset: int, conc: int):
        hit = self.stats.get((provider, dataset, conc))
        return (hit[1], hit[2]) if hit else (None, None)

    def peak(self, provider: str, dataset: int):
        """Best mean over the concurrency sweep, with the level that reached it."""
        best = (None, None)
        for conc in self.concurrency:
            value = self.mean(provider, dataset, conc)
            if value is not None and (best[0] is None or value > best[0]):
                best = (value, conc)
        return best

    def at_top(self, provider: str, dataset: int):
        """The value at the highest concurrency level this provider reached, with
        that level.  For latency the interesting figure is the loaded one, and
        not every provider got to the same top of the sweep."""
        for conc in reversed(self.concurrency):
            value = self.mean(provider, dataset, conc)
            if value is not None:
                return value, conc
        return None, None

    def incomplete(self) -> list[tuple[str, int, list[int]]]:
        """Provider/data-set pairs that did not cover the whole sweep - their
        'peak' is a peak over fewer levels, so it is not directly comparable."""
        gaps = []
        for provider in self.providers:
            for dataset in self.datasets:
                have = [c for c in self.concurrency
                        if self.mean(provider, dataset, c) is not None]
                if have and len(have) < len(self.concurrency):
                    gaps.append((provider, dataset, have))
        return gaps


# ------------------------------------------------------------------ plumbing --
def label_dataset(bench: str, value: int) -> str:
    if bench == "sysbench":
        return f"{value // 1_000_000}M rows/table" if value >= 1_000_000 else f"{value:,} rows/table"
    return f"{value} warehouses"


def compact(v: float) -> str:
    """A large count at a glance: 43.2M, 431k, 86."""
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= scale:
            q = v / scale
            return f"{q:,.1f}{suffix}" if abs(q) < 100 else f"{q:,.0f}{suffix}"
    return f"{v:,.0f}"


def slug_dataset(bench: str, value: int) -> str:
    """The data-set size as a filename fragment: 10m / 160w."""
    if bench == "sysbench":
        return f"{value // 1_000_000}m" if value >= 1_000_000 else f"{value}rows"
    return f"{value}w"


def pretty(provider: str) -> str:
    return {"digitalocean": "DigitalOcean", "exoscale": "Exoscale", "ovhcloud": "OVHcloud",
            "upcloud": "UpCloud", "vultr": "Vultr"}.get(provider, provider.title())


def style(mode: str) -> dict:
    t = THEME[mode]
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": FONT, "font.size": 10,
        "figure.facecolor": t["plane"], "axes.facecolor": t["surface"],
        "savefig.facecolor": t["plane"],
        "text.color": t["primary"], "axes.labelcolor": t["secondary"],
        "xtick.color": t["muted"], "ytick.color": t["muted"],
        "xtick.labelcolor": t["muted"], "ytick.labelcolor": t["muted"],
        "axes.edgecolor": t["axis"], "axes.linewidth": 1.0,
        "xtick.major.size": 0, "ytick.major.size": 0,
        "legend.frameon": False, "figure.dpi": 110, "savefig.dpi": 160,
    })
    return t


def chrome(ax, t: dict, label: str = "", orient: str = "v") -> None:
    """Recessive axes: a hairline solid grid along the value axis only, no
    top/right spines.  orient="h" for bars that run sideways."""
    value_axis = "y" if orient == "v" else "x"
    ax.set_axisbelow(True)
    ax.grid(axis=value_axis, color=t["grid"], linewidth=1.0, linestyle="-")
    ax.grid(axis="x" if value_axis == "y" else "y", visible=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["axis"])
    getattr(ax, f"{value_axis}axis").set_major_formatter(lambda v, _pos: f"{v:,.0f}")
    if label and orient == "v":
        ax.set_ylabel(label, color=t["secondary"], labelpad=8)
    elif label:
        ax.set_xlabel(label, color=t["secondary"], labelpad=8)


def px_to_data(ax, px: float, axis: str = "x") -> float:
    """A display-space length in data units. Only valid once limits are set."""
    inv = ax.transData.inverted()
    o = ax.transData.transform((0, 0))
    if axis == "x":
        return abs(inv.transform((o[0] + px, o[1]))[0] - inv.transform(o)[0])
    return abs(inv.transform((o[0], o[1] + px))[1] - inv.transform(o)[1])


def rounded_barh(ax, y: float, height: float, length: float, color: str,
                 radius_px: float = 4.0):
    """The same bar on its side: 4px rounded data-end, square at the baseline."""
    if length <= 0:
        return
    rx = min(px_to_data(ax, radius_px, "x"), length)
    ry = min(px_to_data(ax, radius_px, "y"), height / 2)
    bottom, top, end = y - height / 2, y + height / 2, length
    k = 0.5523
    verts = [
        (0, bottom), (end - rx, bottom),
        (end - rx + rx * k, bottom), (end, bottom + ry - ry * k), (end, bottom + ry),
        (end, top - ry),
        (end, top - ry + ry * k), (end - rx + rx * k, top), (end - rx, top),
        (0, top), (0, bottom),
    ]
    codes = [MPath.MOVETO, MPath.LINETO,
             MPath.CURVE4, MPath.CURVE4, MPath.CURVE4,
             MPath.LINETO,
             MPath.CURVE4, MPath.CURVE4, MPath.CURVE4,
             MPath.LINETO, MPath.CLOSEPOLY]
    patch = PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", zorder=3)
    ax.add_patch(patch)
    return patch


def figure_legend(fig, providers: list[str], colors: list[str], t: dict,
                  y: float = 0.0, ncol: int | None = None) -> None:
    """A legend is always present for two or more series."""
    handles = [Line2D([], [], color=colors[i], marker=MARKERS[i], linewidth=2,
                      markersize=6.5, markeredgecolor=t["surface"], markeredgewidth=1.6,
                      label=pretty(p))
               for i, p in enumerate(providers)]
    leg = fig.legend(handles=handles, loc="lower center", ncol=ncol or min(len(providers), 6),
                     bbox_to_anchor=(0.5, y), handletextpad=0.5, columnspacing=1.6)
    for text in leg.get_texts():
        text.set_color(t["secondary"])


def titles(fig, title: str, subtitle: str, t: dict) -> None:
    fig.text(0.012, 0.975, title, ha="left", va="top", fontsize=14.5,
             fontweight="semibold", color=t["primary"])
    fig.text(0.012, 0.925, subtitle, ha="left", va="top", fontsize=10,
             color=t["secondary"])


# -------------------------------------------------------------------- charts --
def chart_scaling(agg: Agg, mode: str, out: Path, metric: str, conc_label: str,
                  title: str, subtitle: str, dataset: int,
                  logy: bool = False) -> Path:
    """A metric against the concurrency sweep, for one data-set size.

    logy for latency: p95 here spans two decades across the providers, and on a
    linear axis the slowest one flattens the other five into the baseline.
    """
    t = style(mode)
    colors = SERIES[mode]
    fig, ax = plt.subplots(figsize=(10.3, 5.4))
    xs = np.arange(len(agg.concurrency))
    # One y-scale across every data-set size, even though each size is now its
    # own file: these charts are read side by side, and an axis that re-fitted
    # per file would let a slower data set draw a taller line than a faster one.
    ymax = max((v[2] for v in agg.stats.values()), default=1.0)
    ymin = min((v[1] for v in agg.stats.values() if v[1] > 0), default=1.0)

    chrome(ax, t, metric)
    for i, provider in enumerate(agg.providers):
        means = [agg.mean(provider, dataset, c) for c in agg.concurrency]
        lo = [agg.band(provider, dataset, c)[0] for c in agg.concurrency]
        hi = [agg.band(provider, dataset, c)[1] for c in agg.concurrency]
        keep = [j for j, v in enumerate(means) if v is not None]
        if not keep:
            continue
        kx = xs[keep]
        ax.fill_between(kx, [lo[j] for j in keep], [hi[j] for j in keep],
                        color=colors[i], alpha=0.10, linewidth=0, zorder=1)
        ax.plot(kx, [means[j] for j in keep], color=colors[i], linewidth=2,
                solid_capstyle="round", solid_joinstyle="round",
                marker=MARKERS[i], markersize=6.5, markeredgecolor=t["surface"],
                markeredgewidth=1.6, zorder=4 + i)
    ax.set_xticks(xs, [str(c) for c in agg.concurrency])
    ax.tick_params(axis="x", labelcolor=t["secondary"])
    ax.set_xlim(-0.35, len(xs) - 0.65)
    if logy:
        # 1-2-5 per decade: enough gridlines to read a value off, few enough to
        # stay recessive.  A log axis cannot start at zero, so it is said in the
        # axis label rather than left for the reader to infer from the ticks.
        ax.set_yscale("log")
        ax.set_ylim(ymin / 1.4, ymax * 1.4)
        ax.yaxis.set_major_locator(mticker.LogLocator(base=10, subs=(1, 2, 5), numticks=16))
        ax.yaxis.set_minor_locator(mticker.NullLocator())
        ax.yaxis.set_major_formatter(lambda v, _pos: f"{v:,.6g}")
    else:
        ax.set_ylim(0, ymax * 1.08)
    ax.set_xlabel(conc_label, color=t["secondary"], labelpad=6)
    ax.set_title(label_dataset(agg.bench, dataset), fontsize=11,
                 color=t["primary"], pad=10)

    titles(fig, title, subtitle, t)
    figure_legend(fig, agg.providers, colors, t)
    fig.tight_layout(rect=(0.0, 0.07, 1.0, 0.88))
    fig.savefig(out)
    plt.close(fig)
    return out


def chart_peak(agg: Agg, mode: str, out: Path, metric: str, conc_short: str,
               title: str, subtitle: str, dataset: int) -> Path:
    """Peak throughput per provider at one data-set size, one bar per provider.

    A ranking of named categories, so the bars run sideways with the names on
    the axis and no legend - the same figure the HTML page draws, so a reader
    who has seen one recognises the other.  Every bar carries its value, the
    level that reached it and the spread of the runs behind it.
    """
    t = style(mode)
    colors = SERIES[mode]
    # Hue follows the provider, never the rank, so sorting cannot repaint anyone.
    slot = {p: colors[i % len(colors)] for i, p in enumerate(agg.providers)}
    rows = sorted(((agg.peak(p, dataset), p) for p in agg.providers),
                  key=lambda r: (r[0][0] is not None, r[0][0] or 0), reverse=True)
    # Only the gaps that apply to the size this file is about.
    gaps = [g for g in agg.incomplete() if g[1] == dataset]

    fig, ax = plt.subplots(figsize=(8.6, 0.66 * len(rows) + (3.0 if gaps else 2.3)))
    chrome(ax, t, metric, orient="h")

    # Scaled to the largest bar across *all* sizes, not just this one, so the
    # three files of a benchmark stay comparable bar-for-bar.
    xmax = max((agg.peak(p, d)[0] or 0.0
                for p in agg.providers for d in agg.datasets), default=1.0)
    ax.set_yticks(range(len(rows)), [pretty(p) for _v, p in rows])
    # The pad clears the colour key that goes in beside each name below.
    ax.tick_params(axis="y", labelcolor=t["secondary"], labelsize=10, pad=16)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    # Provisional; the real headroom is measured off the labels once the axes
    # box exists, because what has to fit is text, not a fraction of the panel.
    ax.set_xlim(0, xmax * 1.30)

    # A peak taken over a partial sweep is not comparable with a full one.  Say
    # it rather than letting the bar imply otherwise.
    note = "Incomplete sweep, peak taken over fewer levels: " + "; ".join(
        f"{pretty(p)} {label_dataset(agg.bench, d)} ({conc_short}="
        f"{', '.join(str(c) for c in have)} only)" for p, d, have in gaps)
    # Wrapped to the figure, not run off its right edge: a footnote that is cut
    # in half is worse than no footnote, because it reads as complete.
    lines = textwrap.wrap(note, width=int((fig.get_figwidth() - 0.3) / 0.0666)) \
        if gaps else []
    foot = 0.012 + 0.0175 * len(lines)

    # Bar geometry is sized in display pixels, so the final axes box has to be
    # settled first - both the limits and the layout feed that transform.
    titles(fig, title, subtitle, t)
    fig.tight_layout(rect=(0.0, foot + 0.06, 1.0, 0.87))
    if lines:
        fig.text(0.012, 0.010, "\n".join(lines), ha="left", va="bottom",
                 fontsize=8, color=t["muted"], linespacing=1.5)
    fig.canvas.draw()

    detail = {}
    for (_v, at), provider in rows:
        line = f"{conc_short}={at}"
        cell = agg.stats.get((provider, dataset, at))
        if cell:
            _mean, lo, hi, runs = cell
            line += f"  ·  {lo:,.0f}–{hi:,.0f} / n={runs}"
        detail[provider] = line

    def text_px(s: str, size: float, weight: str = "normal") -> float:
        probe = ax.text(0, 0, s, fontsize=size, fontweight=weight)
        width = probe.get_window_extent(renderer=fig.canvas.get_renderer()).width
        probe.remove()
        return width

    # The value and its detail line hang off the bar tip, so the panel has to
    # hold the widest of them.  A fixed multiplier only guesses: it clipped the
    # longest run range ("58,217-59,077 / n=3") and would waste a third of the
    # panel when every range is short.  Reserve what the text actually needs.
    widest = max(text_px("no data", 9) if value is None
                 else max(text_px(f"{value:,.0f}", 10, "semibold"),
                          text_px(detail[provider], 8))
                 for (value, _at), provider in rows)
    panel = ax.get_window_extent().width
    ax.set_xlim(0, xmax * panel / max(panel - (12.0 + widest + 6.0), 40.0))
    fig.canvas.draw()

    bar_h = min(0.62, px_to_data(ax, 24.0, "y"))
    sw = px_to_data(ax, 9.0)                       # the 9px key beside the name
    halo = [pe.withStroke(linewidth=2.5, foreground=t["surface"])]
    for i, ((value, at), provider) in enumerate(rows):
        # Identity comes from a key beside the name, never from colouring the
        # text itself - the same arrangement as the HTML.
        ax.add_patch(Rectangle((-sw, i - px_to_data(ax, 4.5, "y")),
                               sw, px_to_data(ax, 9.0, "y"),
                               facecolor=slot[provider], edgecolor="none",
                               clip_on=False, zorder=4))
        if value is None:
            ax.text(px_to_data(ax, 12.0), i, "no data", va="center",
                    fontsize=9, color=t["muted"])
            continue
        rounded_barh(ax, i, bar_h, value, slot[provider])
        # Bars -> value at the tip, outside the bar end so it is never clipped.
        # The labels sit out in the gridded field, so each carries a 2px surface
        # halo - otherwise a gridline runs straight through the digits.
        tx = value + px_to_data(ax, 12.0)
        ax.text(tx, i - px_to_data(ax, 5.0, "y"), f"{value:,.0f}", va="center",
                fontsize=10, fontweight="semibold", color=t["primary"],
                path_effects=halo)
        ax.text(tx, i + px_to_data(ax, 8.0, "y"), detail[provider], va="center",
                fontsize=8, color=t["muted"], path_effects=halo)

    fig.savefig(out)
    plt.close(fig)
    return out


def chart_cost(work: HostWork, mode: str, out: Path, title: str,
               subtitle: str) -> Path:
    """Transactions committed per USD, one bar per provider - the whole campaign
    of each host, both benchmarks and their schema builds together.

    A ranking of named categories, so the bars run sideways with the names on
    the axis: the order is the message, and no label has to be rotated.
    """
    t = style(mode)
    colors = SERIES[mode]
    # Hue follows the provider, never the rank, so the ranking below cannot
    # repaint anyone.
    slot = {p: colors[i % len(colors)] for i, p in enumerate(work.providers)}
    rows = sorted(((work.per_usd(p), p) for p in work.providers),
                  key=lambda r: (r[0] is not None, r[0] or 0), reverse=True)

    fig, ax = plt.subplots(figsize=(8.6, 0.72 * len(rows) + 3.0))
    chrome(ax, t, "Transactions committed per USD billed", orient="h")
    # Counts run to the millions; full digits on every tick would be a wall of
    # zeroes.  The exact figures are in the table and the CSV.
    ax.xaxis.set_major_formatter(lambda v, _pos: compact(v))
    ax.set_yticks(range(len(rows)), [pretty(p) for _v, p in rows])
    ax.tick_params(axis="y", labelcolor=t["secondary"], labelsize=10)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    xmax = max((v or 0.0 for v, _p in rows), default=1.0)
    # Room on the right for the value and its two inputs.
    ax.set_xlim(0, xmax * 1.38)

    note = (
        "From the server's own counters (commits + rollbacks), start to end of "
        "every session: benchmark runs and ramp-ups, schema builds and data "
        "loads, and idle time in between. The spend is what each provider has "
        "billed for running the host up to now, usage included, so the hours "
        "each campaign took are already in the denominator - idle hours cost "
        "money and commit nothing, which is the point. sysbench loads its data "
        "in autocommit, which MySQL does not count as a transaction, so that "
        "build shows up in the rows inserted rather than here."
    )
    lines = textwrap.wrap(note, width=int((fig.get_figwidth() - 0.3) / 0.0666))
    foot = 0.012 + 0.0145 * len(lines)

    titles(fig, title, subtitle, t)
    fig.tight_layout(rect=(0.0, foot + 0.05, 1.0, 0.86))
    fig.text(0.012, 0.010, "\n".join(lines), ha="left", va="bottom",
             fontsize=8, color=t["muted"], linespacing=1.5)
    fig.canvas.draw()

    bar_h = min(0.62, px_to_data(ax, 24.0, "y"))
    for i, (value, provider) in enumerate(rows):
        if value is None:
            ax.text(px_to_data(ax, 12.0), i, "not priced", va="center",
                    fontsize=9, color=t["muted"])
            continue
        rounded_barh(ax, i, bar_h, value, slot[provider])
        tx = value + px_to_data(ax, 12.0)
        total = work.total(provider)
        # The labels sit out in the gridded field, so each carries a 2px surface
        # halo - otherwise a gridline runs straight through the digits.
        halo = [pe.withStroke(linewidth=2.5, foreground=t["surface"])]
        ax.text(tx, i - px_to_data(ax, 5.0, "y"), compact(value), va="center",
                fontsize=10, fontweight="semibold", color=t["primary"],
                path_effects=halo)
        # The quotient's two inputs, so the bar is never an unexplained ratio.
        ax.text(tx, i + px_to_data(ax, 8.0, "y"),
                f"{compact(total['txn'])} / {cost_usd(provider):,.2f} USD"
                f"  ·  {total['hours']:,.0f} h",
                va="center", fontsize=8, color=t["muted"], path_effects=halo)

    fig.savefig(out)
    plt.close(fig)
    return out


# --------------------------------------------------------------- table view --
def write_table(agg: Agg, out: Path, dataset_col: str, conc_col: str, metric: str) -> Path:
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", dataset_col, conc_col, f"{metric}_mean",
                    f"{metric}_min", f"{metric}_max", "runs"])
        for provider in agg.providers:
            for dataset in agg.datasets:
                for conc in agg.concurrency:
                    hit = agg.stats.get((provider, dataset, conc))
                    if not hit:
                        continue
                    mean, lo, hi, cnt = hit
                    w.writerow([provider, dataset, conc, f"{mean:.2f}",
                                f"{lo:.2f}", f"{hi:.2f}", cnt])
    return out


def report_costs(providers: list[str]) -> None:
    print(f"\ncost of the benchmarked instance (FX as of {FX_ASOF}, {FX_SOURCE})")
    print("  " + f"{'provider':<14}{'as billed':>14}{'USD per unit':>14}{'USD':>12}")
    for provider in providers:
        native, usd = cost_native(provider), cost_usd(provider)
        if native is None or usd is None:
            print("  " + f"{pretty(provider):<14}{'not priced':>14}")
            continue
        amount, currency = native
        rate = FX_USD_PER[currency]
        print("  " + f"{pretty(provider):<14}{f'{amount:,.0f} {currency}':>14}"
              f"{rate:>14.6f}{usd:>12,.2f}")


def report_host_efficiency(work: HostWork) -> None:
    """Cost efficiency as the host measured it: every transaction the database
    committed over the whole campaign, per USD of what the instance cost."""
    print("\ncost efficiency - transactions committed on the database host per USD")
    print("  (the server's own counters, start to end of every session: benchmark")
    print("   runs and ramp-ups, schema builds and data loads, and idle time)")
    print("  " + f"{'provider':<14}{'USD':>7}{'sessions':>9}{'host-h':>8}"
                 f"{'transactions':>15}{'in runs':>14}{'builds/idle':>13}"
                 f"{'rows inserted':>15}{'txn per USD':>14}")
    ranked = []
    for provider in work.providers:
        total = work.total(provider)
        usd = cost_usd(provider)
        per = work.per_usd(provider)
        if per is not None:
            ranked.append((per, provider, total))
        print("  " + f"{pretty(provider):<14}"
              + (f"{usd:>7,.0f}" if usd else f"{'-':>7}")
              + f"{total['sessions']:>9}{total['hours']:>8,.0f}"
              + f"{compact(total['txn']):>15}{compact(total['in_run']):>14}"
              + f"{compact(total['setup']):>13}{compact(total['rows']):>15}"
              + (f"{compact(per) + ' / USD':>14}" if per is not None
                 else f"{'not priced':>14}"))
    if ranked:
        ranked.sort(reverse=True)
        best, worst = ranked[0], ranked[-1]
        print(f"  best: {pretty(best[1])} committed {compact(best[2]['txn'])} "
              f"transactions = {compact(best[0])} per USD, {best[0] / worst[0]:.1f}x "
              f"{pretty(worst[1])} ({compact(worst[0])} per USD)")
    # The prices are accrued usage to date, not a flat monthly rate, so a longer
    # campaign is already paid for in the denominator and the ratio does not
    # reward one.  What the hours do show is the rate each host was charged at.
    rate = {p: cost_usd(p) / work.total(p)["hours"]
            for p in work.providers
            if cost_usd(p) and work.total(p)["hours"] > 0}
    if rate:
        cheap = min(rate, key=lambda p: rate[p])
        dear = max(rate, key=lambda p: rate[p])
        print(f"  spend is usage accrued to date, hours included, so the rate "
              f"is what differs: {rate[cheap]:.2f} USD per host-hour at "
              f"{pretty(cheap)} to {rate[dear]:.2f} at {pretty(dear)}")
    for rec in work.skipped:
        print(f"  skipped {rec['name']}: {rec['why']}")


def write_cost_table(work: HostWork, out: Path) -> Path:
    """Transactions, price and transactions-per-USD: one row per session, plus
    the whole-campaign row each provider is actually billed for."""
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", "scope", "session", "started_utc", "benchmarks",
                    "host_hours", "measured_runs", "transactions",
                    "transactions_in_runs", "transactions_builds_and_idle",
                    "queries", "insert_statements", "innodb_rows_inserted",
                    "cost_as_billed", "currency", "usd_per_unit", "cost_usd",
                    "transactions_per_usd"])

        def row(provider: str, scope: str, rec: dict, session: str, stamp: str):
            native, usd = cost_native(provider), cost_usd(provider)
            # Per-session rows carry the price only on the campaign row: the
            # instance is billed once, not once per session.
            billed = scope == "campaign"
            w.writerow([
                provider, scope, session, stamp, " + ".join(rec["benches"]),
                f"{rec['hours']:.2f}", rec["runs"], rec["txn"], rec["in_run"],
                rec["setup"], rec["queries"], rec["inserts"], rec["rows"],
                "" if not billed or native is None else f"{native[0]:.2f}",
                "" if not billed or native is None else native[1],
                "" if not billed or native is None else f"{FX_USD_PER[native[1]]:.6f}",
                "" if not billed or usd is None else f"{usd:.2f}",
                "" if not billed or usd is None else f"{rec['txn'] / usd:.1f}",
            ])

        for provider in work.providers:
            for rec in work.of(provider):
                row(provider, "session", rec, rec["name"], rec["stamp"])
            row(provider, "campaign", work.total(provider), "", "")
    return out


def report(agg: Agg, metric: str, conc_label: str) -> None:
    print(f"\n{agg.bench}: {metric} peak per provider "
          f"(mean over runs, best {conc_label.lower()})")
    head = "  " + f"{'provider':<14}" + "".join(
        f"{label_dataset(agg.bench, d):>24}" for d in agg.datasets)
    print(head)
    for provider in agg.providers:
        line = "  " + f"{pretty(provider):<14}"
        for dataset in agg.datasets:
            value, at = agg.peak(provider, dataset)
            line += f"{(f'{value:,.0f} @ {at}' if value else 'no data'):>24}"
        print(line)


def report_latency(agg: Agg, metric: str, conc_short: str) -> None:
    """p95 under the heaviest load each provider reached - the level is printed
    with it, because not every provider finished the sweep."""
    print(f"\n{agg.bench}: {metric} at the top of the sweep "
          f"(mean over runs; @ is the {conc_short} level)")
    print("  " + f"{'provider':<14}" + "".join(
        f"{label_dataset(agg.bench, d):>24}" for d in agg.datasets))
    for provider in agg.providers:
        line = "  " + f"{pretty(provider):<14}"
        for dataset in agg.datasets:
            value, at = agg.at_top(provider, dataset)
            line += f"{(f'{value:,.1f} ms @ {at}' if value else 'no data'):>24}"
        print(line)
    if agg.missing:
        print(f"  {agg.missing} measurement(s) had no p95 and are not in the "
              f"latency charts")


# ----------------------------------------------------------------- main ------
BENCH_SPEC = {
    "sysbench": dict(
        metric="TPS (transactions/s)", conc_label="Threads", conc_short="t",
        dataset_col="rows_per_table", conc_col="threads",
        title="sysbench oltp_read_write - throughput",
        sub="Managed MySQL, 10 tables; mean of the measured 20 min window across runs, band = min-max",
        peak_title="sysbench oltp_read_write - peak throughput",
        peak_sub="Best thread level per provider, at the selected data-set size",
        table="summary_sysbench_tps.csv",
        # Latency spans two orders of magnitude across providers, so the axis is
        # logarithmic; on a linear one the fast five collapse into the baseline.
        p95_metric="p95 latency (ms, log scale) - lower is better",
        p95_title="sysbench oltp_read_write - 95th percentile latency",
        p95_sub="sysbench's own p95 over the measured 20 min window; mean across runs, band = min-max",
        p95_note="sysbench reports a fresh statistics block after the ramp-up, so this "
                 "percentile covers the measured window only - the same window as the TPS above.",
        p95_table="summary_sysbench_p95_ms.csv",
    ),
    "hammerdb": dict(
        metric="NOPM (new orders/min)", conc_label="Virtual users", conc_short="vu",
        dataset_col="warehouses", conc_col="vu",
        title="HammerDB TPC-C - throughput",
        sub="Managed MySQL; mean of the measured 20 min window across runs, band = min-max",
        peak_title="HammerDB TPC-C - peak throughput",
        peak_sub="Best virtual-user level per provider, at the selected warehouse count",
        table="summary_hammerdb_nopm.csv",
        p95_metric="New-Order p95 latency (ms, log scale) - lower is better",
        p95_title="HammerDB TPC-C - 95th percentile New-Order latency",
        # HammerDB's time profile is never reset between ramp-up and the measured
        # window, so unlike NOPM this percentile includes the ramp-up.
        p95_sub="HammerDB xtprof p95 merged across virtual users, ramp-up included; "
                "mean across runs, band = min-max",
        p95_note="HammerDB's time profile is never reset, so unlike the NOPM above this "
                 "percentile includes the 5 min ramp-up - it reads a little better than a "
                 "window-only figure would, and is not directly comparable with sysbench's.",
        p95_table="summary_hammerdb_p95_ms.csv",
    ),
}

# Cost efficiency spans the benchmarks rather than sitting inside one, so its
# copy lives next to them rather than in either spec.
COST_SPEC = dict(
    title="Managed MySQL - cost efficiency",
    sub="Transactions committed on the database host per USD billed for running it",
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-r", "--results", type=Path, default=Path("results"),
                    help="results root (default: %(default)s)")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("analysis"),
                    help="where the graphs and tables go (default: %(default)s)")
    ap.add_argument("--theme", choices=("light", "dark", "both"), default="both",
                    help="render light, dark, or both (default: %(default)s)")
    ap.add_argument("--format", default="png", help="image format (default: %(default)s)")
    ap.add_argument("--html", dest="html", default="index.html",
                    help="name of the HTML report inside --outdir (default: %(default)s)")
    ap.add_argument("--no-html", dest="html", action="store_const", const="",
                    help="skip the HTML report")
    ap.add_argument("--no-images", dest="images", action="store_false",
                    help="skip the PNGs and write only the HTML report and CSVs")
    args = ap.parse_args(argv)

    if not args.results.is_dir():
        print(f"analyze_results.py: no such results directory: {args.results}", file=sys.stderr)
        return 1

    sessions = discover(args.results)
    if not sessions:
        print(f"analyze_results.py: no result CSVs under {args.results}", file=sys.stderr)
        return 1
    for s in sessions:
        load(s)

    print(f"results root: {args.results.resolve()}")
    for s in sessions:
        note = f"  ({s.dropped} row(s) without a result dropped)" if s.dropped else ""
        print(f"  {s.bench:<9} {pretty(s.provider):<14} {s.stamp}  "
              f"{len(s.rows):>3} measurements{note}")

    # Read straight off the results tree, not off the sessions: a directory can
    # hold both benchmarks, and the host is billed once either way.
    work = HostWork(args.results)

    report_costs(sorted({s.provider for s in sessions}))
    report_host_efficiency(work)

    args.outdir.mkdir(parents=True, exist_ok=True)
    modes = ("light", "dark") if args.theme == "both" else (args.theme,)
    written: list[Path] = []
    aggs: dict[str, Agg] = {}
    lats: dict[str, Agg] = {}

    for bench, spec in BENCH_SPEC.items():
        group = [s for s in sessions if s.bench == bench and s.rows]
        if not group:
            print(f"\n{bench}: no usable rows, skipped")
            continue
        agg = Agg(bench, group)
        if len(agg.providers) > MAXSERIES:
            print(f"\n{bench}: {len(agg.providers)} providers exceeds the "
                  f"{MAXSERIES}-series palette cap; plotting the first {MAXSERIES}",
                  file=sys.stderr)
            agg.providers = agg.providers[:MAXSERIES]

        aggs[bench] = agg
        report(agg, spec["metric"], spec["conc_label"])
        written.append(write_table(agg, args.outdir / spec["table"],
                                   spec["dataset_col"], spec["conc_col"],
                                   spec["metric"].split()[0].lower()))

        # Throughput says how much work got through; p95 says what it cost the
        # slowest 5% of statements.  Same sweep, same providers, own axis.
        lat = Agg(bench, group, field="p95")
        lat.providers = [p for p in agg.providers if p in lat.providers]
        lats[bench] = lat
        if lat.providers:
            report_latency(lat, spec["p95_metric"].split(" (")[0], spec["conc_short"])
            written.append(write_table(lat, args.outdir / spec["p95_table"],
                                       spec["dataset_col"], spec["conc_col"], "p95_ms"))
        if not args.images:
            continue
        # One graph per file: every image holds a single plot for a single
        # data-set size.  The y-scale is still shared across the sizes of a
        # benchmark, so the separate files remain comparable.
        for mode in modes:
            suffix = "" if args.theme != "both" else f"-{mode}"
            metric_slug = "tps" if bench == "sysbench" else "nopm"
            for dataset in agg.datasets:
                ds = slug_dataset(bench, dataset)
                written.append(chart_scaling(
                    agg, mode,
                    args.outdir / f"{bench}-{metric_slug}-vs-{spec['conc_col']}"
                                  f"-{ds}{suffix}.{args.format}",
                    spec["metric"], spec["conc_label"], spec["title"], spec["sub"],
                    dataset))
                written.append(chart_peak(
                    agg, mode,
                    args.outdir / f"{bench}-peak-{metric_slug}-{ds}{suffix}.{args.format}",
                    spec["metric"], spec["conc_short"], spec["peak_title"],
                    spec["peak_sub"], dataset))
                if dataset in lat.datasets:
                    written.append(chart_scaling(
                        lat, mode,
                        args.outdir / f"{bench}-p95-vs-{spec['conc_col']}"
                                      f"-{ds}{suffix}.{args.format}",
                        spec["p95_metric"], spec["conc_label"], spec["p95_title"],
                        spec["p95_sub"], dataset, logy=True))

    # One chart, not one per benchmark: the instance is billed once for
    # everything that ran on it.
    if work.providers:
        written.append(write_cost_table(work, args.outdir / "summary_cost_efficiency.csv"))
        if args.images:
            for mode in modes:
                suffix = "" if args.theme != "both" else f"-{mode}"
                written.append(chart_cost(
                    work, mode,
                    args.outdir / f"cost-efficiency{suffix}.{args.format}",
                    COST_SPEC["title"], COST_SPEC["sub"]))

    if args.html and aggs:
        payload = html_report.build_payload(
            aggs, lats, sessions, work, args.results.resolve(), BENCH_SPEC,
            COST_SPEC, pretty, label_dataset, cost_usd, cost_native,
            {"asof": FX_ASOF, "source": FX_SOURCE, "usdPer": FX_USD_PER})
        written.append(html_report.write(payload, args.outdir / args.html))

    print("\nwrote:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
