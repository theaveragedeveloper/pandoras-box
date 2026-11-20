#!/usr/bin/env python3
"""Search Hub — one search box over all offline sources on the 5424.

Runs on the laptop (Windows 11, Python 3.10+) with the standard library
only — no pip installs, nothing to download. It fans a query out to every
enabled plug-in (Kiwix ZIMs, Recoll PDFs, offline maps, own files) and
merges the results on one page, grouped by source.

Usage (on the laptop, from this folder):
    python hub.py                 (uses config.json, binds 127.0.0.1:8899)
    python hub.py --serve         (binds 0.0.0.0:8899 — serve the phone
                                   over the laptop's Wi-Fi hotspot)
    python hub.py --port 8899 --host 127.0.0.1
    python hub.py --config config.json --no-demo
    python hub.py --demo          (force sample-data fallback on)

Default bind is localhost-only (audit flaw #7, round 4/5): the hub is
only reachable on the LAN / hotspot in serving mode, started
explicitly with --serve (or --host 0.0.0.0). In serving mode, absolute
filesystem paths are stripped from results sent to non-localhost
clients, so hotspot clients see file names, not the laptop's tree.

First run on Windows will prompt for firewall access — allow it on
private networks so the phone on the laptop's Wi-Fi hotspot can reach
the hub. The hub itself makes no outbound network calls, ever.

Endpoints:
    GET  /                the search UI (static/ui.html)
    GET  /api/search?q=   JSON: {query, answer: {title, text, url} |
                          groups: [{source_id, name,
                          available, note, results: [{title, snippet,
                          url, badge, detail, demo}]}]}
                          (answer is the lead of the top Kiwix article
                          — the direct answer — when one could be read)
    GET  /api/status      JSON: per-source availability + counts
    GET  /api/config      JSON: the configured sources (no secrets)
    POST /api/sources     add a source: {"name", "plugin", ...params}
                          appends to config.json and reloads the registry

Results are grouped by source, not one merged ranking — that is an
honest limit: a map place and a manual passage are not rankable against
each other. A plug-in that is down never fails the whole search; its
group shows as unavailable.
"""

import argparse
import copy
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from plugins import load_plugins  # noqa: E402
import geocode as geocode_mod  # noqa: E402  (stdlib-only address search)

LOG_PATH = os.path.join(BASE_DIR, "hub.log")
# The actual port the hub bound to (after any port fallback) — written
# at startup so the watchdog and serve_toggle.ps1 can find the hub even
# when it took the next port because 8899 was busy (round 9).
PORT_FILE = os.path.join(BASE_DIR, "hub.port")
# Cap a single /tiles/ response chunk (round 9): a Range like bytes=0-
# on a 3 GB extract must not try to read the whole remainder into RAM.
TILE_CHUNK_MAX_BYTES = 8 * 1024 * 1024
_LOG_LOCK = threading.Lock()


def _log(msg):
    """Append one timestamped line to hub.log (audit flaw #14, round 4).

    The console stays quiet (log_message is a no-op) but a dead hub
    leaves a trace. Never raises — a logging failure must not kill a
    search. (Round 9: the lock keeps concurrent threads from
    interleaving lines, and the broad except covers even a msg whose
    str() itself raises.)
    """
    try:
        line = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            + " " + str(msg) + "\n"
        )
        with _LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


class _LeadParser(HTMLParser):
    """Extract the lead paragraphs of a Wikipedia article page.

    Collects the text of the first <p> elements inside the
    mw-parser-output content div, stopping at the first <h2> (the end
    of the lead section). Citation superscripts are skipped, so the
    result reads as a plain summary. Any parse problem just yields
    fewer paragraphs — it never raises.
    """

    def __init__(self):
        super().__init__()
        self.paras = []
        self._in_content = False
        self._depth = 0
        self._in_p = False
        self._in_sup = False
        self._cur = []
        self._hit_h2 = False

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            d = dict(attrs)
            if "mw-parser-output" in d.get("class", "") and not self._in_content:
                self._in_content = True
                self._depth = 1
                return
            if self._in_content:
                self._depth += 1
        if not self._in_content or self._hit_h2:
            return
        if tag == "h2":
            self._hit_h2 = True
            return
        if tag == "p" and not self._in_p:
            self._in_p = True
            self._cur = []
        if tag == "sup":
            self._in_sup = True

    def handle_endtag(self, tag):
        if tag == "div" and self._in_content:
            self._depth -= 1
            if self._depth <= 0:
                self._in_content = False
        if tag == "p" and self._in_p:
            self._in_p = False
            text = re.sub(r"\s+", " ", "".join(self._cur)).strip()
            text = re.sub(r"\[\d+\]", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 80:
                self.paras.append(text)
            self._cur = []
        if tag == "sup":
            self._in_sup = False

    def handle_data(self, data):
        if self._in_p and self._in_content and not self._hit_h2 and not self._in_sup:
            self._cur.append(data)


# Cap an article fetch for the direct answer: the lead is near the top,
# so 512 KB is far more than enough, and a runaway article must not
# fill RAM or blow the search deadline. A truncated read still parses
# — the lead paragraphs are complete well before the cut.
ANSWER_MAX_BYTES = 512 * 1024
ANSWER_TIMEOUT_SECONDS = 4


def _fetch_lead(base_url, href):
    """Fetch a Kiwix article and return its lead text, or None.

    href is the kiwix-serve path (e.g. /<zim>/A/Tourniquet). Returns the
    first one or two lead paragraphs joined, plain text, citations
    stripped. Any failure (unreachable, bad HTML, no lead) returns None
    so the search still works without an answer box.
    """
    url = base_url.rstrip("/") + href
    req = urllib.request.Request(url, headers={"User-Agent": "search-hub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=ANSWER_TIMEOUT_SECONDS) as resp:
            raw = resp.read(ANSWER_MAX_BYTES)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    html = raw.decode("utf-8", errors="replace")
    parser = _LeadParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - a bad article never kills a search
        return None
    paras = parser.paras[:2]
    if not paras:
        return None
    return " ".join(paras)


# Words that carry no meaning for picking the best article — question
# words and filler. Stripping them leaves the core noun the question
# is about (e.g. "what is a tourniquet" -> "tourniquet").
_ANSWER_STOPWORDS = {
    "what", "is", "are", "was", "were", "how", "to", "do", "does", "did",
    "a", "an", "the", "apply", "use", "using", "used", "get", "find",
    "where", "when", "why", "who", "whom", "which", "can", "could",
    "should", "would", "will", "shall", "may", "might", "i", "you", "we",
    "it", "this", "that", "these", "those", "of", "for", "in", "on",
    "with", "by", "at", "from", "and", "or", "my", "me", "please",
}


def _answer_score_phrase(phrase, title):
    """How well one phrase's core noun matches a result title.

    Higher is better. An exact title match for the core noun wins over
    a title that merely contains it, so "Tourniquet" beats "Stop the
    Bleeding (Tourniquet album)" for "what is a tourniquet".
    """
    core = [t for t in re.findall(r"[a-z0-9]+", phrase.lower())
            if t not in _ANSWER_STOPWORDS]
    if not core:
        return 0
    t = title.lower().strip()
    core_phrase = " ".join(core)
    if t == core_phrase:
        return 100
    if t.startswith(core_phrase):
        return 60
    words = set(re.findall(r"[a-z0-9]+", t))
    if set(core) <= words:
        return 40
    hits = len(words & set(core))
    if hits:
        return 10 + hits
    return 0


# Query expansion (query_expand.py, Part 1): the answer scorer takes
# the best score across the original query and its expansions, so
# "Suture" scores 100 for "how to give stitches" via the "suture"
# expansion. Fail-open: without the module, only the original query
# is scored, exactly as before.
try:
    from query_expand import expand_query as _expand_query
except ImportError:  # noqa: BLE001 - expansion is optional
    def _expand_query(query):
        return [query]


def _answer_score(query, title):
    """Best title match across the query and its expansions."""
    best = 0
    for variant in _expand_query(query):
        score = _answer_score_phrase(variant, title)
        if score > best:
            best = score
    return best


# Built-in fallback config (reliability, round 9): if config.json is
# missing or corrupt, the hub starts from this instead of refusing to
# start. It mirrors the shipped config.json — same five sources, demo
# OFF, localhost-only — so a fresh start behaves exactly like a normal
# start, just with no library-specific settings.
DEFAULT_CONFIG = {
    "demo_fallback": False,
    "host": "127.0.0.1",
    "port": 8899,
    "maps_dir": "C:\\Library\\maps",
    "library": {"zim_snapshots": {}, "recoll_index_date": ""},
    "sources": [
        {
            "badge": "Reference",
            "base_url": "http://127.0.0.1:8080",
            "enabled": True,
            "id": "kiwix",
            "max_results": 8,
            "medical_zims": ["wikimed"],
            "name": "Wikipedia & Reference",
            "plugin": "kiwix",
            "timeout_seconds": 5,
        },
        {
            "badge": "Manual",
            "config_dir": "",
            "enabled": True,
            "id": "recoll",
            "manual_markers": ["manual", "field-manual", "fm-", "tc-", "trex", "army"],
            "max_results": 8,
            "medical_markers": ["medical", "hesperian", "wikimed", "tccc", "doctor", "dentist"],
            "name": "Field Manuals & Documents",
            "plugin": "recoll",
            "timeout_seconds": 20,
        },
        {
            "badge": "Place",
            "enabled": True,
            "gazetteer": "fixtures/gazetteer-ne.json",
            "quads": "fixtures/usgs-quads-ma.json",
            "id": "maps",
            "max_results": 8,
            "name": "Maps & Places",
            "plugin": "maps",
            "viewer_url_template": "/map?lat={lat}&lon={lon}&zoom=14",
        },
        {
            "badge": "Radio",
            "data": "fixtures/frequencies.json",
            "enabled": True,
            "id": "frequencies",
            "max_results": 8,
            "name": "Repeaters & Frequencies",
            "plugin": "frequencies",
        },
        {
            "badge": "My Files",
            "enabled": True,
            "folder": "fixtures/myfiles",
            "id": "myfiles",
            "max_results": 8,
            "name": "My Files",
            "plugin": "myfiles",
        },
    ],
}


class Hub:
    """Owns config + plug-in registry; the HTTP handler calls into this."""

    def __init__(self, config_path, demo_fallback=None):
        self.config_path = os.path.abspath(config_path)
        self.lock = threading.Lock()
        self._started_at = time.monotonic()
        # Bound on concurrent HTTP searches (round 9): a burst of
        # requests must not spawn an unbounded pile of Recoll
        # subprocesses on the i5-8350U. Extra searches get a 503 with
        # a retry hint instead of queuing behind a hung backend.
        self._search_slots = threading.BoundedSemaphore(4)
        # Search-result cache (round 10): repeat queries skip the whole
        # fan-out. Key is the normalised query; TTL 10 min, capped at
        # 200 entries (oldest evicted). Stdlib only, no new service.
        # The cache is process memory — restarting the hub clears it.
        self._search_cache = OrderedDict()
        self._search_cache_lock = threading.Lock()
        # CLI override for demo_fallback (e.g. --no-demo). It is applied
        # inside load() after the file is read, so it survives reloads.
        self._demo_override = demo_fallback
        self._status_cache = None  # (monotonic time, status dict)
        self.load()

    def load(self):
        # Safe mode (round 9): a missing or corrupt config must never
        # stop the hub from starting. load() recovers (last-known-good,
        # then built-in defaults) instead of raising.
        try:
            config = self._read_config_file()
        except (OSError, ValueError) as exc:
            config = self._recover_config(exc)
        self._apply_config(config)

    def _read_config_file(self):
        with open(self.config_path, encoding="utf-8") as fh:
            config = json.load(fh)
        self._validate_config(config)
        return config

    @staticmethod
    def _validate_config(config):
        if not isinstance(config, dict):
            raise ValueError("config is not a JSON object")
        sources = config.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError("config 'sources' must be a list")
        host = config.get("host", "0.0.0.0")
        if not isinstance(host, str) or not host:
            raise ValueError(f"config 'host' must be a non-empty string: {host!r}")
        try:
            port = int(config.get("port", 8899))
        except (TypeError, ValueError):
            raise ValueError(f"config 'port' is not a number: {config.get('port')!r}")
        if not 0 <= port <= 65535:
            raise ValueError(f"config 'port' out of range: {port}")

    def _recover_config(self, exc):
        """Config file missing or corrupt: back it up, try the
        last-known-good copy, else fall back to built-in defaults.

        The corrupt file is preserved (never deleted) under a
        timestamped name, and a usable last-known-good copy is written
        back over the main file so the next start is clean.
        """
        if os.path.exists(self.config_path):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            backup = f"{self.config_path}.corrupt-{stamp}"
            try:
                shutil.copyfile(self.config_path, backup)
                _log(f"config unreadable ({exc}); backed up to {backup}")
            except OSError:
                _log(f"config unreadable ({exc}); could not back it up")
        else:
            _log(f"config file missing ({exc}); starting from defaults")
        good_path = self.config_path + ".good"
        try:
            with open(good_path, encoding="utf-8") as fh:
                config = json.load(fh)
            self._validate_config(config)
            _log(f"config restored from last-known-good copy ({good_path})")
            try:
                self._atomic_write(self.config_path, config)
            except OSError:
                pass  # running from the good copy in memory is enough
            return config
        except (OSError, ValueError):
            pass
        _log("no usable last-known-good config; starting from built-in defaults")
        return copy.deepcopy(DEFAULT_CONFIG)

    def _apply_config(self, config):
        config["sources"] = config.get("sources", [])
        config["port"] = int(config.get("port", 8899))
        # Effective demo flag: the CLI override (--no-demo) wins over
        # the file, but it is NOT written back to the file — it stays
        # a runtime choice, so adding a source later cannot make it
        # permanent by accident.
        if self._demo_override is not None:
            self.demo_fallback = bool(self._demo_override)
        else:
            self.demo_fallback = bool(config.get("demo_fallback", True))
        self.config = config
        ctx = {
            "base_dir": BASE_DIR,
            "demo_fallback": self.demo_fallback,
        }
        self.plugins = load_plugins(self.config, ctx)
        self._status_cache = None
        self._library_roots_cache = None
        # A config change can change what a search returns, so the
        # cached fan-outs are stale — drop them (round 10).
        self.clear_search_cache()

    @staticmethod
    def _atomic_write(path, config):
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)

    def save_config(self):
        # Atomic write (audit flaw #14, round 4): write to a temp file
        # in the same directory and rename over the old config, so a
        # crash mid-save cannot leave a half-written config.json.
        self._atomic_write(self.config_path, self.config)
        # Last-known-good copy (round 9): a successful save is a known
        # good config. Best effort — a failure here must not break the
        # save that just succeeded.
        try:
            self._atomic_write(self.config_path + ".good", self.config)
        except OSError:
            _log("could not write last-known-good config copy")

    # Overall search deadline (round 9): one slow backend must not hold
    # the whole search hostage. Each plug-in already has its own
    # timeout; the deadline bounds the total wall time, and a plug-in
    # that misses it is marked timed-out while the others still answer.
    # The threads are daemons, so a hung plug-in can never block the
    # process from exiting — it dies with its own timeout at worst.
    SEARCH_DEADLINE_SECONDS = 30
    STATUS_DEADLINE_SECONDS = 10
    # Search-result cache (round 10): a repeat query within 10 minutes
    # returns the cached fan-out instead of re-paying Recoll + Kiwix.
    # Corpora change rarely, so a 10-minute staleness window is fine;
    # the cache is cleared on config reload and by POST /api/cache/clear.
    SEARCH_CACHE_TTL_SECONDS = 600
    SEARCH_CACHE_MAX_ENTRIES = 200

    def clear_search_cache(self):
        """Drop every cached search result (manual clear)."""
        with self._search_cache_lock:
            self._search_cache.clear()

    def health(self):
        """Liveness + basic stats for diagnostics and the status strip."""
        return {
            "ok": True,
            "port": self.config.get("port"),
            "sources": len(self.config.get("sources", [])),
            "plugins": len(self.plugins),
            "search_cache": len(self._search_cache),
            "uptime_seconds": round(time.monotonic() - self._started_at),
        }

    def places(self):
        """Full gazetteer place list (for the command palette).

        Returns the maps plug-in's normalised places (name, type, lat,
        lon, county, note). Small (303 entries) — the palette fuzzy-
        searches it in memory, so no server round-trip per keystroke.
        """
        for plugin in self.plugins:
            if getattr(plugin, "id", "") == "maps":
                try:
                    return plugin._load() or []
                except Exception:  # noqa: BLE001 - palette must not break search
                    return []
        return []

    def library_roots(self):
        """{rootname: dir} for the /files/ route (audit 2026-11-20).

        Built from the plug-ins: the Recoll plug-in reads its topdirs
        from recoll.conf (FieldManuals -> "manuals", MyFiles ->
        "myfiles"), and the My Files plug-in contributes its folder.
        Cached per config load; a config reload rebuilds it.
        """
        if getattr(self, "_library_roots_cache", None) is not None:
            return self._library_roots_cache
        roots = {}
        for plugin in self.plugins:
            try:
                if getattr(plugin, "id", "") == "recoll":
                    roots.update(plugin.library_roots())
                elif getattr(plugin, "id", "") == "myfiles":
                    folder = getattr(plugin, "folder", "")
                    if folder:
                        roots.setdefault("myfiles", folder)
            except Exception:  # noqa: BLE001 - a bad plug-in must not break serving
                continue
        self._library_roots_cache = roots
        return roots

    def palette_search(self, query, limit=8):
        """FTS5 title search over manual titles (palette.db).

        The palette index is built by build-palette-index.py from the
        FieldManuals filenames (titles only, not full text). Fail-open:
        a missing or corrupt DB returns no rows and the palette falls
        back to its local fuzzy lists. A fresh read-only connection is
        opened per call (cheap for a small DB) so no connection is ever
        shared across handler threads.
        """
        query = (query or "").strip()
        if not query or len(query) < 2:
            return []
        path = os.path.join(BASE_DIR, "palette.db")
        if not os.path.isfile(path):
            return []
        try:
            terms = " ".join(f'"{t}"*' for t in query.split() if t)
            if not terms:
                return []
            conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
            try:
                # GROUP BY title: the same manual exists as PDF + EPUB
                # + MD, so one title would otherwise appear three times.
                rows = conn.execute(
                    "SELECT title, kind, detail FROM palette "
                    "WHERE palette MATCH ? GROUP BY title "
                    "ORDER BY rank LIMIT ?",
                    (terms, limit),
                ).fetchall()
            finally:
                conn.close()
            return [
                {"title": r[0], "kind": r[1], "detail": r[2] or ""}
                for r in rows
            ]
        except sqlite3.Error:
            return []

    def geocode_db_path(self):
        """Locate the offline address index (addresses.db).

        Built by scripts/build-address-index.py from an OpenStreetMap
        extract. Looked for in: config geocode_db, then addresses.db
        next to hub.py, then the conventional sibling
        <parent>/geocode/addresses.db (on the laptop the hub lives in
        C:\\Library\\search-hub and the index in C:\\Library\\geocode).
        Returns "" when no index exists.
        """
        candidates = []
        cfg = (self.config.get("geocode_db") or "").strip()
        if cfg:
            # An explicit config path wins outright: no fallback, so
            # a test (or a moved index) gets a deterministic answer.
            candidates.append(cfg)
        else:
            candidates.append(os.path.join(BASE_DIR, "addresses.db"))
            candidates.append(os.path.join(
                os.path.dirname(BASE_DIR), "geocode", "addresses.db"))
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return ""

    def geocode_search(self, query, limit=10):
        """Offline street-address search over the OSM address index.

        Fail-open like palette_search: a missing or corrupt index
        returns no rows. A fresh read-only connection is opened per
        call so no connection is shared across handler threads.
        Returns (results, available).
        """
        query = (query or "").strip()
        if not query:
            return [], False
        path = self.geocode_db_path()
        if not path:
            return [], False
        try:
            conn = geocode_mod.open_index(path)
            try:
                return geocode_mod.search(conn, query, limit), True
            finally:
                conn.close()
        except (sqlite3.Error, ValueError):
            return [], False

    def _search_one(self, plugin, query):
        started = time.monotonic()
        results, available, note = [], True, ""
        search_failed = False
        try:
            results = plugin.search(query) or []
        except Exception as exc:  # noqa: BLE001 - one bad plugin never kills a search
            available, note = False, f"plug-in error: {exc}"
            results = []
            # Round 9 fix (caught by the fault-injection test): a
            # search() that raises means the source is down — the
            # status() call below must not overwrite available with
            # True afterwards, or the rail would show a green dot for
            # a source that just threw.
            search_failed = True
        if not isinstance(results, list):
            results = []
        results = [r for r in results if isinstance(r, dict)]
        # "available" describes the real backend, not the demo fallback:
        # a group can serve sample results while its backend is down.
        # Real (non-demo) results prove the backend is up, so skip the
        # extra status round-trip (an HTTP request / subprocess spawn
        # per plug-in per search). Demo-only or empty results still
        # need a status check, so the rail tells the truth.
        real = [r for r in results if not r.get("demo")]
        if real:
            available, note = True, ""
        else:
            try:
                st = plugin.status()
            except Exception as exc:  # noqa: BLE001
                st = {"available": False, "detail": f"status error: {exc}"}
            if not isinstance(st, dict):
                st = {"available": False,
                      "detail": "plug-in returned a bad status"}
            if not search_failed:
                available = bool(st.get("available", True))
            if not results and not note:
                note = st.get("detail", "")
        return {
            "source_id": getattr(plugin, "source_id", plugin.id),
            "name": getattr(plugin, "display_name", plugin.name),
            "badge": getattr(plugin, "display_badge", plugin.badge),
            "available": available,
            "note": note,
            "results": results,
            "ms": round((time.monotonic() - started) * 1000),
        }

    def _direct_answer(self, query, groups):
        """Lead paragraph of the best-matching Kiwix result, or None.

        A question should get a clear answer at the top, not only a
        list of links. Scores Kiwix results by how well the title
        matches the question's core noun (an exact title match beats a
        title that merely contains it), fetches the best article from
        kiwix-serve, and returns its lead. Tries a second result if the
        first yields no lead. Best-effort: any failure returns None and
        the search still shows its groups.
        """
        base_url = ""
        for plugin in self.plugins:
            if getattr(plugin, "kind", "") == "kiwix":
                base_url = getattr(plugin, "base_url", "") or ""
                break
        if not base_url:
            return None
        cands = []
        for group in groups:
            for res in group.get("results", []):
                if res.get("demo"):
                    continue
                url = res.get("url", "")
                if not url.startswith("/kiwix/"):
                    continue
                # The MiniLM re-rank score (Part 2), when the re-ranker
                # ran. An exact title match is the strongest evidence
                # an article IS the concept, so the title score sorts
                # first; the semantic score breaks ties (and decides
                # when no title matches well). Without a re-ranker
                # every result has 0.0 and the title score decides,
                # as before.
                cands.append((
                    _answer_score(query, res.get("title", "")),
                    float(res.get("_rerank", 0.0) or 0.0),
                    res,
                ))
        # Highest title score first, then re-rank score; original
        # order breaks ties.
        cands.sort(key=lambda c: (-c[0], -c[1]))
        tried = 0
        for _, _, res in cands:
            href = res["url"][len("/kiwix"):]
            text = _fetch_lead(base_url, href)
            if text:
                return {
                    "title": res.get("title", ""),
                    "text": text,
                    "url": res["url"],
                }
            tried += 1
            if tried >= 2:
                return None
        return None

    def search(self, query):
        """Fan-out search with a result cache (round 10).

        A repeat query within the TTL returns the cached fan-out
        instead of re-paying Recoll + Kiwix. The cached result is
        deep-copied on read so a caller (e.g. sanitize_for_remote)
        can never mutate the cache, and deep-copied on write so later
        in-place changes to the returned dict cannot corrupt it.
        """
        key = (query or "").strip().lower()
        if key:
            with self._search_cache_lock:
                hit = self._search_cache.get(key)
                if hit is not None:
                    when, cached = hit
                    if time.monotonic() - when < self.SEARCH_CACHE_TTL_SECONDS:
                        # Refresh recency (LRU): move to the end.
                        self._search_cache.move_to_end(key)
                        return copy.deepcopy(cached)
                    del self._search_cache[key]
        result = self._search_uncached(query)
        if key:
            with self._search_cache_lock:
                self._search_cache[key] = (
                    time.monotonic(), copy.deepcopy(result)
                )
                while len(self._search_cache) > self.SEARCH_CACHE_MAX_ENTRIES:
                    self._search_cache.popitem(last=False)
        return result

    def _search_uncached(self, query):
        plugins = [p for p in self.plugins if p.enabled]
        if not plugins:
            return {"query": query, "groups": []}
        results = {}

        def run(i, plugin):
            try:
                results[i] = self._search_one(plugin, query)
            except Exception as exc:  # noqa: BLE001 - _search_one guards
                # itself; this is the last-resort net for a plug-in
                # whose attribute access itself blew up.
                results[i] = {
                    "source_id": getattr(plugin, "source_id", plugin.id),
                    "name": getattr(plugin, "display_name", plugin.name),
                    "badge": getattr(plugin, "display_badge", plugin.badge),
                    "available": False,
                    "note": f"plug-in error: {exc}",
                    "results": [],
                    "ms": 0,
                }

        threads = [
            threading.Thread(target=run, args=(i, p), daemon=True)
            for i, p in enumerate(plugins)
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + self.SEARCH_DEADLINE_SECONDS
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
        groups = []
        for i, plugin in enumerate(plugins):
            if i in results:
                groups.append(results[i])
            else:
                _log(f"search timed out for source "
                     f"{getattr(plugin, 'source_id', plugin.id)}")
                groups.append({
                    "source_id": getattr(plugin, "source_id", plugin.id),
                    "name": getattr(plugin, "display_name", plugin.name),
                    "badge": getattr(plugin, "display_badge", plugin.badge),
                    "available": False,
                    "note": f"search timed out after "
                            f"{self.SEARCH_DEADLINE_SECONDS}s",
                    "results": [],
                    "ms": round(self.SEARCH_DEADLINE_SECONDS * 1000),
                })
        # Direct answer: the lead of the top Kiwix article, so a
        # question gets a clear answer at the top instead of only a
        # list of links. Best-effort — any failure just omits it.
        answer = self._direct_answer(query, groups)
        # Strip internal keys (e.g. "_rerank" from the re-ranker) so
        # they never leak into the JSON the UI receives.
        for group in groups:
            for res in group.get("results", []):
                for key in [k for k in res if k.startswith("_")]:
                    del res[key]
        out = {"query": query, "groups": groups}
        if answer:
            out["answer"] = answer
        return out

    # Status is polled by the UI rail; cache it briefly so a poll does
    # not spawn a Recoll process and an HTTP request every time. The
    # cache is dropped whenever the source list changes (add_source).
    STATUS_CACHE_SECONDS = 5

    def _status_one(self, plugin):
        try:
            st = plugin.status()
        except Exception as exc:  # noqa: BLE001
            st = {"available": False, "detail": f"status error: {exc}"}
        if not isinstance(st, dict):
            st = {"available": False,
                  "detail": "plug-in returned a bad status"}
        available = bool(st.get("available", True))
        return {
            "id": getattr(plugin, "source_id", plugin.id),
            "name": getattr(plugin, "display_name", plugin.name),
            "badge": getattr(plugin, "display_badge", plugin.badge),
            "available": available,
            "detail": st.get("detail", ""),
            # Amber dot in the rail: backend down, but sample data
            # is served so the UI still demonstrates the source.
            "demo": (not available) and self.demo_fallback,
        }

    def status(self):
        now = time.monotonic()
        if self._status_cache is not None:
            cached_at, cached = self._status_cache
            if now - cached_at < self.STATUS_CACHE_SECONDS:
                return cached
        plugins = [p for p in self.plugins if p.enabled]
        out = [None] * len(plugins)

        def run(i, plugin):
            try:
                out[i] = self._status_one(plugin)
            except Exception as exc:  # noqa: BLE001 - last-resort net
                out[i] = {
                    "id": getattr(plugin, "source_id", plugin.id),
                    "name": getattr(plugin, "display_name", plugin.name),
                    "badge": getattr(plugin, "display_badge", plugin.badge),
                    "available": False,
                    "detail": f"status error: {exc}",
                    "demo": False,
                }

        threads = [
            threading.Thread(target=run, args=(i, p), daemon=True)
            for i, p in enumerate(plugins)
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + self.STATUS_DEADLINE_SECONDS
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
        sources = []
        for i, plugin in enumerate(plugins):
            if out[i] is not None:
                sources.append(out[i])
            else:
                _log(f"status timed out for source "
                     f"{getattr(plugin, 'source_id', plugin.id)}")
                sources.append({
                    "id": getattr(plugin, "source_id", plugin.id),
                    "name": getattr(plugin, "display_name", plugin.name),
                    "badge": getattr(plugin, "display_badge", plugin.badge),
                    "available": False,
                    "detail": "status check timed out",
                    "demo": False,
                })
        # Library snapshot dates (GAP_LIST item 10). Filled in at setup
        # in config.json "library": ZIM dates per ZIM id (YYYY-MM) and
        # the date the Recoll index was last built. Until then, "not set"
        # so staleness is visible instead of silent.
        lib = self.config.get("library", {}) or {}
        if not isinstance(lib, dict):
            lib = {}
        zims = lib.get("zim_snapshots", {}) or {}
        if not isinstance(zims, dict):
            zims = {}
        recoll_date = lib.get("recoll_index_date", "") or ""
        if not isinstance(recoll_date, str):
            recoll_date = ""
        result = {
            "sources": sources,
            "library": {
                "zim_snapshots": zims,
                "zim_snapshot_status": (
                    "not set" if not zims else f"{len(zims)} ZIM(s) dated"
                ),
                "recoll_index_date": recoll_date or "not set",
            },
        }
        self._status_cache = (now, result)
        return result

    def add_source(self, body):
        name = (body.get("name") or "").strip()
        plugin = (body.get("plugin") or "").strip()
        if not name:
            raise ValueError("name is required")
        if not plugin:
            raise ValueError("plugin is required")
        if plugin not in ("kiwix", "recoll", "maps", "myfiles", "frequencies"):
            raise ValueError(f"unknown plugin: {plugin}")
        source_id = (
            re.sub(r"-+", "-", "".join(
                ch.lower() if ch.isalnum() else "-" for ch in name
            ).strip("-"))
            or plugin
        )
        entry = {"id": source_id, "name": name, "plugin": plugin, "enabled": True}
        for key, value in body.items():
            if key not in ("id", "name", "plugin"):
                entry[key] = value
        if "badge" not in entry:
            entry["badge"] = {"kiwix": "Reference", "recoll": "Manual",
                              "maps": "Place", "myfiles": "My Files",
                              "frequencies": "Radio"}[plugin]
        with self.lock:
            existing = {getattr(p, "source_id", p.id) for p in self.plugins}
            if source_id in existing:
                raise ValueError(f"source id already exists: {source_id}")
            self.config.setdefault("sources", []).append(entry)
            self.save_config()
            self.load()
        return entry


def _clamp_range(start, end, size):
    """Clamp a byte range to the per-response chunk cap (round 9).

    A Range like bytes=0- on a 3 GB extract must not read the whole
    remainder into RAM. The caller answers 206 with the clamped range;
    pmtiles.js fetches in chunks anyway.
    """
    if end - start + 1 > TILE_CHUNK_MAX_BYTES:
        end = start + TILE_CHUNK_MAX_BYTES - 1
    return start, end


def _bind_server(host, port, handler, tries=10):
    """Bind the hub, falling to the next port if the configured one is
    busy (round 9). A laptop that already has something on 8899 must
    not leave the owner with a dead hub — it takes the next free port and
    says which one. Returns (server, actual_port).
    """
    last_exc = None
    for attempt in range(tries):
        candidate = port if port == 0 else port + attempt
        try:
            return ThreadingHTTPServer((host, candidate), handler), candidate
        except OSError as exc:
            last_exc = exc
            _log(f"cannot bind {host}:{candidate}: {exc}")
    raise last_exc


def _strip_paths(text):
    """Replace absolute filesystem paths in a string with file names.

    Used when serving hotspot clients (audit flaw #7): a phone on the
    hotspot must not see the laptop's directory tree. Hub-relative
    URLs (/kiwix/..., /map?...) pass through untouched.
    """
    if not isinstance(text, str) or not text:
        return text

    def _base(match):
        p = match.group(0)
        p = p.replace("file:///", "").replace("file://", "").replace("file:", "")
        # Normalise backslashes first: os.path.basename on Linux does
        # not split on "\", so a Windows path would pass through whole.
        name = os.path.basename(p.replace("\\", "/").rstrip("/"))
        return name or p

    # Windows drive paths, with or without a file:// prefix, and UNC paths.
    text = re.sub(r"(?:file:///?)?[A-Za-z]:[\\/][^\s\"'<>]*", _base, text)
    text = re.sub(r"\\\\[^\s\"'<>]+", _base, text)
    # Absolute POSIX paths (dev machines) — but not hub-relative URLs.
    def _posix(match):
        if match.group(0).startswith(("/kiwix", "/map", "/api", "/tiles")):
            return match.group(0)
        return os.path.basename(match.group(0))

    text = re.sub(r"/(?:[\w.\-]+/)+[\w.\-]+", _posix, text)
    return text


def _looks_like_file_path(url):
    """True if a result URL is a local filesystem path, not a web link."""
    if not url or not isinstance(url, str):
        return False
    if url.startswith(("file://", "file:")):
        return True
    if url.startswith("\\\\"):
        return True
    if len(url) >= 3 and url[0].isalpha() and url[1] == ":" and url[2] in "\\/":
        return True
    # Hub-relative URLs (/kiwix/..., /map?..., /pdf/..., /files/...)
    # are web links, not filesystem paths — never blank them. A
    # /map?lat=42.2215&lon=-71.0015&zoom=14 URL has "." in its
    # coordinates and was wrongly blanked for remote clients (audit
    # 2026-11-20, 3f). Only a non-hub absolute POSIX path with a file
    # extension counts.
    if url.startswith("/"):
        if url.startswith(("/kiwix", "/map", "/api", "/tiles", "/pdf", "/files", "/static")):
            return False
        return "." in url.rsplit("/", 1)[-1]
    return False


def sanitize_for_remote(search_result):
    """Strip absolute filesystem paths from a /api/search payload.

    For non-localhost clients: file-path result URLs become "" (the
    phone cannot open them anyway) and `detail` lines are reduced to
    file names. Hub-relative URLs (/kiwix/..., /map?...) are kept.
    My Files snippets are the owner's own file contents (F19), so
    they are stripped for remote clients — Kiwix/Recoll snippets are
    public reference material and are kept. Returns a new dict; the
    hub's own result objects are untouched.
    """
    groups = []
    for group in search_result.get("groups", []):
        results = []
        for r in group.get("results", []):
            r = dict(r)
            if _looks_like_file_path(r.get("url", "")):
                r["url"] = ""
            r["detail"] = _strip_paths(r.get("detail", ""))
            if group.get("source_id") == "myfiles":
                r["snippet"] = ""
            results.append(r)
        group = dict(group)
        group["results"] = results
        # The group note comes from the plug-in status detail when a
        # source returns no results — it can carry an absolute path
        # (e.g. "50 files in C:\Library\MyFiles"). Strip it too
        # (audit 2026-11-20, 3c).
        group["note"] = _strip_paths(group.get("note", ""))
        groups.append(group)
    out = dict(search_result)
    out["groups"] = groups
    return out


def sanitize_status_for_remote(status_result):
    """Strip absolute filesystem paths from /api/status detail lines."""
    sources = []
    for s in status_result.get("sources", []):
        s = dict(s)
        s["detail"] = _strip_paths(s.get("detail", ""))
        sources.append(s)
    out = dict(status_result)
    out["sources"] = sources
    return out


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the console quiet
            pass

        def _send_json(self, obj, code=200):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path, content_type):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                self.send_error(404, "not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # Max proxied article size: a ZIM article page with images is a
        # few MB at most; anything bigger is a runaway, not an article.
        PROXY_MAX_BYTES = 25 * 1024 * 1024

        def _kiwix_base_url(self):
            for plugin in hub.plugins:
                if getattr(plugin, "kind", "") == "kiwix":
                    return getattr(plugin, "base_url", "")
            return ""

        def _proxy_kiwix(self, subpath, query):
            base = self._kiwix_base_url()
            if not base:
                self._send_json({"error": "no kiwix source configured"}, 502)
                return
            url = base + subpath
            if query:
                url += "?" + query
            req = urllib.request.Request(
                url, headers={"User-Agent": "search-hub/1.0"}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = resp.read(self.PROXY_MAX_BYTES + 1)
                    if len(body) > self.PROXY_MAX_BYTES:
                        self._send_json(
                            {"error": "kiwix response too large"}, 502
                        )
                        return
                    ctype = resp.headers.get(
                        "Content-Type", "application/octet-stream"
                    )
                    if ctype.startswith("text/html"):
                        text = body.decode("utf-8", errors="replace")
                        # Rewrite absolute in-page links so they stay
                        # inside the proxy (and keep working from the
                        # phone): /<zim>/... -> /kiwix/<zim>/...
                        text = text.replace(
                            'href="/', 'href="/kiwix/'
                        ).replace('src="/', 'src="/kiwix/')
                        body = text.encode("utf-8")
                        ctype = "text/html; charset=utf-8"
                    self.send_response(resp.status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except urllib.error.HTTPError as exc:
                self._send_json(
                    {"error": f"kiwix-serve returned {exc.code}"}, 502
                )
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                self._send_json({"error": "kiwix-serve not reachable"}, 502)

        def _is_local(self):
            # Localhost-only writes (POST /api/sources) and the
            # path-stripping rule both key off this: a client on the
            # hotspot is not the laptop, even though it is trusted to
            # search.
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _send_pmtiles(self, name):
            """Serve a .pmtiles extract from the configured maps dir.

            pmtiles.js fetches the archive with HTTP Range requests, so
            the handler honours Range: bytes=a-b (206 + Content-Range)
            and answers 404 when the extract is not there yet. Only
            plain file names ending in .pmtiles are served, and only
            from maps_dir — no path traversal.
            """
            maps_dir = hub.config.get("maps_dir", "") or ""
            if (
                not maps_dir
                or not name.endswith(".pmtiles")
                or "/" in name
                or "\\" in name
                or ".." in name
            ):
                self.send_error(404, "not found")
                return
            path = os.path.join(maps_dir, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                # ASCII only: BaseHTTPRequestHandler's error pages are
                # latin-1, so a non-ASCII message here would 500.
                self.send_error(404, "no map extract yet (see static/map.html)")
                return
            start, end = 0, size - 1
            ranged = False
            range_hdr = (self.headers.get("Range") or "").strip()
            if range_hdr:
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                if m:
                    a, b = m.groups()
                    if a:
                        start = int(a)
                    if b:
                        end = int(b)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    ranged = True
            # Clamp the chunk: a full GET (or an open-ended Range) on a
            # multi-GB extract must not read the whole file into RAM.
            # The response is partial (206) when clamped, which is what
            # pmtiles.js expects anyway — it fetches in chunks.
            start, end = _clamp_range(start, end, size)
            if end < size - 1:
                ranged = True
            length = end - start + 1
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(length)
            except OSError:
                self.send_error(404, "not found")
                return
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", "application/x-protomaps-tiles-archive")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(data)

        def _send_library_file(self, root, rel):
            """Serve one file from a library root: /files/<root>/<rel>.

            Audit 2026-11-20: Recoll/My Files results carried file://
            URLs, which browsers block from http:// pages — so a manual
            PDF result was unopenable. The plug-ins now rewrite those
            URLs to /files/<root>/<relpath>, and this route serves them.

            Only the roots from hub.library_roots() (Recoll topdirs +
            the My Files folder). The rel path is resolved and verified
            to stay under its root (no traversal, no absolute paths,
            no symlinks escaping the root). Content-Type by extension;
            PDFs served inline so the browser's PDF viewer opens them;
            Range supported so large files stream.
            """
            roots = hub.library_roots()
            base = roots.get(root)
            if not base:
                self.send_error(404, "not found")
                return
            rel = urllib.parse.unquote(rel)
            if not rel or rel.startswith(("/", "\\")) or ".." in rel.split("/"):
                self.send_error(404, "not found")
                return
            try:
                base_real = os.path.realpath(base)
                full = os.path.realpath(os.path.join(base_real, rel))
                if os.path.commonpath([full, base_real]) != base_real:
                    self.send_error(404, "not found")
                    return
                size = os.path.getsize(full)
            except (OSError, ValueError):
                self.send_error(404, "not found")
                return
            ext = os.path.splitext(full)[1].lower()
            ctype = {
                ".pdf": "application/pdf",
                ".epub": "application/epub+zip",
                ".md": "text/plain; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".htm": "text/html; charset=utf-8",
                ".csv": "text/csv; charset=utf-8",
            }.get(ext, "application/octet-stream")
            start, end = 0, size - 1
            ranged = False
            range_hdr = (self.headers.get("Range") or "").strip()
            if range_hdr:
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                if m:
                    a, b = m.groups()
                    if a:
                        start = int(a)
                    if b:
                        end = int(b)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    ranged = True
            length = end - start + 1
            try:
                with open(full, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(length)
            except OSError:
                self.send_error(404, "not found")
                return
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if ext == ".pdf":
                self.send_header(
                    "Content-Disposition",
                    f'inline; filename="{os.path.basename(full)}"',
                )
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(data)

        def _send_map_pdf(self, subdir, name):
            """Serve a PDF map: a USGS topo quad GeoPDF from
            maps_dir/quads/ or a T.REX print PDF from
            maps_dir/trex-arms/.

            Only .pdf files, plain file names, only from those two
            subdirs — no path traversal. Honours Range so the
            browser's built-in PDF viewer can stream instead of
            downloading the whole file first.
            """
            maps_dir = hub.config.get("maps_dir", "") or ""
            if (
                not maps_dir
                or subdir not in ("quads", "trex-arms")
                or not name.endswith(".pdf")
                or "/" in name
                or "\\" in name
                or ".." in name
            ):
                self.send_error(404, "not found")
                return
            path = os.path.join(maps_dir, subdir, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                self.send_error(404, "not found")
                return
            start, end = 0, size - 1
            ranged = False
            range_hdr = (self.headers.get("Range") or "").strip()
            if range_hdr:
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                if m:
                    a, b = m.groups()
                    if a:
                        start = int(a)
                    if b:
                        end = int(b)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    ranged = True
            length = end - start + 1
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(length)
            except OSError:
                self.send_error(404, "not found")
                return
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Disposition", f'inline; filename="{name}"'
            )
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(data)

        def _send_topo_tile(self, rel):
            """Serve one USGS topo raster tile: {z}/{x}/{y}.jpg.

            Tiles are downloaded into maps_dir/topo/ by the topo
            downloader. Only digit z/x and a .jpg/.png y are served,
            and only from maps_dir/topo — no path traversal. Missing
            tiles 404 (MapLibre renders the background there).
            """
            parts = rel.split("/")
            if len(parts) != 3:
                self.send_error(404, "not found")
                return
            z, x, yfile = parts
            if not (z.isdigit() and x.isdigit()):
                self.send_error(404, "not found")
                return
            if yfile.endswith(".jpg"):
                ctype = "image/jpeg"
            elif yfile.endswith(".png"):
                ctype = "image/png"
            else:
                self.send_error(404, "not found")
                return
            y = yfile.rsplit(".", 1)[0]
            if not y.isdigit():
                self.send_error(404, "not found")
                return
            maps_dir = hub.config.get("maps_dir", "") or ""
            if not maps_dir:
                self.send_error(404, "not found")
                return
            path = os.path.join(maps_dir, "topo", z, x, yfile)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                self.send_error(404, "not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _trex_mbtiles_path(self):
            """First *.mbtiles in maps_dir/trex-arms (the ATAK export)."""
            maps_dir = hub.config.get("maps_dir", "") or ""
            if not maps_dir:
                return ""
            found = sorted(glob.glob(os.path.join(maps_dir, "trex-arms", "*.mbtiles")))
            return found[0] if found else ""

        def _send_trex_tile(self, rel):
            """Serve one T.REX ATAK raster tile: {z}/{x}/{y}.jpg.

            Tiles live in the MBTiles SQLite file in maps_dir/trex-arms/
            (the ATAK export, z12-z15, JPEG). MBTiles stores rows in TMS
            order (counted from the bottom), while the map asks in XYZ
            order (from the top), so the row is flipped: tms_y =
            (2**z - 1) - y. Only digit z/x/y are served; missing tiles
            404 (MapLibre renders the background there). The DB is
            opened read-only per request — no writes, no lock contention.
            """
            parts = rel.split("/")
            if len(parts) != 3:
                self.send_error(404, "not found")
                return
            z, x, yfile = parts
            if not (z.isdigit() and x.isdigit()):
                self.send_error(404, "not found")
                return
            if yfile.endswith(".jpg"):
                pass
            elif yfile.endswith(".png"):
                pass
            else:
                self.send_error(404, "not found")
                return
            y = yfile.rsplit(".", 1)[0]
            if not y.isdigit():
                self.send_error(404, "not found")
                return
            z, x, y = int(z), int(x), int(y)
            if not (0 <= z <= 20):
                self.send_error(404, "not found")
                return
            db_path = self._trex_mbtiles_path()
            if not db_path:
                self.send_error(404, "not found")
                return
            tms_y = (1 << z) - 1 - y
            try:
                conn = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
                try:
                    row = conn.execute(
                        "SELECT tile_data FROM tiles WHERE zoom_level=? "
                        "AND tile_column=? AND tile_row=?",
                        (z, x, tms_y),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error:
                self.send_error(404, "not found")
                return
            if not row:
                self.send_error(404, "not found")
                return
            data = row[0]
            if data[:3] == b"\xff\xd8\xff":
                ctype = "image/jpeg"
            elif data[:4] == b"\x89PNG":
                ctype = "image/png"
            else:
                ctype = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                self._do_get()
            except Exception as exc:  # noqa: BLE001 - never drop a connection
                _log(f"GET {self.path!r} failed: {exc}")
                try:
                    self._send_json({"error": f"hub error: {exc}"}, 500)
                except OSError:
                    pass  # client already gone; nothing to send to

        def _do_get(self):
            parsed = urlparse(self.path)
            if parsed.path == "/kiwix" or parsed.path.startswith("/kiwix/"):
                # Kiwix article pages, proxied so the links the hub
                # returns work from any device (the phone included) —
                # a direct 127.0.0.1:8080 link would point at the phone
                # itself (audit flaw #3, round 4).
                subpath = parsed.path[len("/kiwix"):] or "/"
                self._proxy_kiwix(subpath, parsed.query)
                return
            if parsed.path == "/map":
                # Offline map viewer (audit flaw #11, round 5): vendored
                # MapLibre GL JS + pmtiles.js over the local extract.
                self._send_file(os.path.join(BASE_DIR, "static", "map.html"), "text/html; charset=utf-8")
            elif parsed.path.startswith("/files/"):
                # Library files: /files/<root>/<relpath> (audit
                # 2026-11-20). Recoll/My Files results link here
                # instead of file:// URLs, so a manual PDF actually
                # opens in the browser (and from the phone).
                rest = parsed.path[len("/files/"):]
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    self._send_library_file(parts[0], urllib.parse.unquote(parts[1]))
                else:
                    self.send_error(404, "not found")
            elif parsed.path.startswith("/pdf/"):
                # PDF maps: /pdf/quads/<file>.pdf (USGS topo quad
                # GeoPDF) or /pdf/trex-arms/<file>.pdf (T.REX print
                # PDF). Served inline so the browser's PDF viewer
                # opens it, including from the phone.
                rest = parsed.path[len("/pdf/"):]
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    self._send_map_pdf(parts[0], urllib.parse.unquote(parts[1]))
                else:
                    self.send_error(404, "not found")
            elif parsed.path.startswith("/tiles/topo/"):
                # USGS topo raster tiles ({z}/{x}/{y}.jpg), downloaded
                # into maps_dir/topo/ — the real topo basemap.
                self._send_topo_tile(parsed.path[len("/tiles/topo/"):])
            elif parsed.path.startswith("/tiles/trex/"):
                # T.REX ATAK raster tiles ({z}/{x}/{y}.jpg) from the
                # MBTiles file in maps_dir/trex-arms/ — the ATAK view.
                self._send_trex_tile(parsed.path[len("/tiles/trex/"):])
            elif parsed.path.startswith("/tiles/"):
                self._send_pmtiles(parsed.path[len("/tiles/"):])
            elif parsed.path.startswith("/static/vendor/"):
                # Vendored viewer libraries only (maplibre-gl.js/.css,
                # pmtiles.js, glyph PBFs) — no other static files are
                # exposed, and no path traversal out of static/vendor/.
                name = urllib.parse.unquote(parsed.path[len("/static/vendor/"):])
                if ".." in name or "\\" in name:
                    self.send_error(404, "not found")
                elif name.startswith("glyphs/") and name.endswith(".pbf"):
                    self._send_file(
                        os.path.join(BASE_DIR, "static", "vendor", name),
                        "application/octet-stream",
                    )
                elif (
                    "/" not in name
                    and (name.endswith(".js") or name.endswith(".css"))
                ):
                    ctype = (
                        "text/javascript; charset=utf-8"
                        if name.endswith(".js")
                        else "text/css; charset=utf-8"
                    )
                    self._send_file(
                        os.path.join(BASE_DIR, "static", "vendor", name), ctype
                    )
                else:
                    self.send_error(404, "not found")
            elif parsed.path == "/":
                self._send_file(os.path.join(BASE_DIR, "static", "ui.html"), "text/html; charset=utf-8")
            elif parsed.path == "/index.html":
                self._send_file(os.path.join(BASE_DIR, "static", "ui.html"), "text/html; charset=utf-8")
            elif parsed.path == "/api/search":
                query = parse_qs(parsed.query).get("q", [""])[0][:500]
                # Bound concurrent searches (round 9): the search slot
                # semaphore caps in-flight searches at 4. An extra
                # request gets a 503 with a retry hint instead of
                # queuing behind a hung backend.
                if not hub._search_slots.acquire(blocking=False):
                    self._send_json(
                        {"error": "hub busy — too many concurrent "
                                  "searches; retry in a second"}, 503)
                    return
                try:
                    result = hub.search(query)
                finally:
                    hub._search_slots.release()
                if not self._is_local():
                    # Hotspot clients get file names, not the laptop's
                    # directory tree (audit flaw #7).
                    result = sanitize_for_remote(result)
                self._send_json(result)
            elif parsed.path == "/api/status":
                result = hub.status()
                if not self._is_local():
                    result = sanitize_status_for_remote(result)
                self._send_json(result)
            elif parsed.path == "/api/health":
                # Liveness + basic stats (round 10 hardening).
                self._send_json(hub.health())
            elif parsed.path == "/api/palette":
                # Fast FTS5 title search for the command palette
                # (round 10). Manual titles only — places and
                # frequencies are small enough for the palette to
                # fuzzy-search in memory.
                query = parse_qs(parsed.query).get("q", [""])[0][:200]
                self._send_json({"results": hub.palette_search(query)})
            elif parsed.path == "/api/places":
                # Full gazetteer place list for the command palette
                # (round 10). Small (303 entries) — the palette
                # fuzzy-searches it in memory.
                # For remote (hotspot) clients, drop entries flagged as
                # private meeting points or a home area (audit
                # 2026-11-20, 3g): a stranger on the hotspot must
                # not learn where the family meets or where home
                # is. Localhost gets the full list (the laptop's
                # own palette needs it).
                places = hub.places()
                if not self._is_local():
                    places = [
                        p for p in places
                        if p.get("type") != "Rally Point"
                        and "home area" not in (p.get("note") or "").lower()
                        and "rally" not in (p.get("name") or "").lower()
                    ]
                self._send_json({"places": places})
            elif parsed.path == "/api/config":
                # Localhost-only (audit 2026-11-20): the config carries
                # absolute laptop paths, and a hotspot client has no
                # business reading it.
                if not self._is_local():
                    self._send_json({"error": "config is localhost-only"}, 403)
                    return
                self._send_json({"sources": hub.config.get("sources", [])})
            elif parsed.path == "/api/frequencies":
                # Full frequency list for the Radio tile — the raw
                # entries from the frequencies plug-in (NOAA + ham +
                # GMRS), so the tile can show every frequency with
                # its details without a search.
                entries = []
                for plugin in hub.plugins:
                    if getattr(plugin, "id", "") == "frequencies":
                        try:
                            entries = plugin._load() or []
                        except Exception:
                            entries = []
                        break
                self._send_json({"entries": entries})
            elif parsed.path == "/api/geocode":
                # Offline street-address search (2026-11-20): the
                # map's go-to box asks here when the input starts
                # with a house number. Addresses are public OSM
                # data, so no remote-client filtering applies.
                # Fail-open: no index yet -> empty results and
                # available False.
                query = parse_qs(parsed.query).get("q", [""])[0][:200]
                results, available = hub.geocode_search(query)
                self._send_json({"results": results, "available": available})
            else:
                self.send_error(404, "not found")

        def do_POST(self):
            try:
                self._do_post()
            except Exception as exc:  # noqa: BLE001 - never drop a connection
                _log(f"POST {self.path!r} failed: {exc}")
                try:
                    self._send_json({"error": f"hub error: {exc}"}, 500)
                except OSError:
                    pass  # client already gone; nothing to send to

        def _do_post(self):
            # Writes are localhost-only: phones on the hotspot may search,
            # but only the laptop itself may change the source list.
            if not self._is_local():
                self._send_json({"error": "source changes are localhost-only"}, 403)
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/cache/clear":
                # Manual search-cache clear (round 10). Localhost-only,
                # like all writes.
                hub.clear_search_cache()
                self._send_json({"cleared": True})
                return
            if parsed.path != "/api/sources":
                self.send_error(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json({"error": "invalid Content-Length"}, 400)
                return
            if length < 0 or length > 65536:
                self._send_json({"error": "request body too large (max 64 KiB)"}, 413)
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, OSError):
                self._send_json({"error": "invalid JSON body"}, 400)
                return
            if not isinstance(body, dict):
                # A JSON array / string / number has no .get — without
                # this check add_source() blows up with AttributeError
                # and the client gets a 500 instead of a 400.
                self._send_json({"error": "request body must be a JSON object"}, 400)
                return
            try:
                entry = hub.add_source(body)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"added": entry}, 201)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search Hub — offline search for the 5424.")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    parser.add_argument("--host", default=None, help="bind host (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: from config)")
    parser.add_argument("--serve", action="store_true",
                        help="serving mode: bind 0.0.0.0 so the phone on the "
                             "laptop hotspot can reach the hub (default is "
                             "localhost-only; audit flaw #7)")
    parser.add_argument("--no-demo", action="store_true", help="disable sample-data fallback")
    parser.add_argument("--demo", action="store_true", help="force sample-data fallback on (dry run before the library exists)")
    parser.add_argument("--no-pause", action="store_true",
                        help="ignored by the hub itself; for run_hub.bat — "
                             "no 'press any key' wait, exit code passed "
                             "through so Task Scheduler can restart it")
    args = parser.parse_args(argv)

    if args.no_demo:
        demo_override = False
    elif args.demo:
        demo_override = True
    else:
        demo_override = None
    hub = Hub(args.config, demo_fallback=demo_override)

    host = args.host or hub.config.get("host", "127.0.0.1")
    if args.serve:
        host = "0.0.0.0"
    port = args.port or hub.config.get("port", 8899)
    serving = host not in ("127.0.0.1", "::1", "localhost")
    try:
        server, port = _bind_server(host, port, make_handler(hub))
    except OSError as exc:
        print(f"[hub] cannot bind {host}:{port} (tried {port}..{port + 9}): {exc}",
              file=sys.stderr)
        _log(f"cannot bind {host}:{port} after 10 tries: {exc}")
        return 1
    # Record the actual port (round 9): the watchdog and serve_toggle.ps1
    # read hub.port so they find the hub even when it fell back to the
    # next port because 8899 was busy.
    try:
        with open(PORT_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(port))
    except OSError:
        pass
    _log(f"started on {host}:{port} (serving mode: {serving}; "
         f"demo fallback: {hub.demo_fallback})")
    print(f"[hub] Search Hub running at http://{host}:{port}/")
    if serving:
        print("[hub] SERVING MODE: reachable on the LAN / hotspot; "
              "absolute paths are stripped from results for remote clients.")
    else:
        print("[hub] Localhost-only. Run with --serve to serve the phone "
              "over the hotspot.")
    print("[hub] Sources: " + ", ".join(
        f"{getattr(p, 'display_name', p.name)} ({getattr(p, 'source_id', p.id)})"
        for p in hub.plugins
    ))
    print("[hub] Demo fallback is " + ("ON (sample data until the real backends are up)"
                                        if hub.demo_fallback else "OFF"))
    print("[hub] Log: " + LOG_PATH)
    print("[hub] Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("stopped.")
        print("\n[hub] stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
