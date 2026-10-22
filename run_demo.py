"""Start the complete IMF demonstrator: PDGS simulator + IMF (services + web portal).

    python run_demo.py            # continue where the last run stopped
    python run_demo.py --reset    # fresh start: new 90-day mission history, empty database
    python run_demo.py --reset --l1-format netcdf4   # L1 products as netCDF4 instead of HDF5

Then open http://127.0.0.1:8000 and log in as admin/admin, expert/expert or guest/guest.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def wait_for(url: str, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def config_for_format(l1_format: str) -> dict[str, str]:
    """Environment for the IMF process: a copy of the configuration whose L1 product type uses `l1_format`."""
    source = Path(os.environ.get("IMF_CONFIG", ROOT / "config" / "imf.yaml"))
    cfg = yaml.safe_load(source.read_text(encoding="utf-8"))
    types = cfg["pdgs"]["product_types"]
    if all(t.get("format", "hdf5") == l1_format for t in types if t.get("level", "L1") == "L1"):
        return dict(os.environ)
    for t in types:
        if t.get("level", "L1") == "L1":
            t["format"] = l1_format
    target = DATA / f"imf-{l1_format}.yaml"
    target.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {**os.environ, "IMF_CONFIG": str(target)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete data/ (database, reports, simulator clock)")
    ap.add_argument("--days", type=float, default=90.0, help="mission history on first start (default 90)")
    ap.add_argument("--orbit-every", type=float, default=30.0,
                    help="real seconds per simulated orbit in live mode (default 30)")
    ap.add_argument("--l1-format", default="hdf5", choices=["hdf5", "netcdf4"],
                    help="file format of the simulated L1 products (default hdf5)")
    args = ap.parse_args()

    if args.reset and DATA.exists():
        print("Resetting demo data ...")
        shutil.rmtree(DATA)
    DATA.mkdir(exist_ok=True)

    accel = 101.0 * 60.0 / args.orbit_every
    py = sys.executable
    procs = []
    try:
        print("Starting PDGS simulator on http://127.0.0.1:8100 ...")
        procs.append(subprocess.Popen([py, "-m", "pdgs_sim.server", "--days", str(args.days), "--accel", str(accel),
                                       "--state", str(DATA / "pdgs_sim" / "state.json"), "--l1-format", args.l1_format],
                                      cwd=ROOT))
        if not wait_for("http://127.0.0.1:8100/status"):
            raise SystemExit("PDGS simulator did not start")
        print("Starting IMF on http://127.0.0.1:8000 ...")
        procs.append(subprocess.Popen([py, "-m", "imf.web.app"], cwd=ROOT, env=config_for_format(args.l1_format)))
        if not wait_for("http://127.0.0.1:8000/login", 60):
            raise SystemExit("IMF did not start")
        print("\n  IMF portal:   http://127.0.0.1:8000   (admin/admin · expert/expert · guest/guest)"
              "\n  API docs:     http://127.0.0.1:8000/api/docs"
              "\n  PDGS sim:     http://127.0.0.1:8100/docs"
              "\n\n  The initial backfill of the mission history runs in the background (~1-2 min)."
              "\n  Press Ctrl+C to stop.\n")
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
