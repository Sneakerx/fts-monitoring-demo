"""Windowed analyses of the example instrument: L0 -> L1 completeness and mission-timeline gaps,
SSD requirement compliance."""
from __future__ import annotations

from datetime import datetime

from ..monitoring import timeline
from .analyses import AnalysisContext, AnalysisOutput, analysis


def _runs(items: list, gap_ok) -> list[list]:
    """Split a sorted list into runs of consecutive items."""
    runs: list[list] = []
    for it in items:
        if runs and gap_ok(runs[-1][-1], it):
            runs[-1].append(it)
        else:
            runs.append([it])
    return runs


@analysis("L1_COMPLETENESS", inputs=("L0", "L1"))
def completeness(ctx: AnalysisContext, start: datetime, end: datetime) -> list[AnalysisOutput]:
    """Could L0 be processed to L1? Daily completeness, L0->L1 product links and timeline gaps.
    Products of both levels are matched by orbit."""
    l0 = ctx.products("L0", start, end)
    # every L1 product delivered by the PDGS counts (a corrupted one is flagged separately as FAILED)
    l1 = {p.orbit: p for p in ctx.products("L1", start, end, status=None)}
    for p in l0:
        if p.orbit in l1:
            ctx.link(p.id, l1[p.orbit].id, "L0->L1")

    if ctx.is_nominal:
        period = ctx.orbit_period
        # data gaps: no L0 products for more than 1.5 orbits (incl. the boundary to the previous window)
        prev = ctx.last_product_before("L0", start)
        seq = ([prev] if prev else []) + l0
        for a, b in zip(seq, seq[1:]):
            if b.sensing_start - a.sensing_stop > 0.5 * period:
                timeline.add_entry(ctx.s, "DATA_GAP", "No products received from the PDGS", a.sensing_stop,
                                   b.sensing_start, rule="L1_COMPLETENESS", merge_within=period,
                                   attributes={"orbits_missing": round((b.sensing_start - a.sensing_stop) / period)})
        # L1 gaps: L0 available but no L1 of the nominal baseline
        missing = [p for p in l0 if p.orbit not in l1]
        for run in _runs(missing, lambda a, b: b.orbit - a.orbit <= 1):
            timeline.add_entry(ctx.s, "L1_GAP", f"L0 not processed to L1 (orbits {run[0].orbit}-{run[-1].orbit})",
                               run[0].sensing_start, run[-1].sensing_stop, rule="L1_COMPLETENESS",
                               baseline=ctx.baseline, merge_within=period,
                               attributes={"orbits": [p.orbit for p in run]})
    if not l0:
        return []
    done = sum(1 for p in l0 if p.orbit in l1)
    return [AnalysisOutput("L1_COMPLETENESS", start + (end - start) / 2, 100.0 * done / len(l0))]


@analysis("SSD_COMPLIANCE", inputs=("FTS_L1B",))
def ssd_compliance(ctx: AnalysisContext, start: datetime, end: datetime) -> list[AnalysisOutput]:
    """Daily fraction of products whose spectral sampling distance meets the requirement."""
    req = ctx.requirement("SSD")
    vals = ctx.values("SSD", start, end)
    if req is None or not vals:
        return []
    ok = sum(1 for v in vals if req.complies(v))
    return [AnalysisOutput("SSD_COMPLIANCE", start + (end - start) / 2, 100.0 * ok / len(vals))]
