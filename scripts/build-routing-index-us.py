"""Build a CONTRACTED offline road-routing index (SQLite) from OSM PBF extract(s).

A US-wide successor to build-routing-index.py. The original stores every
road shape node (~9M nodes / ~17.8M directed edges for MA+RI+CT alone);
the full US file would be ~80M nodes / ~200M directed edges, far too big
to load into the hub's in-memory Python graph and far too slow to route
over. This builder contracts degree-2 shape nodes out of the graph --
chains are followed ACROSS way boundaries, so a long road that OSM
splits into many short ways still becomes a handful of edges:

  * pass 1 scans the drivable ways once (node locations resolved from
    a disk-backed location store, so no separate node pass over the
    ~1.5B-node US file is needed) and records every directed segment
    (a, b, name, speed, length, endpoint coords) in a temp table;
  * a SQL step finds the junction nodes: a node is a junction unless it
    has exactly two distinct neighbours carrying a single road name
    (undirected degree 2, one name). Everything else -- intersections,
    dead ends, road-name changes, one-way merges -- stays a graph node;
  * the segments are streamed out in `a` order into sorted in-memory
    arrays (a, b, name, speed, length, end coords), so a chain walk
    can follow a road by binary search instead of random database
    lookups -- 250M random SQLite probes would take hours, the sorted
    arrays walk in minutes;
  * the walk starts one chain at every junction for each of its
    outgoing segments, follows the single continuation through each
    contractible node (the neighbour that is not the one it came
    from), splits at junctions and at sharp bends (>15 deg cumulative
    heading change, so long curves keep their shape), and emits one
    contracted edge per chain with summed length/time. Chain endpoints
    become the graph's nodes;
  * the finish collapses parallel edges (fastest wins), drops self-loops,
    computes connected components (union-find, so the router can reject
    a disconnected pair instantly instead of searching the whole graph),
    and vacuums the temp tables away.

Output schema (what routing.py reads):
  meta(key, value)   -- coverage, source files, built date, counts, phases
  nodes(id, lat, lon)
  edges(a, b, name, length_m, time_s)  -- directed; PRIMARY KEY (a, b)
  nodecomp(id, comp) -- connected-component id per node (undirected)

Usage:
  python build-routing-index-us.py us-latest.osm.pbf routing-us.db
      --coverage "United States (CONUS, Alaska, Hawaii)"
  python build-routing-index-us.py massachusetts-latest.osm.pbf test.db
      --coverage "Massachusetts (contracted test)"

Notes:
  - Needs pyosmium (already on the laptop: osmium 4.3.2 via pip).
  - Same road filter as build-routing-index.py: drivable roads only
    (motorway down to service/track, plus *_link); footways, cycleways,
    paths, pedestrian areas skipped, as are access=no/private and
    motor_vehicle=no ways. One-way tags and roundabouts are honoured.
  - Road names fall back to the route ref (I-90, MA 9) when a way has
    no name. Travel time uses the maxspeed tag when present, else a
    default per road class.
  - Safe to re-run: the output DB is rebuilt from scratch each time.
    Progress is printed to stdout; run it detached on the laptop
    (the full US file takes hours).
  - Segment length uses haversine, exactly like build-routing-index.py,
    so distances match the uncontracted index. In-memory coordinates
    are float32 (~1 m precision, which is all routing, snapping, and
    geometry need); the stored node table carries the same ~1 m
    coordinates, since chain endpoints come from the walk.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from array import array
from bisect import bisect_left, bisect_right

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osmium  # noqa: E402  (pyosmium; installed on the laptop)

from routing import haversine_m, ordinal_turns  # noqa: E402
from routing_build import TurnRestrictions, build_routing_file  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS seg (
    a INTEGER NOT NULL, b INTEGER NOT NULL, n INTEGER NOT NULL,
    s REAL NOT NULL, len REAL NOT NULL,
    alat REAL NOT NULL, alon REAL NOT NULL,
    blat REAL NOT NULL, blon REAL NOT NULL);
CREATE TABLE IF NOT EXISTS names (
    idx INTEGER PRIMARY KEY, name TEXT NOT NULL);
-- junction is created by compute_junctions (CREATE TABLE ... AS SELECT),
-- not here: creating it empty first would collide with that statement.
CREATE TABLE IF NOT EXISTS edges_raw (
    a INTEGER NOT NULL, b INTEGER NOT NULL, name TEXT NOT NULL,
    length_m REAL NOT NULL, time_s REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edges (
    a INTEGER NOT NULL, b INTEGER NOT NULL, name TEXT NOT NULL,
    length_m REAL NOT NULL, time_s REAL NOT NULL,
    PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodecomp (
    id INTEGER PRIMARY KEY, comp INTEGER NOT NULL);
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

# A chain is split when its heading has turned this far from the
# heading of its first segment, so a contracted edge never cuts a
# bend by more than about this angle.
BEND_DEGREES = 15.0


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


def _drivable_way(w, excluded=frozenset()):
    """(name, speed_mps, forward, backward) for a drivable way, else None.

    `excluded` is a set of highway classes to leave out of the index
    (e.g. {"service"}: driveways and parking aisles carry no through
    traffic but are most of the ways in the file, so excluding them
    cuts the graph by about a third; routes to them snap to the
    nearest through road instead)."""
    highway = w.tags.get("highway")
    if highway not in SPEEDS_KMH or highway in excluded:
        return None
    if w.tags.get("access") in ("no", "private"):
        return None
    if w.tags.get("motor_vehicle") == "no":
        return None
    if (w.tags.get("area") or "").strip().lower() == "yes":
        return None
    name = (w.tags.get("name") or "").strip()
    if not name:
        name = (w.tags.get("ref") or "").strip().split(";")[0].strip()
    speed = parse_maxspeed(w.tags.get("maxspeed"))
    if speed is None or not 5 <= speed <= 130:
        speed = SPEEDS_KMH[highway]
    oneway = (w.tags.get("oneway") or "").strip().lower()
    junction = (w.tags.get("junction") or "").strip().lower()
    forward = oneway != "-1"
    backward = oneway not in ("yes", "true", "1") and junction != "roundabout"
    return name, speed * 1000.0 / 3600.0, forward, backward


class SegHandler(osmium.SimpleHandler):
    """Pass 1: record every directed segment of every drivable way,
    with its length, speed, and both endpoint coordinates."""

    def __init__(self, conn, restrictions, excluded=frozenset()):
        super().__init__()
        self.conn = conn
        self.excluded = excluded
        self.restrictions = restrictions
        self.batch = []
        self.names = {}
        self.ways_seen = 0
        self.ways_kept = 0
        self.segments = 0
        self.started = time.time()

    def _name_idx(self, name):
        idx = self.names.get(name)
        if idx is None:
            idx = len(self.names)
            self.names[name] = idx
        return idx

    def way(self, w):
        parsed = _drivable_way(w, self.excluded)
        if parsed is None:
            return
        name, speed_mps, forward, backward = parsed
        # Runs of consecutive nodes with valid locations; a gap splits
        # the way instead of guessing a segment (same rule as the
        # uncontracted builder).
        refs = []
        for n in w.nodes:
            loc = n.location
            if not loc.valid():
                refs.append(None)
            else:
                refs.append((n.ref, loc.lat, loc.lon))
        if len(refs) < 2:
            return
        self.ways_seen += 1
        nidx = self._name_idx(name)
        kept = False
        for u, v in zip(refs, refs[1:]):
            if u is None or v is None:
                continue
            a_id, a_lat, a_lon = u
            b_id, b_lat, b_lon = v
            if a_id == b_id:
                continue
            kept = True
            length = haversine_m(a_lat, a_lon, b_lat, b_lon)
            if length <= 0:
                continue
            if forward:
                self.restrictions.record(w.id, a_id, b_id)
                self.batch.append((a_id, b_id, nidx, speed_mps, length,
                                   a_lat, a_lon, b_lat, b_lon))
            if backward:
                self.restrictions.record(w.id, b_id, a_id)
                self.batch.append((b_id, a_id, nidx, speed_mps, length,
                                   b_lat, b_lon, a_lat, a_lon))
            if len(self.batch) >= 100000:
                self._flush()
        if kept:
            self.ways_kept += 1
        if self.ways_seen % 1000000 == 0:
            print(f"  ways: {self.ways_seen:,} seen, {self.ways_kept:,} kept, "
                  f"{self.segments:,} segments "
                  f"({time.time() - self.started:.0f}s)", flush=True)

    def _flush(self):
        if not self.batch:
            return
        self.conn.executemany(
            "INSERT INTO seg (a, b, n, s, len, alat, alon, blat, blon)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self.batch)
        self.segments += len(self.batch)
        self.batch = []

    def done(self):
        self._flush()
        self.conn.commit()
        if self.names:
            self.conn.executemany(
                "INSERT INTO names (idx, name) VALUES (?, ?)",
                [(idx, name) for name, idx in self.names.items()])
            self.conn.commit()


def compute_junctions(conn):
    """Junction nodes, from the seg table (undirected degree + names).

    A node is a junction unless it has exactly two distinct neighbours
    carrying a single road name. The UNION dedups the two directed
    edges of a two-way segment into one undirected edge, so a shape
    node on a two-way road counts degree 2, not 4.
    """
    started = time.time()
    print("finding junctions (undirected degree != 2 or name change)...",
          flush=True)
    conn.execute(
        "CREATE TABLE junction AS"
        " SELECT node AS id FROM ("
        "  SELECT a AS node, b AS nbr, n FROM seg"
        "  UNION"
        "  SELECT b AS node, a AS nbr, n FROM seg"
        ") GROUP BY node"
        " HAVING COUNT(*) <> 2 OR COUNT(DISTINCT n) > 1")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM junction").fetchone()[0]
    print(f"  junctions: {count:,} ({time.time() - started:.0f}s)", flush=True)
    return count


def _unit_xy(lat1, lon1, lat2, lon2, cos_lat):
    """Unit (x, y) direction of a segment, equirectangular, no trig."""
    dx = (lon2 - lon1) * cos_lat
    dy = lat2 - lat1
    norm = math.hypot(dx, dy)
    if norm <= 0:
        return None
    return (dx / norm, dy / norm)


def _pack_coords(lat, lon):
    """Pack a (lat, lon) pair into one int (two float32s).

    Junction coordinates are stored packed in a dict (one int per
    junction instead of a tuple of two float objects) to halve the
    memory of the junction map on a US-scale build. float32 is ~1 m
    precision, which is all routing, snapping, and geometry need.
    """
    import struct
    return int.from_bytes(struct.pack("<ff", lat, lon), "little",
                          signed=True)


def _unpack_coords(packed):
    """Unpack a (lat, lon) pair packed by _pack_coords."""
    import struct
    lat, lon = struct.unpack(
        "<ff", packed.to_bytes(8, "little", signed=True))
    return lat, lon


def build_sorted_arrays(conn, junctions):
    """Stream seg in `a` order into sorted parallel arrays.

    Returns (arrays, junction_coords): arrays is a dict of array('q')
    / array('i') / array('f') columns sorted by `a`, so the walk can
    find a node's outgoing segments with two binary searches; and
    junction_coords maps every junction id to its packed (lat, lon)
    (see _pack_coords), taken from the first segment row for that
    node. The junction id set doubles as the walk's membership test
    (junction_coords keys).
    """
    started = time.time()
    print("building sorted segment arrays (ORDER BY a)...", flush=True)
    a_arr = array("q")
    b_arr = array("q")
    n_arr = array("i")
    s_arr = array("f")
    len_arr = array("f")
    # End-node coordinates as float32 (~1 m): the walk only needs
    # them for bend directions and chain endpoints, and float64
    # here would cost ~4 GB extra on a US-scale build.
    blat_arr = array("f")
    blon_arr = array("f")
    junction_coords = {}
    prev_a = None
    count = 0
    for (a, b, n, s, length, alat, alon, blat, blon) in conn.execute(
            "SELECT a, b, n, s, len, alat, alon, blat, blon"
            " FROM seg ORDER BY a"):
        if a != prev_a:
            prev_a = a
            if a in junctions:
                junction_coords[a] = _pack_coords(alat, alon)
        a_arr.append(a)
        b_arr.append(b)
        n_arr.append(n)
        s_arr.append(s)
        len_arr.append(length)
        blat_arr.append(blat)
        blon_arr.append(blon)
        count += 1
        if count % 50000000 == 0:
            print(f"  arrays: {count:,} segments "
                  f"({time.time() - started:.0f}s)", flush=True)
    print(f"  arrays: {count:,} segments, "
          f"{len(junction_coords):,} junction coords "
          f"({time.time() - started:.0f}s)", flush=True)
    arrays = {"a": a_arr, "b": b_arr, "n": n_arr, "s": s_arr,
              "len": len_arr, "blat": blat_arr, "blon": blon_arr}
    return arrays, junction_coords


def walk_and_emit(conn, arrays, junction_coords, names,
                  bend_degrees=BEND_DEGREES):
    """Contract the graph by walking chains through the sorted arrays.

    One walk starts at every junction for each of its outgoing
    segments and follows the single continuation through each
    contractible node (the neighbour that is not the one it came
    from). A chain ends at a junction, at a sharp bend, at a dead
    end, or when it would loop back on itself (the loop itself is
    dropped: it leads nowhere). Each chain becomes one contracted
    edge with summed length/time; chain endpoints become graph nodes.
    """
    started = time.time()
    print("walking chains (cross-way contraction)...", flush=True)
    a_arr = arrays["a"]
    b_arr = arrays["b"]
    n_arr = arrays["n"]
    s_arr = arrays["s"]
    len_arr = arrays["len"]
    blat_arr = arrays["blat"]
    blon_arr = arrays["blon"]
    bend_cos = math.cos(math.radians(bend_degrees))
    edge_batch = []
    node_batch = []
    walks = 0
    chains = 0
    edges = 0

    def emit(s_id, s_lat, s_lon, e_id, e_lat, e_lon,
             name_idx, length_m, time_s):
        nonlocal chains, edges
        if s_id == e_id or length_m <= 0:
            return
        name = names.get(name_idx, "")
        edge_batch.append((s_id, e_id, name, length_m, time_s))
        node_batch.append((s_id, s_lat, s_lon))
        node_batch.append((e_id, e_lat, e_lon))
        chains += 1
        if len(edge_batch) >= 100000:
            flush()

    def flush():
        nonlocal edges
        if edge_batch:
            conn.executemany(
                "INSERT INTO edges_raw (a, b, name, length_m, time_s)"
                " VALUES (?, ?, ?, ?, ?)", edge_batch)
            edges += len(edge_batch)
            edge_batch.clear()
        if node_batch:
            conn.executemany(
                "INSERT OR IGNORE INTO nodes (id, lat, lon)"
                " VALUES (?, ?, ?)", node_batch)
            node_batch.clear()

    for j_id, packed in junction_coords.items():
        j_lat, j_lon = _unpack_coords(packed)
        lo = bisect_left(a_arr, j_id)
        hi = bisect_right(a_arr, j_id, lo)
        for k in range(lo, hi):
            walks += 1
            # Chain starts at the junction, first segment J -> X.
            x_id = b_arr[k]
            x_lat = blat_arr[k]
            x_lon = blon_arr[k]
            name_idx = n_arr[k]
            chain_len = len_arr[k]
            chain_time = len_arr[k] / s_arr[k]
            chain_cos_lat = math.cos(math.radians(j_lat))
            chain_dir = _unit_xy(j_lat, j_lon, x_lat, x_lon, chain_cos_lat)
            start_id, start_lat, start_lon = j_id, j_lat, j_lon
            prev_id = j_id
            cur_id, cur_lat, cur_lon = x_id, x_lat, x_lon
            visited = {j_id, x_id}
            while True:
                if cur_id in junction_coords:
                    emit(start_id, start_lat, start_lon,
                         cur_id, cur_lat, cur_lon,
                         name_idx, chain_len, chain_time)
                    break
                # Contractible node: the continuation is the outgoing
                # segment whose far end is not the node we came from.
                # (Parallel same-name segments are possible; take the
                # fastest -- they are the same road either way.)
                lo2 = bisect_left(a_arr, cur_id)
                hi2 = bisect_right(a_arr, cur_id, lo2)
                best_m = -1
                best_t = None
                for m in range(lo2, hi2):
                    if b_arr[m] == prev_id:
                        continue
                    t = len_arr[m] / s_arr[m]
                    if best_t is None or t < best_t:
                        best_t = t
                        best_m = m
                if best_m < 0:
                    # No continuation (a sink that is not a junction):
                    # the chain ends here.
                    emit(start_id, start_lat, start_lon,
                         cur_id, cur_lat, cur_lon,
                         name_idx, chain_len, chain_time)
                    break
                y_id = b_arr[best_m]
                y_lat = blat_arr[best_m]
                y_lon = blon_arr[best_m]
                direction = _unit_xy(cur_lat, cur_lon, y_lat, y_lon,
                                     chain_cos_lat)
                bent = (direction is None or chain_dir is None
                        or (direction[0] * chain_dir[0]
                            + direction[1] * chain_dir[1]) < bend_cos)
                if y_id in visited:
                    # Walking into a loop: keep the chain up to the
                    # current node, drop the loop itself.
                    emit(start_id, start_lat, start_lon,
                         cur_id, cur_lat, cur_lon,
                         name_idx, chain_len, chain_time)
                    break
                if bent:
                    emit(start_id, start_lat, start_lon,
                         cur_id, cur_lat, cur_lon,
                         name_idx, chain_len, chain_time)
                    start_id, start_lat, start_lon = cur_id, cur_lat, cur_lon
                    chain_len = len_arr[best_m]
                    chain_time = best_t
                    name_idx = n_arr[best_m]
                    chain_dir = direction
                    chain_cos_lat = math.cos(math.radians(cur_lat))
                else:
                    chain_len += len_arr[best_m]
                    chain_time += best_t
                visited.add(y_id)
                prev_id = cur_id
                cur_id, cur_lat, cur_lon = y_id, y_lat, y_lon
        if walks % 1000000 == 0:
            print(f"  walks: {walks:,} started, {chains:,} chains, "
                  f"{edges:,} edges ({time.time() - started:.0f}s)",
                  flush=True)
    flush()
    conn.commit()
    print(f"  walks: {walks:,} started, {chains:,} chains, "
          f"{edges:,} contracted edges "
          f"({time.time() - started:.0f}s)", flush=True)
    return chains, edges


def compute_components(conn):
    """Connected components (undirected) over the final edges table.

    Union-find in Python, streamed from SQLite (no fetchall of the
    whole edge list). Writes nodecomp(id, comp); the router uses it to
    reject a disconnected pair instantly instead of searching the
    whole graph (e.g. CONUS -> Hawaii).
    """
    started = time.time()
    print("computing connected components (union-find)...", flush=True)
    parent = array("i")  # parent[ordinal] = ordinal of parent
    ordinal = {}  # node id -> ordinal

    def find(x):
        # Iterative find with path halving.
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Attach the larger ordinal under the smaller (deterministic).
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    def _ord(node_id):
        o = ordinal.get(node_id)
        if o is None:
            o = len(ordinal)
            ordinal[node_id] = o
            parent.append(o)
        return o

    edge_count = 0
    for a, b in conn.execute("SELECT a, b FROM edges"):
        union(_ord(a), _ord(b))
        edge_count += 1
        if edge_count % 10000000 == 0:
            print(f"  components: {edge_count:,} edges "
                  f"({time.time() - started:.0f}s)", flush=True)
    # Component label = the root node's OSM id (stable across runs for
    # the same graph); write nodecomp in batches.
    roots = {}
    batch = []
    for node_id, o in ordinal.items():
        r = find(o)
        if r not in roots:
            roots[r] = node_id
        batch.append((node_id, roots[r]))
        if len(batch) >= 100000:
            conn.executemany(
                "INSERT INTO nodecomp (id, comp) VALUES (?, ?)", batch)
            batch = []
    if batch:
        conn.executemany(
            "INSERT INTO nodecomp (id, comp) VALUES (?, ?)", batch)
    conn.commit()
    print(f"  components: {len(roots):,} over {edge_count:,} edges "
          f"({time.time() - started:.0f}s)", flush=True)
    return len(roots)


def export_csr(conn):
    """Write the CSR blobs the hub loads in seconds (csr table).

    Streams nodes (id order) and edges (a, b order -- SQLite sorts
    once here, during the build, so the hub never pays for it), and
    packs the graph into arrays: lat/lon/off/nbr/elen/etime/ename,
    plus the component ids and the road-name list. The hub's
    Router.load reads these blobs directly; the plain tables stay
    as the fallback (and as the human-readable record). Returns
    (nodes, edges).
    """
    started = time.time()
    print("exporting CSR blobs for fast loading...", flush=True)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS csr ("
        " key TEXT PRIMARY KEY, blob BLOB NOT NULL)")
    lat = array("d")
    lon = array("d")
    ord_of = {}
    for node_id, nlat, nlon in conn.execute(
            "SELECT id, lat, lon FROM nodes ORDER BY id"):
        ord_of[node_id] = len(lat)
        lat.append(nlat)
        lon.append(nlon)
    n_nodes = len(lat)
    off = array("q")
    nbr = array("i")
    elen = array("f")
    etime = array("f")
    ename = array("i")
    names = []
    name_idx = {}
    next_off = 0
    edge_total = 0
    for a, b, name, length_m, time_s in conn.execute(
            "SELECT a, b, name, length_m, time_s FROM edges"
            " ORDER BY a, b"):
        oa = ord_of[a]
        while next_off <= oa:
            off.append(len(nbr))
            next_off += 1
        idx = name_idx.get(name)
        if idx is None:
            idx = len(names)
            name_idx[name] = idx
            names.append(name)
        nbr.append(ord_of[b])
        elen.append(length_m)
        etime.append(time_s)
        ename.append(idx)
        edge_total += 1
    while next_off <= n_nodes:
        off.append(len(nbr))
        next_off += 1
    comp = array("q", [0]) * n_nodes
    for node_id, c in conn.execute("SELECT id, comp FROM nodecomp"):
        ordinal = ord_of.get(node_id)
        if ordinal is not None:
            comp[ordinal] = c
    turns = ordinal_turns(conn, ord_of)
    del ord_of
    blobs = {
        "lat": lat.tobytes(), "lon": lon.tobytes(),
        "off": off.tobytes(), "nbr": nbr.tobytes(),
        "elen": elen.tobytes(), "etime": etime.tobytes(),
        "ename": ename.tobytes(), "comp": comp.tobytes(),
        "names_json": json.dumps(names).encode("utf-8"),
        "turns_json": json.dumps(turns).encode("utf-8"),
    }
    conn.executemany(
        "INSERT OR REPLACE INTO csr (key, blob) VALUES (?, ?)",
        [(key, blob) for key, blob in blobs.items()])
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('csr', '1')")
    conn.commit()
    print(f"  csr: {n_nodes:,} nodes, {edge_total:,} edges, "
          f"{sum(len(b) for b in blobs.values()) / 1048576:.0f} MB of blobs "
          f"({time.time() - started:.0f}s)", flush=True)
    return n_nodes, edge_total


def build(inputs, output, coverage="", bend_degrees=BEND_DEGREES,
          excluded=frozenset(), resume=False):
    return build_routing_file(
        lambda staging: _build(inputs, staging, coverage, bend_degrees, excluded, resume),
        output, resume=resume)


def _build(inputs, output, coverage, bend_degrees, excluded, resume):
    overall = time.time()
    conn = sqlite3.connect(output)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-262144")
        timings = {}
        if resume:
            # Reuse a DB left behind by a killed build: pass 1 (seg)
            # and the junction table are already complete, so skip
            # straight to the sorted arrays + walk. The constants below
            # are the timings of the specific killed run this resume
            # was written for (US build, 2026-11-22); they keep the
            # meta timings honest for that build, not for reuse.
            for table in ("seg", "junction", "names"):
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if n == 0:
                    raise SystemExit(
                        f"resume: {table} is empty in {output}")
            junction_count = conn.execute(
                "SELECT COUNT(*) FROM junction").fetchone()[0]
            timings["pass1_s"] = 4342
            timings["junctions_s"] = 2670
            print(f"resuming: seg/junction/names present, "
                  f"{junction_count:,} junctions "
                  f"(killed run: pass 1 4342s, junctions 2670s)",
                  flush=True)
        else:
            conn.executescript(SCHEMA)

            # -- pass 1: segments --------------------------------------
            started = time.time()
            restrictions = TurnRestrictions(inputs)
            handler = SegHandler(conn, restrictions, excluded=excluded)
            for path in inputs:
                print(f"pass 1 (segments): {path}", flush=True)
                # Disk-backed location store: the US file has ~1.5B nodes,
                # far too many for an in-memory store on this laptop.
                handler.apply_file(path, locations=True,
                                   idx="sparse_file_array")
                handler.done()
                print(f"  ways: {handler.ways_seen:,} seen, "
                      f"{handler.ways_kept:,} kept, "
                      f"{handler.segments:,} directed segments, "
                      f"{len(handler.names):,} distinct names", flush=True)
            restrictions.write(conn)
            timings["pass1_s"] = round(time.time() - started)
            print(f"pass 1 done in {timings['pass1_s']}s", flush=True)

            # -- junctions -----------------------------------------------
            started = time.time()
            junction_count = compute_junctions(conn)
            timings["junctions_s"] = round(time.time() - started)

        # -- sorted arrays + walk -----------------------------------------
        started = time.time()
        junctions = set()
        for (node_id,) in conn.execute("SELECT id FROM junction"):
            junctions.add(node_id)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='turn_restrictions'").fetchone():
            for incoming, via, outgoing in conn.execute("SELECT incoming, via, outgoing FROM turn_restrictions"):
                junctions.update((incoming, via))
                if outgoing is not None:
                    junctions.add(outgoing)
        print(f"  junction set loaded: {len(junctions):,} ids", flush=True)
        arrays, junction_coords = build_sorted_arrays(conn, junctions)
        del junctions
        # The seg table is no longer needed once the arrays exist;
        # drop it now to free disk before the vacuum at the end.
        conn.execute("DROP TABLE seg")
        conn.commit()
        names = {idx: name for idx, name in
                 conn.execute("SELECT idx, name FROM names")}
        chains, _edges = walk_and_emit(conn, arrays, junction_coords,
                                       names, bend_degrees=bend_degrees)
        del arrays, junction_coords
        timings["walk_s"] = round(time.time() - started)
        print(f"walk done in {timings['walk_s']}s", flush=True)

        # -- finish: collapse, components, meta -------------------------
        started = time.time()
        print("collapsing parallel edges (fastest wins)...", flush=True)
        conn.execute(
            "INSERT OR IGNORE INTO edges (a, b, name, length_m, time_s)"
            " SELECT a, b, name, length_m, time_s FROM edges_raw"
            " WHERE a <> b ORDER BY time_s ASC, (name <> '') DESC")
        conn.execute("DROP TABLE edges_raw")
        counts = {}
        for table in ("nodes", "edges"):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ncomp = compute_components(conn)
        export_csr(conn)
        conn.execute("DROP TABLE names")
        conn.execute("DROP TABLE junction")
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
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("junctions", str(junction_count)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("chains", str(chains)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("components", str(ncomp)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("bend_degrees", str(bend_degrees)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("contracted", "1"))
        if resume:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("resumed", "1"))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("excluded_highway", ",".join(sorted(excluded)) or "none"))
        for key, val in sorted(timings.items()):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (key, str(val)))
        conn.commit()
        print("vacuuming...", flush=True)
        conn.execute("VACUUM")
        conn.commit()
        timings["finish_s"] = round(time.time() - started)
        timings["total_s"] = round(time.time() - overall)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("total_s", str(timings["total_s"])))
        conn.commit()
        print(f"done: {counts['nodes']:,} nodes, {counts['edges']:,} "
              f"directed edges, {ncomp:,} components -> {output} "
              f"({timings['total_s']}s total)", flush=True)
        return counts
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a contracted offline road-routing index "
                    "from OSM PBF extract(s).")
    parser.add_argument("inputs", nargs="+",
                        help="input .osm.pbf file(s), then the output .db")
    parser.add_argument("--coverage", default="",
                        help="coverage label stored in the index "
                             "(e.g. 'United States (CONUS, Alaska, Hawaii)')")
    parser.add_argument("--bend-degrees", type=float, default=BEND_DEGREES,
                        help="split a chain when its heading turns this "
                             "far from its first segment (default 15)")
    parser.add_argument("--exclude-highway", action="append", default=[],
                        metavar="CLASS",
                        help="leave a highway class out of the index "
                             "(repeatable, e.g. --exclude-highway service)")
    parser.add_argument("--resume", action="store_true",
                        help="reuse an interrupted staging DB that already "
                             "has seg/junction/names (from a killed run) "
                             "and continue from the sorted arrays; the "
                             "input PBF is then used only for meta")
    args = parser.parse_args(argv)
    if len(args.inputs) < 2:
        parser.error("need at least one input PBF and an output .db")
    *inputs, output = args.inputs
    for path in inputs:
        if not os.path.isfile(path):
            parser.error(f"input not found: {path}")
    build(inputs, output, coverage=args.coverage,
          bend_degrees=args.bend_degrees,
          excluded=set(args.exclude_highway), resume=args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
