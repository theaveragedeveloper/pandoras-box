"""Maps plug-in — offline place search over the self-hosted map data.

This is the "Place" source. It searches a local gazetteer — a small
database of named places (towns, hospitals, water sources, landmarks,
trailheads, landmarks) with coordinates — and returns matches with
lat/lon plus a link into the local map viewer.

The shipped gazetteer is fixtures/gazetteer-ne.json: 303 New England
places (Nov 13, 2026), curated by hand from USGS GNIS place names and
public gazetteer coordinates — every state capital and county seat,
cities and larger towns, major hospitals (incl. all Level 1 trauma
centers), major water bodies, high points, and airports, plus the
original Braintree-area entries. See fixtures/gazetteer-ne.README.md
for the source note and accuracy limits (town centers, ~1-3 km for
smaller towns; 12 towns geocoder-verified Nov 13, 2026). Same-name
towns in different states carry the state in their aliases, so
"manchester nh" and "manchester ct" find different places.

Data sources, in priority order (cfg "gazetteer"):
- a JSON file: [{"name", "type", "lat", "lon", "county", "note"}, ...]
  (fixtures/gazetteer-ne.json ships as the New England set)
- a CSV file with columns: name, type, lat, lon, county, note
  (e.g. an export from USGS GNIS, or a hand-built list)

Matching is token-based: every query token must appear in the name,
aliases, or type. Rank: name starts-with a token > name contains >
type/alias match. No network, no tile rendering — this is a name lookup,
not a map render, so it costs nothing on battery.

Each result carries a "viewer_url" built from cfg "viewer_url_template"
with {lat}, {lon} and {zoom} filled in — point it at the local tile
server (e.g. a TileServer-GL / pmtiles viewer on http://localhost:8081)
once that is running. Until then the link is empty and the result still
shows the coordinates.

Later: printable-map and ATAK/Garmin export pipelines (GAP_LIST items
7-8) are setup-time steps, not part of this plug-in.

Quad index (GAP_LIST item 2, wired in round 8): the plug-in also reads
a USGS topo quad index (cfg "quads", default fixtures/usgs-quads-ma.json)
— one entry per downloaded 7.5-minute quad with its bounding box. A
place result is annotated with the quad that covers it ("Braintree
quad"), and searching a quad name ("braintree quad") returns the quad
itself. Quads with a null bbox (not yet filled from the TNM Downloader
export) are still findable by name, but cannot annotate a place — the
result says "bbox pending download" instead of guessing.
"""

import csv
import json
import os
import re
import urllib.parse

from plugins.base import PluginBase

COORDINATES = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*")


class Plugin(PluginBase):
    id = "maps"
    name = "Maps & Places"
    badge = "Place"
    kind = "maps"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.gazetteer = self.data_path("gazetteer", "fixtures/places.json")
        self.viewer_url_template = cfg.get("viewer_url_template", "")
        self.quads_file = self.data_path("quads", "")
        self._places = None
        self._places_mtime = None
        self._quads = None
        self._quads_mtime = None

    # -- data ------------------------------------------------------------
    def _load(self):
        # Reload if the file changed since the last load (round 9):
        # swapping in a new gazetteer at setup must take effect without
        # a hub restart. A missing file caches as empty until it appears.
        try:
            mtime = os.path.getmtime(self.gazetteer)
        except OSError:
            mtime = None
        if self._places is not None and mtime == self._places_mtime:
            return self._places
        places = []
        try:
            if self.gazetteer.lower().endswith(".csv"):
                with open(self.gazetteer, newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        norm = self._normalise(row)
                        if norm is not None:
                            places.append(norm)
            else:
                with open(self.gazetteer, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                if not isinstance(data, list):
                    raise ValueError("gazetteer JSON is not a list")
                for row in data:
                    # One bad row (a stray string, a null) must not take
                    # the whole place source down.
                    if not isinstance(row, dict):
                        continue
                    norm = self._normalise(row)
                    if norm is not None:
                        places.append(norm)
        except (OSError, ValueError, KeyError):
            places = []
        self._places = places
        self._places_mtime = mtime
        return places

    @staticmethod
    def _normalise(row):
        try:
            lat = float(row.get("lat") or 0)
            lon = float(row.get("lon") or 0)
        except (TypeError, ValueError):
            return None
        if lat == 0.0 and lon == 0.0:
            # No usable coordinates — a place with no position is not
            # searchable as a place; skip it rather than pinning it
            # at Null Island (0, 0).
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            # A place with no name is not a place.
            return None
        return {
            "name": name,
            "type": str(row.get("type") or "Place").strip(),
            "lat": lat,
            "lon": lon,
            "county": str(row.get("county") or "").strip(),
            "note": str(row.get("note") or "").strip(),
            "aliases": str(row.get("aliases") or "").strip(),
            "public": row.get("public") is True,
        }

    def places(self, *, shared=False):
        places = self._load()
        if not shared:
            return places
        # Shared (hotspot) clients get only rows explicitly marked
        # public, and never a rally point or home-area row. Both
        # markers are checked as substrings across ALL text fields
        # (name, type, aliases, note, county), with "-" and spaces
        # stripped first so "home-area" and "homearea" match too.
        # The public flag is the primary gate; this text filter is
        # the second layer (audit 2026-11-21, Sec H1/M2).
        kept = []
        for p in places:
            if not p["public"]:
                continue
            text = " ".join([p["name"], p["type"], p["aliases"],
                             p["note"], p["county"]]).lower()
            squashed = text.replace("-", "").replace(" ", "")
            if "rally" in text or "homearea" in squashed:
                continue
            kept.append(p)
        return kept

    # -- quads -----------------------------------------------------------
    def _load_quads(self):
        """USGS topo quad index: [{name, state, scale, bbox, file, note}].

        bbox is [west, south, east, north] or null until the TNM
        Downloader export fills it in. A quad with no bbox is still
        findable by name — it just cannot annotate a place yet.
        """
        if self._quads is not None:
            # Same mtime reload as _load (round 9): the quad index is
            # filled in from the TNM export at setup, and the hub must
            # pick it up without a restart.
            try:
                mtime = os.path.getmtime(self.quads_file) if self.quads_file else None
            except OSError:
                mtime = None
            if mtime == self._quads_mtime:
                return self._quads
        quads = []
        if self.quads_file:
            try:
                with open(self.quads_file, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                rows = data.get("quads", []) if isinstance(data, dict) else []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    norm = self._normalise_quad(row)
                    if norm is not None:
                        quads.append(norm)
            except (OSError, ValueError, KeyError):
                quads = []
        self._quads = quads
        try:
            self._quads_mtime = (
                os.path.getmtime(self.quads_file) if self.quads_file else None
            )
        except OSError:
            self._quads_mtime = None
        return quads

    @staticmethod
    def _normalise_quad(row):
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        bbox = row.get("bbox")
        if bbox is not None:
            try:
                west, south, east, north = (float(v) for v in bbox)
            except (TypeError, ValueError):
                return None
            if not (west < east and south < north):
                return None
            bbox = [west, south, east, north]
        return {
            "name": name,
            "state": str(row.get("state") or "").strip(),
            "scale": str(row.get("scale") or "1:24000").strip(),
            "bbox": bbox,
            "file": str(row.get("file") or "").strip(),
            "note": str(row.get("note") or "").strip(),
        }

    def _quad_for(self, lat, lon):
        """The quad whose bbox contains a point, or None."""
        for quad in self._load_quads():
            bbox = quad["bbox"]
            if bbox is None:
                continue
            west, south, east, north = bbox
            if west <= lon <= east and south <= lat <= north:
                return quad
        return None

    def _quad_result(self, quad):
        """One result for a quad matched by name."""
        bbox = quad["bbox"]
        if bbox is not None:
            west, south, east, north = bbox
            lat = (south + north) / 2
            lon = (west + east) / 2
            detail = (
                f"Topo quad · {quad['scale']} · {quad['state']} · "
                f"{west:.3f},{south:.3f} to {east:.3f},{north:.3f}"
            )
            url = ""
            if self.viewer_url_template:
                try:
                    url = self.viewer_url_template.format(
                        lat=lat, lon=lon, zoom=14
                    )
                except (KeyError, IndexError, ValueError):
                    url = ""
        else:
            # No bbox yet: no centre to point the viewer at, so no
            # link — the detail says so instead of guessing.
            lat = lon = None
            url = ""
            detail = f"Topo quad · {quad['scale']} · {quad['state']} · bbox pending download"
        if quad["file"]:
            detail += f" · {quad['file']}"
        pdf_url = ""
        if quad["file"]:
            pdf_url = "/pdf/quads/" + urllib.parse.quote(quad["file"])
        return {
            "title": f"{quad['name']} (USGS topo quad)",
            "snippet": quad["note"] or "7.5-minute topo quad, GeoPDF.",
            "url": url,
            "pdf_url": pdf_url,
            "badge": "Place",
            "detail": detail,
            "demo": False,
            "lat": lat,
            "lon": lon,
        }

    # -- contract --------------------------------------------------------
    def search(self, query, *, shared=False):
        coordinates = COORDINATES.fullmatch(query)
        if coordinates:
            lat, lon = map(float, coordinates.groups())
            if abs(lat) <= 90 and abs(lon) <= 180:
                return [{"title": f"{lat:g}, {lon:g}", "snippet": "Open these coordinates on the offline map.",
                         "url": f"/map?lat={lat}&lon={lon}&zoom=14", "badge": "Place",
                         "lat": lat, "lon": lon, "exact": True, "demo": False}]
            return []
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []
        scored = []
        places = self.places(shared=shared)
        for place in places:
            name = place["name"].lower()
            hay = name + " " + place["type"].lower() + " " + place["aliases"].lower()
            if not all(t in hay for t in tokens):
                continue
            if any(name.startswith(t) for t in tokens):
                score = 3
            elif any(t in name for t in tokens):
                score = 2
            else:
                score = 1
            scored.append((score, place))
        scored.sort(key=lambda sp: (-sp[0], sp[1]["name"]))
        out = []
        for _, place in scored[: self.max_results]:
            viewer = ""
            if self.viewer_url_template and place["lat"] and place["lon"]:
                try:
                    viewer = self.viewer_url_template.format(
                        lat=place["lat"], lon=place["lon"], zoom=14
                    )
                except (KeyError, IndexError, ValueError):
                    # A template with unknown {fields} must not kill
                    # the whole place search.
                    viewer = ""
            where = place["county"] or ""
            detail = (
                f"{place['type']} · {place['lat']:.4f}, {place['lon']:.4f}"
                + (f" · {where}" if where else "")
            )
            # Which USGS topo quad covers this place? Only answerable
            # once the quad bboxes are filled from the TNM export.
            quad = self._quad_for(place["lat"], place["lon"])
            if quad is not None:
                detail += f" · {quad['name']} quad"
            quad_pdf = ""
            if quad is not None and quad["file"]:
                quad_pdf = "/pdf/quads/" + urllib.parse.quote(quad["file"])
            out.append(
                {
                    "title": place["name"],
                    "snippet": place["note"]
                    or f"{place['type']}" + (f" — {where}" if where else ""),
                    "url": viewer,
                    "pdf_url": quad_pdf,
                    "badge": "Place",
                    "detail": detail,
                    "demo": False,
                    "lat": place["lat"],
                    "lon": place["lon"],
                }
            )
        # Quad name matches ("braintree quad") come after the places,
        # capped by max_results like everything else.
        # "braintree quad": no USGS quad is named Braintree — Braintree
        # is covered by the Blue Hills quad. When the query contains
        # "quad", resolve the named place to its covering quad via
        # _quad_for and return that quad (audit 2026-11-20). Dedupe
        # against the name-matching loop below (a query like
        # "blue hills quad" matches both paths).
        seen_quads = set()
        if len(out) < self.max_results and "quad" in tokens:
            place_tokens = [t for t in tokens if t != "quad"]
            if place_tokens:
                best = None
                for place in places:
                    name = place["name"].lower()
                    hay = name + " " + place["type"].lower() + " " + place["aliases"].lower()
                    if not all(t in hay for t in place_tokens):
                        continue
                    if any(name.startswith(t) for t in place_tokens):
                        score = 3
                    elif any(t in name for t in place_tokens):
                        score = 2
                    else:
                        score = 1
                    if best is None or score > best[0]:
                        best = (score, place)
                if best is not None:
                    quad = self._quad_for(best[1]["lat"], best[1]["lon"])
                    if quad is not None and quad["name"] not in seen_quads:
                        seen_quads.add(quad["name"])
                        out.append(self._quad_result(quad))
        if len(out) < self.max_results:
            for quad in self._load_quads():
                if quad["name"] in seen_quads:
                    continue
                hay = (
                    quad["name"].lower()
                    + " topo quad " + quad["state"].lower()
                    + " " + quad["note"].lower()
                )
                if all(t in hay for t in tokens):
                    seen_quads.add(quad["name"])
                    out.append(self._quad_result(quad))
                    if len(out) >= self.max_results:
                        break
        return out

    def status(self, *, shared=False):
        places = self._load()
        quads = self._load_quads()
        if places:
            detail = f"{len(places)} places in gazetteer"
            if quads:
                detail += f" · {len(quads)} topo quads indexed"
            if shared:
                detail = ""
            return {"available": True, "detail": detail}
        return {"available": False, "detail": "gazetteer not found or empty"}
