# Download USGS topo raster tiles for the rest of the Northeast
# (New York, New Jersey, Pennsylvania), zooms 12-15, for Pandora's
# Box Mk1 ATAK MBTiles exports. Runs on the laptop; launched detached
# via CIM so it keeps going after the SSH session closes. Adapted from
# download-ma-topo-z13-15.py (same tile service, same resume-safe
# pattern); only the bboxes / zooms / log differ.
# Tiles: https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}
# Output: C:\Library\Maps\topo\{z}\{x}\{y}.jpg  (same tree the Topo style reads)
# Log:    C:\Library\Maps\topo-northeast-download.log (append)
# Skips tiles that already exist (resume-safe). Writes via a temp file
# then renames, so an existing file is always complete. Stops on 429/403.
# Expected volume: ~891k tiles attempted (upper bound; ocean tiles 404
# and are not saved), ~17 GB worst case at the MA layer's ~19 KB/tile.
# Source: USGS topo, public domain, free, bulk-downloadable.
# Attribution: USGS (already in the MBTiles metadata).

import concurrent.futures
import math
import os
import threading
import time
import urllib.error
import urllib.request

BASE_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
OUT_DIR = r"C:\Library\Maps\topo"
LOG_PATH = r"C:\Library\Maps\topo-northeast-download.log"
# (name, lon_min, lon_max, lat_min, lat_max) — state bounds + 0.1 deg buffer.
BBOXES = [
    ("NY", -79.86, -71.76, 40.40, 45.12),
    ("NJ", -75.66, -73.80, 38.83, 41.46),
    ("PA", -80.62, -74.59, 39.62, 42.37),
]
ZOOMS = (12, 13, 14, 15)
WORKERS = 16
RETRIES = 3
TIMEOUT = 20
PROGRESS_EVERY = 5000

STOP = threading.Event()
COUNTS = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
COUNT_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


def log(msg):
    # File only: NO print(). A CIM-detached process has no usable
    # stdout, and a print() there raises OSError and kills the script.
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    with LOG_LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def x_of(lon, z):
    n = 2 ** z
    return int((lon + 180.0) / 360.0 * n)


def y_of(lat, z):
    n = 2 ** z
    lr = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(lr)) / math.pi) / 2.0 * n)


def tile_list():
    tiles = []
    for name, lon_min, lon_max, lat_min, lat_max in BBOXES:
        for z in ZOOMS:
            n = 2 ** z
            xmin, xmax = x_of(lon_min, z), x_of(lon_max, z)
            ymin = max(0, min(y_of(lat_max, z), n - 1))
            ymax = max(0, min(y_of(lat_min, z), n - 1))
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    tiles.append((z, x, y))
    return tiles


def fetch_tile(z, x, y):
    """Download one tile. Returns (status, nbytes): status is
    'ok', 'skip', 'fail', or 'blocked' (429/403 -> stop everything)."""
    path = os.path.join(OUT_DIR, str(z), str(x), str(y) + ".jpg")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "skip", 0
    url = BASE_URL.format(z=z, y=y, x=x)
    req = urllib.request.Request(url, headers={"User-Agent": "PandoraBox-Mk1/1.0"})
    last_err = None
    for attempt in range(1, RETRIES + 1):
        if STOP.is_set():
            return "fail", 0
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            if len(data) < 2 or data[0] != 0xFF or data[1] != 0xD8:
                last_err = "not a JPEG (%d bytes)" % len(data)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            return "ok", len(data)
        except urllib.error.HTTPError as e:
            if e.code in (429, 403):
                return "blocked", 0
            if e.code == 404:
                # No tile here (ocean / outside coverage): fail fast,
                # no retries — a 404 is not transient, and the bbox
                # holds thousands of ocean tiles at z14-15.
                last_err = "HTTP 404"
                break
            last_err = "HTTP %s" % e.code
        except Exception as e:  # noqa: BLE001 - network is flaky; retry
            last_err = str(e)[:120]
        time.sleep(1)
    log("FAIL z=%d x=%d y=%d (%s)" % (z, x, y, last_err))
    return "fail", 0


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    tiles = tile_list()
    total = len(tiles)
    log("START total_tiles=%d zooms=12-15 bboxes=%s workers=%d" % (
        total, ",".join(b[0] for b in BBOXES), WORKERS))
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_tile, z, x, y): (z, x, y) for z, x, y in tiles}
        for fut in concurrent.futures.as_completed(futures):
            try:
                status, nbytes = fut.result()
            except Exception as e:  # noqa: BLE001 - a worker should not kill the run
                status, nbytes = "fail", 0
                log("WORKER-ERROR %s" % str(e)[:120])
            if status == "blocked":
                STOP.set()
                log("BLOCKED (429/403 from tile service) - stopping")
                for f in futures:
                    f.cancel()
                break
            with COUNT_LOCK:
                if status == "ok":
                    COUNTS["downloaded"] += 1
                    COUNTS["bytes"] += nbytes
                elif status == "skip":
                    COUNTS["skipped"] += 1
                else:
                    COUNTS["failed"] += 1
                done += 1
                if done % PROGRESS_EVERY == 0:
                    log("PROGRESS done=%d/%d downloaded=%d skipped=%d failed=%d MB=%.1f" % (
                        done, total, COUNTS["downloaded"], COUNTS["skipped"],
                        COUNTS["failed"], COUNTS["bytes"] / 1e6))
    mb = COUNTS["bytes"] / 1e6
    if STOP.is_set():
        log("STOPPED downloaded=%d skipped=%d failed=%d MB=%.1f" % (
            COUNTS["downloaded"], COUNTS["skipped"], COUNTS["failed"], mb))
    else:
        log("DONE total=%d downloaded=%d skipped=%d failed=%d MB=%.1f" % (
            total, COUNTS["downloaded"], COUNTS["skipped"], COUNTS["failed"], mb))


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
