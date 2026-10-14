"""Matplotlib figures for reports (object API, Agg backend - safe in background threads)."""
from __future__ import annotations

import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

INK = "#1f2a37"
SERIES = "#1f5fa8"
WARN = "#d99a00"
ALARM = "#c62828"
GRID = "#d9dee5"
PALETTE = ["#1f5fa8", "#d1495b", "#2a9d8f", "#8e3b9a", "#e9a03b", "#3d5a80", "#6a994e", "#bc6c25",
           "#5e548e", "#9c6644", "#264653", "#e76f51"]


def group_figure(group: str, series: list[dict], start: datetime, end: datetime) -> Figure:
    """series: [{code, name, unit, limit (Limit|None), lines: [{label, times, values, levels}]}]"""
    n = len(series)
    fig = Figure(figsize=(11.0, 1.9 * n + 0.8), layout="constrained")
    fig.suptitle(f"{group} parameters", fontsize=12, color=INK, x=0.01, ha="left", fontweight="bold")
    axes = fig.subplots(n, 1, sharex=True, squeeze=False)[:, 0]
    for ax, sr in zip(axes, series):
        lim = sr["limit"]
        if lim is not None:
            for v, c, ls in ((lim.warn_lo, WARN, "--"), (lim.warn_hi, WARN, "--"),
                             (lim.alarm_lo, ALARM, "-"), (lim.alarm_hi, ALARM, "-")):
                if v is not None:
                    ax.axhline(v, color=c, lw=1.0, ls=ls, alpha=0.9)
        lines = [ln for ln in sr["lines"] if ln["times"]]
        if lines:
            multi = len(lines) > 1
            for i, ln in enumerate(lines):
                color = PALETTE[i % len(PALETTE)] if multi else SERIES
                ax.plot(ln["times"], ln["values"], ".-", ms=2.5 if multi else 3, lw=0.6 if multi else 0.8,
                        color=color, label=ln["label"] or None)
                for lvl, c in {1: WARN, 2: ALARM}.items():
                    pts = [(t, v) for t, v, l in zip(ln["times"], ln["values"], ln["levels"]) if l == lvl]
                    if pts:
                        ax.plot([p[0] for p in pts], [p[1] for p in pts], "o", ms=4, color=c, zorder=3)
            if multi and len(lines) <= 12:
                ax.legend(fontsize=6, ncol=min(6, len(lines)), loc="upper left", frameon=False)
            _autoscale(ax, [v for ln in lines for v in ln["values"]], lim)
        else:
            ax.text(0.5, 0.5, "no data in period", transform=ax.transAxes, ha="center", va="center",
                    color="#8a94a3")
        ax.set_title(f"{sr['name']} [{sr['unit']}]", fontsize=9, loc="left", color=INK)
        ax.ticklabel_format(axis="y", useOffset=False)
        ax.grid(True, color=GRID, lw=0.6)
        ax.tick_params(labelsize=8, colors=INK)
        for spine in ax.spines.values():
            spine.set_color(GRID)
    axes[-1].set_xlim(start, end)
    loc = mdates.AutoDateLocator()
    axes[-1].xaxis.set_major_locator(loc)
    axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    return fig


def spectrum_figure(title: str, unit: str, curves: list[dict]) -> Figure:
    """curves: [{band, axis, values, template_axis, template_values}] - one panel per band."""
    n = len(curves)
    fig = Figure(figsize=(11.0, 2.6 * n + 0.6), layout="constrained")
    fig.suptitle(title, fontsize=12, color=INK, x=0.01, ha="left", fontweight="bold")
    axes = fig.subplots(n, 1, squeeze=False)[:, 0]
    for ax, c in zip(axes, curves):
        ax.plot(c["axis"], c["values"], "-", lw=1.1, color=SERIES, label="measured (period mean)")
        if c.get("template_axis"):
            ax.plot(c["template_axis"], c["template_values"], "--", lw=1.1, color=ALARM, label="spectral template")
        ax.set_title(f"Band {c['band']} [{unit}]", fontsize=9, loc="left", color=INK)
        ax.set_xlabel("wavenumber [cm⁻¹]", fontsize=8)
        ax.grid(True, color=GRID, lw=0.6)
        ax.tick_params(labelsize=8, colors=INK)
        ax.legend(fontsize=7, frameon=False)
    return fig


def _autoscale(ax, values: list[float], lim) -> None:
    """Scale to the data, include near-by limits but don't let far-away limits flatten the curve."""
    lo, hi = min(values), max(values)
    span = max(hi - lo, abs(hi) * 1e-3, 1e-9)
    if lim is not None:
        for v in (lim.warn_lo, lim.warn_hi):
            if v is not None and abs(v - (lo + hi) / 2) < 3 * span:
                lo, hi = min(lo, v), max(hi, v)
    pad = 0.12 * (hi - lo or span)
    ax.set_ylim(lo - pad, hi + pad)


def to_png(fig: Figure, dpi: int = 110) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    return buf.getvalue()
