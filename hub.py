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
explicitly with --serve (or --host 0.0.0.0). Hotspot clients can only
read sources and file roots explicitly shared in config.json.

First run on Windows will prompt for firewall access — allow it on
private networks so the phone on the laptop's Wi-Fi hotspot can reach
the hub. The hub itself makes no outbound network calls, ever.

Endpoints:
    GET  /                the search UI (static/ui.html)
    GET  /api/search?q=   JSON: {query, answer: {title, text, url} |
                          groups: [{source_id, name,
                          available, note, results: [{title, snippet,
                          url, badge, detail, demo}]}]}
                          (answer is a source excerpt when a strong
                          article or indexed manual passage is found)
                          Optional &source= limits search to one source ID.
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
import secrets
import shutil
import socket
import sqlite3
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from plugins import load_plugins  # noqa: E402
from plugins.frequencies import exact_frequency  # noqa: E402
from plugins.kiwix import open_reader, reader_origin  # noqa: E402
from plugins.maps import COORDINATES  # noqa: E402
from plugins.recoll import MANUAL_ID  # noqa: E402
from query_expand import expand_query, search_words  # noqa: E402
import geocode as geocode_mod  # noqa: E402  (stdlib-only address search)
from pins import PinStore  # noqa: E402  (server-side pin store)
from family_plan_store import FamilyPlanStore, PlanConflict  # noqa: E402
try:
    from family_plan import family_plan as _family_plan_fn  # noqa: E402  (optional legacy plan)
except ImportError:
    _family_plan_fn = None
try:
    import routing as routing_mod  # noqa: E402  (stdlib-only road router)
    from routing import NoRouteError as _NoRouteError  # noqa: E402
    from routing import RouteTimeoutError as _RouteTimeoutError  # noqa: E402
    from routing import RoutingUnavailableError as _RoutingUnavailableError  # noqa: E402
except ImportError:  # a checkout without routing.py still starts
    routing_mod = None

    class _NoRouteError(Exception):
        """Stub when routing.py is absent."""

    class _RouteTimeoutError(Exception):
        """Stub when routing.py is absent."""

    class _RoutingUnavailableError(Exception):
        """Stub when routing.py is absent."""
from sdrpp import Receiver, ReceiverError, valid_frequency  # noqa: E402
from sqlite_files import open_readonly  # noqa: E402

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
ANSWER_TIMEOUT_SECONDS = 2


def _fetch_lead(base_url, href, *, timeout):
    """Fetch a Kiwix article and return its lead text, or None.

    href is the kiwix-serve path (e.g. /<zim>/A/Tourniquet). Returns the
    first one or two lead paragraphs joined, plain text, citations
    stripped. Any failure (unreachable, bad HTML, no lead) returns None
    so the search still works without an answer box.
    """
    url = base_url.rstrip("/") + href
    req = urllib.request.Request(url, headers={"User-Agent": "search-hub/1.0"})
    try:
        with open_reader(req, timeout=timeout) as resp:
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


# Question words and filler omitted when comparing a query to its evidence.
_ANSWER_STOPWORDS = {
    "what", "is", "are", "was", "were", "how", "to", "do", "does", "did",
    "a", "an", "the", "apply", "use", "using", "used", "get", "find",
    "where", "when", "why", "who", "whom", "which", "can", "could",
    "should", "would", "will", "shall", "may", "might", "i", "you", "we",
    "it", "this", "that", "these", "those", "of", "for", "in", "on",
    "with", "by", "at", "from", "and", "or", "my", "me", "please",
}


def _match_terms(phrase):
    return tuple(word for word in search_words(phrase) if word not in _ANSWER_STOPWORDS)


def _passage_score(terms, title, passage):
    # Tech L5: dedupe terms (a repeated word must not drag the score
    # down) and compare title words on the same stopword-stripped
    # token set as the query terms.
    terms = tuple(dict.fromkeys(terms))
    if not terms:
        return 0
    words = search_words(passage)
    if not set(terms) <= set(words):
        return 0
    title_words = tuple(word for word in search_words(title)
                        if word not in _ANSWER_STOPWORDS)
    if len(set(terms)) == 1 and terms != title_words:
        return 0
    # Prefer a phrase reproduced in the passage to scattered terms.
    phrase = " ".join(terms)
    return 96 if f" {phrase} " in f" {' '.join(words)} " else 84


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
            "badge": "Supplies",
            "db": "inventory.db",
            "enabled": True,
            "id": "inventory",
            "max_results": 8,
            "name": "Supplies & Inventory",
            "plugin": "inventory",
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
        self.family_plan_store = FamilyPlanStore(self.config_path)
        self.lock = threading.Lock()
        self._started_at = time.monotonic()
        self.listener_port = None
        self.instance_id = secrets.token_hex(16)
        # Bound on concurrent HTTP searches (round 9): a burst of
        # requests must not spawn an unbounded pile of Recoll
        # subprocesses on the i5-8350U. Extra searches get a 503 with
        # a retry hint instead of queuing behind a hung backend.
        self._search_slots = threading.BoundedSemaphore(4)
        self._download_slots = threading.BoundedSemaphore(4)
        # Bound on concurrent road routes (audit #3): A* is the
        # heaviest work the hub does — one cross-country route can
        # run for minutes — so routes get their own 2-slot semaphore
        # instead of sharing the search slots (a route flood must not
        # eat the search slots, or vice versa).
        self._route_slots = threading.BoundedSemaphore(2)
        # Bound on concurrent geocode queries (audit #4): a separate
        # 4-slot semaphore, same size as search, so a geocode flood
        # cannot eat the search slots (or vice versa).
        self._geocode_slots = threading.BoundedSemaphore(4)
        # Per-IP in-flight limits for remote (hotspot) clients (Sec
        # M5, 2026-11-21): at most 2 concurrent searches and 2
        # concurrent downloads per IP, so one guest cannot starve the
        # laptop owner out of the global 4-slot semaphores above.
        # Routes are heavier, so a remote client gets at most 1
        # concurrent route (audit #3). Local clients are exempt. The
        # tables are keyed by client IP; the hotspot audience is a
        # handful of devices, so they stay tiny. Use
        # remote_search_slot() / remote_download_slot() /
        # remote_route_slot().
        self._remote_slots_lock = threading.Lock()
        self._remote_search_slots = {}
        self._remote_download_slots = {}
        self._remote_route_slots = {}
        self._remote_geocode_slots = {}
        # Search-result cache (round 10): repeat queries skip the whole
        # fan-out. Key is the normalised query; TTL 10 min, capped at
        # 200 entries (oldest evicted). Stdlib only, no new service.
        # The cache is process memory — restarting the hub clears it.
        self._search_cache = OrderedDict()
        self._search_cache_lock = threading.Lock()
        self._search_cache_generation = 0
        # CLI override for demo_fallback (e.g. --no-demo). It is applied
        # inside load() after the file is read, so it survives reloads.
        self._demo_override = demo_fallback
        self._status_cache = None  # (monotonic time, status dict)
        self.receiver = Receiver()
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
        # Server-side pin store (pins.py): the hub is the source of
        # truth for map pins, so they survive a browser clear, a
        # different browser, and a laptop restart. The path is
        # overridable in config ("pins_db") so tests can point it at
        # a throwaway file; otherwise pins.db next to hub.py.
        _pins_db = config.get("pins_db", "pins.db") or "pins.db"
        if not os.path.isabs(_pins_db):
            _pins_db = os.path.join(BASE_DIR, _pins_db)
        self.pin_store = PinStore(_pins_db)
        self._status_cache = None
        self._shared_status_cache = None
        self._library_roots_cache = None
        # A config change can change the routing index, so the cached
        # router is dropped with the status caches.
        self._router = None
        self._router_lock = threading.Lock()
        # A config change can change what a search returns, so the
        # cached fan-outs are stale — drop them (round 10).
        self.clear_search_cache()

    @staticmethod
    def _atomic_write(path, config):
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
            # Rel L4: fsync before the rename, so a power loss right
            # after the write cannot leave a zero-length config behind.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)

    def save_config(self, config=None):
        # Atomic write (audit flaw #14, round 4): write to a temp file
        # in the same directory and rename over the old config, so a
        # crash mid-save cannot leave a half-written config.json.
        config = self.config if config is None else config
        self._atomic_write(self.config_path, config)
        # Last-known-good copy (round 9): a successful save is a known
        # good config. Best effort — a failure here must not break the
        # save that just succeeded.
        try:
            self._atomic_write(self.config_path + ".good", config)
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
    # A* time budget (audit #3): the slowest observed legitimate
    # route (cross-country, plain A*) takes ~276 s (Braintree to LA,
    # 4.6 min), so a 600 s deadline gives ~2x headroom for a loaded
    # machine while still bounding a pathological search. The route
    # semaphore bounds how many run at once; this bounds how long
    # one runs.
    ROUTE_DEADLINE_SECONDS = 600
    # Search-result cache (round 10): a repeat query within 10 minutes
    # returns the cached fan-out instead of re-paying Recoll + Kiwix.
    # Corpora change rarely, so a 10-minute staleness window is fine;
    # the cache is cleared on config reload and by POST /api/cache/clear.
    SEARCH_CACHE_TTL_SECONDS = 600
    SEARCH_CACHE_MAX_ENTRIES = 200

    def _remote_slot(self, table, ip, size=2):
        """Per-IP BoundedSemaphore for remote clients (Sec M5)."""
        with self._remote_slots_lock:
            slot = table.get(ip)
            if slot is None:
                slot = threading.BoundedSemaphore(size)
                table[ip] = slot
            return slot

    def remote_search_slot(self, ip):
        return self._remote_slot(self._remote_search_slots, ip)

    def remote_download_slot(self, ip):
        return self._remote_slot(self._remote_download_slots, ip)

    def remote_route_slot(self, ip):
        # Audit #3: routes are heavier than searches, so a remote
        # client gets 1 concurrent route, not 2.
        return self._remote_slot(self._remote_route_slots, ip, size=1)

    def remote_geocode_slot(self, ip):
        # Audit #4: same per-IP cap as search (2).
        return self._remote_slot(self._remote_geocode_slots, ip)

    def clear_search_cache(self):
        """Drop every cached search result (manual clear)."""
        with self._search_cache_lock:
            self._search_cache.clear()
            self._search_cache_generation += 1

    def health(self):
        """Liveness + basic stats for diagnostics and the status strip.

        Liveness ONLY (Rel L1): ok=True just means the process
        answers. It must never be used as a readiness signal —
        readiness is /api/status (per-source, content-checked) plus
        the UI's content-based readiness check. No backends are
        consulted here, and none should be added without a separate
        degraded flag.
        """
        return {
            "ok": True,
            "port": self.listener_port if self.listener_port is not None else self.config.get("port"),
            "sources": len(self.config.get("sources", [])),
            "plugins": len(self.plugins),
            "search_cache": len(self._search_cache),
            "uptime_seconds": round(time.monotonic() - self._started_at),
        }

    @staticmethod
    def source_id(plugin):
        return getattr(plugin, "source_id", plugin.id)

    def kiwix_reader(self):
        return next((p for p in self.plugins if p.enabled and p.kind == "kiwix"), None)

    def reader_prefix(self, reader):
        if reader is self.kiwix_reader():
            return "/kiwix"
        return "/kiwix-readers/" + urllib.parse.quote(self.source_id(reader), safe="")

    def reader_route(self, path):
        if path == "/kiwix" or path.startswith("/kiwix/"):
            return self.kiwix_reader(), "/kiwix", path[len("/kiwix"):] or "/"
        _, _, identifier, *tail = path.split("/", 3)
        source = urllib.parse.unquote(identifier)
        reader = next((p for p in self.plugins if p.enabled and p.kind == "kiwix"
                       and self.source_id(p) == source), None)
        return reader, "/kiwix-readers/" + identifier, "/" + (tail[0] if tail else "")

    def shared_setting(self, key):
        settings = self.config.get("hotspot")
        values = settings.get(key) if isinstance(settings, dict) else None
        return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()

    def shared_plugins(self):
        allowed = self.shared_setting("sources")
        return [p for p in self.plugins if p.enabled and self.source_id(p) in allowed
                and p.kind in ("maps", "recoll", "frequencies", "kiwix")]

    def hotspot_options(self):
        shared_sources = self.shared_setting("sources")
        shared_roots = self.shared_setting("file_roots")
        sources, roots = [], {}
        for plugin in self.plugins:
            if not plugin.enabled or plugin.kind not in ("maps", "recoll", "frequencies", "kiwix"):
                continue
            source = self.source_id(plugin)
            sources.append({"id": source, "name": plugin.display_name, "kind": plugin.kind,
                            "shared": source in shared_sources})
            if plugin.kind == "recoll":
                for name, path in plugin.library_roots().items():
                    if (name and name != "myfiles" and self.library_roots().get(name) == path
                            and not self.is_private_library_path(path)):
                        root = roots.setdefault(name, {"id": name, "sources": [],
                                                       "shared": name in shared_roots})
                        root["sources"].append(source)
        return {"sources": sources, "file_roots": list(roots.values())}

    def set_hotspot(self, body):
        with self.lock:
            options = self.hotspot_options()
            selected = {}
            for key in ("sources", "file_roots"):
                values = body.get(key)
                allowed = {entry["id"] for entry in options[key]}
                if (not isinstance(values, list) or any(
                        not isinstance(value, str) or value not in allowed for value in values)):
                    raise ValueError("Choose from the sources and file roots listed in Settings.")
                selected[key] = list(dict.fromkeys(values))
            if any(root["id"] in selected["file_roots"] and
                   not set(root["sources"]).intersection(selected["sources"])
                   for root in options["file_roots"]):
                raise ValueError("Share a folder’s Recoll source before sharing the folder.")
            config = {**self.config, "hotspot": selected}
            self.save_config(config)
            self.config = config
            self._shared_status_cache = None
            self.clear_search_cache()
            return self.hotspot_options()

    def places(self, *, shared=False):
        """Full gazetteer place list (for the command palette).

        Returns the maps plug-in's normalised places (name, type, lat,
        lon, county, note). Small (303 entries) — the palette fuzzy-
        searches it in memory, so no server round-trip per keystroke.
        """
        for plugin in self.shared_plugins() if shared else self.plugins:
            if plugin.enabled and plugin.kind == "maps":
                try:
                    return plugin.places(shared=shared)
                except Exception:  # noqa: BLE001 - palette must not break search
                    return []
        return []

    def inventory_plugin(self):
        """The inventory plug-in, if one is configured and enabled."""
        for plugin in self.plugins:
            if plugin.enabled and plugin.kind == "inventory":
                return plugin
        return None

    def inventory_state(self):
        """Full inventory state for the UI: items, summary, gaps, expiry.

        Inventory is the household's own stock, so it is never shared
        over the hotspot (the kind gate in shared_plugins() already
        excludes it) and the API below is localhost-only.
        """
        plugin = self.inventory_plugin()
        if plugin is None:
            return {"error": "Inventory is not configured."}
        try:
            return plugin.state()
        except sqlite3.Error as exc:
            # Rel C2: a corrupt/unreadable DB must surface as an error
            # (the handler turns this into a 503), never as an empty
            # 200 with "0 days of water". The message stays generic —
            # no exception text reaches the client.
            _log(f"inventory state failed (sqlite): {exc}")
            return {"error": "Inventory unavailable"}
        except Exception as exc:  # noqa: BLE001 - a broken DB must not 500 the UI
            _log(f"inventory state failed: {exc}")
            return {"error": "Inventory unavailable"}

    def inventory_write(self, body):
        """Apply one inventory write (add / update / delete / use /
        restock / set_settings).

        Returns the fresh state dict so the UI can re-render in one
        round trip. Raises ValueError on bad input (the handler turns
        that into a 400).
        """
        plugin = self.inventory_plugin()
        if plugin is None:
            raise ValueError("Inventory is not configured.")
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        action = str(body.get("action") or "").strip().lower()
        if action == "add":
            plugin.add_item(body.get("item") or body)
        elif action == "update":
            plugin.update_item(body.get("id"), body.get("item") or body)
        elif action == "delete":
            plugin.delete_item(body.get("id"))
        elif action == "use":
            plugin.use_item(body.get("id"))
        elif action == "restock":
            plugin.restock_item(body.get("id"))
        elif action == "set_settings":
            plugin.set_settings(body.get("settings") or {})
        else:
            raise ValueError("Unknown inventory action (add, update, delete, use, restock, set_settings).")
        # Tech L7: wrap state() exactly like inventory_state() does —
        # a state() failure after a successful write returns a clean
        # error dict (the handler turns it into a 503), not a 500.
        return self.inventory_state()

    def pins_list(self):
        """All saved pins, from the server-side store (pins.db)."""
        try:
            try:
                return {"pins": self.pin_store.list_pins(shared=False)}
            except TypeError:
                # PinStore predates the shared= contract; the plain
                # call is equivalent for this localhost-only endpoint.
                return {"pins": self.pin_store.list_pins()}
        except sqlite3.Error as exc:
            # Rel H1: a corrupt/unreadable pins DB must surface as an
            # error (the handler turns this into a 503), never as an
            # empty 200 that a browser could mistake for an empty store
            # and then overwrite with a migration.
            _log(f"pins list failed (sqlite): {exc}")
            return {"error": "Pin storage unavailable"}
        except Exception as exc:  # noqa: BLE001 - a broken DB must not 500 the UI
            _log(f"pins list failed: {exc}")
            return {"error": "Pin storage unavailable"}

    def pins_write(self, body):
        """POST /api/pins actions. Raises ValueError (400) on bad input.

        add: {"action": "add", "pin": {...}} — one pin (a house: label
        is the name, address is the geocoded display address).
        update: {"action": "update", "pin": {...}} — one pin, by id.
        delete: {"action": "delete", "id": "..."} — one pin, by id.
        replace: {"action": "replace", "pins": [...]} — whole list
        (the browser sync pushes the full list on every pin change).
        import: {"action": "import", "pins": [...], "replace": bool}
        — merge (default, skips ids already stored) or replace.
        """
        if not isinstance(body, dict):
            raise ValueError("A JSON object is required.")
        action = body.get("action")
        if action == "add":
            pin = self.pin_store.add_pin(body.get("pin") or {})
            return {"pins": self.pin_store.list_pins(), "pin": pin}
        if action == "update":
            pin = self.pin_store.update_pin(body.get("pin") or {})
            return {"pins": self.pin_store.list_pins(), "pin": pin}
        if action == "delete":
            result = self.pin_store.delete_pin(body.get("id"))
            return {"pins": self.pin_store.list_pins(), "deleted": result["deleted"]}
        if action == "replace":
            pins = body.get("pins")
            if not isinstance(pins, list):
                # Tech M4: a missing "pins" key must not be treated as
                # an intentional empty replace — that would wipe the
                # store. The key must be present AND a list.
                raise ValueError('replace requires a "pins" list')
            # Explicit user replace: allow_shrink=True. The pins.js
            # sync path no longer uses replace, so the default
            # (False) stays safe for any other caller.
            try:
                pins = self.pin_store.replace_all(pins, allow_shrink=True)
            except TypeError:
                # replace_all predates the allow_shrink flag.
                pins = self.pin_store.replace_all(pins)
            return {"pins": pins}
        if action == "import":
            pins = body.get("pins")
            if not isinstance(pins, list):
                raise ValueError('import requires a "pins" list')
            result = self.pin_store.import_pins(
                pins, replace=bool(body.get("replace")))
            return {"pins": result["pins"], "added": result["added"],
                    "skipped": result["skipped"]}
        raise ValueError("Unknown pins action (add, update, delete, replace, import).")

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

    def shared_file_root(self, name):
        if name in ("myfiles", "") or name not in self.shared_setting("file_roots"):
            return ""
        root = self.library_roots().get(name)
        if root and any(p.kind == "recoll" and p.library_roots().get(name) == root
                        for p in self.shared_plugins()):
            return root
        return ""

    def is_private_library_path(self, path):
        roots = [p.folder for p in self.plugins if p.kind == "myfiles" and p.folder]
        roots.extend(p.library_roots().get("myfiles") for p in self.plugins if p.kind == "recoll")
        roots.extend((self.family_plan_store.path.parent, Path(BASE_DIR) / "family_plan.py"))
        resolved = Path(os.path.realpath(path))
        return any(resolved.is_relative_to(os.path.realpath(root)) for root in roots if root)

    def browse_source(self, source, root, folder, page, *, shared):
        """List one directory from a source the client can already open."""
        plugins = self.shared_plugins() if shared else self.plugins
        plugin = next((p for p in plugins if p.enabled and self.source_id(p) == source
                       and p.kind in ("recoll", "myfiles")), None)
        if plugin is None:
            raise FileNotFoundError
        if plugin.kind == "myfiles":
            candidates = {"myfiles": plugin.folder}
        else:
            candidates = {name: path for name, path in plugin.library_roots().items()
                          if name != "myfiles"}
        roots = {}
        for name, path in candidates.items():
            served = self.shared_file_root(name) if shared else self.library_roots().get(name)
            if path and served and os.path.realpath(path) == os.path.realpath(served):
                if not shared or not self.is_private_library_path(served):
                    roots[name] = served

        if not roots:
            return {"roots": [], "entries": [], "message": "No readable folders for this source."}
        root = root or ("manuals" if "manuals" in roots else next(iter(roots)))
        if root not in roots:
            raise FileNotFoundError
        if folder and ("\\" in folder or "\0" in folder or
                       any(part in ("", ".", "..") for part in folder.split("/"))):
            raise FileNotFoundError

        base = Path(os.path.realpath(roots[root]))
        directory = Path(os.path.realpath(base / folder))
        if not directory.is_relative_to(base) or (shared and self.is_private_library_path(directory)):
            raise FileNotFoundError

        entries = []
        message = ""
        try:
            with os.scandir(directory) as children:
                for entry in children:
                    if "\\" in entry.name or (shared and self.is_private_library_path(entry.path)):
                        continue
                    is_folder = entry.is_dir(follow_symlinks=False)
                    if is_folder or entry.is_file(follow_symlinks=False):
                        entries.append((is_folder, entry.name))
        except OSError:
            entries.clear()
            message = "This folder is unavailable."
        entries.sort(key=lambda item: (not item[0], item[1].casefold(), item[1]))
        size = 100
        page = min(max(1, page), max(1, (len(entries) + size - 1) // size))
        start = (page - 1) * size
        prefix = "/files/" + urllib.parse.quote(root, safe="") + "/"
        items = []
        for is_folder, name in entries[start:start + size]:
            rel = f"{folder}/{name}" if folder else name
            try:
                encoded = urllib.parse.quote(rel)
            except UnicodeError:
                continue  # File names that cannot round-trip through a URL.
            item = {"name": name, "folder": rel} if is_folder else {"name": name, "url": prefix + encoded}
            items.append(item)
        return {"roots": list(roots), "root": root, "folder": folder, "page": page,
                "more": start + size < len(entries), "entries": items, "message": message}

    def shared_manual(self, plugin, entry):
        path = urlparse(entry.get("url") or "").path
        parts = path.split("/", 3)
        if len(parts) != 4 or parts[1] != "files":
            return False
        root = self.shared_file_root(parts[2])
        if not root or plugin.library_roots().get(parts[2]) != root:
            return False
        for source in (entry.get("_matched_path"), entry.get("detail")):
            if not source:
                return False
            try:
                base = os.path.realpath(root)
                if (os.path.commonpath((base, os.path.realpath(source))) != base
                        or self.is_private_library_path(source)):
                    return False
            except (ValueError, OSError):
                return False
        return True

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
            terms = " ".join('"' + term.replace('"', '""') + '"*' for term in query.split())
            if not terms:
                return []
            conn = open_readonly(path)
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

    def geocode_search(self, query, limit=10, deadline_seconds=None):
        """Offline street-address search over the OSM address index.

        Fail-open like palette_search: a missing or corrupt index
        returns no rows. A fresh read-only connection is opened per
        call so no connection is shared across handler threads.
        Returns (results, available). The FTS5 query is cut off past
        deadline_seconds (audit #4; default SEARCH_DEADLINE_SECONDS)
        via a SQLite progress handler, so one pathological prefix
        query cannot run unbounded.
        """
        query = (query or "").strip()
        if not query:
            return [], False
        path = self.geocode_db_path()
        if not path:
            return [], False
        if deadline_seconds is None:
            deadline_seconds = self.SEARCH_DEADLINE_SECONDS
        deadline = time.monotonic() + deadline_seconds
        if time.monotonic() >= deadline:
            return [], False
        try:
            conn = geocode_mod.open_index(path)
            try:
                def _progress():
                    return 1 if time.monotonic() >= deadline else 0
                conn.set_progress_handler(_progress, 1000)
                return geocode_mod.search(conn, query, limit), True
            finally:
                conn.close()
        except (sqlite3.Error, ValueError):
            return [], False

    def family_plan(self):
        """Read the saved plan or seed the editor from the optional legacy plan."""
        try:
            plan = self.family_plan_store.read()
        except (OSError, ValueError, sqlite3.Error):
            return {"error": "The saved family plan could not be read."}

        legacy = {}
        if _family_plan_fn is not None:
            try:
                legacy = _family_plan_fn(self) or {}
            except Exception:  # A legacy plan can depend on optional local files.
                legacy = {"error": "The previous family plan could not be read."}
        if plan is None:
            plan = {"revision": 0, "contacts": legacy.get("contacts", []),
                    "meeting_points": legacy.get("meeting_points", []),
                    "instructions": legacy.get("instructions", "")}
            if legacy.get("meeting_points") and "instructions" not in legacy:
                plan["instructions"] = "If phones are down, check the meeting points at 08:00 and 18:00."
            if legacy.get("error"):
                plan["notice"] = "The previous family plan could not be read."
        readiness = legacy.get("readiness")
        if not readiness:
            plugin = self.inventory_plugin()
            try:
                readiness = plugin.summary() if plugin else {"error": "Inventory is not configured."}
                if plugin:
                    readiness["gaps"] = [gap for gap in readiness["gaps"] if gap["id"] in ("water", "food")]
            except (OSError, sqlite3.Error, ValueError):
                readiness = {"error": "Inventory unavailable."}
        return {**plan, "readiness": readiness}

    def routing_db_path(self):
        """Locate the road-routing index (routing.db).

        Same pattern as geocode_db_path: config routing_db wins, else
        routing.db next to hub.py, else the conventional sibling
        <parent>/geocode/routing.db (on the laptop the hub lives in
        C:\\Library\\search-hub and the index in C:\\Library\\geocode).
        Returns "" when no index exists.
        """
        candidates = []
        cfg = (self.config.get("routing_db") or "").strip()
        if cfg:
            # An explicit config path wins outright: no fallback, so
            # a test (or a moved index) gets a deterministic answer.
            candidates.append(cfg)
        else:
            candidates.append(os.path.join(BASE_DIR, "routing.db"))
            candidates.append(os.path.join(
                os.path.dirname(BASE_DIR), "geocode", "routing.db"))
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return ""

    def router(self):
        """The cached Router for this hub, loaded on first use.

        Raises _RoutingUnavailableError when the module or the index
        is missing. The load (a full graph read) happens once per hub
        process; later routes reuse the in-memory graph.
        """
        if routing_mod is None:
            raise _RoutingUnavailableError("road routing is not installed")
        with self._router_lock:
            if self._router is None:
                path = self.routing_db_path()
                if not path:
                    raise _RoutingUnavailableError(
                        "road routing is not installed yet")
                router = routing_mod.Router(path)
                nodes, edges = router.load()
                _log(f"routing index loaded: {nodes:,} nodes, "
                     f"{edges:,} directed edges ({path})")
                self._router = router
            return self._router

    def _resolve_route_point(self, raw):
        """One route endpoint: "lat,lon" or an address string.

        Returns (lat, lon, display). Raises ValueError on bad input
        and LookupError when an address does not resolve.
        """
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("give a starting point and a destination")
        match = COORDINATES.fullmatch(raw)
        if match:
            lat, lon = map(float, match.groups())
            if abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon, f"{lat:g}, {lon:g}"
            raise ValueError("coordinates out of range")
        results, available = self.geocode_search(raw, limit=1)
        if results:
            first = results[0]
            return first["lat"], first["lon"], first.get("display") or raw
        if not available:
            raise ValueError("address lookup is unavailable (no address index)")
        raise LookupError(f"could not find address: {raw}")

    def route(self, from_raw, to_raw):
        """Turn-by-turn road route between two endpoints.

        Each endpoint is "lat,lon" or an address (resolved through
        the offline address index). Returns the router payload with
        the resolved display names attached. Raises ValueError (bad
        input), LookupError (unresolvable address),
        _RoutingUnavailableError (no index), _NoRouteError, or
        _RouteTimeoutError (audit #3: the A* search ran past
        ROUTE_DEADLINE_SECONDS).
        """
        from_lat, from_lon, from_display = self._resolve_route_point(from_raw)
        to_lat, to_lon, to_display = self._resolve_route_point(to_raw)
        result = self.router().route(from_lat, from_lon, to_lat, to_lon,
                                     deadline_seconds=self.ROUTE_DEADLINE_SECONDS)
        result["from"]["display"] = from_display
        result["to"]["display"] = to_display
        return result

    def _search_one(self, plugin, query, *, shared=False):
        started = time.monotonic()
        results, available, note = [], True, ""
        search_failed = False
        try:
            try:
                # The shared flag is passed through so a plug-in can
                # refuse or redact for hotspot clients (contract with
                # the plug-in owners). A plug-in that predates the
                # shared= parameter falls back to the plain call; the
                # hub still filters shared results below.
                results = plugin.search(query, shared=shared) or []
            except TypeError:
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
        if plugin.kind == "kiwix":
            prefix = self.reader_prefix(plugin)
            results = [{**r, "url": prefix + r["url"][len("/kiwix"):]}
                       if (r.get("url") or "").startswith("/kiwix/") else r for r in results]
        if shared and plugin.kind == "recoll":
            results = [{**r, "detail": os.path.basename(r["detail"])}
                       for r in results if self.shared_manual(plugin, r)]
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
                try:
                    st = plugin.status(shared=shared)
                except TypeError:
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

    def _indexed_pdf_link(self, row):
        """Check that the cited page still resolves to the indexed file."""
        pdf_url = row.get("pdf_url") or ""
        parsed = urlparse(pdf_url)
        parts = parsed.path.split("/", 3)
        if len(parts) != 4:
            return ""
        _, _, root, relpath = parts
        base = self.library_roots().get(root)
        if not base or parsed.fragment != f"page={row['page']}":
            return ""
        try:
            library = Path(base).resolve()
            citation = (library / urllib.parse.unquote(relpath)).resolve()
            # Tech M3: compare against the final served PDF path (after
            # the PDF-twin resolution), not _matched_path — for an
            # EPUB/MD match the citation is the PDF twin, so comparing
            # against the EPUB/MD path could never match and the best
            # match never fired for those manuals.
            served = row.get("_served_path") or row.get("_matched_path")
            if not served:
                return ""
            indexed = Path(served).resolve()
            if citation.is_relative_to(library) and citation == indexed and citation.is_file():
                return pdf_url
        except (OSError, RuntimeError, ValueError):
            pass
        return ""

    def _best_match(self, query, groups, deadline, *, shared=False):
        """Promote one verifiable excerpt when the evidence is strong.

        Shared (hotspot) searches never get a direct answer: the
        answer box can carry a full manual passage plus a /files/ PDF
        link, so shared=True returns None immediately (Sec M3). This
        is defense in depth — the call site also only calls this for
        local searches — and sanitize_for_remote() drops any answer
        key as a third layer.
        """
        if shared:
            return None
        if set(search_words(query)).intersection(
                {"not", "without", "avoid", "never", "except", "versus", "vs"}):
            return None
        variants = [_match_terms(phrase) for phrase in expand_query(query)]
        candidates = {}
        for group in groups:
            for row in group["results"]:
                if row.get("demo"):
                    continue
                title = row.get("title") or ""
                url = row.get("url") or ""
                text = row.get("snippet") or ""
                page = row.get("page")
                if url.startswith(("/kiwix/", "/kiwix-readers/")):
                    # Tech L5: strip stopwords from title words too,
                    # so the comparison runs on the same normalised
                    # token sets as the query terms.
                    title_words = tuple(word for word in search_words(title)
                                        if word not in _ANSWER_STOPWORDS)
                    score = max((92 - 3 * i for i, terms in enumerate(variants)
                                 if terms and terms == title_words), default=0)
                    kind = "article"
                elif (type(page) is int and page > 0 and text
                      and not text.startswith("Document: ")
                      and (row.get("pdf_url") or "").startswith("/files/")
                      and row.get("_matched_path")):
                    score = max((_passage_score(terms, title, text) - 3 * i
                                 for i, terms in enumerate(variants)), default=0)
                    kind = "manual"
                else:
                    continue
                if score < 80:
                    continue
                if kind == "manual":
                    url = self._indexed_pdf_link(row)
                    if not url:
                        continue
                answer = {"title": title, "url": url, "source": group["name"], "kind": kind}
                if kind == "manual":
                    answer.update(text=text, page=page)
                if score > candidates.get(url, (0, None))[0]:
                    candidates[url] = (score, answer)
        ranked = sorted(candidates.values(), key=lambda item: item[0], reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 4):
            return None
        answer = ranked[0][1]
        if answer["kind"] == "article":
            reader, _, href = self.reader_route(answer["url"])
            remaining = min(ANSWER_TIMEOUT_SECONDS, deadline - time.monotonic())
            if not getattr(reader, "base_url", "") or remaining <= 0:
                return None
            answer["text"] = _fetch_lead(reader.base_url, href, timeout=remaining)
            if not answer["text"]:
                return None
        return answer

    def search(self, query, source_id="", *, fresh=False, shared=False):
        """Fan-out search with a result cache (round 10).

        A repeat query within the TTL returns the cached fan-out
        instead of re-paying Recoll + Kiwix. The cached result is
        deep-copied on read so a caller (e.g. sanitize_for_remote)
        can never mutate the cache, and deep-copied on write so later
        in-place changes to the returned dict cannot corrupt it.
        """
        normalized_query = (query or "").strip().lower()
        def run_search():
            if shared:
                return self._search_uncached(query, source_id, shared=True)
            return self._search_uncached(query, source_id)

        if fresh or not normalized_query:
            return run_search()
        key = (normalized_query, source_id, shared)
        with self._search_cache_lock:
            generation = self._search_cache_generation
            hit = self._search_cache.get(key)
            if hit is not None:
                when, cached = hit
                if time.monotonic() - when < self.SEARCH_CACHE_TTL_SECONDS:
                    self._search_cache.move_to_end(key)
                    return copy.deepcopy(cached)
                del self._search_cache[key]
        result = run_search()
        with self._search_cache_lock:
            # A search started before a cache clear may still finish.
            if generation == self._search_cache_generation:
                self._search_cache[key] = (
                    time.monotonic(), copy.deepcopy(result)
                )
                while len(self._search_cache) > self.SEARCH_CACHE_MAX_ENTRIES:
                    self._search_cache.popitem(last=False)
        return result

    def _search_uncached(self, query, source_id, *, shared=False):
        plugins = [
            p for p in (self.shared_plugins() if shared else self.plugins)
            if p.enabled and (not source_id or
                              getattr(p, "source_id", p.id) == source_id)
        ]
        if not source_id:
            kind = ""
            # Tech L10: strip the query once before both patterns —
            # COORDINATES used to run on the unstripped query while
            # MANUAL_ID ran on the stripped one.
            stripped = (query or "").strip()
            if COORDINATES.fullmatch(stripped):
                kind = "maps"
            elif exact_frequency(query) is not None:
                kind = "frequencies"
            elif MANUAL_ID.fullmatch(stripped):
                kind = "recoll"
            if kind:
                matching = [p for p in plugins if p.kind == kind]
                if matching:
                    plugins = matching
        if not plugins:
            return {"query": query, "groups": []}
        results = {}

        def run(i, plugin):
            try:
                results[i] = self._search_one(plugin, query, shared=shared)
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
        # Keep the existing local-only excerpt policy. Hotspot searches
        # still use the filtered results from explicitly shared sources.
        # The call-site guard stays AND the shared flag is passed
        # through, so _best_match() itself refuses shared callers
        # (Sec M3, defense in depth).
        answer = self._best_match(query, groups, deadline, shared=shared) if not shared else None
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

    def _status_one(self, plugin, *, shared=False):
        try:
            try:
                st = plugin.status(shared=shared)
            except TypeError:
                # Plug-in predates the shared= parameter.
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

    def status(self, *, shared=False):
        policy = self.config
        now = time.monotonic()
        cache = self._shared_status_cache if shared else self._status_cache
        if cache is not None:
            cached_at, cached = cache
            if now - cached_at < self.STATUS_CACHE_SECONDS:
                return cached
        plugins = self.shared_plugins() if shared else [p for p in self.plugins if p.enabled]
        out = [None] * len(plugins)

        def run(i, plugin):
            try:
                out[i] = self._status_one(plugin, shared=shared)
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
            source = out[i]
            if source is None:
                _log(f"status timed out for source "
                     f"{getattr(plugin, 'source_id', plugin.id)}")
                source = {
                    "id": getattr(plugin, "source_id", plugin.id),
                    "name": getattr(plugin, "display_name", plugin.name),
                    "badge": getattr(plugin, "display_badge", plugin.badge),
                    "available": False,
                    "detail": "status check timed out",
                    "demo": False,
                }
            info = {**source, "kind": plugin.kind}
            if plugin.kind == "kiwix":
                info["reader_url"] = self.reader_prefix(plugin) + "/"
            sources.append(info)
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
        if shared:
            result = {"shared": True, "sources": [{**source, "detail": ""} for source in sources]}
            if self.config is policy:
                self._shared_status_cache = (now, result)
        else:
            self._status_cache = (now, result)
        return result

    # Per-plugin config keys a new source may carry (Sec L6). Before
    # this, add_source() copied every body key verbatim into the
    # persisted config, so a local caller could point a Recoll
    # config_dir / My Files folder at any directory and then share it
    # over the hotspot. Anything not listed here is rejected.
    _SOURCE_KEYS = {
        "kiwix": {"base_url", "medical_zims"},
        "recoll": {"config_dir", "manual_markers", "medical_markers"},
        "maps": {"gazetteer", "quads", "viewer_url_template"},
        "myfiles": {"folder"},
        "frequencies": {"data"},
    }
    _COMMON_SOURCE_KEYS = {"badge", "enabled", "max_results", "timeout_seconds"}

    @staticmethod
    def _validate_source_path(key, value):
        """Validate a path option for a new source (Sec L6).

        The path must exist and live under the hub directory or its
        parent (the library root — C:\\Library on the laptop, which
        holds FieldManuals, MyFiles, the Recoll config, and the map
        fixtures). An empty string means "use the plug-in default"
        and is allowed through. Anything outside the library is
        rejected, so a source can never be pointed at an arbitrary
        directory and then shared over the hotspot.
        """
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a file path")
        if not value:
            return value
        path = value if os.path.isabs(value) else os.path.join(BASE_DIR, value)
        real = os.path.realpath(path)
        parents = [os.path.realpath(BASE_DIR),
                   os.path.realpath(os.path.dirname(BASE_DIR))]
        if not any(os.path.normcase(real) == os.path.normcase(parent)
                   or os.path.normcase(real).startswith(
                       os.path.normcase(parent) + os.sep)
                   for parent in parents):
            raise ValueError(f"{key} must be inside the library ({parents[1]})")
        if key in ("config_dir", "folder"):
            if not os.path.isdir(real):
                raise ValueError(f"{key} is not an existing folder: {value}")
        elif not os.path.isfile(real):
            raise ValueError(f"{key} is not an existing file: {value}")
        return value

    @staticmethod
    def _validate_source_option(plugin, key, value):
        """Validate one add_source() option value; returns the stored value."""
        allowed = Hub._SOURCE_KEYS.get(plugin, set()) | Hub._COMMON_SOURCE_KEYS
        if key not in allowed:
            raise ValueError(f"unsupported option for {plugin}: {key}")
        if key == "max_results":
            if type(value) is not int or not 1 <= value <= 50:
                raise ValueError("max_results must be an integer between 1 and 50")
            return value
        if key == "timeout_seconds":
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 1 <= value <= 60):
                raise ValueError("timeout_seconds must be a number between 1 and 60")
            return value
        if key == "enabled":
            if type(value) is not bool:
                raise ValueError("enabled must be true or false")
            return value
        if key == "badge":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("badge must be non-empty text")
            return value.strip()
        if key == "base_url":
            reader_origin(value)  # raises unless a local reader URL
            return value
        if key in ("medical_zims", "manual_markers", "medical_markers"):
            if (not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)):
                raise ValueError(f"{key} must be a list of text")
            return list(value)
        if key == "viewer_url_template":
            if not isinstance(value, str):
                raise ValueError("viewer_url_template must be text")
            return value
        if key in ("config_dir", "folder", "gazetteer", "quads", "data"):
            return Hub._validate_source_path(key, value)
        raise ValueError(f"unsupported option for {plugin}: {key}")

    def add_source(self, body):
        name = body.get("name")
        plugin = body.get("plugin")
        if not isinstance(name, str) or not isinstance(plugin, str):
            raise ValueError("name and plugin must be text")
        name, plugin = name.strip(), plugin.strip()
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
                entry[key] = self._validate_source_option(plugin, key, value)
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
    return start, min(end, size - 1, start + TILE_CHUNK_MAX_BYTES - 1)


class IPv6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


# Rel L2: ThreadingHTTPServer spawns a thread per connection, with no
# built-in cap — mitigated, not removed, by design: search and download
# work are capped by the 4-slot global semaphores plus the per-IP
# 2-slot limits (Sec M5), status spawns one thread per plug-in
# (bounded by plug-in count), and the audience is localhost + the
# hotspot. The listen backlog is bounded below instead of the socket
# default, so a connection burst fails fast rather than piling up.
class _HubHTTPServer(ThreadingHTTPServer):
    request_queue_size = 32


class _HubIPv6HTTPServer(IPv6HTTPServer):
    request_queue_size = 32


def _bind_server(host, port, handler, tries=10):
    """Bind the hub, falling to the next port if the configured one is
    busy (round 9). A laptop that already has something on 8899 must
    not leave the owner with a dead hub — it takes the next free port and
    says which one. Returns (server, actual_port).
    """
    server_type = _HubIPv6HTTPServer if ":" in host else _HubHTTPServer
    last_exc = None
    for attempt in range(tries):
        candidate = port if port == 0 else port + attempt
        try:
            return server_type((host, candidate), handler), candidate
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


_READER_URL = re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?/")


def _rewrite_reader_url(url):
    """Rewrite an absolute local kiwix-serve URL to the hub's /kiwix/
    proxy prefix (Sec M4). Demo Kiwix results carry absolute
    http://127.0.0.1:8080/<zim>/A/... URLs, which would point at the
    phone itself for a hotspot client; the proxy prefix works from
    any device. Other URLs pass through unchanged."""
    if not isinstance(url, str):
        return url
    return _READER_URL.sub("/kiwix/", url, count=1)


def sanitize_for_remote(search_result):
    """Strip absolute filesystem paths from a /api/search payload.

    For non-localhost clients: file-path result URLs become "" (the
    phone cannot open them anyway) and `detail` lines are reduced to
    file names. Hub-relative URLs (/kiwix/..., /map?...) are kept.
    My Files snippets are the owner's own file contents (F19), so
    they are stripped for remote clients — Kiwix/Recoll snippets are
    public reference material and are kept. Returns a new dict; the
    hub's own result objects are untouched.

    Sec M3/M4 (2026-11-21): also drops the direct-answer box
    (a full manual passage + /files/ PDF link must never reach a
    hotspot client), blanks pdf_url unless it is a hub-relative
    /files/ link, strips paths from snippet and title as well as
    detail, and rewrites absolute kiwix-serve URLs to the /kiwix/
    proxy prefix.
    """
    groups = []
    for group in search_result.get("groups", []):
        results = []
        for r in group.get("results", []):
            r = dict(r)
            if _looks_like_file_path(r.get("url", "")):
                r["url"] = ""
            if "url" in r:
                r["url"] = _rewrite_reader_url(r.get("url", ""))
            if "pdf_url" in r:
                pdf_url = r.get("pdf_url") or ""
                # Keep only a hub-relative /files/ link; a file://
                # path or an absolute reader URL is blanked.
                r["pdf_url"] = pdf_url if pdf_url.startswith("/files/") else ""
            for key in ("detail", "snippet", "title"):
                if key in r:
                    r[key] = _strip_paths(r.get(key, ""))
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
    # The direct answer is never built for shared searches; drop it
    # here anyway as defense in depth (Sec M3).
    out.pop("answer", None)
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _resource_error(self, code, title, detail, *, json_error=None):
            if "text/html" not in self.headers.get("Accept", ""):
                if json_error:
                    self._send_json({"error": json_error}, code)
                else:
                    self.send_error(code, "not found")
                return
            data = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Pandora’s Box</title>
<style>
body {{ color-scheme:dark; background:#080b0f; color:#f7f9fc; font:1rem/1.5 system-ui,sans-serif; margin:0; padding:24px; }}
main {{ max-width:34rem; margin:8vh auto 0; background:#0d1117; border:1px solid #21262d; border-radius:12px; padding:clamp(16px,5vw,32px); }}
h1 {{ margin-top:0; font-size:1.5rem; }} p {{ color:#9aa5b4; }} a {{ color:#00C2FF; }}
nav {{ display:flex; flex-wrap:wrap; gap:8px 20px; margin-top:24px; }}
</style>
<main><h1>{escape(title)}</h1><p>{escape(detail)}</p>
<nav aria-label="Recovery">
<a href="/" target="_blank" rel="noopener">Command Center</a>
<a href="/?view=settings" target="_blank" rel="noopener">Open Settings</a>
</nav></main></html>""".encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _missing_file(self):
            self._resource_error(404, "Offline file unavailable",
                                 "The file could not be opened. Check the local library or search for another copy.")

        def _range_not_satisfiable(self, size):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_file(self, path, content_type):
            # Sec L7: only a regular file under BASE_DIR/static/, and
            # never over 25 MB — the old code read any path whole into
            # RAM with no cap and no type check.
            try:
                real = os.path.realpath(path)
                static_root = os.path.realpath(os.path.join(BASE_DIR, "static"))
                if os.path.commonpath([real, static_root]) != static_root:
                    raise OSError("outside static")
                info = os.stat(real)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 25 * 1024 * 1024:
                    raise OSError("not a servable file")
                with open(real, "rb") as fh:
                    data = fh.read()
            except (OSError, ValueError):
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
        # kiwix-serve API roots that are never article content (audit
        # #7): the proxy serves content fetches only. A request whose
        # first subpath segment is one of these — the OPDS catalog,
        # the search/suggest pages, the viewer skin, the library
        # root — is denied with a 404, so a hotspot client cannot
        # enumerate or query the kiwix-serve API through the hub.
        KIWIX_API_ROOTS = ("catalog", "search", "suggest", "skin",
                           "viewer", "random", "meta", "cache")
        # Sandbox CSP for served HTML (audit #6): the same value
        # _write_document sends for HTML files and _proxy_kiwix sends
        # for proxied articles, so a script in installed ZIM content
        # cannot run as the hub origin. One constant so the two
        # senders cannot drift apart.
        HTML_SANDBOX_CSP = ("sandbox; default-src 'none'; "
                             "img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'")

        def _proxy_kiwix(self, reader, prefix, subpath, query):
            if not self._local_host() and reader not in hub.shared_plugins():
                self._resource_error(404, "Kiwix is laptop-only",
                                     "Ask the owner to share the reference library.")
                return
            # Audit #7: content fetches only — /content/<zim>/... and
            # /<zim>/... (the legacy form the hub links to). Anything
            # whose first segment is a kiwix-serve API root, or the
            # bare library root, is denied. The subpath is unquoted
            # before the check: kiwix-serve decodes the path before
            # routing, so an encoded first segment (/%63atalog/…)
            # must be judged in its decoded form, or it slips past.
            parts = [p for p in urllib.parse.unquote(subpath).split("/") if p]
            if not parts or parts[0] in self.KIWIX_API_ROOTS:
                self._resource_error(404, "Kiwix page unavailable",
                                     "That part of the reader is not shared.",
                                     json_error="kiwix subpath not allowed")
                return
            base = getattr(reader, "base_url", "")
            if not base:
                self._resource_error(502, "Kiwix is not configured",
                                     "Check the offline library sources in Settings.",
                                     json_error="no kiwix source configured")
                return
            url = base + subpath
            if query:
                url += "?" + query
            # Audit #8: cap proxied fetches with the download slots
            # (the /files/ pattern) — a flood of large-article
            # fetches must not starve the owner's downloads.
            remote_download_slot = None
            if not self._local_host():
                remote_download_slot = hub.remote_download_slot(self.client_address[0])
                if not remote_download_slot.acquire(blocking=False):
                    self._resource_error(503, "Hub busy",
                                         "Too many concurrent downloads from this client; retry in a second.",
                                         json_error="too many concurrent downloads from this client")
                    return
            if not hub._download_slots.acquire(blocking=False):
                if remote_download_slot is not None:
                    remote_download_slot.release()
                self._resource_error(503, "Hub busy",
                                     "Too many concurrent downloads; retry in a second.",
                                     json_error="hub busy — too many concurrent downloads")
                return
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "search-hub/1.0"}
                )
                try:
                    with open_reader(req, timeout=5) as resp:
                        body = resp.read(self.PROXY_MAX_BYTES + 1)
                        if len(body) > self.PROXY_MAX_BYTES:
                            self._resource_error(502, "Kiwix page could not open",
                                                 "The reader returned a page too large for the hub. Try another article.",
                                                 json_error="kiwix response too large")
                            return
                        ctype = resp.headers.get(
                            "Content-Type", "application/octet-stream"
                        )
                        if ctype.startswith("text/html"):
                            text = body.decode("utf-8", errors="replace")
                            # Route root-relative links, assets, and GET
                            # forms through the local reader proxy.
                            text = re.sub(
                                r"""(\s(?:href|src|action)\s*=\s*["'])/""",
                                lambda match: match[1] + prefix + "/", text, flags=re.IGNORECASE,
                            )
                            body = text.encode("utf-8")
                            ctype = "text/html; charset=utf-8"
                        self.send_response(resp.status)
                        self.send_header("Content-Type", ctype)
                        self.send_header("X-Content-Type-Options", "nosniff")
                        if ctype.startswith("text/html"):
                            # Audit #6: sandbox proxied articles the same
                            # way _write_document sandboxes served HTML.
                            self.send_header("Content-Security-Policy",
                                             self.HTML_SANDBOX_CSP)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        self._resource_error(404, "Kiwix page unavailable",
                                             "The reader could not find this page. Search again to find content in the installed archives.",
                                             json_error="kiwix-serve returned 404")
                    else:
                        self._resource_error(502, "Kiwix page could not open",
                                             "The reader reported an error. Check its status in Settings.",
                                             json_error=f"kiwix-serve returned {exc.code}")
                except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                    self._resource_error(502, "Kiwix reader unavailable",
                                         "Start the local Kiwix reader or check its status in Settings.",
                                         json_error="kiwix-serve not reachable")
            finally:
                hub._download_slots.release()
                if remote_download_slot is not None:
                    remote_download_slot.release()

        def _is_local(self):
            # Localhost-only writes (POST /api/sources) and the
            # path-stripping rule both key off this: a client on the
            # hotspot is not the laptop, even though it is trusted to
            # search.
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _pmtiles_path(self, name):
            maps_dir = hub.config.get("maps_dir", "") or ""
            if (
                not maps_dir
                or not name.endswith(".pmtiles")
                or "/" in name
                or "\\" in name
                or ":" in name
                or "\x00" in name
                or ".." in name
            ):
                return ""
            return os.path.join(maps_dir, name)

        def _send_pmtiles(self, name):
            """Serve a local .pmtiles archive with bounded Range reads."""
            path = self._pmtiles_path(name)
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
                # Sec L3: bound the digits like _write_document()
                # does — an unbounded int() on a huge Range raised
                # ValueError (Python's int-digit limit) and 500'd.
                # A malformed Range is 416, not a full read.
                m = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_hdr)
                if not m:
                    self._range_not_satisfiable(size)
                    return
                a, b = m.groups()
                if a:
                    start = int(a)
                if b:
                    end = int(b)
                if start > end or start >= size:
                    self._range_not_satisfiable(size)
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
            local = bool(self._local_host())
            base = hub.library_roots().get(root) if local else hub.shared_file_root(root)
            if not base:
                self._missing_file()
                return
            if not rel or rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
                # Sec L26: the ".." check also splits on backslash
                # (a "..\\x" segment on Windows). The realpath +
                # commonpath containment below stays the authoritative
                # control; this is the cheap first gate.
                self._missing_file()
                return
            try:
                base_real = os.path.realpath(base)
                full = os.path.realpath(os.path.join(base_real, rel))
                if (os.path.commonpath([full, base_real]) != base_real or not os.path.isfile(full)
                        or (not local and hub.is_private_library_path(full))):
                    self._missing_file()
                    return
            except (OSError, ValueError):
                self._missing_file()
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
            self._stream_document(full, ctype)

        def _stream_document(self, path, content_type):
            if not os.path.isfile(path):
                self._missing_file()
                return
            # Sec M5: remote clients get at most 2 concurrent
            # downloads each (local clients are exempt); the global
            # 4-slot semaphore below still applies to everyone.
            remote_slot = None
            if not self._is_local():
                remote_slot = hub.remote_download_slot(self.client_address[0])
                if not remote_slot.acquire(blocking=False):
                    self._resource_error(503, "Downloads busy",
                                         "Too many concurrent downloads from this client.",
                                         json_error="too many concurrent downloads")
                    return
            if not hub._download_slots.acquire(blocking=False):
                if remote_slot is not None:
                    remote_slot.release()
                self._resource_error(503, "Downloads busy", "Try again when another download finishes.",
                                     json_error="downloads busy")
                return
            try:
                try:
                    file = open(path, "rb")
                except OSError:
                    self._missing_file()
                    return
                with file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        self._missing_file()
                        return
                    self._write_document(file, info.st_size, path, content_type)
            finally:
                hub._download_slots.release()
                if remote_slot is not None:
                    remote_slot.release()

        def _write_document(self, file, size, path, content_type):
            start, end = 0, size - 1
            parts = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})",
                                 self.headers.get("Range", "").strip())
            ranged = bool(parts and any(parts.groups()))
            if ranged:
                first, last = parts.groups()
                if first:
                    start = int(first)
                    if last:
                        end = min(end, int(last))
                else:
                    start = max(0, size - int(last))
                if start > end:
                    self._range_not_satisfiable(size)
                    return
            length = end - start + 1
            file.seek(start)
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, no-store")
            if content_type.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 self.HTML_SANDBOX_CSP)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if content_type == "application/pdf":
                filename = urllib.parse.quote(os.path.basename(path), safe="")
                self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{filename}")
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            timeout = self.connection.gettimeout()
            self.connection.settimeout(15)
            try:
                self.end_headers()
                while length:
                    chunk = file.read(min(length, 128 * 1024))
                    if not chunk:
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
                    length -= len(chunk)
            except OSError:
                self.close_connection = True
            finally:
                self.connection.settimeout(timeout)

        def _map_pdf_path(self, subdir, name):
            maps_dir = hub.config.get("maps_dir", "") or ""
            if (
                not maps_dir
                or subdir not in ("quads", "trex-arms")
                or not name.endswith(".pdf")
                or "/" in name
                or "\\" in name
                or ":" in name
                or "\x00" in name
                or ".." in name
            ):
                return ""
            return os.path.join(maps_dir, subdir, name)

        def _send_map_pdf(self, subdir, name):
            """Serve an installed GeoPDF or T.REX print PDF, with Range support.

            T.REX print PDFs are field manuals, not maps: a shared
            client only gets them when the Recoll source is shared too
            (Sec M1) — sharing Maps alone must not share the manuals.
            Quads stay gated on Maps (the map-route gate in _do_get).
            """
            if subdir == "trex-arms" and not self._local_host():
                if "recoll" not in {p.kind for p in hub.shared_plugins()}:
                    self._resource_error(404, "Field manuals are laptop-only",
                                         "Ask the owner to share the field manuals.")
                    return
            self._stream_document(self._map_pdf_path(subdir, name), "application/pdf")

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

        # ATAK MBTiles layers (2026-11-22): every *.mbtiles in
        # maps_dir/trex-arms is one selectable ATAK layer. The first
        # file (sorted) is the legacy default: the T.REX Braintree
        # export. Later files are USGS topo packaged the same way
        # (see scripts/build-atak-mbtiles.py). A layer id is the
        # filename stem — never a path — so a tile URL cannot
        # traverse out of the directory.
        def _trex_mbtiles(self):
            """All ATAK MBTiles layers: [{id, path}] sorted by id."""
            maps_dir = hub.config.get("maps_dir", "") or ""
            if not maps_dir:
                return []
            found = sorted(glob.glob(os.path.join(maps_dir, "trex-arms", "*.mbtiles")))
            return [{"id": os.path.splitext(os.path.basename(p))[0], "path": p}
                    for p in found]

        def _trex_mbtiles_path(self, layer_id=None):
            """Path of one ATAK layer (default: the first, the T.REX
            Braintree export). An unknown id returns '' — never a
            path the caller did not get from _trex_mbtiles()."""
            layers = self._trex_mbtiles()
            if not layers:
                return ""
            if layer_id is None:
                return layers[0]["path"]
            for layer in layers:
                if layer["id"] == layer_id:
                    return layer["path"]
            return ""

        def _trex_layers(self):
            """Layer descriptors for /api/map-config: id, label,
            centre, bounds and zoom range, read from each file's
            MBTiles metadata. Centre/bounds identify the covered
            area, so this stays localhost-only like the old
            trex_center. A file without usable metadata is skipped,
            except the T.REX file, which falls back to its known
            Braintree centre/bounds."""
            labels = {
                "trex-arms-19TCG3056073516-atak-z12-15": "Braintree (T.REX)",
                "usgs-massachusetts-z12-15": "Massachusetts (USGS topo)",
                "usgs-new-england-z10-12": "New England (USGS topo)",
                "usgs-new-york-z12-15": "New York (USGS topo)",
                "usgs-new-jersey-z12-15": "New Jersey (USGS topo)",
                "usgs-pennsylvania-z12-15": "Pennsylvania (USGS topo)",
            }
            layers = []
            for layer in self._trex_mbtiles():
                info = {"id": layer["id"],
                        "label": labels.get(layer["id"], layer["id"]),
                        "center": None, "bounds": None, "zoom": 14,
                        "minzoom": 12, "maxzoom": 15}
                try:
                    conn = open_readonly(layer["path"])
                    try:
                        meta = dict(conn.execute(
                            "SELECT name, value FROM metadata").fetchall())
                    finally:
                        conn.close()
                except (sqlite3.Error, OSError):
                    meta = {}
                try:
                    c = [float(v) for v in meta.get("center", "").split(",")]
                    b = [float(v) for v in meta.get("bounds", "").split(",")]
                    if len(c) >= 2 and len(b) == 4:
                        info["center"] = [c[0], c[1]]
                        info["bounds"] = [[b[0], b[1]], [b[2], b[3]]]
                        if len(c) >= 3:
                            info["zoom"] = int(c[2])
                except ValueError:
                    pass
                try:
                    info["minzoom"] = int(meta.get("minzoom", info["minzoom"]))
                    info["maxzoom"] = int(meta.get("maxzoom", info["maxzoom"]))
                except ValueError:
                    pass
                if info["center"] is None:
                    if layer["id"].startswith("trex-arms"):
                        info["center"] = [-71.052185, 42.19545]
                        info["bounds"] = [[-71.213547, 42.120315], [-70.890823, 42.270586]]
                    else:
                        continue
                layers.append(info)
            return layers

        def _send_trex_tile(self, rel):
            """Serve one ATAK raster tile: {z}/{x}/{y}.jpg (legacy:
            the first layer) or <layer-id>/{z}/{x}/{y}.jpg.

            Tiles live in the MBTiles SQLite files in
            maps_dir/trex-arms/ (the T.REX ATAK export, z12-z15,
            JPEG, plus the USGS topo exports built by
            scripts/build-atak-mbtiles.py). MBTiles stores rows in
            TMS order (counted from the bottom), while the map asks
            in XYZ order (from the top), so the row is flipped:
            tms_y = (2**z - 1) - y. Only digit z/x/y are served;
            the layer id must be a known MBTiles stem. Missing
            tiles 404 (MapLibre renders the background there). The
            DB is opened read-only per request — no writes, no lock
            contention.
            """
            parts = rel.split("/")
            if len(parts) == 4:
                layer_id, z, x, yfile = parts
            elif len(parts) == 3:
                layer_id, z, x, yfile = None, parts[0], parts[1], parts[2]
            else:
                self.send_error(404, "not found")
                return
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
            db_path = self._trex_mbtiles_path(layer_id)
            if not db_path:
                self.send_error(404, "not found")
                return
            # Sec (audit #2): the T.REX export covers the home area,
            # so it is gated like the T.REX print PDFs
            # (_send_map_pdf) — a shared client only gets it when
            # the Recoll source is shared too. Sharing Maps alone
            # shares only the USGS topo layers. The layer id is the
            # file stem, so the default (no-layer) form — which
            # resolves to the first sorted layer, the T.REX export —
            # is gated the same as an explicit T.REX id.
            effective_id = os.path.splitext(os.path.basename(db_path))[0]
            if effective_id.startswith("trex-arms") and not self._local_host():
                if "recoll" not in {p.kind for p in hub.shared_plugins()}:
                    self._resource_error(404, "Field manuals are laptop-only",
                                         "Ask the owner to share the field manuals.")
                    return
            tms_y = (1 << z) - 1 - y
            try:
                conn = open_readonly(db_path)
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
                    self._send_json({"error": "Hub request failed."}, 500)
                except OSError:
                    pass  # client already gone; nothing to send to

        def _do_get(self):
            parsed = urlparse(self.path)
            shared = not self._local_host()
            policy = hub.config
            shared_kinds = {p.kind for p in hub.shared_plugins()} if shared else set()
            map_route = (parsed.path in ("/map", "/api/maps/availability", "/api/geocode",
                                        "/api/route")
                         or parsed.path.startswith(("/pdf/", "/tiles/")))
            if shared and map_route and "maps" not in shared_kinds:
                self._resource_error(404, "Maps are laptop-only",
                                     "Ask the owner to share the offline maps.")
                return
            if parsed.path == "/kiwix" or parsed.path.startswith(("/kiwix/", "/kiwix-readers/")):
                # Kiwix article pages, proxied so the links the hub
                # returns work from any device (the phone included) —
                # a direct 127.0.0.1:8080 link would point at the phone
                # itself (audit flaw #3, round 4).
                reader, prefix, subpath = hub.reader_route(parsed.path)
                self._proxy_kiwix(reader, prefix, subpath, parsed.query)
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
                    self._send_library_file(urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[1]))
                else:
                    self._missing_file()
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
                    self._missing_file()
            elif parsed.path == "/api/maps/availability":
                params = parse_qs(parsed.query)
                source = params.get("src", [""])[0]
                maps_dir = hub.config.get("maps_dir", "") or ""
                # Stop at the first matching tile, without walking the
                # entire raster library whenever a user switches styles.
                topo = bool(maps_dir and any(
                    next(glob.iglob(os.path.join(glob.escape(maps_dir), "topo", "*", "*", "*." + ext)), None)
                    for ext in ("jpg", "png")
                ))
                self._send_json({
                    "vector": os.path.isfile(self._pmtiles_path(source.removeprefix("/tiles/"))),
                    "topo": topo,
                    "trex": os.path.isfile(self._trex_mbtiles_path()),
                })
            elif parsed.path.startswith("/tiles/topo/"):
                # USGS topo raster tiles ({z}/{x}/{y}.jpg), downloaded
                # into maps_dir/topo/ — the real topo basemap.
                self._send_topo_tile(parsed.path[len("/tiles/topo/"):])
            elif parsed.path.startswith("/tiles/trex/"):
                # ATAK raster tiles from the MBTiles files in
                # maps_dir/trex-arms/ — the ATAK view. Either
                # {z}/{x}/{y}.jpg (first layer) or
                # <layer-id>/{z}/{x}/{y}.jpg.
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
            elif parsed.path == "/static/pins.js":
                self._send_file(os.path.join(BASE_DIR, "static", "pins.js"), "text/javascript; charset=utf-8")
            elif parsed.path.startswith("/static/brand/"):
                name = parsed.path.removeprefix("/static/brand/")
                if name in ("book.svg", "bookmark.svg", "box-arrow-up-right.svg", "broadcast.svg",
                            "chevron-right.svg", "file-earmark-text.svg", "folder2.svg",
                            "list-ul.svg", "map.svg", "search.svg"):
                    content_type = "image/svg+xml"
                else:
                    content_type = {"pandora.png": "image/png", "terrain.webp": "image/webp",
                                    "inter.woff2": "font/woff2"}.get(name)
                if content_type:
                    self._send_file(os.path.join(BASE_DIR, "static", "brand", name), content_type)
                else:
                    self.send_error(404, "not found")
            elif parsed.path == "/api/source-files":
                params = parse_qs(parsed.query)
                source = params.get("source", [""])[0]
                root = params.get("root", [""])[0]
                folder = params.get("folder", [""])[0]
                try:
                    page = int(params.get("page", ["1"])[0])
                    result = hub.browse_source(source, root, folder, page, shared=shared)
                except (OSError, ValueError):
                    self._send_json({"error": "Source or folder unavailable."}, 404)
                else:
                    self._send_json(result)
            elif parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                query = params.get("q", [""])[0][:500]
                source_id = params.get("source", [""])[0]
                # Bound concurrent searches (round 9): the search slot
                # semaphore caps in-flight searches at 4. An extra
                # request gets a 503 with a retry hint instead of
                # queuing behind a hung backend. Remote clients get
                # at most 2 concurrent searches each (Sec M5); local
                # clients are exempt.
                remote_search_slot = None
                if shared:
                    remote_search_slot = hub.remote_search_slot(self.client_address[0])
                    if not remote_search_slot.acquire(blocking=False):
                        self._send_json(
                            {"error": "too many concurrent searches from "
                                      "this client; retry in a second"}, 503)
                        return
                if not hub._search_slots.acquire(blocking=False):
                    if remote_search_slot is not None:
                        remote_search_slot.release()
                    self._send_json(
                        {"error": "hub busy — too many concurrent "
                                  "searches; retry in a second"}, 503)
                    return
                try:
                    result = hub.search(query, source_id, fresh=params.get("fresh") == ["1"],
                                        shared=shared)
                finally:
                    hub._search_slots.release()
                    if remote_search_slot is not None:
                        remote_search_slot.release()
                if shared:
                    if hub.config is not policy:
                        self._send_json({"error": "Sharing changed. Search again."}, 409)
                        return
                    result = sanitize_for_remote(result)
                    for group in result["groups"]:
                        group["note"] = "" if group["available"] else "Source unavailable."
                for group in result["groups"]:
                    for entry in group.get("results", []):
                        pdf_url = entry.get("pdf_url") or ""
                        if pdf_url.startswith("/pdf/quads/"):
                            name = urllib.parse.unquote(pdf_url.removeprefix("/pdf/quads/"))
                            entry["pdf_available"] = os.path.isfile(self._map_pdf_path("quads", name))
                self._send_json(result)
            elif parsed.path == "/api/status":
                result = hub.status(shared=shared)
                if shared and hub.config is not policy:
                    self._send_json({"error": "Sharing changed. Refresh source status."}, 409)
                    return
                if shared:
                    # Sec L4: call the sanitiser as a second layer
                    # after status(shared=True), which already blanks
                    # the detail lines.
                    result = sanitize_status_for_remote(result)
                self._send_json(result)
            elif parsed.path == "/api/health":
                # Liveness only (Rel L1) — never a readiness signal.
                # Shared clients get just ok + the shared source
                # count: no port, no total source/plugin counts, no
                # search_cache size, no uptime (Sec L2).
                if shared:
                    self._send_json({"ok": True, "sources": len(hub.shared_plugins())})
                else:
                    health = hub.health()
                    health.update(service="pandora-search-hub", instance=hub.instance_id)
                    self._send_json(health)
            elif parsed.path == "/api/map-config":
                # T.REX ATAK centre/bounds, previously hardcoded in
                # static/map.html (Sec M9 follow-up): served only to
                # the laptop — the values identify the home area, so
                # shared clients get a 403 and fall back to a generic
                # CONUS centre.
                if shared:
                    self._send_json({"error": "map configuration is localhost-only"}, 403)
                    return
                # trex_layers lists every ATAK MBTiles layer (id,
                # label, centre, bounds, zoom range) from the files'
                # metadata; the first layer stays the legacy
                # trex_center/trex_bounds default, so a client that
                # only knows those keys keeps working. With no
                # MBTiles installed the hardcoded Braintree values
                # stand, as before.
                layers = self._trex_layers()
                payload = {
                    "trex_center": [-71.052185, 42.19545],
                    "trex_bounds": [[-71.213547, 42.120315], [-70.890823, 42.270586]],
                    "trex_layers": layers,
                }
                if layers:
                    payload["trex_center"] = layers[0]["center"]
                    payload["trex_bounds"] = layers[0]["bounds"]
                self._send_json(payload)
            elif parsed.path == "/api/hotspot":
                if shared:
                    self._send_json({"error": "Manage sharing from the laptop."}, 403)
                else:
                    self._send_json(hub.hotspot_options())
            elif parsed.path == "/api/radio/check":
                if not self._local_host():
                    self._send_json({"error": "Check SDR++ from the local Search Hub on the laptop."}, 403)
                    return
                try:
                    frequency = hub.receiver.check(hub.config)
                except ReceiverError as exc:
                    self._send_json({"error": str(exc)}, 503)
                    return
                self._send_json({"frequency_hz": frequency})
            elif parsed.path == "/api/palette":
                # Fast FTS5 title search for the command palette
                # (round 10). Manual titles only — places and
                # frequencies are small enough for the palette to
                # fuzzy-search in memory.
                query = parse_qs(parsed.query).get("q", [""])[0][:200]
                self._send_json({"results": [] if shared else hub.palette_search(query)})
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
                self._send_json({"places": hub.places(shared=shared)})
            elif parsed.path == "/api/config":
                # Localhost-only (audit 2026-11-20): the config carries
                # absolute laptop paths, and a hotspot client has no
                # business reading it.
                if not self._local_host():
                    self._send_json({"error": "config is localhost-only"}, 403)
                    return
                self._send_json({"sources": hub.config.get("sources", [])})
            elif parsed.path == "/api/frequencies":
                # Full frequency list for the Radio tile — the raw
                # entries from the frequencies plug-in (NOAA + ham +
                # GMRS), so the tile can show every frequency with
                # its details without a search.
                entries = []
                for plugin in hub.shared_plugins() if shared else hub.plugins:
                    if plugin.enabled and plugin.kind == "frequencies":
                        try:
                            entries = plugin._load() or []
                        except Exception:
                            entries = []
                        break
                self._send_json({"entries": entries})
            elif parsed.path == "/api/inventory":
                # Household supplies: items, days of water/food, the
                # shopping list, and the expiry watch. Localhost-only:
                # the family's stock levels are private, like config.
                if not self._local_host():
                    self._send_json({"error": "inventory is localhost-only"}, 403)
                    return
                state = hub.inventory_state()
                if "error" in state:
                    self._send_json(state, 503)
                    return
                self._send_json(state)
            elif parsed.path == "/api/pins":
                # Saved map pins (houses, places): the hub is the
                # source of truth, so pins survive a browser clear
                # and a different browser. Localhost-only: the
                # family's saved locations are private.
                if not self._local_host():
                    self._send_json({"error": "pins are localhost-only"}, 403)
                    return
                state = hub.pins_list()
                if "error" in state:
                    # Rel H1: a corrupt/unreadable pins DB is a 503,
                    # never a 200 with an empty list.
                    self._send_json(state, 503)
                    return
                self._send_json(state)
            elif parsed.path == "/api/family-plan":
                # Household emergency plan: contacts, meeting points,
                # water/food readiness. Localhost-only, like inventory
                # and config — the family's meeting points and stock
                # levels are private.
                if not self._local_host():
                    self._send_json({"error": "family plan is localhost-only"}, 403)
                    return
                state = hub.family_plan()
                if "error" in state:
                    self._send_json(state, 503)
                    return
                self._send_json(state)
            elif parsed.path == "/api/geocode":
                # Offline street-address search (2026-11-20): the
                # map's go-to box asks here when the input starts
                # with a house number. Addresses are public OSM data,
                # so shared clients get results too when Maps is
                # shared — the map-route gate above already 404s when
                # it isn't (Sec L5). Fail-open: no index yet -> empty
                # results and available False.
                query = parse_qs(parsed.query).get("q", [""])[0][:200]
                # Bound concurrent geocode queries (audit #4): the
                # /api/search slot pattern (global 4, 2 per remote
                # client) with a separate semaphore, so a geocode
                # flood cannot eat the search slots (or vice versa).
                remote_geocode_slot = None
                if shared:
                    remote_geocode_slot = hub.remote_geocode_slot(self.client_address[0])
                    if not remote_geocode_slot.acquire(blocking=False):
                        self._send_json(
                            {"error": "too many concurrent address "
                                      "lookups from this client; retry in a second"}, 503)
                        return
                if not hub._geocode_slots.acquire(blocking=False):
                    if remote_geocode_slot is not None:
                        remote_geocode_slot.release()
                    self._send_json(
                        {"error": "hub busy — too many concurrent "
                                  "address lookups; retry in a second"}, 503)
                    return
                try:
                    results, available = hub.geocode_search(query)
                finally:
                    hub._geocode_slots.release()
                    if remote_geocode_slot is not None:
                        remote_geocode_slot.release()
                self._send_json({"results": results, "available": available})
            elif parsed.path == "/api/route":
                # Turn-by-turn road routing (2026-11-22): ?from= &to=
                # are each "lat,lon" or an address (resolved through
                # the offline address index). Roads are public OSM
                # data, so shared clients get routes too when Maps is
                # shared — the map-route gate above already 404s when
                # it isn't. Errors are clean by kind: 400 bad input,
                # 404 unresolvable address or no road route, 503 no
                # routing index installed yet.
                params = parse_qs(parsed.query)
                # Bound concurrent routes (audit #3): A* is the
                # heaviest work the hub does, so routes get their own
                # 2-slot semaphore and remote clients get at most 1
                # each — the /api/search slot pattern, but a separate
                # semaphore so a route flood cannot eat the search
                # slots (or vice versa). An extra request gets a 503
                # with a retry hint instead of queuing behind a route
                # that runs for minutes.
                remote_route_slot = None
                if shared:
                    remote_route_slot = hub.remote_route_slot(self.client_address[0])
                    if not remote_route_slot.acquire(blocking=False):
                        self._send_json(
                            {"error": "too many concurrent routes from "
                                      "this client; retry in a second"}, 503)
                        return
                if not hub._route_slots.acquire(blocking=False):
                    if remote_route_slot is not None:
                        remote_route_slot.release()
                    self._send_json(
                        {"error": "hub busy — too many concurrent "
                                  "routes; retry in a second"}, 503)
                    return
                try:
                    result = hub.route(params.get("from", [""])[0][:500],
                                       params.get("to", [""])[0][:500])
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                except LookupError as exc:
                    self._send_json({"error": str(exc)}, 404)
                    return
                except _NoRouteError as exc:
                    self._send_json({"error": str(exc)}, 404)
                    return
                except _RouteTimeoutError as exc:
                    self._send_json({"error": str(exc)}, 503)
                    return
                except _RoutingUnavailableError as exc:
                    self._send_json({"error": str(exc)}, 503)
                    return
                finally:
                    hub._route_slots.release()
                    if remote_route_slot is not None:
                        remote_route_slot.release()
                self._send_json(result)
            else:
                self.send_error(404, "not found")

        def do_POST(self):
            try:
                self._do_post()
            except Exception as exc:  # noqa: BLE001 - never drop a connection
                # Sec L1: generic message only, matching do_GET — the
                # exception text is logged, never sent to the client.
                _log(f"POST {self.path!r} failed: {exc}")
                try:
                    self._send_json({"error": "Hub request failed."}, 500)
                except OSError:
                    pass  # client already gone; nothing to send to

        def _do_post(self):
            # Writes are localhost-only: phones on the hotspot may search,
            # but only the laptop itself may change the source list.
            self.close_connection = True
            host = self._local_host()
            if not host or self.headers.get("Origin") != "http://" + host:
                self._send_json({"error": "source changes are localhost-only"}, 403)
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/radio/tune":
                self._tune_radio()
                return
            if parsed.path == "/api/cache/clear":
                # Manual search-cache clear (round 10). Localhost-only,
                # like all writes.
                hub.clear_search_cache()
                self._send_json({"cleared": True})
                return
            if parsed.path not in ("/api/inventory", "/api/pins", "/api/family-plan", "/api/sources", "/api/hotspot"):
                self.send_error(404, "not found")
                return
            if self.headers.get_content_type() != "application/json":
                self._send_json({"error": "Expected JSON."}, 415)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json({"error": "invalid Content-Length"}, 400)
                return
            # Pin imports and whole-plan edits can exceed 64 KiB of JSON.
            _body_limit = 262144 if parsed.path in ("/api/pins", "/api/family-plan") else 65536
            if length < 0 or length > _body_limit:
                self._send_json(
                    {"error": "request body too large (max 64 KiB)"}
                    if _body_limit == 65536 else
                    {"error": "request body too large (max 256 KiB)"}, 413)
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
            if parsed.path == "/api/family-plan":
                try:
                    self._send_json(hub.family_plan_store.save(body))
                except PlanConflict:
                    self._send_json({"error": "The plan changed in another tab. Cancel this edit to load the latest plan."}, 409)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                except (OSError, sqlite3.Error):
                    self._send_json({"error": "The family plan could not be saved."}, 503)
                return
            try:
                if parsed.path == "/api/hotspot":
                    self._send_json(hub.set_hotspot(body))
                    return
                if parsed.path == "/api/inventory":
                    result = hub.inventory_write(body)
                    # Tech L7: a state() failure after a successful
                    # write is a 503 with the clean error dict, not
                    # a 200 carrying the error.
                    self._send_json(result, 503 if "error" in result else 200)
                    return
                if parsed.path == "/api/pins":
                    self._send_json(hub.pins_write(body))
                    return
                entry = hub.add_source(body)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            except sqlite3.Error as exc:
                # Rel C2/H1: a corrupt or locked inventory/pins DB
                # must not surface as a 500 with internals — a clean
                # 503 instead.
                _log(f"POST {parsed.path} database error: {exc}")
                message = ("Inventory unavailable" if parsed.path == "/api/inventory"
                           else "Pin storage unavailable" if parsed.path == "/api/pins"
                           else "Database unavailable")
                self._send_json({"error": message}, 503)
                return
            self._send_json({"added": entry}, 201)

        def _local_host(self):
            host = self.headers.get("Host", "").lower()
            if self._is_local() and re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host):
                return host
            return ""

        def _tune_radio(self):
            # A website in another origin must not be able to launch
            # or tune a desktop app through the localhost hub.
            self.close_connection = True
            host = self._local_host()
            if not host or self.headers.get("Origin") != "http://" + host:
                self._send_json({"error": "Tune from the local Search Hub on the laptop."}, 403)
                return
            if self.headers.get_content_type() != "application/json":
                self._send_json({"error": "Expected JSON."}, 415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128:
                    raise ValueError
                body = json.loads(self.rfile.read(length))
                frequency = body["frequency_hz"]
                if not valid_frequency(frequency):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                self._send_json({"error": "Enter a valid receive frequency in Hz."}, 400)
                return
            try:
                actual = hub.receiver.tune(frequency, hub.config)
            except ReceiverError as exc:
                self._send_json({"error": str(exc)}, 503)
                return
            self._send_json({"frequency_hz": actual})

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
    requested_port = args.port if args.port is not None else hub.config["port"]
    port = requested_port
    serving = host not in ("127.0.0.1", "::1", "localhost")
    try:
        server, port = _bind_server(host, port, make_handler(hub))
    except OSError as exc:
        print(f"[hub] cannot bind {host}:{port} (tried {port}..{port + 9}): {exc}",
              file=sys.stderr)
        _log(f"cannot bind {host}:{port} after 10 tries: {exc}")
        return 1
    port = server.server_address[1]
    hub.listener_port = port
    # Keep the existing port file and record the identity of this launch
    # for the watchdog.
    try:
        hub._atomic_write(PORT_FILE + ".json", {
            "instance": hub.instance_id, "pid": os.getpid(), "port": port,
            "host": host, "requested_port": requested_port, "recorded_at": time.time(),
        })
        with open(PORT_FILE + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(str(port))
            # Rel L4: fsync before the rename, like _atomic_write().
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(PORT_FILE + ".tmp", PORT_FILE)
    except OSError:
        pass
    _log(f"started on {host}:{port} (serving mode: {serving}; "
         f"demo fallback: {hub.demo_fallback})")
    if host in ("0.0.0.0", "127.0.0.1", "localhost"):
        print(f"[hub] Open http://127.0.0.1:{port}/ on the laptop.")
    elif host == "::1":
        print(f"[hub] Open http://[::1]:{port}/ on the laptop.")
    else:
        print(f"[hub] Search Hub listening on {host}:{port}.")
    if requested_port and port != requested_port:
        print("[hub] Using a fallback port. Saved items remain with the browser URL where they were created.")
    if serving:
        print("[hub] SERVING MODE: reachable on the LAN / hotspot; "
              "only explicitly shared sources are available to remote clients.")
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
    # Warm the road-routing graph in the background: the first route
    # otherwise pays the full graph load (~75 s for the 17.7M-edge
    # index). A daemon thread, so a missing index or a slow disk can
    # never delay startup or block shutdown — the lazy load in
    # Hub.router() is unchanged for a route that arrives first.
    def _warm_router():
        try:
            hub.router()
        except Exception as exc:  # noqa: BLE001 - warm-up must never raise
            _log(f"routing warm-up skipped: {exc}")

    threading.Thread(target=_warm_router, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("stopped.")
        print("\n[hub] stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
