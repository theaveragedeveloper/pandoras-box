"""Build an offline address index from an OpenStreetMap PBF extract.

Reads nodes and ways carrying addr:housenumber + addr:street and writes
them into a SQLite database with an FTS5 search table (see geocode.py).

Usage:
  python build-address-index.py <input.osm.pbf> <output.db> [--limit N]
      [--default-state MA]

Notes:
  - Way addresses need a node-location store to compute a centroid; this
    uses pyosmium's disk-based sparse file array, so the build works on a
    full-country extract without holding all node locations in RAM. The
    store's temp files land in the current working directory: run from the
    directory that should hold them (e.g. C:\\Library\\geocode).
  - Dedup is on (housenumber, street_norm, city, state): first occurrence wins.
    A node and a building way for the same address collapse to one row.
  - Progress is printed to stdout; the process is safe to re-run (the
    output DB is rebuilt from scratch each time).
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osmium

from geocode import normalize_street, normalize_token
from sqlite_files import rebuild_index

SCHEMA = """
CREATE TABLE IF NOT EXISTS addresses (
    housenumber TEXT NOT NULL,
    street_norm TEXT NOT NULL,
    street_raw TEXT NOT NULL,
    city TEXT NOT NULL,
    postcode TEXT NOT NULL,
    state TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    display TEXT NOT NULL,
    UNIQUE (housenumber, street_norm, city, state)
);
CREATE VIRTUAL TABLE IF NOT EXISTS addresses_fts USING fts5(
    housenumber, street_norm, city, postcode, state,
    content='addresses', content_rowid='rowid', tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class AddressLimitReached(Exception):
    """Stop the scan when a test build reaches its address limit."""


class AddressHandler(osmium.SimpleHandler):
    def __init__(self, conn, default_state="", nodes_only=False, limit=0):
        super().__init__()
        self.conn = conn
        self.default_state = default_state
        self.nodes_only = nodes_only
        self.limit = limit
        self.seen_objects = 0
        self.found = 0
        self.inserted = 0
        self.batch = []
        self.started = time.time()

    def _record(self, tags, lat, lon):
        housenumber = (tags.get("addr:housenumber") or "").strip()
        street_raw = (tags.get("addr:street") or "").strip()
        if not housenumber or not street_raw or lat is None or lon is None:
            return
        street_norm = normalize_street(street_raw)
        if not street_norm:
            return
        city = normalize_token(
            tags.get("addr:city") or tags.get("addr:town")
            or tags.get("addr:village") or tags.get("addr:hamlet") or ""
        )
        postcode = (tags.get("addr:postcode") or "").strip()
        state = (tags.get("addr:state") or self.default_state).strip().upper()
        parts = [housenumber + " " + street_raw]
        if city:
            parts.append(city.title())
        if state:
            parts.append(state)
        if postcode:
            parts.append(postcode)
        display = ", ".join(parts)
        self.batch.append((
            housenumber.lower(), street_norm, street_raw, city,
            postcode, state, lat, lon, display,
        ))
        self.found += 1
        if len(self.batch) >= 50000:
            self._flush()
        if self.limit and self.found >= self.limit:
            raise AddressLimitReached

    def _flush(self):
        if not self.batch:
            return
        cur = self.conn.cursor()
        for row in self.batch:
            cur.execute(
                "INSERT OR IGNORE INTO addresses"
                " (housenumber, street_norm, street_raw, city, postcode,"
                "  state, lat, lon, display) VALUES (?,?,?,?,?,?,?,?,?)",
                row,
            )
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO addresses_fts(rowid, housenumber,"
                    " street_norm, city, postcode, state)"
                    " VALUES (?,?,?,?,?,?)",
                    (cur.lastrowid, row[0], row[1], row[3], row[4], row[5]),
                )
                self.inserted += 1
        self.conn.commit()
        self.batch = []

    def _progress(self):
        elapsed = time.time() - self.started
        print(
            "  ... %d objects, %d addresses found, %d indexed, %.0fs"
            % (self.seen_objects, self.found, self.inserted, elapsed),
            flush=True,
        )

    def node(self, n):
        self.seen_objects += 1
        if self.seen_objects % 1000000 == 0:
            self._progress()
        if "addr:housenumber" in n.tags and "addr:street" in n.tags:
            loc = n.location
            if loc.valid():
                self._record(n.tags, loc.lat, loc.lon)

    def way(self, w):
        if self.nodes_only:
            return
        self.seen_objects += 1
        if self.seen_objects % 1000000 == 0:
            self._progress()
        if "addr:housenumber" not in w.tags or "addr:street" not in w.tags:
            return
        lats, lons = [], []
        for nd in w.nodes:
            if not nd.location.valid():
                return
            lats.append(nd.location.lat)
            lons.append(nd.location.lon)
        if not lats:
            return
        self._record(w.tags, sum(lats) / len(lats), sum(lons) / len(lons))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("db")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N address objects (testing only)")
    ap.add_argument("--default-state", default="")
    ap.add_argument("--nodes-only", action="store_true",
                    help="index node addresses only (no way centroids,"
                    " no node-location store; use when the temp disk"
                    " for the location store is too small)")
    args = ap.parse_args()

    if args.limit < 0:
        ap.error("--limit must be zero or greater")
    with rebuild_index(args.db) as conn:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.executescript(SCHEMA)
        handler = AddressHandler(conn, args.default_state, args.nodes_only, args.limit)
        print("Reading %s ..." % args.pbf, flush=True)
        try:
            if args.nodes_only:
                handler.apply_file(args.pbf, locations=False)
            else:
                handler.apply_file(args.pbf, locations=True, idx="sparse_file_array")
        except AddressLimitReached:
            pass
        handler._flush()

        conn.execute("CREATE INDEX IF NOT EXISTS idx_addresses_city ON addresses(city)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('source', ?),"
            " ('built', ?), ('addresses', ?)",
            (os.path.basename(args.pbf),
             time.strftime("%Y-%m-%d %H:%M:%S"),
             str(handler.inserted)),
        )
    print(
        "Done: %d objects scanned, %d addresses found, %d indexed in %.0fs"
        % (handler.seen_objects, handler.found, handler.inserted,
           time.time() - handler.started),
        flush=True,
    )


if __name__ == "__main__":
    main()
