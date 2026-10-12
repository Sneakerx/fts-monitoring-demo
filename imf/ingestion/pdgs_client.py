"""Client for an OData-style PDGS product catalogue.

The IMF depends only on the `PdgsClient` interface; adapting to an operational PDGS
(e.g. a PRIP/LTA or the Copernicus Data Space OData API) means changing the base URL,
product type and - if required - adding authentication here.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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


class PdgsError(RuntimeError):
    pass


class PdgsClient:
    def __init__(self, base_url: str, product_type: str, timeout_s: float = 30.0, retries: int = 3,
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

    def query_new(self, published_after: datetime | None, top: int = 100) -> list[ProductEntry]:
        flt = f"ProductType eq '{self.product_type}'"
        if published_after is not None:
            flt += f" and PublicationDate gt {iso(published_after)}"
        r = self._get(f"{self.base_url}/Products",
                      params={"$filter": flt, "$orderby": "PublicationDate asc", "$top": str(top)})
        out = []
        for v in r.json().get("value", []):
            out.append(ProductEntry(
                pdgs_id=v["Id"], name=v["Name"], product_type=v.get("ProductType", self.product_type),
                sensing_start=parse_iso(v["ContentDate"]["Start"]),
                sensing_stop=parse_iso(v["ContentDate"]["End"]),
                publication_date=parse_iso(v["PublicationDate"]),
                size_bytes=v.get("ContentLength"), orbit=v.get("OrbitNumber"),
            ))
        return out

    def download(self, entry: ProductEntry, target_dir: Path) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / entry.name
        r = self._get(f"{self.base_url}/Products({entry.pdgs_id})/$value")
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.replace(target)
        return target
