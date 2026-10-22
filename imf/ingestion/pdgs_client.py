"""Client for an OData-style PDGS product catalogue.

The IMF depends only on the `PdgsClient` interface; adapting to an operational PDGS
(e.g. a PRIP/LTA or the Copernicus Data Space OData API) means changing the base URL,
product types and - if required - adding authentication here.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from ..timeutil import iso, parse_iso

log = logging.getLogger(__name__)


@dataclass
class ProductEntry:
    pdgs_id: str
    name: str
    product_type: str
    sensing_start: datetime
    sensing_stop: datetime
    publication_date: datetime
    size_bytes: int | None
    orbit: int | None
    attributes: dict = field(default_factory=dict)

    @property
    def baseline(self) -> str:
        return str(self.attributes.get("baseline", ""))


class PdgsError(RuntimeError):
    pass


class PdgsClient:
    def __init__(self, base_url: str, product_type: str | None = None, timeout_s: float = 30.0, retries: int = 3,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.product_type = product_type
        self.retries = retries
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, **kw) -> httpx.Response:
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                r = self._http.get(url, **kw)
                r.raise_for_status()
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise PdgsError(f"{exc.response.status_code} for {url}") from exc
                if attempt == self.retries:
                    raise PdgsError(f"PDGS request failed after {attempt} attempts: {exc}") from exc
                log.warning("PDGS request failed (%s), retry %d/%d in %.0fs", exc, attempt, self.retries, delay)
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def query_new(self, published_after: datetime | None, top: int = 100, product_type: str | None = None,
                  baseline: str | None = None) -> list[ProductEntry]:
        ptype = product_type or self.product_type
        if not ptype:
            raise ValueError("product type required")
        flt = f"ProductType eq '{ptype}'"
        if published_after is not None:
            flt += f" and PublicationDate gt {iso(published_after)}"
        if baseline:
            flt += (" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'baseline' and "
                    f"att/OData.CSC.StringAttribute/Value eq '{baseline}')")
        r = self._get(f"{self.base_url}/Products",
                      params={"$filter": flt, "$orderby": "PublicationDate asc", "$top": str(top),
                              "$expand": "Attributes"})
        out = []
        for v in r.json().get("value", []):
            attrs = {a["Name"]: a.get("Value") for a in v.get("Attributes", []) if "Name" in a}
            out.append(ProductEntry(
                pdgs_id=v["Id"], name=v["Name"], product_type=v.get("ProductType", ptype),
                sensing_start=parse_iso(v["ContentDate"]["Start"]),
                sensing_stop=parse_iso(v["ContentDate"]["End"]),
                publication_date=parse_iso(v["PublicationDate"]),
                size_bytes=v.get("ContentLength"), orbit=v.get("OrbitNumber", attrs.get("orbitNumber")),
                attributes=attrs,
            ))
        return out

    def download(self, entry: ProductEntry, target_dir: Path) -> tuple[Path, str]:
        """Download a product; returns (path, sha256)."""
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / entry.name
        r = self._get(f"{self.base_url}/Products({entry.pdgs_id})/$value")
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.replace(target)
        return target, hashlib.sha256(r.content).hexdigest()
