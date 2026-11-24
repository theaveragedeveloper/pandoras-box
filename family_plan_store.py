"""Store the household plan under the laptop user's home directory."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


MAX_ENTRIES = 25
SCHEMA = """CREATE TABLE IF NOT EXISTS family_plan (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL,
    plan TEXT NOT NULL
)"""


class PlanConflict(Exception):
    """Another editor saved the plan after this draft was opened."""


def _text(item, key, label, limit, *, required=False):
    value = item.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"Enter text for {label}.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"Enter {label}.")
    if len(value) > limit:
        raise ValueError(f"Use at most {limit} characters for {label}.")
    return value


def _rows(plan, key, label):
    rows = plan.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Send a valid list of {label}.")
    if len(rows) > MAX_ENTRIES:
        raise ValueError(f"Use at most {MAX_ENTRIES} {label}.")
    return rows


def _clean_plan(body):
    if not isinstance(body, dict):
        raise ValueError("Send a valid family plan.")
    contacts = [{
        "label": _text(row, "label", "a contact name", 120, required=True),
        "value": _text(row, "value", "a phone number or contact method", 200, required=True),
        "detail": _text(row, "detail", "contact notes", 300),
    } for row in _rows(body, "contacts", "contacts")]
    points = []
    for row in _rows(body, "meeting_points", "meeting points"):
        point = {"label": _text(row, "label", "a meeting point name", 120, required=True),
                 "address": _text(row, "address", "meeting point directions", 300)}
        lat, lon = row.get("lat"), row.get("lon")
        if lat is not None or lon is not None:
            if (type(lat) not in (int, float) or type(lon) not in (int, float)
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                raise ValueError("Enter a latitude from −90 to 90 and a longitude from −180 to 180.")
            point.update(lat=lat, lon=lon)
        points.append(point)
    return {"contacts": contacts, "meeting_points": points,
            "instructions": _text(body, "instructions", "household instructions", 3000)}


class FamilyPlanStore:
    def __init__(self, config_path):
        config_key = os.path.normcase(os.path.realpath(config_path))
        identifier = hashlib.sha256(os.fsencode(config_key)).hexdigest()[:32]
        self.path = Path.home() / ".pandora-box" / "family-plans" / (identifier + ".db")

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(SCHEMA)
            yield conn

    def read(self):
        try:
            self.path.stat()
        except FileNotFoundError:
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT revision, plan FROM family_plan WHERE id = 1").fetchone()
        return {**_clean_plan(json.loads(row[1])), "revision": row[0]} if row else None

    def save(self, body):
        expected = body.get("revision")
        if type(expected) is not int or not 0 <= expected < 2**53 - 1:
            raise ValueError("Reload the plan before saving.")
        plan = _clean_plan(body)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision FROM family_plan WHERE id = 1").fetchone()
            if (row[0] if row else 0) != expected:
                raise PlanConflict
            revision = expected + 1
            conn.execute("""INSERT INTO family_plan (id, revision, plan) VALUES (1, ?, ?)
                         ON CONFLICT(id) DO UPDATE SET revision = excluded.revision, plan = excluded.plan""",
                         (revision, json.dumps(plan)))
        return {**plan, "revision": revision}
