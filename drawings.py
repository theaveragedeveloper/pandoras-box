"""Server-side drawing store — the hub is the source of truth for
map drawings (grid-down planning marks).

the owner draws on the map to plan resources and locations: a marker
(point), a line, a polygon (area), or a circle (radius). Each drawn
item has a label and a type (water, food, fuel, medical, shelter,
hazard, rally point, other). Like pins, drawings live in SQLite
(drawings.db, next to the hub code) so they survive a browser clear,
a different browser, and a laptop restart — and like pins they are
localhost-only: the family's marked resources are private, never
shared to hotspot clients.

Drawing format: {id, kind, type, label, geometry, created, updated}.
``geometry`` depends on the kind:
- marker: {"lat": float, "lon": float}
- line: {"points": [[lat, lon], ...]} — at least 2 points
- polygon: {"points": [[lat, lon], ...]} — at least 3 points
  (the ring is closed implicitly when rendered)
- circle: {"lat": float, "lon": float, "radius_m": float} — centre
  plus radius in metres (1 m to 100 km)

Storage: one SQLite file, stdlib only. No new dependencies.
"""

import json
import sqlite3
import threading
from datetime import date

DRAWING_KINDS = ("marker", "line", "polygon", "circle")
DRAWING_TYPES = ("water", "food", "fuel", "medical", "shelter",
                 "hazard", "rally", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drawings (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    type TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    geometry TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT ''
);
"""

# Maximum drawings the store will hold.
MAX_DRAWINGS = 1000
# Maximum length of a drawing id.
MAX_DRAWING_ID_LEN = 128
# Maximum points in one line or polygon.
MAX_POINTS = 500
# Circle radius bounds, in metres.
MIN_RADIUS_M = 1
MAX_RADIUS_M = 100000


def _valid_latlon(lat, lon):
    """Finite lat/lon in range (mirrors pins.valid_pin)."""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:  # NaN
        return False
    return abs(lat) <= 90 and abs(lon) <= 180


def _valid_point(point):
    """One [lat, lon] pair (a list/tuple of two numbers)."""
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        return False
    return _valid_latlon(point[0], point[1])


def valid_geometry(kind, geometry):
    """Geometry matches the kind (shape + ranges)."""
    if not isinstance(geometry, dict):
        return False
    if kind == "marker":
        return _valid_latlon(geometry.get("lat"), geometry.get("lon"))
    if kind in ("line", "polygon"):
        points = geometry.get("points")
        if not isinstance(points, list):
            return False
        if kind == "line" and not 2 <= len(points) <= MAX_POINTS:
            return False
        if kind == "polygon" and not 3 <= len(points) <= MAX_POINTS:
            return False
        return all(_valid_point(p) for p in points)
    if kind == "circle":
        try:
            radius = float(geometry.get("radius_m"))
        except (TypeError, ValueError):
            return False
        if radius != radius:  # NaN
            return False
        return (MIN_RADIUS_M <= radius <= MAX_RADIUS_M
                and _valid_latlon(geometry.get("lat"), geometry.get("lon")))
    return False


def valid_drawing(drawing):
    """id + kind + type + label + geometry, all in shape."""
    if not isinstance(drawing, dict):
        return False
    drawing_id = drawing.get("id")
    if isinstance(drawing_id, bool):
        return False
    if isinstance(drawing_id, int):
        if drawing_id < 0 or drawing_id > 2**53 - 1:
            return False
    elif isinstance(drawing_id, str):
        if not drawing_id.strip():
            return False
        if len(drawing_id.strip()) > MAX_DRAWING_ID_LEN:
            return False
    else:
        return False
    if drawing.get("kind") not in DRAWING_KINDS:
        return False
    if drawing.get("type") not in DRAWING_TYPES:
        return False
    label = drawing.get("label")
    if label is not None and not isinstance(label, str):
        return False
    return valid_geometry(drawing.get("kind"), drawing.get("geometry"))


def clean_drawing(drawing):
    """Normalise a drawing dict for storage (id as text, label trimmed,
    geometry reduced to its canonical shape)."""
    kind = drawing["kind"]
    geometry = drawing["geometry"]
    if kind == "marker":
        clean_geometry = {"lat": float(geometry["lat"]),
                          "lon": float(geometry["lon"])}
    elif kind in ("line", "polygon"):
        clean_geometry = {"points": [[float(p[0]), float(p[1])]
                                     for p in geometry["points"]]}
    else:  # circle
        clean_geometry = {"lat": float(geometry["lat"]),
                          "lon": float(geometry["lon"]),
                          "radius_m": float(geometry["radius_m"])}
    return {
        "id": str(drawing.get("id")).strip(),
        "kind": kind,
        "type": drawing["type"],
        "label": str(drawing.get("label") or "").strip()[:200],
        "geometry": clean_geometry,
    }


def _short_id(drawing_id):
    """Truncate an id for error messages (never echo it verbatim)."""
    return str(drawing_id)[:40]


class DrawingStore:
    """SQLite-backed drawing list. One connection per call, serialised."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    def list_drawings(self):
        """All drawings, oldest first.

        Raises sqlite3.Error if the database cannot be read (a corrupt
        DB must surface as an error, never as an empty store).
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM drawings ORDER BY created, id").fetchall()
                return [{
                    "id": r["id"],
                    "kind": r["kind"],
                    "type": r["type"],
                    "label": r["label"],
                    "geometry": json.loads(r["geometry"]),
                    "created": r["created"],
                } for r in rows]
            finally:
                conn.close()

    def add_drawing(self, drawing):
        """Insert one drawing. Raises ValueError on bad input or duplicate id."""
        if not valid_drawing(drawing):
            raise ValueError(
                "Drawing needs an id, a kind (marker/line/polygon/circle), "
                "a type, and valid geometry.")
        clean = clean_drawing(drawing)
        today = date.today().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM drawings").fetchone()[0]
                if count >= MAX_DRAWINGS:
                    raise ValueError(
                        f"Drawing store is full ({MAX_DRAWINGS} drawings).")
                row = conn.execute(
                    "SELECT id FROM drawings WHERE id = ?",
                    (clean["id"],)).fetchone()
                if row is not None:
                    raise ValueError(
                        f"A drawing with id {_short_id(clean['id'])} already exists.")
                conn.execute(
                    "INSERT INTO drawings (id, kind, type, label, geometry, created, updated)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (clean["id"], clean["kind"], clean["type"], clean["label"],
                     json.dumps(clean["geometry"], sort_keys=True),
                     today, today),
                )
                conn.commit()
                return clean
            finally:
                conn.close()

    def update_drawing(self, drawing):
        """Update one drawing by id (label/type/geometry). Raises ValueError."""
        if not valid_drawing(drawing):
            raise ValueError(
                "Drawing needs an id, a kind (marker/line/polygon/circle), "
                "a type, and valid geometry.")
        clean = clean_drawing(drawing)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id FROM drawings WHERE id = ?",
                    (clean["id"],)).fetchone()
                if row is None:
                    raise ValueError(
                        f"No drawing with id {_short_id(clean['id'])}.")
                conn.execute(
                    "UPDATE drawings SET kind=?, type=?, label=?, geometry=?, updated=?"
                    " WHERE id=?",
                    (clean["kind"], clean["type"], clean["label"],
                     json.dumps(clean["geometry"], sort_keys=True),
                     date.today().isoformat(), clean["id"]),
                )
                conn.commit()
                return clean
            finally:
                conn.close()

    def delete_drawing(self, drawing_id):
        """Delete one drawing by id. Raises ValueError if it does not exist."""
        if isinstance(drawing_id, bool):
            raise ValueError("Drawing id is required.")
        if drawing_id is None or (isinstance(drawing_id, str)
                                  and not drawing_id.strip()):
            raise ValueError("Drawing id is required.")
        drawing_id = str(drawing_id).strip()
        if len(drawing_id) > MAX_DRAWING_ID_LEN:
            raise ValueError("Drawing id is too long (max 128 characters).")
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM drawings WHERE id = ?", (drawing_id,))
                if cur.rowcount == 0:
                    raise ValueError(
                        f"No drawing with id {_short_id(drawing_id)}.")
                conn.commit()
                return {"deleted": drawing_id}
            finally:
                conn.close()
