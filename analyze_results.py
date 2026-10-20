#!/usr/bin/env python3
"""
analyze_results.py - build TPS / NOPM graphs from a cloud-mysql-perf.sh results tree.

The results root holds one directory per benchmark session, named
<provider>-<UTC stamp>[-<label>].  A session is classified by the result CSV it
contains, so the sysbench and the HammerDB session of a provider are kept apart
regardless of the order they were run in:

    <provider>_sysbench_results.csv  -> sysbench oltp_read_write, metric TPS
    <provider>_hammerdb_results.csv  -> HammerDB TPC-C,           metric NOPM

One graph per image file: a scaling chart, a peak chart and a cost-efficiency
chart for each data-set size, each emitted once per theme (light / dark), plus
the table view of the same aggregates as CSV.  Charts of the same benchmark
share a y-scale across the sizes, so separate files stay comparable.  Rows whose
metric is empty or NA are dropped and reported - an aborted run leaves those
behind.

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
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.path import Path as MPath
from matplotlib.patches import PathPatch

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
# What the benchmarked instance cost at each provider, as billed - in the
# provider's own currency, because that is the number on the invoice.  Cost
# efficiency is throughput per USD of that spend, so every price is converted
# through one dated rate table rather than a mix of eyeballed conversions.
COSTS = {
    "exoscale": (75, "CHF"),
    "scaleway": (49, "EUR"),
    "vultr": (51, "USD"),
    "upcloud": (34, "USD"),
    "digitalocean": (16, "USD"),
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
        self.window_s = session_window(path)

    def __repr__(self) -> str:
        return f"<{self.provider} {self.stamp} {self.bench} n={len(self.rows)}>"


# The HammerDB CSV reports rates, not counts, so turning them back into work
# done needs the length of the measured window.  Read it from the run's own
# config rather than assuming the harness default, which is overridable.
WINDOW_DEFAULT_S = 20 * 60
DURATION_LINE = re.compile(r"^duration_min\s+(\d+)")


def session_window(path: Path) -> int:
    for cfg in sorted(path.glob("*_config.txt")):
        for line in cfg.read_text(errors="replace").splitlines():
            m = DURATION_LINE.match(line)
            if m:
                return int(m.group(1)) * 60
    print(f"analyze_results.py: no duration_min in {path.name}, assuming "
          f"{WINDOW_DEFAULT_S // 60} min", file=sys.stderr)
    return WINDOW_DEFAULT_S


def discover(root: Path) -> list[Session]:
    sessions = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        m = DIRNAME.match(d.name)
        provider = m.group("provider").lower() if m else d.name.split("-")[0].lower()
        stamp = m.group("stamp") if m else ""
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


def load(session: Session) -> None:
    """Read the session CSV into rows of {dataset, conc, metric, txn, run}.

    `txn` is the work the measurement actually did - a count, not a rate - so
    that the runs of a provider can be summed.  sysbench reports the count
    itself (its post-ramp-up block, hence transactions == tps * window);
    HammerDB reports only rates, so the count comes back from tpm * window.
    """
    # sysbench: provider,benchmark,run,tables,rows_per_table,threads,
    #           elapsed_s,transactions,tps,queries,qps,...
    # hammerdb: provider,benchmark,run,warehouses,vu,nopm,tpm,status,log_errors
    key = ("rows_per_table", "threads", "tps") if session.bench == "sysbench" \
        else ("warehouses", "vu", "nopm")
    minutes = session.window_s / 60.0
    with session.csv_path.open(newline="") as fh:
        for rec in csv.DictReader(fh):
            dataset, conc, metric = (_num(rec.get(k, "")) for k in key)
            if None in (dataset, conc, metric) or metric <= 0:
                session.dropped += 1
                continue
            if session.bench == "sysbench":
                # Counted by sysbench; derived only if that column is missing.
                txn = _num(rec.get("transactions", "")) or metric * session.window_s
                nord = txn
            else:
                # TPC-C counts all five transaction types; new orders are the
                # metric of record, so both are kept.
                tpm = _num(rec.get("tpm", "")) or 0.0
                nord = metric * minutes
                txn = tpm * minutes or nord
            session.rows.append(dict(
                dataset=int(dataset), conc=int(conc), metric=metric,
                txn=txn, nord=nord,
                run=int(_num(rec.get("run", "0")) or 0),
                provider=rec.get("provider", session.provider).strip() or session.provider,
            ))


# --------------------------------------------------------------- aggregation --
class Agg:
    """mean / min / max of a metric per (provider, dataset, concurrency), plus
    the individual run values behind each of those means."""

    def __init__(self, bench: str, sessions: list[Session]):
        self.bench = bench
        self.sessions = sessions
        bucket: dict[tuple[str, int, int], list[float]] = defaultdict(list)
        runs: dict[tuple[str, int, int], list[tuple[int, float]]] = defaultdict(list)
        # Work done, summed over every measurement: all concurrency levels, all
        # runs.  [transactions, new-order transactions, measurements].
        work: dict[tuple[str, int], list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for s in sessions:
            for r in s.rows:
                bucket[(s.provider, r["dataset"], r["conc"])].append(r["metric"])
                runs[(s.provider, r["dataset"], r["conc"])].append((r["run"], r["metric"]))
                w = work[(s.provider, r["dataset"])]
                w[0] += r["txn"]
                w[1] += r["nord"]
                w[2] += 1
        self.work = dict(work)
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

    def total(self, provider: str, dataset: int | None = None):
        """Transactions summed over all runs - of one data-set size, or of the
        whole benchmark when dataset is None.  Returns (txn, new orders, n)."""
        keys = [(provider, dataset)] if dataset is not None else \
            [(provider, d) for d in self.datasets]
        rows = [self.work[k] for k in keys if k in self.work]
        if not rows:
            return (None, None, 0)
        return (sum(r[0] for r in rows), sum(r[1] for r in rows),
                int(sum(r[2] for r in rows)))

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


def txn_word(bench: str) -> str:
    """What one counted unit of work is called in this benchmark."""
    return "transactions" if bench == "sysbench" else "TPC-C transactions"


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


def chrome(ax, t: dict, ylabel: str = "") -> None:
    """Recessive axes: hairline solid y-grid, no top/right spines."""
    ax.set_axisbelow(True)
    ax.grid(axis="y", color=t["grid"], linewidth=1.0, linestyle="-")
    ax.grid(axis="x", visible=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["axis"])
    ax.yaxis.set_major_formatter(lambda v, _pos: f"{v:,.0f}")
    if ylabel:
        ax.set_ylabel(ylabel, color=t["secondary"], labelpad=8)


def px_to_data(ax, px: float, axis: str = "x") -> float:
    """A display-space length in data units. Only valid once limits are set."""
    inv = ax.transData.inverted()
    o = ax.transData.transform((0, 0))
    if axis == "x":
        return abs(inv.transform((o[0] + px, o[1]))[0] - inv.transform(o)[0])
    return abs(inv.transform((o[0], o[1] + px))[1] - inv.transform(o)[1])


def rounded_bar(ax, x: float, width: float, height: float, color: str,
                radius_px: float = 4.0):
    """A column with a 4px rounded cap, square at the baseline."""
    if height <= 0:
        return
    rx = min(px_to_data(ax, radius_px, "x"), width / 2)
    ry = min(px_to_data(ax, radius_px, "y"), height)
    left, right, top = x - width / 2, x + width / 2, height
    k = 0.5523  # circular-arc control-point factor
    verts = [
        (left, 0), (left, top - ry),
        (left, top - ry + ry * k), (left + rx - rx * k, top), (left + rx, top),
        (right - rx, top),
        (right - rx + rx * k, top), (right, top - ry + ry * k), (right, top - ry),
        (right, 0), (left, 0),
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
                  title: str, subtitle: str, dataset: int) -> Path:
    """Throughput against the concurrency sweep, for one data-set size."""
    t = style(mode)
    colors = SERIES[mode]
    fig, ax = plt.subplots(figsize=(10.3, 5.4))
    xs = np.arange(len(agg.concurrency))
    # One y-scale across every data-set size, even though each size is now its
    # own file: these charts are read side by side, and an axis that re-fitted
    # per file would let a slower data set draw a taller line than a faster one.
    ymax = max((v[2] for v in agg.stats.values()), default=1.0)

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
               title: str, subtitle: str, dataset: int, per_usd: bool = False) -> Path:
    """Peak throughput per provider at one data-set size, one bar per provider.

    With per_usd the bar is instead every transaction the provider completed at
    that size - summed over all concurrency levels and all runs - divided by
    what the instance cost, so it reads as work bought per dollar.
    """
    t = style(mode)
    colors = SERIES[mode]
    # Only the gaps that apply to the size this file is about.
    gaps = [g for g in agg.incomplete() if g[1] == dataset]
    # Sized to the content: six 24px bars need very little width, and the only
    # thing here that wants more is the title.
    fig, ax = plt.subplots(figsize=(5.6, 5.3 if gaps else 5.0))
    unit = metric.split()[0]
    # Only the first letter: .capitalize() would turn "TPC-C" into "Tpc-c".
    word = txn_word(agg.bench)
    chrome(ax, t, f"{word[0].upper()}{word[1:]} per USD of spend"
           if per_usd else metric)
    if per_usd:
        # Counts run into the tens of millions; full digits on every tick would
        # be a wall of zeroes.
        ax.yaxis.set_major_formatter(lambda v, _pos: compact(v))

    def value_at(provider: str, dataset: int) -> tuple[float | None, float | None]:
        """(bar height, the count behind it) - the count is None off per_usd."""
        if not per_usd:
            return agg.peak(provider, dataset)
        txn, _nord, _n = agg.total(provider, dataset)
        usd = cost_usd(provider)
        # An unpriced provider gets no bar; a zero-height one would read as
        # "no work per dollar" rather than "price unknown".
        return (None, None) if not txn or not usd else (txn / usd, txn)

    peaks = {(0, i): value_at(p, dataset) for i, p in enumerate(agg.providers)}
    # Scaled to the largest bar across *all* sizes, not just this one, so the
    # three files of a benchmark stay comparable bar-for-bar.
    ymax = max((value_at(p, d)[0] or 0.0
                for p in agg.providers for d in agg.datasets), default=1.0)

    ax.set_xticks([0], [label_dataset(agg.bench, dataset)])
    ax.tick_params(axis="x", labelcolor=t["secondary"], labelsize=10)
    ax.set_xlim(-0.55, 0.55)
    ax.set_ylim(0, ymax * 1.16)

    # A peak taken over a partial sweep is not comparable with a full one, and a
    # sum over fewer measurements even less so - it is less work, not slower
    # work.  Say it rather than letting the bar imply otherwise.
    note = ("Summed over fewer measurements, so not comparable: " if per_usd else
            "Incomplete sweep, peak taken over fewer levels: ") + "; ".join(
        f"{pretty(p)} {label_dataset(agg.bench, d)} ({conc_short}="
        f"{', '.join(str(c) for c in have)} only)" for p, d, have in gaps)
    # Wrapped to the figure, not run off its right edge: a footnote that is cut
    # in half is worse than no footnote, because it reads as complete.
    lines = textwrap.wrap(note, width=int((fig.get_figwidth() - 0.3) / 0.0666)) \
        if gaps else []
    foot = 0.055 + 0.019 * len(lines)

    # Bar geometry is sized in display pixels, so the final axes box has to be
    # settled first - both the limits and the layout feed that transform.
    titles(fig, title, subtitle, t)
    # Six entries do not fit one row at this width, so two rows of three - and
    # the plot box gives up the height for them.
    figure_legend(fig, agg.providers, colors, t,
                  y=foot - 0.045 if gaps else 0.005, ncol=3)
    fig.tight_layout(rect=(0.0, foot + 0.105 if gaps else 0.115, 1.0, 0.88))
    if lines:
        fig.text(0.012, 0.010, "\n".join(lines), ha="left", va="bottom",
                 fontsize=8, color=t["muted"], linespacing=1.5)
    fig.canvas.draw()

    n = len(agg.providers)
    gap = px_to_data(ax, 2.0)               # 2px surface gap between neighbours
    # Bars are capped at 24px and never fill the slot - the leftover is air.
    bar_w = min(0.80 / n - gap, px_to_data(ax, 24.0))
    pitch = bar_w + gap

    for (g, i), (value, at) in peaks.items():
        if value is None:
            continue
        x = g + (i - (n - 1) / 2) * pitch
        rounded_bar(ax, x, bar_w, value, colors[i])
        # Label selectively: only the leader of each group, which is the one the
        # chart is about. Every value stays in the table view, so nothing is
        # gated - that is also the relief for the light-mode slots under 3:1.
        leader = max((v[0] for (gg, _), v in peaks.items()
                      if gg == g and v[0] is not None), default=None)
        if value == leader:
            # In cost mode the second line is the quotient's two inputs, so the
            # bar is never an unexplained ratio.
            second = (f"{compact(at)} / {cost_usd(agg.providers[i]):,.2f} USD"
                      if per_usd else f"{conc_short}={at}")
            ax.annotate(f"{compact(value) if per_usd else f'{value:,.0f}'}\n{second}",
                        (x, value), textcoords="offset points", xytext=(0, 6),
                        ha="center", va="bottom", fontsize=8.5,
                        color=t["secondary"], linespacing=1.3, zorder=5)

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


def report_efficiency(agg: Agg, metric: str) -> None:
    """Cost efficiency as total work per dollar: every transaction of every run
    at a data-set size, divided by the price of the instance."""
    word = txn_word(agg.bench)
    print(f"\n{agg.bench}: cost efficiency - {word} completed per USD of spend")
    print("  (sum over all concurrency levels and all runs of the measured window)")
    head = "  " + f"{'provider':<14}{'USD':>9}{'n':>5}" + "".join(
        f"{label_dataset(agg.bench, d):>26}" for d in agg.datasets) + f"{'all sizes':>26}"
    print(head)
    ranked = []
    for provider in agg.providers:
        usd = cost_usd(provider)
        _t, _n, count = agg.total(provider)
        line = ("  " + f"{pretty(provider):<14}"
                + (f"{usd:>9,.0f}" if usd else f"{'-':>9}") + f"{count:>5}")
        for dataset in list(agg.datasets) + [None]:
            txn, _nord, _n = agg.total(provider, dataset)
            if not txn or not usd:
                line += f"{'no data' if not txn else 'not priced':>26}"
                continue
            per = txn / usd
            line += f"{f'{compact(per)} / USD':>26}"
            if dataset is None:
                ranked.append((per, provider, txn))
        print(line)
    if ranked:
        ranked.sort(reverse=True)
        best, worst = ranked[0], ranked[-1]
        print(f"  totals: {pretty(best[1])} completed {compact(best[2])} {word} "
              f"= {compact(best[0])} per USD, "
              f"{best[0] / worst[0]:.1f}x {pretty(worst[1])} ({compact(worst[0])} per USD)")
    # A sum is only as comparable as its coverage: a provider with missing
    # measurements shows less work done, which is not the same as slower work.
    counts = {p: agg.total(p)[2] for p in agg.providers}
    full = max(counts.values(), default=0)
    short = [(p, c) for p, c in counts.items() if c < full]
    if short:
        print("  NOT comparable - fewer measurements in the sum: " + "; ".join(
            f"{pretty(p)} n={c} of {full}" for p, c in short))


def write_cost_table(aggs: dict, out: Path) -> Path:
    """Work done, price and work-per-USD, one row per provider/data-set size
    plus an all-sizes row.  The peak rate stays in as context for the sum."""
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["benchmark", "provider", "dataset", "dataset_label",
                    "measurements", "total_transactions", "total_new_orders",
                    "peak_metric", "peak", "at_concurrency",
                    "cost_as_billed", "currency", "usd_per_unit", "cost_usd",
                    "transactions_per_usd", "new_orders_per_usd"])
        for bench, agg in aggs.items():
            unit = BENCH_SPEC[bench]["metric"].split()[0]
            for provider in agg.providers:
                native, usd = cost_native(provider), cost_usd(provider)
                for dataset in list(agg.datasets) + [None]:
                    txn, nord, count = agg.total(provider, dataset)
                    if not txn:
                        continue
                    peak, at = (agg.peak(provider, dataset) if dataset is not None
                                else (None, None))
                    w.writerow([
                        bench, provider,
                        "" if dataset is None else dataset,
                        "all sizes" if dataset is None else label_dataset(bench, dataset),
                        count, f"{txn:.0f}", f"{nord:.0f}",
                        unit, "" if peak is None else f"{peak:.2f}",
                        "" if at is None else at,
                        "" if native is None else f"{native[0]:.2f}",
                        "" if native is None else native[1],
                        "" if native is None else f"{FX_USD_PER[native[1]]:.6f}",
                        "" if usd is None else f"{usd:.2f}",
                        "" if usd is None else f"{txn / usd:.1f}",
                        "" if usd is None else f"{nord / usd:.1f}",
                    ])
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


# ----------------------------------------------------------------- main ------
BENCH_SPEC = {
    "sysbench": dict(
        metric="TPS (transactions/s)", conc_label="Threads", conc_short="t",
        dataset_col="rows_per_table", conc_col="threads",
        title="sysbench oltp_read_write - throughput",
        sub="Managed MySQL, 10 tables; mean of the measured 20 min window across runs, band = min-max",
        peak_title="sysbench oltp_read_write - peak throughput",
        peak_sub="Best thread level per provider, at the data-set size named below",
        eff_title="sysbench oltp_read_write - cost efficiency",
        eff_sub="Transactions completed across all runs per USD of instance spend",
        table="summary_sysbench_tps.csv",
    ),
    "hammerdb": dict(
        metric="NOPM (new orders/min)", conc_label="Virtual users", conc_short="vu",
        dataset_col="warehouses", conc_col="vu",
        title="HammerDB TPC-C - throughput",
        sub="Managed MySQL; mean of the measured 20 min window across runs, band = min-max",
        peak_title="HammerDB TPC-C - peak throughput",
        peak_sub="Best virtual-user level per provider, at the warehouse count named below",
        eff_title="HammerDB TPC-C - cost efficiency",
        eff_sub="TPC-C transactions across all runs per USD of instance spend",
        table="summary_hammerdb_nopm.csv",
    ),
}


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

    report_costs(sorted({s.provider for s in sessions}))

    args.outdir.mkdir(parents=True, exist_ok=True)
    modes = ("light", "dark") if args.theme == "both" else (args.theme,)
    written: list[Path] = []
    aggs: dict[str, Agg] = {}

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
        report_efficiency(agg, spec["metric"])
        written.append(write_table(agg, args.outdir / spec["table"],
                                   spec["dataset_col"], spec["conc_col"],
                                   spec["metric"].split()[0].lower()))
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
                written.append(chart_peak(
                    agg, mode,
                    args.outdir / f"{bench}-cost-efficiency-{ds}{suffix}.{args.format}",
                    spec["metric"], spec["conc_short"], spec["eff_title"],
                    spec["eff_sub"], dataset, per_usd=True))

    if aggs:
        written.append(write_cost_table(aggs, args.outdir / "summary_cost_efficiency.csv"))

    if args.html and aggs:
        payload = html_report.build_payload(
            aggs, sessions, args.results.resolve(), BENCH_SPEC, pretty, label_dataset,
            cost_usd, cost_native,
            {"asof": FX_ASOF, "source": FX_SOURCE, "usdPer": FX_USD_PER})
        written.append(html_report.write(payload, args.outdir / args.html))

    print("\nwrote:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
