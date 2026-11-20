"""Fetch MA USGS topo quads from the TNM API and build the hub quad index.

Queries the USGS National Map TNM Access API for US Topo GeoPDFs
overlapping the MA bbox, dedupes to the newest edition per quad name,
and writes fixtures/usgs-quads-ma.json in the hub's format:
  bbox [west, south, east, north], file = local GeoPDF filename.
"""
import json
import re
import urllib.request
import urllib.parse

MA_BBOX = (-73.51, 41.23, -69.86, 42.89)  # west, south, east, north
API = "https://tnmaccess.nationalmap.gov/api/v1/products"

def fetch_all():
    items = []
    offset = 0
    step = 500
    while True:
        params = {
            "bbox": ",".join(str(v) for v in MA_BBOX),
            "datasets": "US Topo",
            "prodFormats": "GeoPDF",
            "max": step,
            "offset": offset,
        }
        url = API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        batch = data.get("items", [])
        items.extend(batch)
        total = data.get("total", 0)
        print(f"fetched {len(batch)} (offset {offset}, total {total})")
        offset += len(batch)
        if offset >= total or not batch:
            break
    return items

def parse_title(title):
    # "USGS US Topo 7.5-minute map for Amenia, NY-CT 2010"
    m = re.match(r"USGS US Topo 7\.5-minute map for (.+?),\s*([A-Z\-]+)\s+(\d{4})", title or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2), int(m.group(3))

def main():
    items = fetch_all()
    print(f"total items: {len(items)}")
    # Dedupe by quad name, keep newest publicationDate.
    best = {}
    for it in items:
        parsed = parse_title(it.get("title", ""))
        if not parsed:
            continue
        name, states, year = parsed
        bb = it.get("boundingBox") or {}
        try:
            west = float(bb["minX"]); east = float(bb["maxX"])
            south = float(bb["minY"]); north = float(bb["maxY"])
        except (KeyError, TypeError, ValueError):
            continue
        pub = it.get("publicationDate") or ""
        url = it.get("downloadURL") or ""
        fname = url.rsplit("/", 1)[-1] if url else ""
        size = it.get("sizeInBytes")
        cur = best.get(name)
        if cur is None or pub > cur["pub"]:
            best[name] = {
                "name": name, "states": states, "year": year, "pub": pub,
                "bbox": [west, south, east, north],
                "file": fname, "url": url, "size": size,
            }
    print(f"unique quad names: {len(best)}")
    # Keep quads whose bbox intersects the MA bbox (border quads included).
    w, s, e, n = MA_BBOX
    quads = []
    for q in sorted(best.values(), key=lambda x: x["name"]):
        bw, bs, be, bn = q["bbox"]
        if be < w or bw > e or bn < s or bs > n:
            continue
        quads.append(q)
    print(f"quads overlapping MA bbox: {len(quads)}")
    total_gb = sum(q["size"] or 0 for q in quads) / 1e9
    print(f"total GeoPDF size: {total_gb:.1f} GB")
    # Write the hub index (name/state/scale/bbox/file/note).
    out = {
        "_bbox_format": "[west, south, east, north] in decimal degrees, from the TNM API (tnmaccess.nationalmap.gov), US Topo GeoPDF, newest edition per quad",
        "_columns": ["name", "state", "scale", "bbox", "file", "note"],
        "_source": "USGS The National Map TNM Access API (tnmaccess.nationalmap.gov/api/v1/products) — US Topo, GeoPDF, 7.5-minute series, MA bbox (-73.51,41.23,-69.86,42.89), newest edition per quad name",
        "_download_urls": {q["name"]: q["url"] for q in quads},
        "quads": [
            {
                "name": q["name"],
                "state": "MA" if "MA" in q["states"] else q["states"],
                "scale": "1:24000",
                "bbox": q["bbox"],
                "file": q["file"],
                "note": f"{q['states']} {q['year']} edition. GeoPDF not yet downloaded to the laptop.",
            }
            for q in quads
        ],
    }
    with open("/home/hatch/workspace/goals/anonymous-laptop-setup/search-hub/fixtures/usgs-quads-ma.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print("wrote usgs-quads-ma.json")
    # Spot-check a few known quads.
    for nm in ("Braintree", "Boston South", "Boston North", "Weymouth", "Blue Hills"):
        q = next((x for x in quads if x["name"] == nm), None)
        print(f"  {nm}: {q['bbox'] if q else 'NOT FOUND'} file={q['file'] if q else '-'}")

if __name__ == "__main__":
    main()
