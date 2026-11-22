"""Build an offline road-routing index (SQLite) from OSM PBF extract(s).

Reads the road network out of one or more .osm.pbf files (a Geofabrik
state extract, or the full US file with --bbox) and writes the
routing DB the hub's routing.py reads:

  meta(key, value)   -- coverage, source files, built date, counts
  nodes(id, lat, lon)
  edges(a, b, name, length_m, time_s)  -- directed; two-way roads get
                                          both directions

Usage:
  python build-routing-index.py massachusetts-latest.osm.pbf routing.db
  python build-routing-index.py ma.osm.pbf ri.osm.pbf ct.osm.pbf routing.db
  python build-routing-index.py us-latest.osm.pbf routing.db --bbox -73.8,41.0,-69.8,43.2

Notes:
  - Needs pyosmium (already on the laptop: osmium 4.3.2 via pip).
  - Two passes per input file: pass 1 stores node coordinates in
    SQLite, pass 2 resolves highway ways against them. A way is kept
    when at least two consecutive nodes resolve; gaps (a node missing
    from the extract) split the way instead of guessing a segment.
  - Drivable roads only (motorway down to service/track, plus *_link);
    footways, cycleways, paths, and pedestrian areas are skipped, as
    are access=no/private and motor_vehicle=no ways. One-way tags and
    roundabouts are honoured (one directed edge instead of two).
  - Road names fall back to the route ref (I-90, MA 9) when a way has
    no name. Travel time uses the maxspeed tag when present, else a
    default per road class.
  - Safe to re-run: the output DB is rebuilt from scratch each time.
    Progress is printed to stdout; run it detached on the laptop
    (a state extract takes minutes; the full US file much longer).
"""

import argparse
import math
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osmium  # noqa: E402  (pyosmium; installed on the laptop)

from routing import haversine_m  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodexy (
    id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edges_raw (
    a INTEGER NOT NULL, b INTEGER NOT NULL, name TEXT NOT NULL,
    length_m REAL NOT NULL, time_s REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edges (
    a INTEGER NOT NULL, b INTEGER NOT NULL, name TEXT NOT NULL,
    length_m REAL NOT NULL, time_s REAL NOT NULL,
    PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS edges_a ON edges (a);
"""

# Drivable road classes, with the default speed (km/h) used for travel
# time when the way carries no maxspeed tag.
SPEEDS_KMH = {
    "motorway": 105, "motorway_link": 60,
    "trunk": 90, "trunk_link": 60,
    "primary": 80, "primary_link": 50,
    "secondary": 70, "secondary_link": 50,
    "tertiary": 60, "tertiary_link": 40,
    "unclassified": 50, "residential": 40,
    "living_street": 20, "service": 30,
    "road": 40, "track": 20,
}


def parse_maxspeed(tag):
    """A maxspeed tag value in km/h, or None when absent/unparseable.

    "30 mph" converts to km/h; a bare number is km/h per the OSM spec.
    """
    if not tag:
        return None
    text = tag.strip().lower()
    try:
        if text.endswith("mph"):
            return float(text[:-3].strip()) * 1.60934
        return float(text.split()[0])
    except (TypeError, ValueError):
        return None


class NodeHandler(osmium.SimpleHandler):
    """Pass 1: store every node location (optionally inside a bbox)."""

    def __init__(self, conn, bbox=None):
        super().__init__()
        self.conn = conn
        self.bbox = bbox  # (west, south, east, north) or None
        self.batch = []
        self.count = 0
        self.started = time.time()

    def node(self, n):
        loc = n.location
        if not loc.valid():
            return
        lat, lon = loc.lat, loc.lon
        if self.bbox is not None:
            west, south, east, north = self.bbox
            if not (west <= lon <= east and south <= lat <= north):
                return
        self.batch.append((n.id, lat, lon))
        if len(self.batch) >= 50000:
            self._flush()

    def _flush(self):
        if not self.batch:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO nodexy (id, lat, lon) VALUES (?, ?, ?)",
            self.batch)
        self.count += len(self.batch)
        self.batch = []
        if self.count % 1000000 < 50000:
            print(f"  nodes: {self.count:,} "
                  f"({time.time() - self.started:.0f}s)", flush=True)

    def done(self):
        self._flush()
        self.conn.commit()


class WayHandler(osmium.SimpleHandler):
    """Pass 2: resolve highway ways against nodexy, emit directed edges."""

    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.batch = []
        self.ways_seen = 0
        self.ways_kept = 0
        self.edges = 0
        self.started = time.time()

    def way(self, w):
        highway = w.tags.get("highway")
        if highway not in SPEEDS_KMH:
            return
        if w.tags.get("access") in ("no", "private"):
            return
        if w.tags.get("motor_vehicle") == "no":
            return
        refs = [n.ref for n in w.nodes]
        if len(refs) < 2:
            return
        self.ways_seen += 1
        name = (w.tags.get("name") or "").strip()
        if not name:
            name = (w.tags.get("ref") or "").strip().split(";")[0].strip()
        speed = parse_maxspeed(w.tags.get("maxspeed"))
        if speed is None or not 5 <= speed <= 130:
            speed = SPEEDS_KMH[highway]
        speed_mps = speed * 1000.0 / 3600.0
        oneway = (w.tags.get("oneway") or "").strip().lower()
        junction = (w.tags.get("junction") or "").strip().lower()
        forward = oneway != "-1"
        backward = oneway not in ("yes", "true", "1") and junction != "roundabout"
        # One batched lookup per way for its node coordinates.
        placeholders = ",".join("?" for _ in refs)
        rows = self.conn.execute(
            f"SELECT id, lat, lon FROM nodexy WHERE id IN ({placeholders})",
            refs).fetchall()
        coords = {row[0]: (row[1], row[2]) for row in rows}
        # Runs of consecutive resolved nodes; a gap splits the way.
        run = []
        kept = False

        def flush_run():
            nonlocal kept
            if len(run) < 2:
                return
            kept = True
            for (a, (alat, alon)), (b, (blat, blon)) in zip(run, run[1:]):
                length = haversine_m(alat, alon, blat, blon)
                if length <= 0:
                    continue
                time_s = length / speed_mps
                if forward:
                    self.batch.append((a, b, name, length, time_s))
                if backward:
                    self.batch.append((b, a, name, length, time_s))
                if len(self.batch) >= 50000:
                    self._flush()

        for ref in refs:
            coord = coords.get(ref)
            if coord is None:
                flush_run()
                run = []
            else:
                run.append((ref, coord))
        flush_run()
        if kept:
            self.ways_kept += 1
        if self.ways_seen % 100000 == 0:
            print(f"  ways: {self.ways_seen:,} seen, {self.ways_kept:,} kept, "
                  f"{self.edges:,} edges ({time.time() - self.started:.0f}s)",
                  flush=True)

    def _flush(self):
        if not self.batch:
            return
        self.conn.executemany(
            "INSERT INTO edges_raw (a, b, name, length_m, time_s)"
            " VALUES (?, ?, ?, ?, ?)", self.batch)
        self.edges += len(self.batch)
        self.batch = []

    def done(self):
        self._flush()
        self.conn.commit()


def build(inputs, output, bbox=None, coverage=""):
    if os.path.exists(output):
        os.remove(output)
    conn = sqlite3.connect(output)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(SCHEMA)
        for path in inputs:
            print(f"pass 1 (nodes): {path}", flush=True)
            handler = NodeHandler(conn, bbox=bbox)
            handler.apply_file(path, locations=False)
            handler.done()
            print(f"  stored {handler.count:,} node locations", flush=True)
            print(f"pass 2 (ways): {path}", flush=True)
            whandler = WayHandler(conn)
            whandler.apply_file(path, locations=False)
            whandler.done()
            print(f"  ways: {whandler.ways_seen:,} seen, "
                  f"{whandler.ways_kept:,} kept, {whandler.edges:,} edges",
                  flush=True)
        print("collapsing parallel edges (named ways win)...", flush=True)
        # Parallel edges (same node pair, different ways): keep the
        # named one, then the shortest — INSERT OR IGNORE takes the
        # first row per (a, b), and the ORDER BY fixes which is first.
        conn.execute(
            "INSERT OR IGNORE INTO edges (a, b, name, length_m, time_s)"
            " SELECT a, b, name, length_m, time_s FROM edges_raw"
            " ORDER BY (name <> '') DESC, length_m ASC")
        conn.execute("DROP TABLE edges_raw")
        print("keeping only nodes on roads...", flush=True)
        conn.execute(
            "INSERT INTO nodes (id, lat, lon)"
            " SELECT id, lat, lon FROM nodexy WHERE id IN"
            " (SELECT a FROM edges UNION SELECT b FROM edges)")
        conn.execute("DROP TABLE nodexy")
        counts = {}
        for table in ("nodes", "edges"):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.execute("ANALYZE")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("coverage", coverage or ", ".join(
                os.path.basename(p) for p in inputs)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("sources", "; ".join(inputs)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("built", time.strftime("%Y-%m-%d")))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("nodes", str(counts["nodes"])))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("edges", str(counts["edges"])))
        conn.commit()
        print(f"done: {counts['nodes']:,} nodes, {counts['edges']:,} "
              f"directed edges -> {output}", flush=True)
        return counts
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build an offline road-routing index from OSM PBF extract(s).")
    parser.add_argument("inputs", nargs="+",
                        help="input .osm.pbf file(s), then the output .db")
    parser.add_argument("--bbox", default="",
                        help="west,south,east,north — keep nodes inside "
                             "this box only (for a full-US input)")
    parser.add_argument("--coverage", default="",
                        help="coverage label stored in the index "
                             "(e.g. 'Massachusetts, Rhode Island, Connecticut')")
    args = parser.parse_args(argv)
    if len(args.inputs) < 2:
        parser.error("need at least one input PBF and an output .db")
    *inputs, output = args.inputs
    bbox = None
    if args.bbox:
        try:
            west, south, east, north = (float(v) for v in args.bbox.split(","))
        except ValueError:
            parser.error("--bbox must be west,south,east,north")
        if not (west < east and south < north):
            parser.error("--bbox must be west,south,east,north with west<east, south<north")
        bbox = (west, south, east, north)
    for path in inputs:
        if not os.path.isfile(path):
            parser.error(f"input not found: {path}")
    build(inputs, output, bbox=bbox, coverage=args.coverage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
