"""Supplies & Inventory plug-in — household stock, readiness, shopping list.

Adapted from BUNKR's resource-planning design (MrPatzy/BUNKR, AGPL-3.0):
``bunkr/gaps.py`` (readiness scoring against Ready.gov's "Build a Kit"
checklist) and ``bunkr/db.py`` (the items table and household settings).
The design is adapted, not copied: BUNKR is a multi-user hub with a
field-node sync model; this plug-in is a single-laptop SQLite store
inside the Mk1 search hub, stdlib only.

How it works (BUNKR's model, kept):
- Items live in one table: name, category, quantity, unit, pack_size,
  per_unit (gallons per single item for water, kcal per single item
  for food), essential (the Ready.gov checklist id the item counts
  toward), location, kit, expiry date, and a target quantity.
- Water and food are scored in *days of supply* for the household:
  water_gal / (people x gal/person/day), food_kcal / (people x
  kcal/person/day). Ready.gov: 1 gallon of water per person per day.
- The shopping list is the gaps: every scored line (water, food, each
  checklist essential) whose coverage is below its target, worst first.
- Expiry: expired items are removed from the usable stock; items
  expiring within the warning window are flagged. (BUNKR warns at
  60 days; the Mk1 warns at 30 days.)

Deliberately left out (BUNKR has them; the Mk1 does not need them):
- Firearms & ammunition tracking (BUNKR's admin-only SENSITIVE
  category) — not part of this household tracker.
- Barcode quick-add (needs a camera/scanner workflow).
- Scenario checklists / go-bag kit targets (BUNKR's kits.py).
- The document registry (Ready.gov "Gather Important Documents").
- BUNKR's 14-day default target: the Mk1 defaults to the Ready.gov
  basic-kit 3-day supply, which the owner can raise in settings.

Storage: one SQLite file (cfg "db", default inventory.db, resolved
relative to the hub base dir). No new dependencies — sqlite3 only.
The first open seeds a small, clearly-marked SAMPLE set so the tile
shows something real to look at; the UI labels it as sample until the
owner replaces it with real stock. Seeding happens once (a meta flag),
so deleting the sample items never brings them back.

The plug-in is also a search source (badge "Supplies"): searching
"water" or "beans" finds the stock rows, like any other source.
"""

import math
from contextlib import closing
import os
import sqlite3
import threading
from datetime import date, timedelta

from plugins.base import PluginBase

# Ready.gov "Build a Kit" categories (BUNKR's CATEGORIES, minus
# "firearms" — deliberately not tracked here).
CATEGORIES = {
    "water": "Water",
    "food": "Food",
    "medical": "Medical & hygiene",
    "power": "Power & light",
    "comms": "Communication & navigation",
    "shelter": "Shelter, warmth & tools",
    "sanitation": "Sanitation",
    "documents": "Documents & cash",
    "vehicle": "Vehicle",
    "other": "Other",
}

# Ready.gov "Build a Kit" essentials (BUNKR's ESSENTIALS, minus the
# household-specific ones: infant_supplies, pet_food, glasses, rx_meds
# — those are still taggable via OPTIONAL_ESSENTIALS, but they do not
# create gaps for a 2-adult household that does not track them).
# scale: "household" (fixed count) or "person" (count per person).
ESSENTIALS = [
    {"id": "first_aid_kit", "category": "medical", "label": "First aid kit", "qty": 1, "scale": "household"},
    {"id": "otc_meds", "category": "medical", "label": "Pain relievers, antidiarrheal, antacid", "qty": 1, "scale": "household"},
    {"id": "masks", "category": "medical", "label": "Dust / N95 masks", "qty": 2, "scale": "person"},
    {"id": "hygiene", "category": "medical", "label": "Soap, hand sanitizer, feminine supplies", "qty": 1, "scale": "household"},
    {"id": "flashlight", "category": "power", "label": "Flashlight", "qty": 1, "scale": "person"},
    {"id": "batteries", "category": "power", "label": "Spare battery packs (sets)", "qty": 2, "scale": "household"},
    {"id": "power_bank", "category": "power", "label": "Phone charger + backup battery", "qty": 1, "scale": "household"},
    {"id": "radio", "category": "comms", "label": "Battery / hand-crank NOAA weather radio", "qty": 1, "scale": "household"},
    {"id": "whistle", "category": "comms", "label": "Whistle to signal for help", "qty": 1, "scale": "person"},
    {"id": "paper_maps", "category": "comms", "label": "Local paper maps", "qty": 1, "scale": "household"},
    {"id": "contact_list", "category": "comms", "label": "Printed emergency contact list", "qty": 1, "scale": "household"},
    {"id": "sheeting_tape", "category": "shelter", "label": "Plastic sheeting + duct tape", "qty": 1, "scale": "household"},
    {"id": "utility_tool", "category": "shelter", "label": "Wrench / pliers to shut off utilities", "qty": 1, "scale": "household"},
    {"id": "can_opener", "category": "shelter", "label": "Manual can opener", "qty": 1, "scale": "household"},
    {"id": "fire_extinguisher", "category": "shelter", "label": "Fire extinguisher", "qty": 1, "scale": "household"},
    {"id": "matches", "category": "shelter", "label": "Matches in waterproof container", "qty": 1, "scale": "household"},
    {"id": "bedding", "category": "shelter", "label": "Sleeping bag / warm blanket", "qty": 1, "scale": "person"},
    {"id": "clothing", "category": "shelter", "label": "Change of clothes + sturdy shoes", "qty": 1, "scale": "person"},
    {"id": "wipes", "category": "sanitation", "label": "Moist towelettes", "qty": 1, "scale": "household"},
    {"id": "garbage_bags", "category": "sanitation", "label": "Garbage bags + plastic ties", "qty": 1, "scale": "household"},
    {"id": "toilet_paper", "category": "sanitation", "label": "Toilet paper (rolls)", "qty": 4, "scale": "person"},
    {"id": "bleach", "category": "sanitation", "label": "Unscented bleach (disinfect / treat water)", "qty": 1, "scale": "household"},
    {"id": "cash", "category": "documents", "label": "Cash in small bills ($1 to $20)", "qty": 100, "scale": "dollars"},
]

# Taggable but not gap-scored (household-specific; BUNKR marks them
# optional). An item tagged with one of these still counts toward its
# own line if the owner tracks it, but it never creates a gap by itself.
OPTIONAL_ESSENTIALS = [
    {"id": "rx_meds", "category": "medical", "label": "Prescription medication (days of supply)", "qty": None, "scale": "days"},
    {"id": "glasses", "category": "medical", "label": "Spare glasses / contact solution", "qty": 1, "scale": "household"},
    {"id": "infant_supplies", "category": "food", "label": "Infant formula, bottles, diapers", "qty": 1, "scale": "household"},
    {"id": "pet_food", "category": "food", "label": "Pet food (days of supply)", "qty": None, "scale": "pet_days"},
]

ALL_ESSENTIALS = ESSENTIALS + OPTIONAL_ESSENTIALS
ESSENTIAL_IDS = {e["id"] for e in ALL_ESSENTIALS}

# Household settings (BUNKR's DEFAULT_SETTINGS, adapted): the Mk1
# defaults to the Ready.gov basic-kit 3-day supply; BUNKR defaults to
# 14 days. Water 1 gal/person/day and 2000 kcal/person/day are the
# same in both.
DEFAULT_SETTINGS = {
    "adults": 2,
    "children": 0,
    "pets": 0,
    "target_days": 3,
    "water_gal_per_person": 1.0,
    "water_gal_per_pet": 0.5,
    "kcal_per_person": 2000,
    "cash_per_person": 100,
}

EXPIRY_WARNING_DAYS = 30

# Maximum inventory items the store will hold (Sec L8).
MAX_ITEMS = 2000

# Ranges set_settings() enforces, reused to clamp on read (Sec L12).
SETTING_RANGES = {
    "adults": (0, 20), "children": (0, 20), "pets": (0, 20),
    "target_days": (1, 365),
    "water_gal_per_person": (0.1, 10),
    "water_gal_per_pet": (0, 5),
    "kcal_per_person": (500, 5000),
    "cash_per_person": (0, 10000),
}

# Gallons per unit, for water items with no per_unit set (BUNKR's GAL_PER).
GAL_PER = {"ml": 1 / 3785.411784, "L": 1 / 3.785411784, "fl oz": 1 / 128, "gal": 1.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '',
    pack_size REAL NOT NULL DEFAULT 1,
    per_unit REAL NOT NULL DEFAULT 0,
    size_unit TEXT NOT NULL DEFAULT '',
    essential TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    kit TEXT NOT NULL DEFAULT 'Home',
    expires TEXT NOT NULL DEFAULT '',
    target_qty REAL,
    counts_home INTEGER NOT NULL DEFAULT 1,
    sample INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Sample stock, seeded once on first open. Clearly marked sample=1 and
# labelled SAMPLE in the UI. Deliberately short of the 3-day target so
# the shopping list and the days-of-supply math are visible, and with
# one item expiring soon so the expiry flag shows. These are NOT the
# owner's real stock — replace them.
SAMPLE_ITEMS = [
    {"name": "Water — 1 gal jugs", "category": "water", "quantity": 4, "unit": "gal",
     "pack_size": 1, "per_unit": 1.0, "size_unit": "gal", "essential": "",
     "location": "Pantry", "kit": "Home", "expires": "", "notes": "SAMPLE — replace with your real stock"},
    {"name": "Canned beans", "category": "food", "quantity": 6, "unit": "cans",
     "pack_size": 1, "per_unit": 350, "size_unit": "kcal", "essential": "",
     "location": "Pantry", "kit": "Home", "expires": "", "notes": "SAMPLE — replace with your real stock"},
    {"name": "Rice, 5 lb bag", "category": "food", "quantity": 1, "unit": "bag",
     "pack_size": 1, "per_unit": 8000, "size_unit": "kcal", "essential": "",
     "location": "Pantry", "kit": "Home", "expires": "", "notes": "SAMPLE — replace with your real stock"},
    {"name": "Canned soup", "category": "food", "quantity": 4, "unit": "cans",
     "pack_size": 1, "per_unit": 200, "size_unit": "kcal", "essential": "",
     "location": "Pantry", "kit": "Home", "expires": "+10d", "notes": "SAMPLE — replace with your real stock"},
    {"name": "First aid kit", "category": "medical", "quantity": 1, "unit": "kit",
     "pack_size": 1, "per_unit": 0, "size_unit": "", "essential": "first_aid_kit",
     "location": "Bathroom", "kit": "Home", "expires": "", "notes": "SAMPLE — replace with your real stock"},
    {"name": "Flashlight", "category": "power", "quantity": 1, "unit": "each",
     "pack_size": 1, "per_unit": 0, "size_unit": "", "essential": "flashlight",
     "location": "Go bag", "kit": "Go bag", "expires": "", "notes": "SAMPLE — replace with your real stock"},
]


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _count(item):
    """How many single things an item stands for: 2 cases of 24 = 48."""
    try:
        qty = float(item.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0
    try:
        pack = float(item.get("pack_size") or 1)
    except (TypeError, ValueError):
        pack = 1
    if pack <= 0:
        pack = 1
    return qty * pack


def _water_gallons(item):
    """Gallons of water one item stands for (BUNKR's per_unit model)."""
    if item.get("category") != "water":
        return 0.0
    try:
        per = float(item.get("per_unit") or 0)
    except (TypeError, ValueError):
        per = 0.0
    if per > 0:
        return _count(item) * per
    # No per-unit size: convert from the unit itself (gal/L/fl oz/ml).
    unit = (item.get("unit") or "").strip()
    factor = GAL_PER.get(unit)
    if factor is None:
        return 0.0
    return _count(item) * factor


def _food_kcal(item):
    """Kcal of food one item stands for (BUNKR's per_unit model)."""
    if item.get("category") != "food":
        return 0.0
    try:
        per = float(item.get("per_unit") or 0)
    except (TypeError, ValueError):
        per = 0.0
    if per <= 0:
        return 0.0
    return _count(item) * per


def _target_qty(item):
    """An item's per-item target quantity (>0), or 0 if unset/invalid."""
    try:
        value = float(item.get("target_qty") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _is_unmeasured(item):
    """True when an item's stock silently counts as 0 (Tech L1).

    Water with no per-unit size converts from the unit via GAL_PER;
    a unit outside that table counts as nothing. Food has no unit
    conversion table, so a food item with no per-unit kcal counts
    as nothing too.
    """
    try:
        per = float(item.get("per_unit") or 0)
    except (TypeError, ValueError):
        per = 0.0
    if per > 0:
        return False
    if item.get("category") == "water":
        return (item.get("unit") or "").strip() not in GAL_PER
    if item.get("category") == "food":
        return True
    return False


def _dollars(item):
    """Cash value of one item (BUNKR's dollars(): bills x value each)."""
    try:
        per = float(item.get("per_unit") or 0)
    except (TypeError, ValueError):
        per = 0.0
    return _count(item) * per if per else _count(item)


class Plugin(PluginBase):
    id = "inventory"
    name = "Supplies & Inventory"
    badge = "Supplies"
    kind = "inventory"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.db_path = self.data_path("db", "inventory.db")
        if not self.db_path:
            # An empty path would open a throwaway temp DB (Sec L19):
            # writes would vanish and every read would re-seed.
            raise ValueError("Inventory database path is empty.")
        self._lock = threading.Lock()

    # -- storage ---------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            keys = {row[0] for row in conn.execute("SELECT key FROM settings")}
            seeded = conn.execute("SELECT 1 FROM meta WHERE key = 'seeded'").fetchone()
            if not seeded or not DEFAULT_SETTINGS.keys() <= keys:
                conn.execute("BEGIN IMMEDIATE")
                self._ensure_settings(conn)
                self._ensure_seed(conn)
                conn.commit()
            return conn
        except sqlite3.Error:
            conn.close()
            raise

    def _ensure_settings(self, conn):
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    def _ensure_seed(self, conn):
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        if row is not None:
            return  # seeded once; deleting the samples never brings them back
        today = date.today()
        for item in SAMPLE_ITEMS:
            expires = item.get("expires", "")
            if expires == "+10d":
                expires = (today + timedelta(days=10)).isoformat()
            conn.execute(
                "INSERT INTO items (name, category, quantity, unit, pack_size,"
                " per_unit, size_unit, essential, location, kit, expires,"
                " counts_home, sample, notes, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)",
                (item["name"], item["category"], item["quantity"], item["unit"],
                 item.get("pack_size", 1), item.get("per_unit", 0),
                 item.get("size_unit", ""), item.get("essential", ""),
                 item.get("location", ""), item.get("kit", "Home"),
                 expires, item.get("notes", ""),
                 today.isoformat()),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")

    @staticmethod
    def _row_to_item(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "quantity": row["quantity"],
            "unit": row["unit"],
            "pack_size": row["pack_size"],
            "per_unit": row["per_unit"],
            "size_unit": row["size_unit"],
            "essential": row["essential"],
            "location": row["location"],
            "kit": row["kit"],
            "expires": row["expires"],
            "target_qty": row["target_qty"],
            "counts_home": bool(row["counts_home"]),
            "sample": bool(row["sample"]),
            "notes": row["notes"],
            "updated": row["updated"],
        }

    def _read_items(self, conn):
        return [self._row_to_item(row) for row in conn.execute(
            "SELECT * FROM items ORDER BY category, name")]

    def list_items(self, shared=False):
        """All items, ordered by category then name.

        Raises sqlite3.Error if the database cannot be read (Rel C2:
        a corrupt DB must surface as an error, never as an empty
        stock list). When shared=True the inventory is not shared:
        returns [].
        """
        if shared:
            return []
        with self._lock, closing(self._connect()) as conn:
            return self._read_items(conn)

    @staticmethod
    def _normalise_settings(settings):
        """Coerce + clamp settings to the types/ranges the math expects.

        A hand-edited DB row (negative, huge, NaN) must not make
        summary() report negative/absurd days of supply (Sec L12).
        """
        for key in ("adults", "children", "pets", "target_days"):
            low, high = SETTING_RANGES[key]
            try:
                value = int(settings[key])
            except (TypeError, ValueError):
                value = DEFAULT_SETTINGS[key]
            settings[key] = max(low, min(high, value))
        for key in ("water_gal_per_person", "water_gal_per_pet",
                    "kcal_per_person", "cash_per_person"):
            low, high = SETTING_RANGES[key]
            try:
                value = float(settings[key])
            except (TypeError, ValueError):
                value = DEFAULT_SETTINGS[key]
            if not math.isfinite(value):
                value = DEFAULT_SETTINGS[key]
            settings[key] = max(low, min(high, value))
        settings["adults"] = max(1, settings["adults"]) if settings["adults"] + settings["children"] < 1 else settings["adults"]
        return settings

    def _read_settings(self, conn):
        settings = dict(DEFAULT_SETTINGS)
        for row in conn.execute("SELECT key, value FROM settings"):
            key = row["key"]
            if key not in settings:
                continue
            raw = row["value"]
            try:
                settings[key] = float(raw) if "." in str(raw) else int(raw)
            except (TypeError, ValueError):
                settings[key] = raw
        return self._normalise_settings(settings)

    def get_settings(self):
        """Household settings. Raises sqlite3.Error on DB failure."""
        with self._lock, closing(self._connect()) as conn:
            return self._read_settings(conn)

    def _snapshot(self):
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN")
            return self._read_items(conn), self._read_settings(conn)

    def set_settings(self, values):
        """Validate + store household settings. Raises ValueError on bad input."""
        allowed = {
            "adults": (int, 0, 20), "children": (int, 0, 20), "pets": (int, 0, 20),
            "target_days": (int, 1, 365),
            "water_gal_per_person": (float, 0.1, 10),
            "water_gal_per_pet": (float, 0, 5),
            "kcal_per_person": (float, 500, 5000),
            "cash_per_person": (float, 0, 10000),
        }
        clean = {}
        for key, value in (values or {}).items():
            if key not in allowed:
                continue
            kind, low, high = allowed[key]
            if isinstance(value, bool):
                raise ValueError(f"Setting {key} must be a number.")
            if kind is int:
                # int(2.5) silently truncates and int(True) == 1 (Sec L20):
                # int fields must arrive as real ints.
                if type(value) is not int:
                    raise ValueError(f"Setting {key} must be a whole number.")
                number = value
            else:
                if type(value) not in (int, float):
                    raise ValueError(f"Setting {key} must be a number.")
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError(f"Setting {key} must be a number.")
            if not low <= number <= high:
                raise ValueError(f"Setting {key} must be between {low} and {high}.")
            clean[key] = number
        if not clean:
            raise ValueError("No valid settings given.")
        if "adults" in clean or "children" in clean:
            adults = clean.get("adults", self.get_settings()["adults"])
            children = clean.get("children", self.get_settings()["children"])
            if adults + children < 1:
                raise ValueError("The household needs at least one person.")
        with self._lock:
            conn = self._connect()
            try:
                for key, value in clean.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (key, str(value)),
                    )
                conn.commit()
            finally:
                conn.close()
        return self.get_settings()

    # -- readiness (BUNKR's gaps.analyze, simplified) --------------------
    def summary(self):
        """Days of water/food, essentials coverage, gaps, expiry watch.

        Read items and settings from one consistent database snapshot.
        Raises sqlite3.Error if the database cannot be read.
        """
        return self._summarize(*self._snapshot())

    @staticmethod
    def _summarize(items, settings):
        today = date.today()
        soon = today + timedelta(days=EXPIRY_WARNING_DAYS)
        people = max(1, int(settings["adults"]) + int(settings["children"]))
        pets = int(settings["pets"])
        target_days = max(1, int(settings["target_days"]))

        expired, expiring, usable = [], [], []
        for item in items:
            exp = _parse_date(item.get("expires"))
            if exp and exp < today:
                expired.append(item)
                continue
            if exp and exp <= soon:
                expiring.append(item)
            usable.append(item)
        home = [i for i in usable if i.get("counts_home")]
        home_targets = [i for i in items if i.get("counts_home")]
        usable_ids = {item["id"] for item in home}

        water_gal = sum(_water_gallons(i) for i in home)
        water_need_day = (people * float(settings["water_gal_per_person"])
                          + pets * float(settings["water_gal_per_pet"]))
        water_days = water_gal / water_need_day if water_need_day else 0.0
        kcal = sum(_food_kcal(i) for i in home)
        kcal_day = people * float(settings["kcal_per_person"])
        food_days = kcal / kcal_day if kcal_day else 0.0

        def essential_target(ess):
            if ess["scale"] == "person":
                return ess["qty"] * people
            if ess["scale"] == "dollars":
                return float(settings.get("cash_per_person", 100)) * people
            return ess["qty"]

        lines = [
            {"id": "water", "category": "water", "label": "Drinking water",
             "have": round(water_days, 1), "need": target_days, "unit": "days",
             "detail": f"{water_gal:.1f} gal stored, household uses {water_need_day:.1f} gal/day"},
            {"id": "food", "category": "food", "label": "Non-perishable food",
             "have": round(food_days, 1), "need": target_days, "unit": "days",
             "detail": f"{kcal:,.0f} kcal stored, household needs {kcal_day:,.0f} kcal/day"},
        ]
        for ess in ESSENTIALS:
            need = essential_target(ess)
            if not need:
                continue
            # A per-item target_qty raises the bar for its essential
            # line: the line needs max(essential target, per-item
            # target) when target_qty is set (Tech L2).
            item_target = sum(
                _target_qty(i) for i in home_targets
                if i.get("essential") == ess["id"])
            need = max(need, item_target) if item_target > 0 else need
            if ess["scale"] == "dollars":
                have = sum(_dollars(i) for i in home if i.get("essential") == ess["id"])
            else:
                have = sum(_count(i) for i in home if i.get("essential") == ess["id"])
            lines.append({"id": ess["id"], "category": ess["category"],
                          "label": ess["label"], "have": have, "need": need,
                          "unit": "dollars" if ess["scale"] == "dollars" else "",
                          "detail": ""})
        # Per-item targets on untagged items (not water/food, whose
        # days lines already cover them): one gap line each.
        for item in home_targets:
            target = _target_qty(item)
            if target <= 0:
                continue
            if item.get("essential") or item.get("category") in ("water", "food"):
                continue
            lines.append({"id": f"target-{item['id']}",
                          "category": item.get("category", "other"),
                          "label": item["name"], "have": _count(item) if item["id"] in usable_ids else 0,
                          "need": target,
                          "unit": item.get("unit") or "",
                          "detail": "Per-item target"})
        # Items whose stock counts as 0.0 because the unit is outside
        # the conversion tables and no per-unit size was given
        # (Tech L1): flag them instead of silently undercounting.
        unmeasured = [
            {"id": i["id"], "name": i["name"]} for i in home
            if _is_unmeasured(i)
        ]
        for line in lines:
            line["coverage"] = min(1.0, line["have"] / line["need"]) if line["need"] else 1.0

        gaps = sorted(
            (dict(l, shortfall=round(l["need"] - l["have"], 1))
             for l in lines if l["coverage"] < 1),
            key=lambda l: l["coverage"],
        )
        water_cov = min(1.0, water_days / target_days) if target_days else 1.0
        food_cov = min(1.0, food_days / target_days) if target_days else 1.0
        ess_lines = [l for l in lines if l["id"] not in ("water", "food")]
        ess_cov = (sum(l["coverage"] for l in ess_lines) / len(ess_lines)) if ess_lines else 1.0
        overall = round(100 * (water_cov + food_cov + ess_cov) / 3)

        return {
            "overall": overall,
            "water_days": round(water_days, 1),
            "food_days": round(food_days, 1),
            "target_days": target_days,
            "water_gal": round(water_gal, 1),
            "water_need_day": round(water_need_day, 1),
            "food_kcal": round(kcal),
            "food_need_day": round(kcal_day),
            "people": people,
            "pets": pets,
            "lines": lines,
            "gaps": gaps,
            "expiring": expiring,
            "expired": expired,
            "unmeasured": unmeasured,
            "item_count": len(items),
            "sample": any(i.get("sample") for i in items),
        }

    def state(self, shared=False):
        """Everything the UI needs in one payload.

        Raises sqlite3.Error if the database cannot be read. When
        shared=True the inventory is not shared: returns an error dict.
        """
        if shared:
            return {"error": "Inventory is not shared."}
        items, settings = self._snapshot()
        by_location = {}
        for item in items:
            by_location.setdefault(item.get("location") or "Unsorted", []).append(item)
        return {
            "items": items,
            "by_location": by_location,
            "summary": self._summarize(items, settings),
            "settings": settings,
            "categories": CATEGORIES,
            "essentials": ALL_ESSENTIALS,
        }

    # -- writes ----------------------------------------------------------
    @staticmethod
    def _clean_number(value, field, low, high):
        """Validate one numeric field: reject bools and NaN/inf (Sec L7).

        Raises ValueError on bad input (including None).
        """
        if isinstance(value, bool):
            raise ValueError(f"{field} must be a number.")
        if value is None:
            raise ValueError(f"{field} must be a number.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a number.")
        if not math.isfinite(number):
            raise ValueError(f"{field} must be a number.")
        if number < low or number > high:
            raise ValueError(
                f"{field} must be between {low:,.0f} and {high:,.0f}.")
        return number

    @staticmethod
    def _clean_text(value, field, max_len, default=""):
        """Validate one text field: reject over-long values (Sec L20)."""
        text = str(value or "").strip()
        if not text:
            return default
        if len(text) > max_len:
            raise ValueError(f"{field} is too long (max {max_len} characters).")
        return text

    @staticmethod
    def _clean_item_fields(body):
        """Validate an add/update body. Returns a dict of clean fields.

        ``sample`` is None when the body does not mention it (update
        leaves it unchanged); otherwise 0/1 (Tech L2: an item can be
        un-flagged as sample through update).
        """
        if not isinstance(body, dict):
            raise ValueError("Item must be a JSON object.")
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("Give the item a name.")
        if len(name) > 200:
            raise ValueError("Name is too long (max 200 characters).")
        category = str(body.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            category = "other"
        quantity = Plugin._clean_number(
            body.get("quantity", 0), "Quantity", 0, 1000000)
        unit = Plugin._clean_text(body.get("unit"), "Unit", 20)
        pack_size = Plugin._clean_number(
            body.get("pack_size", 1), "Pack size", 1, 100000)
        per_unit = Plugin._clean_number(
            body.get("per_unit", 0), "Size per item", 0, 1000000)
        size_unit = Plugin._clean_text(body.get("size_unit"), "Size unit", 20)
        essential = str(body.get("essential") or "").strip()
        if essential and essential not in ESSENTIAL_IDS:
            essential = ""
        location = Plugin._clean_text(
            body.get("location"), "Location", 80, default="Unsorted")
        kit = Plugin._clean_text(body.get("kit"), "Kit", 80, default="Home")
        expires = str(body.get("expires") or "").strip()
        if expires:
            parsed = _parse_date(expires)
            if parsed is None:
                raise ValueError("Expiry must be a date (YYYY-MM-DD).")
            if parsed.year < 2000 or parsed.year > 2100:
                raise ValueError("Expiry year must be between 2000 and 2100.")
        target_qty = body.get("target_qty")
        if target_qty is not None and target_qty != "":
            target_qty = Plugin._clean_number(
                target_qty, "Target quantity", 0, 1000000)
        else:
            target_qty = None
        counts_home = body.get("counts_home", True)
        counts_home = bool(counts_home) if isinstance(counts_home, bool) else str(counts_home).lower() in ("1", "true", "yes")
        sample = body.get("sample")
        if sample is None:
            sample = None
        elif isinstance(sample, bool):
            sample = 1 if sample else 0
        elif str(sample).lower() in ("1", "true", "yes"):
            sample = 1
        elif str(sample).lower() in ("0", "false", "no"):
            sample = 0
        else:
            raise ValueError("Sample must be true or false.")
        notes = Plugin._clean_text(body.get("notes"), "Notes", 500)
        return {
            "name": name, "category": category, "quantity": quantity,
            "unit": unit, "pack_size": pack_size, "per_unit": per_unit,
            "size_unit": size_unit, "essential": essential,
            "location": location, "kit": kit, "expires": expires,
            "target_qty": target_qty, "counts_home": 1 if counts_home else 0,
            "sample": sample, "notes": notes,
        }

    @staticmethod
    def _clean_item_id(item_id):
        """Validate an item id before converting (Sec L11).

        Accepts an int or a string of digits only — int() also accepts
        "1_0", whitespace and True, and int(float("inf")) raises
        OverflowError, so validate first. Returns the int id.
        """
        if isinstance(item_id, bool):
            raise ValueError("Item id must be a number.")
        if isinstance(item_id, int):
            value = item_id
        elif isinstance(item_id, str):
            text = item_id.strip()
            if not text.isdigit():
                raise ValueError("Item id must be a number.")
            try:
                value = int(text)
            except (ValueError, OverflowError):
                raise ValueError("Item id must be a number.")
        else:
            raise ValueError("Item id must be a number.")
        if value < 1 or value > 2**63 - 1:
            raise ValueError("Item id is out of range.")
        return value

    def add_item(self, body):
        fields = self._clean_item_fields(body)
        with self._lock:
            conn = self._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                if count >= MAX_ITEMS:
                    raise ValueError(
                        f"Inventory is full ({MAX_ITEMS} items).")
                cur = conn.execute(
                    "INSERT INTO items (name, category, quantity, unit, pack_size,"
                    " per_unit, size_unit, essential, location, kit, expires,"
                    " target_qty, counts_home, sample, notes, updated)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (fields["name"], fields["category"], fields["quantity"],
                     fields["unit"], fields["pack_size"], fields["per_unit"],
                     fields["size_unit"], fields["essential"], fields["location"],
                     fields["kit"], fields["expires"], fields["target_qty"],
                     fields["counts_home"], fields["notes"], date.today().isoformat()),
                )
                item_id = cur.lastrowid
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                return self._row_to_item(row)
            finally:
                conn.close()

    def update_item(self, item_id, body):
        item_id = self._clean_item_id(item_id)
        fields = self._clean_item_fields(body)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, sample FROM items WHERE id = ?", (item_id,)).fetchone()
                if row is None:
                    raise ValueError(f"No item with id {str(item_id)[:40]}.")
                sample = fields["sample"] if fields["sample"] is not None else row["sample"]
                conn.execute(
                    "UPDATE items SET name=?, category=?, quantity=?, unit=?,"
                    " pack_size=?, per_unit=?, size_unit=?, essential=?,"
                    " location=?, kit=?, expires=?, target_qty=?, counts_home=?,"
                    " sample=?, notes=?, updated=? WHERE id=?",
                    (fields["name"], fields["category"], fields["quantity"],
                     fields["unit"], fields["pack_size"], fields["per_unit"],
                     fields["size_unit"], fields["essential"], fields["location"],
                     fields["kit"], fields["expires"], fields["target_qty"],
                     fields["counts_home"], sample, fields["notes"], date.today().isoformat(),
                     item_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                return self._row_to_item(row)
            finally:
                conn.close()

    def delete_item(self, item_id):
        item_id = self._clean_item_id(item_id)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                if cur.rowcount == 0:
                    raise ValueError(f"No item with id {str(item_id)[:40]}.")
                conn.commit()
                return {"deleted": item_id}
            finally:
                conn.close()

    def adjust_quantity(self, item_id, delta):
        """Use (-1) or restock (+1) one unit of an item.

        "Use" consumes one unit as displayed (a can, a gallon, a bag);
        it floors at zero rather than going negative. "Restock" adds
        one unit. For a packed item (e.g. a case of 24) one unit is
        the pack — edit the quantity directly for single bottles.
        Returns the updated item.
        """
        item_id = self._clean_item_id(item_id)
        # delta=True passes `delta in (-1, 1)` (True == 1) and 1.0
        # does too (Sec L13): require a real int.
        if type(delta) is not int or delta not in (-1, 1):
            raise ValueError("Quantity change must be +1 or -1.")
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                if row is None:
                    raise ValueError(f"No item with id {str(item_id)[:40]}.")
                new_qty = max(0.0, float(row["quantity"]) + delta)
                conn.execute(
                    "UPDATE items SET quantity=?, updated=? WHERE id=?",
                    (new_qty, date.today().isoformat(), item_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                return self._row_to_item(row)
            finally:
                conn.close()

    def use_item(self, item_id):
        return self.adjust_quantity(item_id, -1)

    def restock_item(self, item_id):
        return self.adjust_quantity(item_id, 1)

    # -- plug-in contract --------------------------------------------------
    def search(self, query, shared=False):
        """Search stock rows. When shared=True the inventory is not
        shared: returns []. A DB failure also returns [] (the hub
        reports it via status()); it never raises out of search()."""
        if shared:
            return []
        if not isinstance(query, str):
            return []
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []
        try:
            items = self.list_items()
        except sqlite3.Error:
            return []
        out = []
        for item in items:
            hay = " ".join([
                item.get("name", ""), item.get("category", ""),
                item.get("location", ""), item.get("kit", ""),
                item.get("notes", ""),
            ]).lower()
            if not all(t in hay for t in tokens):
                continue
            detail_bits = []
            if item.get("location"):
                detail_bits.append(item["location"])
            if item.get("quantity") is not None:
                qty = item["quantity"]
                unit = item.get("unit") or ""
                detail_bits.append(f"{qty:g} {unit}".strip())
            if item.get("expires"):
                detail_bits.append(f"expires {item['expires']}")
            if item.get("sample"):
                detail_bits.append("SAMPLE — replace with real stock")
            out.append({
                "title": item["name"],
                "snippet": " · ".join(detail_bits),
                "url": "",
                "badge": "Supplies",
                "detail": CATEGORIES.get(item.get("category"), item.get("category", "")),
                "demo": False,
            })
            if len(out) >= self.max_results:
                break
        return out

    def status(self, shared=False):
        if shared:
            return {"available": False, "detail": "Inventory is not shared"}
        try:
            summary = self.summary()
        except sqlite3.Error:
            return {"available": False, "detail": "Inventory database unavailable"}
        detail = (f"{summary['water_days']} days of water, "
                  f"{summary['food_days']} days of food "
                  f"(target {summary['target_days']} days)")
        if summary["expiring"]:
            detail += f" · {len(summary['expiring'])} expiring soon"
        if summary["expired"]:
            detail += f" · {len(summary['expired'])} expired"
        if summary["sample"]:
            detail += " · SAMPLE data — replace with real stock"
        return {"available": True, "detail": detail}
