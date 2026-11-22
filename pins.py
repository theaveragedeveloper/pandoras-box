"""Server-side pin store — the hub is the source of truth for map pins.

Pins used to live only in the browser (localStorage, mk1Pins in
static/pins.js). If the browser data was cleared, or the owner opened the
hub in a different browser, the pins were gone — and they are his own
data (saved places, house locations), not recoverable from sources.

This module stores pins in SQLite (pins.db, next to the hub code) so
they survive a browser clear, a different browser, and a laptop
restart. The browser's localStorage stays as a fast local cache, but
the server is authoritative: the frontend loads pins from the server
on start (pins.js sync()) and pushes every pin change to the server.

Pin format is unchanged from the browser store, so current pins
migrate cleanly: {id, lat, lon, label, address}. ``label`` is the
display name (e.g. "Mom's house"); ``address`` is the geocoded
display address when the pin was saved from an address lookup
(otherwise ""). Legacy numeric ids are preserved as strings on the
first server round-trip (the id is only ever used for identity).
The ``valid()`` rules mirror static/pins.js.

Storage: one SQLite file, stdlib only. No new dependencies.
"""

import os
import sqlite3
import threading
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    id TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT ''
);
"""


def valid_pin(pin):
    """Mirror static/pins.js valid(): id + finite lat/lon in range."""
    if not isinstance(pin, dict):
        return False
    pin_id = pin.get("id")
    if isinstance(pin_id, bool):
        return False
    if isinstance(pin_id, int):
        if pin_id < 0 or pin_id > 2**53 - 1:
            return False
    elif isinstance(pin_id, str):
        if not pin_id.strip():
            return False
        if len(pin_id.strip()) > 128:
            return False
    else:
        return False
    try:
        lat = float(pin.get("lat"))
        lon = float(pin.get("lon"))
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:  # NaN
        return False
    return abs(lat) <= 90 and abs(lon) <= 180


def clean_pin(pin):
    """Normalise a pin dict for storage (id as text, strings trimmed)."""
    return {
        "id": str(pin.get("id")).strip(),
        "lat": float(pin.get("lat")),
        "lon": float(pin.get("lon")),
        "label": str(pin.get("label") or "").strip()[:200],
        "address": str(pin.get("address") or "").strip()[:300],
    }


def _short_id(pin_id):
    """Truncate an id for error messages (never echo it verbatim)."""
    return str(pin_id)[:40]


# Maximum pins the store will hold (Sec L8).
MAX_PINS = 1000
# Maximum length of a pin id (Sec L9).
MAX_PIN_ID_LEN = 128


class PinStore:
    """SQLite-backed pin list. One connection per call, serialised."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    def list_pins(self, shared=False):
        """All pins, oldest first.

        Raises sqlite3.Error if the database cannot be read (Rel C2/H1:
        a corrupt DB must surface as an error, never as an empty store).
        When shared=True the pins are not shared: returns [].
        """
        if shared:
            return []
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM pins ORDER BY created, id").fetchall()
                return [{
                    "id": r["id"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "label": r["label"],
                    "address": r["address"],
                    "created": r["created"],
                } for r in rows]
            finally:
                conn.close()

    def add_pin(self, pin, shared=False):
        """Insert one pin. Raises ValueError on bad input or duplicate id."""
        if shared:
            raise ValueError("Pins are not shared.")
        if not valid_pin(pin):
            raise ValueError("Pin needs an id and valid latitude/longitude.")
        clean = clean_pin(pin)
        today = date.today().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
                if count >= MAX_PINS:
                    raise ValueError(
                        f"Pin store is full ({MAX_PINS} pins).")
                row = conn.execute(
                    "SELECT id FROM pins WHERE id = ?", (clean["id"],)).fetchone()
                if row is not None:
                    raise ValueError(
                        f"A pin with id {_short_id(clean['id'])} already exists.")
                conn.execute(
                    "INSERT INTO pins (id, lat, lon, label, address, created, updated)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (clean["id"], clean["lat"], clean["lon"], clean["label"],
                     clean["address"], today, today),
                )
                conn.commit()
                return clean
            finally:
                conn.close()

    def update_pin(self, pin, shared=False):
        """Update one pin by id (label/address/coords). Raises ValueError."""
        if shared:
            raise ValueError("Pins are not shared.")
        if not valid_pin(pin):
            raise ValueError("Pin needs an id and valid latitude/longitude.")
        clean = clean_pin(pin)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id FROM pins WHERE id = ?", (clean["id"],)).fetchone()
                if row is None:
                    raise ValueError(f"No pin with id {_short_id(clean['id'])}.")
                conn.execute(
                    "UPDATE pins SET lat=?, lon=?, label=?, address=?, updated=?"
                    " WHERE id=?",
                    (clean["lat"], clean["lon"], clean["label"],
                     clean["address"], date.today().isoformat(), clean["id"]),
                )
                conn.commit()
                return clean
            finally:
                conn.close()

    def delete_pin(self, pin_id, shared=False):
        """Delete one pin by id. Raises ValueError if it does not exist."""
        if shared:
            raise ValueError("Pins are not shared.")
        if isinstance(pin_id, bool):
            raise ValueError("Pin id is required.")
        if pin_id is None or (isinstance(pin_id, str) and not pin_id.strip()):
            raise ValueError("Pin id is required.")
        pin_id = str(pin_id).strip()
        if len(pin_id) > MAX_PIN_ID_LEN:
            raise ValueError("Pin id is too long (max 128 characters).")
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM pins WHERE id = ?", (pin_id,))
                if cur.rowcount == 0:
                    raise ValueError(f"No pin with id {_short_id(pin_id)}.")
                conn.commit()
                return {"deleted": pin_id}
            finally:
                conn.close()

    def replace_all(self, pins, allow_shrink=False, shared=False):
        """Replace the whole pin list (explicit import-with-replace only).

        Validates every pin first; a single bad pin rejects the whole
        replacement so a corrupt file cannot wipe the store. Duplicate
        ids in the incoming list are rejected with ValueError (400),
        not an IntegrityError (500). Each pin keeps its existing
        ``created`` date, or a ``created`` date supplied in the payload;
        only genuinely new pins get today. Refuses (ValueError) a
        replacement SHORTER than the stored list unless allow_shrink
        is True, so a stale client cannot silently shrink the store.
        """
        if shared:
            raise ValueError("Pins are not shared.")
        if not isinstance(pins, list):
            raise ValueError("Pins must be a list.")
        if len(pins) > MAX_PINS:
            raise ValueError(f"Too many pins (max {MAX_PINS}).")
        clean = []
        seen = set()
        payload_created = {}
        for pin in pins:
            if not valid_pin(pin):
                raise ValueError("Every pin needs an id and valid latitude/longitude.")
            c = clean_pin(pin)
            if c["id"] in seen:
                raise ValueError(f"Duplicate pin id {_short_id(c['id'])}.")
            seen.add(c["id"])
            clean.append(c)
            if isinstance(pin, dict):
                created = pin.get("created")
                if isinstance(created, str) and created:
                    try:
                        date.fromisoformat(created)
                    except ValueError:
                        pass
                    else:
                        payload_created[c["id"]] = created
        today = date.today().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                stored = conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
                if not allow_shrink and len(clean) < stored:
                    raise ValueError(
                        "Replacement is shorter than the stored pin list; "
                        "pass replace:true for an explicit replace.")
                existing_created = {
                    r["id"]: r["created"]
                    for r in conn.execute("SELECT id, created FROM pins").fetchall()
                }
                conn.execute("DELETE FROM pins")
                result = []
                for pin in clean:
                    created = (existing_created.get(pin["id"])
                               or payload_created.get(pin["id"]) or today)
                    conn.execute(
                        "INSERT INTO pins (id, lat, lon, label, address, created, updated)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (pin["id"], pin["lat"], pin["lon"], pin["label"],
                         pin["address"], created, today),
                    )
                    result.append({**pin, "created": created})
                conn.commit()
                return result
            finally:
                conn.close()

    def import_pins(self, pins, replace=False, shared=False):
        """Merge pins in (import/backup restore).

        Merge (default): adds pins whose id is not already stored and
        skips duplicates. Replace: wipes the store first. Returns
        {"added": n, "skipped": n, "pins": [...]}. The existence check
        runs INSIDE the locked transaction (same connection as the
        INSERTs), so a concurrent add cannot fail the whole import.
        """
        if shared:
            raise ValueError("Pins are not shared.")
        if not isinstance(pins, list):
            raise ValueError("Pins must be a list.")
        if len(pins) > MAX_PINS:
            raise ValueError(f"Too many pins (max {MAX_PINS}).")
        for pin in pins:
            if not valid_pin(pin):
                raise ValueError("Every pin needs an id and valid latitude/longitude.")
        if replace:
            clean = self.replace_all(pins, allow_shrink=True)
            return {"added": len(clean), "skipped": 0, "pins": clean}
        # Clean + dedupe the incoming list up front (duplicate ids in
        # one import are a client error, not a 500).
        clean = []
        seen = set()
        for pin in pins:
            c = clean_pin(pin)
            if c["id"] in seen:
                raise ValueError(f"Duplicate pin id {_short_id(c['id'])}.")
            seen.add(c["id"])
            clean.append(c)
        today = date.today().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                existing = {r["id"] for r in
                            conn.execute("SELECT id FROM pins").fetchall()}
                if len(existing) + len([p for p in clean
                                        if p["id"] not in existing]) > MAX_PINS:
                    raise ValueError(f"Too many pins (max {MAX_PINS}).")
                added = 0
                skipped = 0
                for pin in clean:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO pins"
                        " (id, lat, lon, label, address, created, updated)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (pin["id"], pin["lat"], pin["lon"], pin["label"],
                         pin["address"], today, today),
                    )
                    if cur.rowcount == 1:
                        added += 1
                    else:
                        skipped += 1
                conn.commit()
            finally:
                conn.close()
        return {"added": added, "skipped": skipped, "pins": self.list_pins()}
