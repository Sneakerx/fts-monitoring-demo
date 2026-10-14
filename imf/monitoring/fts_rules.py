"""FTS rules E1-E6: trigger -> recommended action and proposed output (expert approval required).

Proposed outputs are drafts: nothing is sent to the satellite or the processing facility by the IMF
(there is no interface to the FOS); operations implement an approved proposal manually.
"""
from __future__ import annotations

import io
import json
import math
from datetime import timedelta
from statistics import median

import h5py
import numpy as np
from sqlalchemy import select

from ..models import Event, Parameter, Product, VectorSample
from ..timeutil import iso
from .rules import RuleContext, rule

BANDS = {"B1": (645.0, 1210.0), "B2": (1210.0, 2000.0), "B3": (2000.0, 2760.0)}


def _violating_level(ctx: RuleContext, ev: Event) -> float:
    """Median of the samples that confirmed the limit violation (persistence), i.e. the new level."""
    return median(ctx.recent(ev.parameter.code, max(1, ev.n_violations), ev.time, ev.baseline, ev.dims) or [ev.value])


# --- E1 ------------------------------------------------------------------------------------------
@rule("E1_CALIBRATION_MODE", "product", watches=("AUX_EFSI",),
      description="AUXEFSI product detected: special calibration mode was run")
def calibration_mode(ctx: RuleContext, product: Product) -> Event:
    hdr = json.loads(product.header or "{}")
    title = f"Special calibration mode {hdr.get('calibrationMode', 'EFSI')} #{hdr.get('calibrationId', '?')}"
    ev = Event(parameter_id=None, product_id=product.id, severity="INFO", time=product.sensing_start,
               value=None, limit_text="", last_time=product.sensing_stop, n_violations=1,
               cleared_at=product.sensing_stop, status="CLOSED", category="timeline", title=title,
               recommended_action=None)
    ctx.s.add(ev)
    ctx.s.flush()
    ctx.timeline("CALIBRATION_MODE", title, product.sensing_start, product.sensing_stop,
                 product_id=product.id, event_id=ev.id, attributes=hdr)
    return ev


# --- E2 ------------------------------------------------------------------------------------------
@rule("E2_LASER_WAVENUMBER", "limit", watches=("SPEC_SHIFT",),
      description="Spectral calibration threshold reached: update the visible laser wavenumber")
def laser_wavenumber(ctx: RuleContext, ev: Event) -> None:
    shift = _violating_level(ctx, ev)
    current = ctx.approved_value() or 1.0e7 / float(ctx.characterisation["laser_nominal_nm"])
    proposed = current / (1.0 + shift * 1e-6)
    ev.recommended_action = (f"Update the visible laser wavenumber of the L1 processor from {current:.6f} to "
                             f"{proposed:.6f} cm⁻¹ (observed spectral shift {shift:+.2f} ppm)")
    ctx.propose(ev, "value", "Visible laser wavenumber for L1 processing", value=proposed, unit="cm⁻¹",
                payload={"current_cm1": current, "observed_shift_ppm": shift, "n_samples": ev.n_violations,
                         "event_time": iso(ev.time)})


# --- E3 ------------------------------------------------------------------------------------------
@rule("E3_ISRF_SCALING", "limit", watches=("ISRF_FEATURE",),
      description="Radiometric feature with impact on the ISRF: update the radiometric scaling of the ISRF")
def isrf_scaling(ctx: RuleContext, ev: Event) -> None:
    feature = _violating_level(ctx, ev)
    scaling = 1.0 / (1.0 - feature / 100.0)
    ev.recommended_action = (f"Update the radiometric scaling of the ISRF to {scaling:.5f} "
                             f"(radiometric feature depth {feature:.2f} %)")
    ctx.propose(ev, "value", "Radiometric scaling factor for the ISRF", value=scaling, unit="",
                payload={"feature_depth_percent": feature, "event_time": iso(ev.time)})


# --- E4 ------------------------------------------------------------------------------------------
@rule("E4_CTF_UPDATE", "limit", watches=("REF_SCI_DELAY",),
      description="Delay between reference and science signal: update the complex transfer function")
def ctf_update(ctx: RuleContext, ev: Event) -> None:
    delay = _violating_level(ctx, ev)
    nominal = float(ctx.characterisation.get("ref_sci_delay_nominal_us", 2.0))
    d_tau = (delay - nominal) * 1e-6
    v = float(ctx.characterisation.get("scan_speed_cm_s", 1.0))
    lines = ["# Complex transfer function update: CTF_new(nu) = CTF_old(nu) * exp(-i 2 pi f(nu) dtau), "
             "f = 2 v nu", f"# delay change {d_tau * 1e6:+.4f} us, scan speed {v} cm/s",
             "band,wavenumber_cm1,re,im,phase_rad"]
    for band, (lo, hi) in BANDS.items():
        for nu in np.linspace(lo, hi, 32):
            phase = -2.0 * math.pi * (2.0 * v * nu) * d_tau
            lines.append(f"{band},{nu:.3f},{math.cos(phase):.8f},{math.sin(phase):.8f},{phase:.8f}")
    ev.recommended_action = (f"Update the complex transfer function for a reference/science delay of "
                             f"{delay:.3f} us ({d_tau * 1e6:+.3f} us w.r.t. nominal)")
    ctx.propose(ev, "file", "Complex transfer function update (phase correction)", value=delay, unit="µs",
                payload={"delay_us": delay, "delta_us": d_tau * 1e6, "format": "CSV"},
                content=("\n".join(lines) + "\n").encode(),
                filename=f"CTF_UPDATE_{ev.time:%Y%m%dT%H%M%S}_DRAFT.csv")


# --- E5 ------------------------------------------------------------------------------------------
@rule("E5_ZPD_DRIFT", "trend", parameter="ZPD_OFFSET", window_days=10, max_slope_per_day=0.035,
      min_samples=40, step_hours=12, confirm_steps=2, lead_days=7, description="ZPD drift: send telecommand")
def zpd_telecommand(ctx: RuleContext, ev: Event, slope: float, points: list) -> None:
    """Propose a ZPD offset correction that re-centres the ZPD for the next `lead_days` of drift."""
    current = float(median([p[1] for p in points[-15:]]))
    lead = float(ctx.cfg.get("lead_days", 7))
    correction = round(-(current + slope * lead / 2.0), 1)
    tc = {"telecommand": "FTS_SET_ZPD_OFFSET", "parameters": {"zpd_offset_correction_samples": correction},
          "rationale": f"ZPD drift {slope:+.4f} samples/day, current offset {current:+.2f} samples; correction "
                       f"centres the ZPD over the next {lead:g} days",
          "valid_from": iso(ev.time), "status": "DRAFT - to be uplinked by operations after approval"}
    ev.recommended_action = (f"Send telecommand FTS_SET_ZPD_OFFSET with a correction of {correction:+.1f} samples "
                             f"(drift {slope:+.3f} samples/day, current offset {current:+.2f} samples)")
    ctx.propose(ev, "telecommand", "Telecommand FTS_SET_ZPD_OFFSET (re-centre ZPD)", value=correction,
                unit="samples", payload=tc, content=json.dumps(tc, indent=2).encode(),
                filename=f"TC_ZPD_OFFSET_{ev.time:%Y%m%dT%H%M%S}_DRAFT.json")


# --- E6 ------------------------------------------------------------------------------------------
@rule("E6_NESR_DRIFT", "trend", parameter="NESR_IDX_MEAN", window_days=10, max_slope_per_day=0.002,
      min_samples=40, step_hours=12, confirm_steps=2, average_hours=24,
      description="NESR drift: update the NESR AUX file feeding the L1 quality")
def nesr_aux_file(ctx: RuleContext, ev: Event, slope: float, points: list) -> None:
    p = ctx.s.scalar(select(Parameter).where(Parameter.code == "NESR_SPECTRAL"))
    since = ev.time - timedelta(hours=float(ctx.cfg.get("average_hours", 24)))
    rows = [] if p is None else list(ctx.s.scalars(select(VectorSample).where(
        VectorSample.parameter_id == p.id, VectorSample.time > since, VectorSample.time <= ev.time,
        VectorSample.baseline == ev.baseline)))
    by_band: dict[str, list] = {}
    for r in rows:
        by_band.setdefault(r.dims.split("=", 1)[-1], []).append(r)
    bio = io.BytesIO()
    with h5py.File(bio, "w") as f:
        f.attrs.update({"product_type": "AUX_NESR", "status": "DRAFT", "validity_start": iso(ev.time),
                        "generated_by": "IMF rule E6_NESR_DRIFT", "baseline": ev.baseline,
                        "nesr_index_slope_per_day": slope, "n_orbits": len(rows) // max(1, len(by_band) or 1)})
        for band, vs in sorted(by_band.items()):
            axis = np.array(json.loads(vs[0].axis))
            vals = np.mean([json.loads(v.values) for v in vs], axis=0)
            g = f.create_group(f"Band{band[-1]}")
            g["wavenumber"] = axis
            g["nesr"] = vals
            g.attrs["unit"] = "nW/(cm2 sr cm-1)"
    ev.recommended_action = (f"Update the NESR AUX file feeding the L1 quality (NESR index drift "
                             f"{slope:+.4f}/day); draft generated from {len(rows)} orbit-averaged spectra")
    ctx.propose(ev, "file", "NESR AUX file (draft)", value=slope, unit="1/day",
                payload={"n_spectra": len(rows), "bands": sorted(by_band), "averaging_hours": ctx.cfg.get("average_hours")},
                content=bio.getvalue(), filename=f"AUX_NESR_{ev.time:%Y%m%dT%H%M%S}_DRAFT.h5")
