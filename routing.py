"""Offline road routing for the Pandora's Box hub — stdlib only.

Reads the road index built by scripts/build-routing-index.py from an
OpenStreetMap extract (SQLite):

  meta(key TEXT PRIMARY KEY, value TEXT)   -- coverage, source, counts
  nodes(id INTEGER PRIMARY KEY, lat REAL, lon REAL)
  edges(a INTEGER, b INTEGER, name TEXT, length_m REAL, time_s REAL,
        PRIMARY KEY(a, b))                 -- directed; two-way roads
                                           -- have both directions

The graph is loaded into memory once (lazy, on the first route) and
cached for the life of the hub process: a route is then a heap A*
over the in-memory adjacency, which answers in well under a second
for the Massachusetts-size index. The index stays on the laptop
(C:\\Library\\geocode\\routing.db); it is never staged to the repo.

The in-memory graph is compact arrays, not dicts: node coordinates
and CSR (compressed sparse row) edge lists indexed by node ordinal,
about 16 bytes per node and 20 per edge. A dict-of-dicts would need
roughly ten times that -- tens of GB for a US-wide index -- while the
arrays keep even a 30M-node graph to a few GB. Loading streams the
tables twice (once to count outgoing edges per node, once to fill
the CSR arrays) so no sorted order or extra index is required.

Since 2026-11-22 the index is CONTRACTED (scripts/build-routing-index-us.py):
its nodes are junctions, dead ends, road-name changes, and sharp bends
only -- degree-2 shape nodes are folded into single edges with summed
length/time -- so the US-wide graph stays loadable. The contracted
index also carries nodecomp(id, comp): the connected component of
each node, so a route between disconnected parts (CONUS vs Hawaii)
is rejected at once instead of searching the whole graph. Indexes
without nodecomp (the older MA+RI+CT build, the smoke-test grids)
route exactly as before; the component check simply skips.

A route is a list of legs grouped by road name, each with a plain
turn instruction ("Turn left onto Main St", "Continue straight on
Granite St"), so the map can show the road route and the turn list
next to the straight-line distance.
"""

import heapq
import json
import math
import os
import sqlite3
import threading
import time
from array import array
from itertools import accumulate

from sqlite_files import open_readonly

# Fastest plausible road speed, for the A* heuristic (admissible:
# no edge can beat this, so the heuristic never overestimates).
_MAX_SPEED_MPS = 130 / 3.6

# Snap search: grid cells are 0.01 degrees (~1.1 km). Rings are
# searched out to this many cells (~3 km) before giving up.
_SNAP_MAX_RING = 3
_CELL_DEG = 0.01


class RoutingUnavailableError(Exception):
    """The routing index is missing, unreadable, or empty."""


class NoRouteError(Exception):
    """No road route exists between the two points (or neither end is
    near a road). Carried as a clean 404, never a 500."""


class RouteTimeoutError(Exception):
    """A route search exceeded its time budget (audit #3). Carried
    as a clean 503, never a 500."""


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial bearing in degrees (0 = north, clockwise)."""
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(bearing):
    return _COMPASS[round(bearing / 22.5) % 16]


def _turn_text(angle, road):
    """Plain turn instruction for a bearing change of `angle` degrees.

    Positive angle is a right turn, negative a left turn. `road` is
    the road being turned onto ("" for an unnamed road).
    """
    onto = (" onto " + road) if road else ""
    a = abs(angle)
    if a < 20:
        return "Continue straight" + onto
    if a < 60:
        return ("Bear right" if angle > 0 else "Bear left") + onto
    if a < 120:
        return ("Turn right" if angle > 0 else "Turn left") + onto
    if a < 160:
        return ("Sharp right" if angle > 0 else "Sharp left") + onto
    return "Make a U-turn" + onto


class Router:
    """A loaded road graph. Build one per routing DB and reuse it.

    Nodes are held as compact arrays indexed by ordinal (0..N-1),
    assigned in ascending OSM id order at load time, with the edges
    in CSR (compressed sparse row) form: off[i] is the start index
    of node i's outgoing edges in the nbr/len/time/name arrays.
    The public interface (load/snap/route/coverage) is unchanged.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._loaded = False
        self.lat = array("d")    # node ordinal -> latitude
        self.lon = array("d")    # node ordinal -> longitude
        self.off = array("q")    # node ordinal -> first edge index (len N+1)
        self.nbr = array("i")    # edge index -> neighbour ordinal
        self.elen = array("f")   # edge index -> length_m
        self.etime = array("f")  # edge index -> time_s
        self.ename = array("i")  # edge index -> name_idx
        self.comp = None         # node ordinal -> component id, or None
        self.names = []    # name_idx -> road name ("" = unnamed)
        self._name_idx = {}
        self._grid = {}    # (cell_lat, cell_lon) -> [node ordinals]
        self.meta = {}

    def _reset(self):
        """Drop any half-loaded graph so a retry starts clean."""
        self.lat = array("d")
        self.lon = array("d")
        self.off = array("q")
        self.nbr = array("i")
        self.elen = array("f")
        self.etime = array("f")
        self.ename = array("i")
        self.comp = None
        self.names = []
        self._name_idx = {}
        self._grid = {}
        self.meta = {}

    def _load_csr_blobs(self, conn):
        """Load the graph from the csr blobs, or return False.

        The contracted builder packs the graph into arrays (lat/lon/
        off/nbr/elen/etime/ename, plus components and the road-name
        list) so the hub loads in seconds instead of streaming tens
        of millions of rows twice. Any problem (no blobs, a short
        blob, bad JSON) returns False and the caller falls back to
        the plain tables; nothing is committed until every blob has
        parsed, so a failed blob load leaves no half-loaded graph.
        """
        try:
            csr = conn.execute(
                "SELECT value FROM meta WHERE key='csr'").fetchone()
            if csr is None or csr[0] != "1":
                return False
            blobs = dict(conn.execute(
                "SELECT key, blob FROM csr").fetchall())
            lat = array("d")
            lat.frombytes(blobs["lat"])
            lon = array("d")
            lon.frombytes(blobs["lon"])
            off = array("q")
            off.frombytes(blobs["off"])
            nbr = array("i")
            nbr.frombytes(blobs["nbr"])
            elen = array("f")
            elen.frombytes(blobs["elen"])
            etime = array("f")
            etime.frombytes(blobs["etime"])
            ename = array("i")
            ename.frombytes(blobs["ename"])
            names = json.loads(blobs["names_json"].decode("utf-8"))
            if (len(lat) == 0 or len(nbr) == 0
                    or len(off) != len(lat) + 1
                    or off[0] != 0 or off[-1] != len(nbr)
                    or len(elen) != len(nbr)
                    or len(etime) != len(nbr)
                    or len(ename) != len(nbr)
                    or not names or max(ename) >= len(names)
                    or min(ename) < 0):
                return False
            comp = None
            if "comp" in blobs:
                comp = array("q")
                comp.frombytes(blobs["comp"])
                if len(comp) != len(lat):
                    return False
        except (sqlite3.Error, ValueError, KeyError, TypeError,
                UnicodeDecodeError):
            return False
        self.lat, self.lon, self.off = lat, lon, off
        self.nbr, self.elen, self.etime, self.ename = nbr, elen, etime, ename
        self.comp = comp
        self.names = names
        self._name_idx = {name: i for i, name in enumerate(names)}
        for ordinal in range(len(self.lat)):
            cell = (int(self.lat[ordinal] / _CELL_DEG),
                    int(self.lon[ordinal] / _CELL_DEG))
            self._grid.setdefault(cell, []).append(ordinal)
        return True

    # -- loading ---------------------------------------------------------
    def load(self):
        """Load the graph into memory (once; later calls are no-ops).

        Raises RoutingUnavailableError when the DB is missing, empty,
        or unreadable. Returns (nodes, directed edges) for logging.
        """
        with self._lock:
            if self._loaded:
                return len(self.lat), len(self.nbr)
            if not self.db_path or not os.path.isfile(self.db_path):
                raise RoutingUnavailableError(
                    "road routing is not installed yet")
            try:
                conn = open_readonly(self.db_path)
                try:
                    self.meta = dict(conn.execute(
                        "SELECT key, value FROM meta").fetchall())
                    # Fast path: CSR blobs (see _load_csr_blobs). The
                    # plain tables below stay as the fallback.
                    if self._load_csr_blobs(conn):
                        self._loaded = True
                        return len(self.lat), len(self.nbr)
                    # Pass 1 -- nodes, in ascending id order (INTEGER
                    # PRIMARY KEY streams in id order), so the ordinal
                    # is the node's rank in id order. The id->ordinal
                    # map is transient: it is dropped once the edges
                    # and components are built.
                    ord_of = {}
                    for node_id, lat, lon in conn.execute(
                            "SELECT id, lat, lon FROM nodes"):
                        ordinal = len(self.lat)
                        ord_of[node_id] = ordinal
                        self.lat.append(lat)
                        self.lon.append(lon)
                        cell = (int(lat / _CELL_DEG), int(lon / _CELL_DEG))
                        self._grid.setdefault(cell, []).append(ordinal)
                    n_nodes = len(self.lat)
                    if n_nodes == 0:
                        raise RoutingUnavailableError(
                            "road routing index is empty")
                    # Pass 2 -- count outgoing edges per node, and
                    # intern road names (pass 3 reuses the dict).
                    counts = array("i", [0]) * n_nodes
                    edge_total = 0
                    for a, b, name, length_m, time_s in conn.execute(
                            "SELECT a, b, name, length_m, time_s FROM edges"):
                        counts[ord_of[a]] += 1
                        edge_total += 1
                        if name not in self._name_idx:
                            self._name_idx[name] = len(self.names)
                            self.names.append(name)
                    if edge_total == 0:
                        raise RoutingUnavailableError(
                            "road routing index is empty")
                    # Prefix sums -> CSR offsets (len N+1).
                    self.off = array("q", accumulate(counts, initial=0))
                    # Pass 3 -- fill the edge arrays. `cursor` starts
                    # as a copy of off[:-1] and advances per edge.
                    cursor = array("q", self.off[:n_nodes])
                    self.nbr = array("i", [0]) * edge_total
                    self.elen = array("f", [0.0]) * edge_total
                    self.etime = array("f", [0.0]) * edge_total
                    self.ename = array("i", [0]) * edge_total
                    for a, b, name, length_m, time_s in conn.execute(
                            "SELECT a, b, name, length_m, time_s FROM edges"):
                        oa = ord_of[a]
                        slot = cursor[oa]
                        cursor[oa] = slot + 1
                        self.nbr[slot] = ord_of[b]
                        self.elen[slot] = length_m
                        self.etime[slot] = time_s
                        self.ename[slot] = self._name_idx[name]
                    # Components, when the index carries them (the
                    # contracted build does; older indexes do not).
                    # 0 means "unknown" (OSM ids are never 0).
                    try:
                        has_comp = conn.execute(
                            "SELECT COUNT(*) FROM sqlite_master WHERE"
                            " type='table' AND name='nodecomp'"
                        ).fetchone()[0] > 0
                    except sqlite3.Error:
                        has_comp = False
                    if has_comp:
                        self.comp = array("q", [0]) * n_nodes
                        for node_id, comp in conn.execute(
                                "SELECT id, comp FROM nodecomp"):
                            ordinal = ord_of.get(node_id)
                            if ordinal is not None:
                                self.comp[ordinal] = comp
                    del ord_of, counts, cursor
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._reset()
                raise RoutingUnavailableError(
                    "road routing index unreadable") from exc
            except RoutingUnavailableError:
                self._reset()
                raise
            self._loaded = True
            return len(self.lat), len(self.nbr)

    @property
    def coverage(self):
        return self.meta.get("coverage", "")

    # -- snapping ----------------------------------------------------------
    def snap(self, lat, lon):
        """Nearest graph node to a point, or None when no road is near.

        Searches the snap grid in expanding rings (about 1 km per
        ring, out to ~3 km) and returns (node ordinal, distance_m).
        """
        self.load()
        cell_lat, cell_lon = int(lat / _CELL_DEG), int(lon / _CELL_DEG)
        best = None
        for ring in range(_SNAP_MAX_RING + 1):
            for dlat in range(-ring, ring + 1):
                for dlon in range(-ring, ring + 1):
                    if max(abs(dlat), abs(dlon)) != ring:
                        continue
                    for ordinal in self._grid.get(
                            (cell_lat + dlat, cell_lon + dlon), ()):
                        dist = haversine_m(lat, lon,
                                           self.lat[ordinal],
                                           self.lon[ordinal])
                        if best is None or dist < best[1]:
                            best = (ordinal, dist)
            if best is not None:
                return best
        return None

    # -- routing -----------------------------------------------------------
    def _components_differ(self, start_ord, goal_ord):
        """True when the index says the two nodes cannot connect.

        The contracted index carries nodecomp(id, comp), loaded into
        the comp array; two nodes in different components have no
        road route between them at all (CONUS vs Hawaii), so the
        caller can fail at once instead of searching the whole graph.
        Returns False when the index has no components, or when
        either node has no component (0 = unknown): the A* search
        then decides, as before.
        """
        if self.comp is None:
            return False
        start_comp = self.comp[start_ord]
        goal_comp = self.comp[goal_ord]
        return (start_comp != 0 and goal_comp != 0
                and start_comp != goal_comp)

    def route(self, from_lat, from_lon, to_lat, to_lon, deadline_seconds=None):
        """Turn-by-turn road route between two points.

        Returns a dict with from/to (requested + snapped points),
        distance_m, duration_s, legs, and geometry (a [lat, lon]
        polyline). Raises NoRouteError when either end is not near a
        road or the two points are not connected by road, and
        RouteTimeoutError when the search runs past deadline_seconds
        (audit #3: a deadline bounds one unbounded A* search; the
        handler's route semaphore bounds how many run at once).
        """
        self.load()
        start = self.snap(from_lat, from_lon)
        if start is None:
            raise NoRouteError("no road near the starting point")
        goal = self.snap(to_lat, to_lon)
        if goal is None:
            raise NoRouteError("no road near the destination")
        start_ord, start_snap_m = start
        goal_ord, goal_snap_m = goal
        if start_ord == goal_ord:
            raise NoRouteError(
                "start and destination are on the same road point — "
                "pick points further apart")
        if self._components_differ(start_ord, goal_ord):
            raise NoRouteError(
                "no road connection between these points")
        deadline = (time.monotonic() + deadline_seconds
                    if deadline_seconds is not None else None)
        if deadline is not None and time.monotonic() >= deadline:
            raise RouteTimeoutError("route search exceeded its time budget")
        goal_lat, goal_lon = self.lat[goal_ord], self.lon[goal_ord]
        # A*: cost is travel time; the heuristic is the straight-line
        # distance at the fastest plausible speed (admissible).
        g_score = {start_ord: 0.0}
        came = {start_ord: None}  # node -> (prev node, edge tuple)
        open_heap = [(haversine_m(from_lat, from_lon, goal_lat, goal_lon)
                      / _MAX_SPEED_MPS, 0.0, start_ord)]
        visited = set()
        pops = 0
        while open_heap:
            pops += 1
            if (deadline is not None and pops % 1024 == 0
                    and time.monotonic() >= deadline):
                raise RouteTimeoutError("route search exceeded its time budget")
            _f, g, node = heapq.heappop(open_heap)
            if node in visited:
                continue
            visited.add(node)
            if node == goal_ord:
                break
            for ei in range(self.off[node], self.off[node + 1]):
                nbr = self.nbr[ei]
                if nbr in visited:
                    continue
                time_s = self.etime[ei]
                ng = g + time_s
                if ng < g_score.get(nbr, math.inf):
                    g_score[nbr] = ng
                    came[nbr] = (node, (nbr, self.elen[ei], time_s,
                                         self.ename[ei]))
                    h = haversine_m(self.lat[nbr], self.lon[nbr],
                                    goal_lat, goal_lon) / _MAX_SPEED_MPS
                    heapq.heappush(open_heap, (ng + h, ng, nbr))
        if goal_ord not in came or (goal_ord != start_ord
                                    and came[goal_ord] is None):
            raise NoRouteError("no road route found between these points")
        # Rebuild the path: list of (node, edge into it), start first.
        path_nodes = [goal_ord]
        path_edges = []
        node = goal_ord
        while came[node] is not None:
            prev, edge = came[node]
            path_nodes.append(prev)
            path_edges.append(edge)
            node = prev
        path_nodes.reverse()
        path_edges.reverse()
        return self._describe(path_nodes, path_edges,
                              (from_lat, from_lon, start_ord, start_snap_m),
                              (to_lat, to_lon, goal_ord, goal_snap_m))

    def _describe(self, path_nodes, path_edges, start, goal):
        """Turn a node/edge path into legs, instructions, and geometry."""
        total_m = sum(e[1] for e in path_edges)
        total_s = sum(e[2] for e in path_edges)
        # Group consecutive edges by road name into legs. Each group
        # covers its edges' end nodes, starting from the node before
        # its first edge, so turn bearings can be read off the joins.
        groups = []  # [name_idx, [edge, ...], [node, ...]]
        node_iter = iter(path_nodes)
        prev_node = next(node_iter)
        for edge in path_edges:
            node = next(node_iter)
            name_idx = edge[3]
            if groups and groups[-1][0] == name_idx:
                groups[-1][1].append(edge)
                groups[-1][2].append(node)
            else:
                groups.append([name_idx, [edge], [prev_node, node]])
            prev_node = node
        legs = []
        for i, (name_idx, edges, nodes) in enumerate(groups):
            road = self.names[name_idx] or "unnamed road"
            dist_m = sum(e[1] for e in edges)
            dur_s = sum(e[2] for e in edges)
            first_lat, first_lon = self.lat[nodes[0]], self.lon[nodes[0]]
            last_lat, last_lon = self.lat[nodes[-1]], self.lon[nodes[-1]]
            if i == 0:
                text = "Head %s on %s" % (compass(
                    bearing_deg(first_lat, first_lon, last_lat, last_lon)),
                    road)
            else:
                prev_nodes = groups[i - 1][2]
                p_lat, p_lon = (self.lat[prev_nodes[-2]],
                                self.lon[prev_nodes[-2]]) if len(prev_nodes) >= 2 else (self.lat[prev_nodes[0]], self.lon[prev_nodes[0]])
                p2_lat, p2_lon = self.lat[prev_nodes[-1]], self.lon[prev_nodes[-1]]
                in_bearing = bearing_deg(p_lat, p_lon, p2_lat, p2_lon)
                out_bearing = bearing_deg(
                    self.lat[nodes[0]], self.lon[nodes[0]],
                    self.lat[nodes[1]], self.lon[nodes[1]]) if len(nodes) > 1 else in_bearing
                angle = (out_bearing - in_bearing + 540) % 360 - 180
                text = _turn_text(angle, road)
            legs.append({
                "instruction": text,
                "road": road,
                "distance_m": round(dist_m),
                "duration_s": round(dur_s),
            })
        legs.append({"instruction": "Arrive at your destination.",
                     "road": "", "distance_m": 0, "duration_s": 0})
        geometry = [[round(self.lat[n], 6), round(self.lon[n], 6)]
                    for n in path_nodes]
        if len(geometry) > 1500:
            # Decimate long polylines for the JSON payload, keeping
            # the first and last points (turn points are in the legs).
            step = (len(geometry) - 1) / 1499.0
            keep = [geometry[round(i * step)] for i in range(1499)]
            keep.append(geometry[-1])
            geometry = keep
        (flat, flon, _sid, snap_from_m) = start
        (tlat, tlon, _gid, snap_to_m) = goal
        start_lat, start_lon = self.lat[path_nodes[0]], self.lon[path_nodes[0]]
        goal_lat, goal_lon = self.lat[path_nodes[-1]], self.lon[path_nodes[-1]]
        return {
            "from": {"lat": flat, "lon": flon,
                     "snapped_lat": start_lat, "snapped_lon": start_lon,
                     "snap_m": round(snap_from_m)},
            "to": {"lat": tlat, "lon": tlon,
                   "snapped_lat": goal_lat, "snapped_lon": goal_lon,
                   "snap_m": round(snap_to_m)},
            "distance_m": round(total_m),
            "duration_s": round(total_s),
            "legs": legs,
            "geometry": geometry,
            "coverage": self.coverage,
        }
