# Build ATAK MBTiles exports from the USGS topo tiles already on the
# laptop (C:\Library\Maps\topo\{z}\{x}\{y}.jpg, same tiles the hub's Topo
# style serves). Runs on the laptop; launched detached via CIM after the
# z13-15 download finishes, so it keeps going after SSH closes.
#
# Why this instead of re-baking T.REX tiles: the original Braintree
# export was baked in a browser on maps.trex-arms.com (WebGL render of
# T.REX's own styled tiles) in the owner's Chrome on 2026-11-19; the cloud
# browser failed the same bake 3x on GPU/WebGL2 memory, and the T.REX
# tiles are not available offline. This script packages the offline
# USGS topo tiles the hub already owns into the same MBTiles format
# ATAK reads (SQLite, TMS row order, JPEG tiles), so the new exports
# are real ATAK files servable by the same hub route.
#
# Output (C:\Library\Maps\trex-arms\, the dir the hub scans for *.mbtiles):
#   usgs-massachusetts-z12-15.mbtiles  MA + RI / S NH / N CT buffer, z12-15
#   usgs-new-england-z10-12.mbtiles    New England bbox, z10-12 (on-disk only)
# The existing T.REX Braintree file is left untouched.
#
# Log: C:\Library\Maps\atak-build.log (append). File-only logging:
# NO print() - a CIM-detached process has no usable stdout.

import math
import os
import sqlite3
import sys
import time

TOPO_DIR = r"C:\Library\Maps\topo"
OUT_DIR = r"C:\Library\Maps\trex-arms"
LOG_PATH = r"C:\Library\Maps\atak-build.log"

# (filename stem, description, (lon_min, lon_max, lat_min, lat_max), zooms)
LAYERS = [
    ("usgs-massachusetts-z12-15",
     "Massachusetts + RI / S NH / N CT buffer (USGS topo), z12-15",
     (-73.55, -69.85, 41.15, 42.95), (12, 13, 14, 15)),
    ("usgs-new-england-z10-12",
     "New England (USGS topo), z10-12",
     (-73.73, -66.95, 40.98, 47.47), (10, 11, 12)),
    ("usgs-new-york-z12-15",
     "New York (USGS topo), z12-15",
     (-79.86, -71.76, 40.40, 45.12), (12, 13, 14, 15)),
    ("usgs-new-jersey-z12-15",
     "New Jersey (USGS topo), z12-15",
     (-75.66, -73.80, 38.83, 41.46), (12, 13, 14, 15)),
    ("usgs-pennsylvania-z12-15",
     "Pennsylvania (USGS topo), z12-15",
     (-80.62, -74.59, 39.62, 42.37), (12, 13, 14, 15)),
]


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def x_of(lon, z):
    return int((lon + 180.0) / 360.0 * (2 ** z))


def y_of(lat, z):
    lr = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(lr)) / math.pi) / 2.0 * (2 ** z))


def build_layer(stem, description, bbox, zooms):
    lon_min, lon_max, lat_min, lat_max = bbox
    out_path = os.path.join(OUT_DIR, stem + ".mbtiles")
    tmp_path = out_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,"
            " tile_row INTEGER, tile_data BLOB,"
            " PRIMARY KEY (zoom_level, tile_column, tile_row))")
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        total = 0
        per_zoom = {}
        for z in zooms:
            n = 2 ** z
            xmin, xmax = x_of(lon_min, z), x_of(lon_max, z)
            ymin = max(0, min(y_of(lat_max, z), n - 1))
            ymax = max(0, min(y_of(lat_min, z), n - 1))
            rows = []
            count = 0
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    path = os.path.join(TOPO_DIR, str(z), str(x), str(y) + ".jpg")
                    try:
                        with open(path, "rb") as fh:
                            data = fh.read()
                    except OSError:
                        continue
                    if len(data) < 2 or data[0] != 0xFF or data[1] != 0xD8:
                        continue
                    tms_y = (n - 1) - y
                    rows.append((z, x, tms_y, data))
                    if len(rows) >= 500:
                        conn.executemany(
                            "INSERT OR REPLACE INTO tiles"
                            " (zoom_level, tile_column, tile_row, tile_data)"
                            " VALUES (?, ?, ?, ?)", rows)
                        rows = []
                    count += 1
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO tiles"
                    " (zoom_level, tile_column, tile_row, tile_data)"
                    " VALUES (?, ?, ?, ?)", rows)
            per_zoom[z] = count
            total += count
            log("%s z%d: %d tiles" % (stem, z, count))
        center_lon = (lon_min + lon_max) / 2.0
        center_lat = (lat_min + lat_max) / 2.0
        center_zoom = min(zooms) + 2
        meta = [
            ("name", stem),
            ("type", "baselayer"),
            ("version", "1"),
            ("format", "jpg"),
            ("description", description),
            ("bounds", "%.6f,%.6f,%.6f,%.6f" % (lon_min, lat_min, lon_max, lat_max)),
            ("center", "%.6f,%.6f,%d" % (center_lon, center_lat, center_zoom)),
            ("minzoom", str(min(zooms))),
            ("maxzoom", str(max(zooms))),
            ("attribution", "USGS"),
        ]
        conn.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", meta)
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS tiles_idx"
                     " ON tiles (zoom_level, tile_column, tile_row)")
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, out_path)
    size = os.path.getsize(out_path)
    log("%s DONE tiles=%d per_zoom=%s bytes=%d (%.1f MB)" % (
        stem, total, per_zoom, size, size / 1e6))
    return total, size


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # Optional argv[1]: build only layers whose stem contains one of
    # the comma-separated substrings (e.g. "new-york,new-jersey").
    # No argument builds all layers.
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    layers = [layer for layer in LAYERS
              if only is None or any(s in layer[0] for s in only)]
    log("START layers=%d%s" % (len(layers), (" filter=" + ",".join(only)) if only else ""))
    for stem, description, bbox, zooms in layers:
        try:
            build_layer(stem, description, bbox, zooms)
        except Exception:
            import traceback
            log("%s FAILED" % stem)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
    log("DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " FATAL\n")
                fh.write(traceback.format_exc())
        except Exception:
            pass
        raise
