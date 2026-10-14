"""Monitoring-parameter algorithms for L0 headers and auxiliary data.

L0 products are ingested header-only (catalogue attributes); the algorithms build the L0 catalogue
monitoring (number of views / scans per product, packet errors). Ancillary products provide the
corner-cube turn-around positions and the on-board fringe counter used for the ZPD monitoring.
"""
from __future__ import annotations

import numpy as np

from .readers import AncillaryProduct, HeaderProduct
from .registry import Dim, algorithm, dims

L0_VIEWS = {"FTS_L0_BB": "BB", "FTS_L0_DS": "DS", "FTS_L0_EO": "EO"}


@algorithm("L0_EO_SCANS", "L0_PACKET_ERRORS", inputs=("FTS_L0_EO",))
def l0_earth_header(p: HeaderProduct, ctx: dict) -> dict:
    return {"L0_EO_SCANS": p.number("numberOfScans"),
            "L0_PACKET_ERRORS": [Dim(p.number("packetErrors"), dims(view="EO"))]}


@algorithm("L0_CAL_VIEWS", "L0_PACKET_ERRORS", inputs=("FTS_L0_BB", "FTS_L0_DS"))
def l0_calibration_header(p: HeaderProduct, ctx: dict) -> dict:
    view = L0_VIEWS[p.product_type]
    return {"L0_CAL_VIEWS": [Dim(p.number("numberOfViews"), dims(view=view))],
            "L0_PACKET_ERRORS": [Dim(p.number("packetErrors"), dims(view=view))]}


@algorithm("ANC_TA_OFFSET", "ANC_FRINGE_DEV", inputs=("AUX_ANC",))
def ancillary(p: AncillaryProduct, ctx: dict) -> dict:
    return {
        "ANC_TA_OFFSET": float(np.mean(p.turnaround_position()) - ctx["turnaround_nominal_sample"]),
        "ANC_FRINGE_DEV": float(np.mean(p.fringe_count()) - ctx["fringes_nominal"]),
    }
