#!/usr/bin/env python3
"""Compile FAA airspace data into the files the Lucid controller downloads.

Sources (all public FAA UDDS / open-data ArcGIS services):
  - UAS Facility Map grid (LAANC ceilings, 30-arcsecond cells)
  - National Security UAS Flight Restrictions (full-time, part-time, pending)
  - Prohibited Areas

Outputs in dist/:
  - uasfm.bin            binary ceiling grid (format below)
  - restrictions.geojson restriction polygons, tagged by kind
  - manifest.json        version + per-file sha256/size, read by the app

uasfm.bin format ("LAIR" magic, little-endian):
  magic   4s  b"LAIR"
  version u8  1
  count   u32
  records count * (key u32, ceiling i16), sorted by key
  key = latIdx * 43201 + lonIdx, where
  latIdx = round((lat_cell_center + 90) * 120), lonIdx = round((lon + 180) * 120)
  ceiling is feet AGL; 0 means no LAANC authorization in that cell.

Live TFRs are deliberately NOT in this file: they change hourly, so the app
fetches https://tfr.faa.gov/tfrapi/exportTfrList directly when online.
"""

import gzip
import hashlib
import json
import struct
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

UA = {"User-Agent": "sherpa-airspace-compiler/1.0 (https://github.com/freyeslucidbots/sherpa-airspace)"}
ARCGIS = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"

UASFM_LAYER = f"{ARCGIS}/FAA_UAS_FacilityMap_Data/FeatureServer/0"
RESTRICTION_LAYERS = [
    ("nsufr", f"{ARCGIS}/DoD_Mar_13/FeatureServer/0"),
    ("nsufr_part_time", f"{ARCGIS}/Part_Time_National_Security_UAS_Flight_Restrictions/FeatureServer/0"),
    ("nsufr_pending", f"{ARCGIS}/UAS_NSR_Pending/FeatureServer/0"),
    ("prohibited", f"{ARCGIS}/Prohibited_Areas/FeatureServer/0"),
]

PAGE = 2000
RETRIES = 4


def fetch_json(url: str) -> dict:
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                data = json.load(r)
            # ArcGIS reports throttling and other failures as HTTP 200 with an
            # "error" body; treating that as end-of-pages once shipped a
            # silently truncated snapshot (208k of 380k cells). Fail, retry,
            # and ultimately raise so CI publishes nothing instead.
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as e:  # noqa: BLE001 - retry then re-raise
            last = e
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"gave up on {url}") from last


def query_pages(layer: str, params: dict):
    """Yield every feature of an ArcGIS layer, paginated with resultOffset."""
    offset = 0
    while True:
        q = dict(params, resultOffset=offset, resultRecordCount=PAGE, f="geojson")
        data = fetch_json(f"{layer}/query?{urllib.parse.urlencode(q)}")
        feats = data.get("features", [])
        if not feats:
            return
        yield from feats
        offset += len(feats)
        time.sleep(0.3)  # be polite; throttling is what truncates snapshots
        print(f"  {layer.rsplit('/', 2)[-2]}: {offset} features", flush=True)
        if not data.get("properties", {}).get("exceededTransferLimit") and len(feats) < PAGE:
            return


def cell_key(geometry: dict) -> int | None:
    """Grid key from the cell polygon's centroid (30-arcsecond cells)."""
    if not geometry or geometry.get("type") != "Polygon":
        return None
    ring = geometry["coordinates"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    lon = (min(lons) + max(lons)) / 2
    lat = (min(lats) + max(lats)) / 2
    lat_idx = round((lat + 90) * 120)
    lon_idx = round((lon + 180) * 120)
    if not (0 <= lat_idx <= 21600 and 0 <= lon_idx <= 43200):
        return None
    return lat_idx * 43201 + lon_idx


def build_uasfm(dist: Path) -> int:
    cells: dict[int, int] = {}
    for f in query_pages(UASFM_LAYER, {"where": "1=1", "outFields": "CEILING", "returnGeometry": "true", "outSR": "4326"}):
        key = cell_key(f.get("geometry"))
        if key is None:
            continue
        ceiling = f.get("properties", {}).get("CEILING")
        if ceiling is None:
            continue
        ceiling = int(ceiling)
        # Overlapping facility maps: keep the most restrictive ceiling.
        if key not in cells or ceiling < cells[key]:
            cells[key] = ceiling
    out = dist / "uasfm.bin"
    with out.open("wb") as fh:
        fh.write(struct.pack("<4sBI", b"LAIR", 1, len(cells)))
        for key in sorted(cells):
            fh.write(struct.pack("<Ih", key, cells[key]))
    print(f"uasfm.bin: {len(cells)} cells, {out.stat().st_size} bytes")
    return len(cells)


def simplify_ring(ring: list, tolerance: float = 0.0003) -> list:
    """Douglas-Peucker, iterative. ~30 m tolerance: advisory precision, and it
    takes the FAA's raw polygons (12-decimal coords, thousands of vertices,
    37 MB nationwide) down to something a 4 GB controller parses happily."""
    if len(ring) <= 4:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = ring[a][0], ring[a][1]
        bx, by = ring[b][0], ring[b][1]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
        worst, worst_d = -1, tolerance
        for i in range(a + 1, b):
            d = abs(dx * (ay - ring[i][1]) - (ax - ring[i][0]) * dy) / norm
            if d > worst_d:
                worst, worst_d = i, d
        if worst >= 0:
            keep[worst] = True
            stack.append((a, worst))
            stack.append((worst, b))
    out = [p for p, k in zip(ring, keep) if k]
    return out if len(out) >= 4 else ring


def slim_geometry(geometry: dict | None) -> dict | None:
    """Simplified outer rings only, coords quantized to 5 decimals (~1 m)."""
    if not geometry:
        return None
    def slim(ring):
        return [[round(x, 5), round(y, 5)] for x, y, *_ in simplify_ring(ring)]
    if geometry.get("type") == "Polygon":
        return {"type": "Polygon", "coordinates": [slim(geometry["coordinates"][0])]}
    if geometry.get("type") == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[slim(poly[0])] for poly in geometry["coordinates"] if poly]}
    return None


def build_restrictions(dist: Path) -> int:
    features = []
    for kind, layer in RESTRICTION_LAYERS:
        for f in query_pages(layer, {"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326"}):
            p = f.get("properties", {})
            geometry = slim_geometry(f.get("geometry"))
            if geometry is None:
                continue
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "kind": kind,
                    "name": p.get("Facility") or p.get("NAME") or p.get("Base") or "",
                    "state": p.get("State") or p.get("STATE") or "",
                    "floor": p.get("Floor") or p.get("LOWER_VAL") or "",
                    "ceiling": p.get("Ceiling") or p.get("UPPER_VAL") or "",
                    "info": p.get("Reason") or p.get("TYPE_CODE") or "",
                },
            })
    out = dist / "restrictions.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    print(f"restrictions.geojson: {len(features)} features, {out.stat().st_size} bytes")
    return len(features)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    dist = Path(__file__).parent / "dist"
    dist.mkdir(exist_ok=True)
    cells = build_uasfm(dist)
    polys = build_restrictions(dist)
    # A snapshot far below the known baseline (~380k cells, ~2.3k polygons,
    # Oct 2026) means a truncated pull, not a changed national airspace.
    if cells < 300_000 or polys < 1_000:
        print(f"SANITY FAIL: {cells} cells / {polys} polygons - refusing to publish")
        return 1
    # Compress the big grid for the download; the app gunzips on device.
    raw = (dist / "uasfm.bin").read_bytes()
    (dist / "uasfm.bin.gz").write_bytes(gzip.compress(raw, 9))

    version = date.today().isoformat()
    files = {}
    for name in ("uasfm.bin.gz", "restrictions.geojson"):
        p = dist / name
        files[name] = {"sha256": sha256(p), "bytes": p.stat().st_size}
    (dist / "manifest.json").write_text(json.dumps({
        "version": version,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cells": cells,
        "restrictions": polys,
        "files": files,
        "sources": [UASFM_LAYER] + [u for _, u in RESTRICTION_LAYERS],
        "disclaimer": "Compiled from public FAA UDDS data; advisory only, not real time. TFRs are fetched live by the app.",
    }, indent=2))
    print(f"manifest version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
