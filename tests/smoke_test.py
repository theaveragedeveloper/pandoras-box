#!/usr/bin/env python3
"""Smoke test for the Search Hub. Run from the search-hub folder:

    python3 tests/smoke_test.py

Starts the hub on an ephemeral localhost port with a throwaway config
that points at the fixtures, then checks:
  - the UI page loads and contains the design-preview elements
  - /api/search?q=tourniquet returns groups from all five sources
  - /api/search?q=braintree returns Place results from the gazetteer
  - /api/status reports per-source availability
  - POST /api/sources adds a source (and the throwaway config is used,
    so the real config.json is untouched)
  - hardening (round 4): --no-demo actually disables sample data,
    duplicate sources are refused, a plug-in whose status() fails
    cannot kill a search, and the Recoll/maps/myfiles/Kiwix parsers
    tolerate bad input
  - reliability (round 9): safe-mode startup (corrupt/missing config
    starts from defaults with a backup + last-known-good restore),
    fault injection (garbage/slow/missing backends, a raising plug-in,
    two backends down at once), parallel fan-out with a 30 s search
    deadline, the 4-concurrent-search limit (5th gets 503), port
    fallback + hub.port, mtime reload of the frequencies/gazetteer
    files, the kiwix 5 MB page cap, tiles served in 8 MiB chunks, a
    FIFO that must not block My Files, logging that never raises,
    and parse checks on every .ps1 script
  - audit fixes (2026-11-21, section 38): maps shared-filter mutants,
    corrupt inventory/pins DBs answering 503, pins duplicate-id and
    missing-key 400s, the replace shrink guard, pins merge, the
    frequencies tokeniser, geocode ZIP/state handling, the XSS
    escapes, the map-config endpoint, the PDF-twin citation, and
    fresh-clone completeness (pins_db + shipped files)

Exit code 0 = all checks passed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import Hub, make_handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
from sqlite3 import OperationalError  # noqa: E402
from plugins import kiwix as kiwix_plugin  # noqa: E402
from plugins import frequencies as freq_plugin  # noqa: E402
from plugins import maps as maps_plugin  # noqa: E402
from plugins import myfiles as myfiles_plugin  # noqa: E402
from plugins import recoll as recoll_plugin  # noqa: E402
from plugins.base import PluginBase  # noqa: E402
from sqlite_files import open_readonly, rebuild_index  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(name)


def get(server_url, path):
    with urllib.request.urlopen(server_url + path, timeout=10) as resp:
        return resp.status, resp.read()


def post(server_url, path, body):
    req = urllib.request.Request(
        server_url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": server_url},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def check_hotspot(tmp):
    folder = os.path.join(tmp, "hotspot")
    os.mkdir(folder)
    manuals = os.path.join(folder, "FieldManuals")
    personal = os.path.join(folder, "MyFiles")
    os.mkdir(manuals)
    os.mkdir(personal)
    manual = os.path.join(manuals, "guide.txt")
    private = os.path.join(personal, "notes.txt")
    for path, body in ((manual, "shared reference"), (private, "private reference")):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    nested = os.path.join(manuals, "Handbooks")
    os.mkdir(nested)
    with open(os.path.join(nested, "Field & safety #1.txt"), "w", encoding="utf-8") as fh:
        fh.write("nested reference")
    with open(os.path.join(manuals, "large.pdf"), "wb") as fh:
        fh.write(b"%PDF-")
        fh.seek(16 * 1024 * 1024)
        fh.write(b"%%EOF")
    gazetteer = os.path.join(folder, "places.json")
    with open(gazetteer, "w", encoding="utf-8") as fh:
        json.dump([
            {"name": "Sample public place", "public": True, "lat": 42, "lon": -72},
            {"name": "Sample private place", "lat": 43, "lon": -73},
        ], fh)
    config_file = os.path.join(folder, "config.json")
    config = {"demo_fallback": False, "sources": [
        {"plugin": "maps", "id": "map-samples", "gazetteer": gazetteer},
        {"plugin": "recoll", "id": "reference", "config_dir": folder},
        {"plugin": "myfiles", "id": "personal", "folder": personal},
    ]}
    with open(config_file, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    hub = Hub(config_file)
    recoll = next(p for p in hub.plugins if p.kind == "recoll")
    recoll._topdirs = [manuals, personal, folder]
    recoll.search = lambda q: [
        recoll._result_for(path, text) for path, text in
        ((manual, "shared reference"), (private, "private reference"))
    ]

    class HotspotHandler(make_handler(hub)):
        def _is_local(self):
            return False

    def denied(url, path):
        try:
            get(url, path)
        except urllib.error.HTTPError as exc:
            return exc.code == 404
        return False

    with ThreadingHTTPServer(("127.0.0.1", 0), HotspotHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            check("hotspot starts with no shared sources or direct file access",
                  not json.loads(get(url, "/api/search?q=sample")[1])["groups"]
                  and denied(url, "/map") and denied(url, "/files/manuals/guide.txt")
                  and denied(url, "/api/source-files?source=reference"))
            hub.config["hotspot"] = {
                "sources": ["map-samples", "reference", "personal"],
                "file_roots": ["manuals", "myfiles"],
            }
            browse = json.loads(get(url, "/api/source-files?source=reference")[1])
            nested_files = json.loads(get(url, "/api/source-files?source=reference&folder=Handbooks")[1])
            check("manual browse lists shared folders and opens encoded filenames",
                  browse["roots"] == ["manuals"]
                  and {item["name"] for item in browse["entries"]} == {"Handbooks", "guide.txt", "large.pdf"}
                  and nested_files["entries"] == [{"name": "Field & safety #1.txt",
                                                   "url": "/files/manuals/Handbooks/Field%20%26%20safety%20%231.txt"}]
                  and get(url, nested_files["entries"][0]["url"]) == (200, b"nested reference"))
            check("source browse denies private sources, unshared roots and traversal",
                  denied(url, "/api/source-files?source=personal")
                  and denied(url, "/api/source-files?source=reference&root=myfiles")
                  and denied(url, "/api/source-files?source=reference&root=hotspot")
                  and denied(url, "/api/source-files?source=reference&folder=%2e%2e%2fMyFiles"))
            hub.config["hotspot"]["file_roots"].append("hotspot")
            parent = json.loads(get(url, "/api/source-files?source=reference&root=hotspot")[1])
            check("a shared parent root does not list or enter private folders",
                  all(entry.get("folder") != "MyFiles" for entry in parent["entries"])
                  and denied(url, "/api/source-files?source=reference&root=hotspot&folder=MyFiles"))
            result = json.loads(get(url, "/api/search?q=sample&fresh=1")[1])
            groups = {g["source_id"]: g["results"] for g in result["groups"]}
            check("hotspot search excludes personal sources and private Recoll matches",
                  set(groups) == {"map-samples", "reference"}
                  and [r["title"] for r in groups["reference"]] == ["guide"])
            places = json.loads(get(url, "/api/places")[1])["places"]
            check("hotspot place search and palette only expose marked public places",
                  [p["name"] for p in places] == ["Sample public place"]
                  and [p["title"] for p in groups["map-samples"]] == ["Sample public place"])
            check("direct file reads enforce the same hotspot sharing policy",
                  get(url, "/files/manuals/guide.txt") == (200, b"shared reference")
                  and denied(url, "/files/myfiles/notes.txt")
                  and denied(url, "/files/hotspot/MyFiles/notes.txt"))
            request = urllib.request.Request(url + "/files/manuals/large.pdf",
                                              headers={"Range": "bytes=-5"})
            with urllib.request.urlopen(request, timeout=5) as reply:
                check("document suffix ranges return the requested bytes",
                      reply.status == 206 and reply.read() == b"%%EOF")
            with urllib.request.urlopen(url + "/files/manuals/large.pdf", timeout=5) as reply:
                prefix = reply.read(5)
                data = json.loads(get(url, "/api/search?q=sample+public")[1])
                check("shared search responds while a phone download is open",
                      prefix == b"%PDF-" and int(reply.headers["Content-Length"]) > 16 * 1024 * 1024
                      and any(r["title"] == "Sample public place"
                              for g in data["groups"] for r in g["results"]))
        finally:
            server.shutdown()
            worker.join()


def check_smart_search(tmp):
    root = os.path.join(tmp, "FieldManuals")
    os.mkdir(root)
    config = os.path.join(tmp, "smart-search.json")
    with open(config, "w", encoding="utf-8") as fh:
        json.dump({"sources": [{"plugin": "recoll", "id": "manuals", "name": "Manuals"}],
                   "demo_fallback": False,
                   "hotspot": {"sources": ["manuals"], "file_roots": ["manuals"]}}, fh)
    hub = Hub(config)
    hub.plugins[0]._topdirs = [root]
    for name in ("Winterization", "Electrical Reference", "Second Reference", "Unrelated"):
        with open(os.path.join(root, name + ".pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
    calls = []

    def fake_recoll(args, **kwargs):
        if args[-1] == "-h":
            return SimpleNamespace(returncode=0, stdout="")
        # _live_search quotes every word as a literal term (Sec L24);
        # real recollq matches the quoted words as terms, so the stub
        # strips the quotes before lookup to simulate that. The test
        # queries contain no embedded quotes, so a plain strip is exact.
        query = args[-1].replace('"', "")
        calls.append(query)
        hits = {
            "winterize pipes": [("Winterization", 4, "Winterize pipes: referenced chapter.")],
            "winterization": [("Winterization", 5, "Winterization and seasonal maintenance.")],
            "uninterruptible power supply": [("Electrical Reference", 7, "Uninterruptible power supply reference.")],
            "power reference": [("Electrical Reference", 7, "Power reference excerpt."),
                                ("Second Reference", 2, "Power reference excerpt.")],
            "power outlet": [("Unrelated", 1, "Power equipment inventory.")],
        }
        stdout = ""
        for name, page, text in hits.get(query, []):
            from pathlib import Path
            path = Path(root, name + ".pdf").as_uri()
            stdout += f"application/pdf\t[{path}]\nSNIPPETS\n{page}: {text}\n/SNIPPETS\n"
        return SimpleNamespace(returncode=0, stdout=stdout)

    with patch("plugins.recoll.subprocess.run", fake_recoll):
        question = "how do I keep pipes from freezing?"
        result = hub.search(question, fresh=True)
        answer = result.get("answer", {})
        check("plain-language search retries once and quotes the actual indexed page",
              calls == [question, "winterize pipes"]
              and answer.get("text") == "Winterize pipes: referenced chapter."
              and answer.get("source") == "Manuals"
              and answer.get("url", "").endswith("/Winterization.pdf#page=4"))
        corrected = hub.search("winterizaton", fresh=True)
        expanded = hub.search("UPS", fresh=True)
        check("manual search recognizes a misspelling and an abbreviation",
              (corrected.get("answer") or {}).get("page") == 5
              and (expanded.get("answer") or {}).get("page") == 7)
        for query in ("power reference", "power outlet"):
            result = hub.search(query, fresh=True)
            check("ambiguous or weak matches stay in grouped results: " + query,
                  bool(result["groups"][0]["results"]) and "answer" not in result)
        shared = hub.search(question, fresh=True, shared=True)
        check("shared search still finds the authorized manual while excerpts stay laptop-only",
              bool(shared["groups"][0]["results"]) and "answer" not in shared)


def check_index_storage(tmp):
    path = os.path.join(tmp, "offline index #1.db")
    with rebuild_index(path) as db:
        db.execute("CREATE TABLE titles (title TEXT)")
        db.execute("INSERT INTO titles VALUES (?)", ("Original",))
    try:
        with rebuild_index(path) as db:
            db.execute("CREATE TABLE titles (title TEXT)")
            raise RuntimeError("Interrupted rebuild")
    except RuntimeError:
        pass
    with closing(open_readonly(path)) as db:
        check("a failed rebuild leaves the original SQLite index readable",
              db.execute("SELECT title FROM titles").fetchall() == [("Original",)])
        try:
            db.execute("DELETE FROM titles")
        except OperationalError as exc:
            check("offline indexes open read-only", "readonly" in str(exc))
        else:
            check("offline indexes open read-only", False)


def _tiny_routing_db(path):
    """A 3-node road grid + one isolated node, for routing checks."""
    import sqlite3
    from routing import haversine_m
    conn = sqlite3.connect(path)
    conn.executescript("""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE edges (a INTEGER NOT NULL, b INTEGER NOT NULL, name TEXT NOT NULL,
    length_m REAL NOT NULL, time_s REAL NOT NULL, PRIMARY KEY (a, b));
CREATE INDEX edges_a ON edges (a);
""")
    conn.execute("INSERT INTO meta VALUES ('coverage', 'Test grid')")
    nodes = [(1, 42.20, -71.00), (2, 42.21, -71.00),
             (3, 42.21, -71.01), (4, 42.30, -71.05)]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?)", nodes)
    speed_mps = 40 * 1000.0 / 3600.0
    for (a, b, name) in ((1, 2, "Main Street"), (2, 3, "Granite Street")):
        alat, alon = nodes[a - 1][1], nodes[a - 1][2]
        blat, blon = nodes[b - 1][1], nodes[b - 1][2]
        length = haversine_m(alat, alon, blat, blon)
        for x, y in ((a, b), (b, a)):
            conn.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?)",
                (x, y, name, length, length / speed_mps))
    conn.commit()
    conn.close()
    return path


def _tiny_routing_db_two_components(path):
    """Two separate 3-node road grids + a nodecomp table, for the
    disconnected-pair check: routes inside a component work, routes
    across components fail at once via nodecomp instead of searching
    the whole graph (CONUS vs Hawaii on the US-wide index)."""
    import sqlite3
    from routing import haversine_m
    conn = sqlite3.connect(path)
    conn.executescript("""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE edges (a INTEGER NOT NULL, b INTEGER NOT NULL, name TEXT NOT NULL,
    length_m REAL NOT NULL, time_s REAL NOT NULL, PRIMARY KEY (a, b));
CREATE INDEX edges_a ON edges (a);
CREATE TABLE nodecomp (id INTEGER PRIMARY KEY, comp INTEGER NOT NULL);
""")
    conn.execute("INSERT INTO meta VALUES ('coverage', 'Test grid x2')")
    nodes = [(1, 42.20, -71.00), (2, 42.21, -71.00),
             (3, 42.21, -71.01),
             (5, 43.00, -72.00), (6, 43.01, -72.00),
             (7, 43.01, -72.01)]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?)", nodes)
    by_id = {n[0]: (n[1], n[2]) for n in nodes}
    speed_mps = 40 * 1000.0 / 3600.0
    for (a, b, name) in ((1, 2, "Main Street"), (2, 3, "Granite Street"),
                         (5, 6, "Elm Street"), (6, 7, "Oak Street")):
        alat, alon = by_id[a]
        blat, blon = by_id[b]
        length = haversine_m(alat, alon, blat, blon)
        for x, y in ((a, b), (b, a)):
            conn.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?)",
                (x, y, name, length, length / speed_mps))
    conn.executemany("INSERT INTO nodecomp VALUES (?,?)",
                     [(1, 1), (2, 1), (3, 1), (5, 2), (6, 2), (7, 2)])
    conn.commit()
    conn.close()
    return path


def _add_csr_blobs(path):
    """Pack a tiny routing DB's tables into csr blobs, mirroring
    scripts/build-routing-index-us.py's export_csr (reimplemented
    here with stdlib only, since the VM has no pyosmium). Lets the
    smoke test exercise the hub's blob fast-path loader."""
    import json
    import sqlite3
    from array import array
    from itertools import accumulate
    conn = sqlite3.connect(path)
    lat = array("d")
    lon = array("d")
    ord_of = {}
    for node_id, nlat, nlon in conn.execute(
            "SELECT id, lat, lon FROM nodes ORDER BY id"):
        ord_of[node_id] = len(lat)
        lat.append(nlat)
        lon.append(nlon)
    counts = array("i", [0]) * len(lat)
    names = []
    name_idx = {}
    for a, _b, name, _l, _t in conn.execute(
            "SELECT a, b, name, length_m, time_s FROM edges"):
        counts[ord_of[a]] += 1
        if name not in name_idx:
            name_idx[name] = len(names)
            names.append(name)
    off = array("q", accumulate(counts, initial=0))
    cursor = array("q", off[:len(lat)])
    nbr = array("i", [0]) * sum(counts)
    elen = array("f", [0.0]) * sum(counts)
    etime = array("f", [0.0]) * sum(counts)
    ename = array("i", [0]) * sum(counts)
    for a, b, name, length_m, time_s in conn.execute(
            "SELECT a, b, name, length_m, time_s FROM edges"):
        slot = cursor[ord_of[a]]
        cursor[ord_of[a]] = slot + 1
        nbr[slot] = ord_of[b]
        elen[slot] = length_m
        etime[slot] = time_s
        ename[slot] = name_idx[name]
    comp = array("q", [0]) * len(lat)
    if conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            " AND name='nodecomp'").fetchone()[0]:
        for node_id, c in conn.execute("SELECT id, comp FROM nodecomp"):
            comp[ord_of[node_id]] = c
    conn.execute(
        "CREATE TABLE IF NOT EXISTS csr ("
        " key TEXT PRIMARY KEY, blob BLOB NOT NULL)")
    blobs = {"lat": lat.tobytes(), "lon": lon.tobytes(),
             "off": off.tobytes(), "nbr": nbr.tobytes(),
             "elen": elen.tobytes(), "etime": etime.tobytes(),
             "ename": ename.tobytes(), "comp": comp.tobytes(),
             "names_json": json.dumps(names).encode("utf-8")}
    conn.executemany("INSERT OR REPLACE INTO csr (key, blob) VALUES (?, ?)",
                     [(k, v) for k, v in blobs.items()])
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('csr', '1')")
    conn.commit()
    conn.close()
    return path


def check_family_plan(tmp, base, hub, ui_text):
    """Family Plan tab: localhost renders it, shared clients get 403.

    Skipped (not failed) when the family plan module is not shipped —
    the public repo excludes it, so a staged checkout has no family
    data and the endpoint reports it as not installed.
    """
    if not os.path.isfile(os.path.join(BASE_DIR, "family_plan.py")):
        print("SKIP family plan checks — family_plan module not shipped")
        return
    code, body = get(base, "/api/family-plan")
    data = json.loads(body)
    check("family plan serves contacts, meeting points, readiness to localhost",
          code == 200 and len(data.get("contacts", [])) == 6
          and len(data.get("meeting_points", [])) == 2
          and isinstance(data.get("readiness", {}).get("water_days"), (int, float))
          and isinstance(data.get("readiness", {}).get("food_days"), (int, float)),
          f"got {code} {str(data)[:200]}")
    check("family plan contacts carry the town alert signup",
          any("38276" in (c.get("value") or "") for c in data.get("contacts", [])))
    points = data.get("meeting_points", [])
    check("family plan meeting points carry coordinates and addresses",
          all(isinstance(p.get("lat"), (int, float))
              and isinstance(p.get("lon"), (int, float))
              and p.get("address") for p in points) and len(points) == 2)
    check("family plan readiness carries gaps",
          isinstance(data.get("readiness", {}).get("gaps"), list))
    check("UI has the Family Plan tab and view",
          'id="family-plan-view-link"' in ui_text
          and 'id="family-plan-view"' in ui_text
          and "/api/family-plan" in ui_text)

    class SharedHandler(make_handler(hub)):
        def _is_local(self):
            return False

    with ThreadingHTTPServer(("127.0.0.1", 0), SharedHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            try:
                get(url, "/api/family-plan")
                check("shared client is refused the family plan (403)", False)
            except urllib.error.HTTPError as exc:
                check("shared client is refused the family plan (403)",
                      exc.code == 403, f"got {exc.code}")
        finally:
            server.shutdown()
            worker.join()


def check_routing(tmp, base, routing_db):
    """Road routing: unit checks on a tiny grid + HTTP + sharing."""
    from routing import NoRouteError, Router, RoutingUnavailableError
    router = Router(routing_db)
    result = router.route(42.20, -71.00, 42.21, -71.01)
    legs = result["legs"]
    roads = [leg["road"] for leg in legs[:-1]]
    check("router returns turn-by-turn legs with road names",
          roads == ["Main Street", "Granite Street"]
          and legs[0]["instruction"].startswith("Head ")
          and "Granite Street" in legs[1]["instruction"]
          and result["distance_m"] > 1000
          and result["geometry"][0] == [42.2, -71.0]
          and result["geometry"][-1] == [42.21, -71.01],
          str(legs))
    try:
        router.route(42.20, -71.00, 42.30, -71.05)
        check("router fails cleanly for an unconnected destination", False)
    except NoRouteError:
        check("router fails cleanly for an unconnected destination", True)
    try:
        router.route(42.20, -71.00, 40.0, -70.0)
        check("router fails cleanly far from any road", False)
    except NoRouteError:
        check("router fails cleanly far from any road", True)
    try:
        Router(os.path.join(tmp, "no-such.db")).load()
        check("router reports a missing index as unavailable", False)
    except RoutingUnavailableError:
        check("router reports a missing index as unavailable", True)

    # Disconnected components (nodecomp): routes inside a component
    # work; a route across components fails at once, without a
    # whole-graph search.
    two_comp_db = _tiny_routing_db_two_components(
        os.path.join(tmp, "routing-two-comp.db"))
    router2 = Router(two_comp_db)
    result2 = router2.route(43.00, -72.00, 43.01, -72.01)
    check("router routes inside the second component",
          [leg["road"] for leg in result2["legs"][:-1]]
          == ["Elm Street", "Oak Street"], str(result2["legs"]))
    try:
        router2.route(42.20, -71.00, 43.00, -72.00)
        check("router fails fast for a disconnected component pair", False)
    except NoRouteError:
        check("router fails fast for a disconnected component pair", True)

    # CSR blobs: the same two-component graph packed as blobs loads
    # through the fast path and routes identically; a truncated blob
    # falls back to the plain tables and still routes.
    import shutil as _shutil
    import sqlite3 as _sqlite3
    blob_db = os.path.join(tmp, "routing-blob.db")
    _shutil.copyfile(two_comp_db, blob_db)
    _add_csr_blobs(blob_db)
    router3 = Router(blob_db)
    nodes3, edges3 = router3.load()
    check("router loads the blob-packed graph",
          (nodes3, edges3) == (6, 8) and router3.comp is not None,
          f"got {(nodes3, edges3)}")
    result3 = router3.route(42.20, -71.00, 42.21, -71.01)
    check("router routes from the blob-packed graph",
          [leg["road"] for leg in result3["legs"][:-1]]
          == ["Main Street", "Granite Street"], str(result3["legs"]))
    try:
        router3.route(42.20, -71.00, 43.00, -72.00)
        check("blob-packed router fails fast across components", False)
    except NoRouteError:
        check("blob-packed router fails fast across components", True)
    corrupt_db = os.path.join(tmp, "routing-corrupt-blob.db")
    _shutil.copyfile(blob_db, corrupt_db)
    _c = _sqlite3.connect(corrupt_db)
    _blob = _c.execute("SELECT blob FROM csr WHERE key='nbr'").fetchone()[0]
    _c.execute("UPDATE csr SET blob=? WHERE key='nbr'", (_blob[:4],))
    _c.commit()
    _c.close()
    router4 = Router(corrupt_db)
    result4 = router4.route(42.20, -71.00, 42.21, -71.01)
    check("router falls back to tables when a blob is corrupt",
          [leg["road"] for leg in result4["legs"][:-1]]
          == ["Main Street", "Granite Street"], str(result4["legs"]))

    # HTTP: the main test server points at the same tiny index.
    code, body = get(base, "/api/route?from=42.20,-71.00&to=42.21,-71.01")
    data = json.loads(body)
    check("HTTP route returns legs and geometry",
          code == 200 and [leg["road"] for leg in data["legs"][:-1]]
          == ["Main Street", "Granite Street"], f"got {code} {str(data)[:200]}")
    try:
        get(base, "/api/route?from=42.20,-71.00&to=42.30,-71.05")
        check("HTTP route to an unconnected point is 404", False)
    except urllib.error.HTTPError as exc:
        check("HTTP route to an unconnected point is 404",
              exc.code == 404, f"got {exc.code}")
    try:
        get(base, "/api/route?from=&to=42.21,-71.01")
        check("HTTP route with an empty endpoint is 400", False)
    except urllib.error.HTTPError as exc:
        check("HTTP route with an empty endpoint is 400",
              exc.code == 400, f"got {exc.code}")
    # Address endpoints resolve through the offline address index
    # (the fixture has 123 Main Street, Braintree).
    code, body = get(base, "/api/route?from=123%20Main%20Street%2C%20Braintree&to=42.21,-71.01")
    data = json.loads(body)
    check("HTTP route accepts an address as an endpoint",
          code == 200 and "Braintree" in (data["from"].get("display") or ""),
          f"got {code} {str(data)[:200]}")

    # Sharing: routing follows Maps — a shared client gets routes
    # only when Maps is shared, and the map gate 404s otherwise.
    config_file = os.path.join(tmp, "routing-share.json")
    with open(config_file, "w", encoding="utf-8") as fh:
        json.dump({"demo_fallback": False,
                   "sources": [{"plugin": "maps", "id": "maps",
                                "gazetteer": os.path.join(
                                    BASE_DIR, "fixtures", "gazetteer-ne.json")}],
                   "routing_db": routing_db,
                   "hotspot": {"sources": ["maps"], "file_roots": []}}, fh)
    share_hub = Hub(config_file)

    class SharedHandler(make_handler(share_hub)):
        def _is_local(self):
            return False

    with ThreadingHTTPServer(("127.0.0.1", 0), SharedHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            code, body = get(url, "/api/route?from=42.20,-71.00&to=42.21,-71.01")
            check("shared client gets a route when Maps is shared",
                  code == 200, f"got {code}")
            share_hub.config["hotspot"] = {"sources": [], "file_roots": []}
            try:
                get(url, "/api/route?from=42.20,-71.00&to=42.21,-71.01")
                check("shared client is refused routing when Maps is not shared", False)
            except urllib.error.HTTPError as exc:
                check("shared client is refused routing when Maps is not shared",
                      exc.code == 404, f"got {exc.code}")
        finally:
            server.shutdown()
            worker.join()


def main():
    tmp = tempfile.mkdtemp(prefix="hub-test-")
    try:
        check_hotspot(tmp)
        check_smart_search(tmp)
        check_index_storage(tmp)
        # Throwaway config: same sources, but the test owns the file.
        # The shipped default is demo OFF (round 4); the test opts in
        # explicitly so the demo checks below still exercise the fallback.
        config = json.load(open(os.path.join(BASE_DIR, "config.json")))
        config["demo_fallback"] = True
        config["host"] = "127.0.0.1"
        config["port"] = 0
        # Fixture address index with the same street in several towns and states.
        import sqlite3 as _sqlite3
        _geo_db = os.path.join(tmp, "addresses #offline.db")
        _gc = _sqlite3.connect(_geo_db)
        _gc.executescript("""
CREATE TABLE addresses (
    housenumber TEXT NOT NULL, street_norm TEXT NOT NULL,
    street_raw TEXT NOT NULL, city TEXT NOT NULL,
    postcode TEXT NOT NULL, state TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, display TEXT NOT NULL,
    UNIQUE (housenumber, street_norm, city, state));
CREATE VIRTUAL TABLE addresses_fts USING fts5(
    housenumber, street_norm, city, postcode, state,
    content='addresses', content_rowid='rowid', tokenize='unicode61');
""")
        _geo_rows = [
            ("123", "main street", "Main Street", "braintree", "02184", "MA",
             42.204, -71.004, "123 Main Street, Braintree, MA, 02184"),
            ("456", "washington street", "Washington Street", "braintree", "",
             "MA", 42.22, -71.0, "456 Washington Street, Braintree, MA"),
            ("123", "main street", "Main Street", "exampleville", "",
             "MA", 41.1, -71.1, "123 Main Street, Exampleville, MA"),
            ("123", "main street", "Main Street", "exampleville", "",
             "VT", 44.1, -72.1, "123 Main Street, Exampleville, VT"),
        ]
        for _r in _geo_rows:
            _cur = _gc.execute(
                "INSERT INTO addresses (housenumber, street_norm, street_raw,"
                " city, postcode, state, lat, lon, display)"
                " VALUES (?,?,?,?,?,?,?,?,?)", _r)
            _gc.execute(
                "INSERT INTO addresses_fts(rowid, housenumber, street_norm,"
                " city, postcode, state) VALUES (?,?,?,?,?,?)",
                (_cur.lastrowid, _r[0], _r[1], _r[3], _r[4], _r[5]))
        _gc.commit()
        _gc.close()
        config["geocode_db"] = _geo_db
        # Road-routing index for the route checks: a tiny 3-node grid.
        _routing_db = _tiny_routing_db(os.path.join(tmp, "routing.db"))
        config["routing_db"] = _routing_db
        # Inventory DB: point the inventory source at a throwaway
        # file in tmp, so the test seeds and writes there and never
        # touches the workspace inventory.db.
        for _src in config.get("sources", []):
            if _src.get("id") == "inventory":
                _src["db"] = os.path.join(tmp, "inventory.db")
        # Pins DB: same — a throwaway file, so the test never touches
        # the workspace pins.db.
        config["pins_db"] = os.path.join(tmp, "pins.db")
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)

        hub = Hub(config_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hub))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"

        # 1. UI loads.
        code, body = get(base, "/")
        text = body.decode("utf-8")
        check("UI loads (200)", code == 200, f"got {code}")
        check("UI has search box", 'id="q"' in text)
        check("UI has sources rail + Add a source", 'id="addsrc"' in text)
        # The demo query ("tourniquet" in the UI) was removed in a
        # redesign; the covers line below is the round-8 feature that
        # names the sources one search covers.
        check("UI names the sources one search covers",
              'id="covers"' in text and "One search across" in text)
        check("UI names Pandora's Box Mk1", "Pandora" in text)
        check("dashboard keeps search above quick tools and offers appearance settings",
              0 <= text.find('id="results"') < text.find('id="launchers"')
              and 'id="setting-layout"' in text and 'id="setting-theme"' in text)
        check("home editor follows the readiness link and has separate section and tool lists",
              0 <= text.find('id="home-readiness"') < text.find('id="home-edit"')
              and 'aria-controls="home-editor" aria-expanded="false"' in text
              and 'id="home-sections-order"' in text and 'id="home-tools-order"' in text)
        check("map tile links to both extracts and all four styles in the same tab",
              all(f'href="/map?{option}">' in text
                  for option in ("src=/tiles/conus.pmtiles", "src=/tiles/ne.pmtiles",
                                 "style=dark", "style=light", "style=topo", "style=trex")))
        check("library rows render as links in the same tab",
              'class="src" href="${esc(href)}"${scope}><span class="icon source-icon' in text
              and 'class="source-state"' in text and 'icon-chevron source-chevron' in text)
        browse = json.loads(get(base, "/api/source-files?source=myfiles")[1])
        link = next((item.get("url") for item in browse["entries"]
                     if item["name"] == "water-purification-notes.md"), None)
        check("the My Files destination lists files without a query and links to readable content",
              'id="source-browser"' in text and link == "/files/myfiles/water-purification-notes.md"
              and get(base, link)[0] == 200)
        for filename, content_type in (
                ("pandora.png", "image/png"), ("terrain.webp", "image/webp"), ("inter.woff2", "font/woff2")):
            with urllib.request.urlopen(base + "/static/brand/" + filename, timeout=5) as reply:
                check("offline brand asset serves " + filename,
                      reply.status == 200 and reply.headers.get_content_type() == content_type and bool(reply.read(1)))
        check("empty scoped search offers to search all sources",
              "Search all sources" in text)

        # 2. Search: tourniquet hits Reference, Manual/Medical, My Files.
        code, body = get(base, "/api/search?q=tourniquet")
        data = json.loads(body)
        groups = {g["source_id"]: g for g in data["groups"]}
        check("search returns 6 groups", len(data["groups"]) == 6,
              f"got {len(data['groups'])}")
        _, scoped_body = get(base, "/api/search?q=tourniquet&source=recoll")
        scoped = json.loads(scoped_body)
        check("scoped search returns only Recoll, even after a cached all-source search",
              [g["source_id"] for g in scoped["groups"]] == ["recoll"]
              and bool(scoped["groups"][0]["results"]) and "answer" not in scoped)
        _, scoped_body = get(base, "/api/search?q=water&source=myfiles")
        scoped = json.loads(scoped_body)
        check("My Files scope returns only My Files",
              [g["source_id"] for g in scoped["groups"]] == ["myfiles"]
              and bool(scoped["groups"][0]["results"]))
        original_search = hub._search_uncached
        lookup = {"query": "before repair", "groups": []}
        started = threading.Event()
        release = threading.Event()

        def search_stub(query, source_id):
            if query == "in-flight cache test":
                started.set()
                release.wait(timeout=5)
            return dict(lookup)

        hub._search_uncached = search_stub
        try:
            path = "/api/search?q=readiness-cache-test&source=myfiles"
            get(base, path)
            lookup["query"] = "after repair"
            _, fresh = get(base, path + "&fresh=1")
            _, cached = get(base, path)
            check("readiness search bypasses the hub result cache",
                  json.loads(fresh)["query"] == "after repair"
                  and json.loads(cached)["query"] == "before repair")
            worker = threading.Thread(target=hub.search, args=("in-flight cache test",))
            worker.start()
            running = started.wait(timeout=5)
            hub.clear_search_cache()
            release.set()
            worker.join(timeout=5)
            check("an in-flight search cannot refill a cleared cache",
                  running and not worker.is_alive() and hub.health()["search_cache"] == 0)
        finally:
            release.set()
            hub._search_uncached = original_search
        check("kiwix returns tourniquet hits",
              len(groups["kiwix"]["results"]) >= 1)
        check("kiwix demo-flagged (no live kiwix-serve here)",
              all(r.get("demo") for r in groups["kiwix"]["results"]))
        check("recoll returns tourniquet hits",
              len(groups["recoll"]["results"]) >= 1)
        # My Files has no tourniquet hits in the repo fixtures
        # (the family emergency plan is not shipped); check it with
        # "water", which the sample notes do contain.
        code, body = get(base, "/api/search?q=water")
        wdata = json.loads(body)
        wgroups = {g["source_id"]: g for g in wdata["groups"]}
        check("myfiles returns water hits",
              len(wgroups["myfiles"]["results"]) >= 1)
        check("maps returns no tourniquet places (correct: no place matches)",
              len(groups["maps"]["results"]) == 0)
        badges = {r["badge"] for g in data["groups"] for r in g["results"]}
        check("badges include Reference, Medical",
              {"Reference", "Medical"} <= badges, str(badges))
        wbadges = {r["badge"] for g in wdata["groups"] for r in g["results"]}
        check("myfiles water hits carry the My Files badge",
              "My Files" in wbadges, str(wbadges))

        # 3. Place search: braintree finds the gazetteer.
        code, body = get(base, "/api/search?q=braintree")
        data = json.loads(body)
        maps = {g["source_id"]: g for g in data["groups"]}["maps"]
        check("maps finds Braintree places",
              len(maps["results"]) >= 2, f"got {len(maps['results'])}")
        check("place results carry coordinates",
              all("lat" in r and "lon" in r for r in maps["results"]))
        check("place badge is Place",
              all(r["badge"] == "Place" for r in maps["results"]))

        # 3b. Geocode: street addresses resolve offline.
        code, body = get(base, "/api/geocode?q=123+Main+St%2C+Braintree")
        data = json.loads(body)
        check("geocode returns the Main St address",
              data.get("available") is True and len(data["results"]) == 1
              and abs(data["results"][0]["lat"] - 42.204) < 0.001,
              f"got {data}")
        code, body = get(base, "/api/geocode?q=123+Main+Street+Braintree+MA")
        data = json.loads(body)
        check("geocode handles an address typed without commas",
              data.get("available") is True and len(data["results"]) == 1
              and data["results"][0]["city"] == "braintree",
              f"got {data}")
        code, body = get(base, "/api/geocode?q=123+Main+St+Exampleville+VT")
        data = json.loads(body)
        check("geocode honors the state in an address without commas",
              [row["state"] for row in data["results"]] == ["VT"])
        code, body = get(base, "/api/geocode?q=123+Main+St+Unknownville+MA")
        check("geocode does not substitute a different town for an unknown one",
              json.loads(body)["results"] == [])
        code, body = get(base, "/api/geocode?q=999+Nowhere+St")
        data = json.loads(body)
        check("geocode returns empty for an unknown address",
              data.get("available") is True and data["results"] == [],
              f"got {data}")
        # No index configured: fail-open, available False.
        _no_geo_cfg = os.path.join(tmp, "config-nogeo.json")
        _cfg2 = dict(config)
        _cfg2.pop("geocode_db", None)
        _cfg2["geocode_db"] = os.path.join(tmp, "does-not-exist.db")
        with open(_no_geo_cfg, "w", encoding="utf-8") as fh:
            json.dump(_cfg2, fh)
        _hub_nogeo = Hub(_no_geo_cfg)
        _res, _avail = _hub_nogeo.geocode_search("123 Main St")
        check("geocode reports unavailable with no index",
              _res == [] and _avail is False, f"got {_res}, {_avail}")

        # 4. Hospital search crosses sources.
        code, body = get(base, "/api/search?q=hospital")
        data = json.loads(body)
        maps = {g["source_id"]: g for g in data["groups"]}["maps"]
        check("maps finds hospitals",
              any("Hospital" in r["title"] for r in maps["results"]))

        # 4b. Peak + multi-state town search over the real gazetteer.
        code, body = get(base, "/api/search?q=washington")
        data = json.loads(body)
        maps = {g["source_id"]: g for g in data["groups"]}["maps"]
        check("maps finds Mount Washington (peak)",
              any("Mount Washington" in r["title"] for r in maps["results"]))
        code, body = get(base, "/api/search?q=portland")
        data = json.loads(body)
        maps = {g["source_id"]: g for g in data["groups"]}["maps"]
        check("maps finds Portland, ME",
              any(r["title"] == "Portland" and "Cumberland" in r["detail"]
                  for r in maps["results"]))

        # 5. Status endpoint.
        code, body = get(base, "/api/status")
        data = json.loads(body)
        check("status lists 6 sources", len(data["sources"]) == 6)
        by_id = {s["id"]: s for s in data["sources"]}
        check("status identifies the plug-in kind for library navigation",
              all(by_id[k]["kind"] == k for k in
                  ("kiwix", "recoll", "maps", "frequencies", "myfiles", "inventory")))
        check("maps + myfiles available (local data)",
              by_id["maps"]["available"] and by_id["myfiles"]["available"])
        check("kiwix + recoll unavailable here, demo-flagged",
              (not by_id["kiwix"]["available"]) and by_id["kiwix"]["demo"]
              and (not by_id["recoll"]["available"]) and by_id["recoll"]["demo"])
        # The gazetteer count differs between the full copy (303, with
        # the two family meeting points) and the sanitized repo copy
        # (301): expect whatever the fixture on disk holds.
        gazetteer = json.load(open(os.path.join(
            BASE_DIR, "fixtures", "gazetteer-ne.json")))
        expected_places = len(gazetteer if isinstance(gazetteer, list)
                              else gazetteer.get("places", []))
        check("maps gazetteer holds the full New England set "
              f"({expected_places} places)",
              f"{expected_places} places" in by_id["maps"]["detail"],
              by_id["maps"]["detail"])
        check("status includes library snapshots (not set until setup)",
              data.get("library", {}).get("zim_snapshot_status") == "not set"
              and data.get("library", {}).get("recoll_index_date") == "not set",
              str(data.get("library")))

        # 6. Add a source via the API.
        code, body = post(base, "/api/sources",
                          {"name": "Test Notes", "plugin": "myfiles",
                           "folder": "fixtures/myfiles"})
        check("POST /api/sources returns 201", code == 201, f"got {code}")
        code, body = get(base, "/api/config")
        data = json.loads(body)
        check("new source persisted in config",
              any(s["name"] == "Test Notes" for s in data["sources"]))
        code, body = get(base, "/api/search?q=water")
        data = json.loads(body)
        check("new source answers searches",
              any(g["source_id"] == "test-notes" and g["results"]
                  for g in data["groups"]))

        # 7. Bad input is rejected, hub keeps running.
        try:
            post(base, "/api/sources", {"name": "Bad", "plugin": "nope"})
            check("unknown plugin rejected", False, "got 2xx")
        except Exception as exc:  # urllib raises HTTPError on 400
            check("unknown plugin rejected", "400" in str(exc), str(exc))
        code, _ = get(base, "/api/status")
        check("hub still up after bad input", code == 200)
        for path in ("/api/sources", "/api/cache/clear"):
            req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10).close()
                check("settings write requires a same-origin request " + path, False)
            except urllib.error.HTTPError as exc:
                check("settings write requires a same-origin request " + path, exc.code == 403)
        try:
            post(base, "/api/sources", {"name": "Nonlocal reader", "plugin": "kiwix",
                                       "base_url": "https://example.invalid"})
            check("settings rejects a nonlocal reader", False)
        except urllib.error.HTTPError as exc:
            check("settings rejects a nonlocal reader", exc.code == 400)
        tune_calls = []
        original_tune = hub.receiver.tune
        def tune_stub(hz, config):
            tune_calls.append(hz)
            return hz - 1
        hub.receiver.tune = tune_stub
        try:
            tune_request = urllib.request.Request(
                base + "/api/radio/tune", data=b'{"frequency_hz":162400000}',
                headers={"Origin": base, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(tune_request, timeout=10) as response:
                check("local SDR++ request forwards an integer receive frequency",
                      response.status == 200
                      and json.load(response)["frequency_hz"] == 162399999
                      and tune_calls == [162400000])
            for origin, payload, expected in [
                ("https://example.invalid", b'{"frequency_hz":162400000}', 403),
                (base, b'{"frequency_hz":true}', 400),
            ]:
                request = urllib.request.Request(
                    base + "/api/radio/tune", data=payload,
                    headers={"Origin": origin, "Content-Type": "application/json"},
                )
                code = None
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        code = response.status
                except urllib.error.HTTPError as exc:
                    code = exc.code
                    exc.close()
                check(f"SDR++ rejects invalid or cross-origin requests ({expected})",
                      code == expected and tune_calls == [162400000])
        finally:
            hub.receiver.tune = original_tune

        from socketserver import StreamRequestHandler, TCPServer
        from sdrpp import Receiver

        connections = []
        commands = []
        receiver_frequency = 162400000

        class RigctlFixture(StreamRequestHandler):
            def handle(self):
                nonlocal receiver_frequency
                connections.append(self.client_address)
                for line in self.rfile:
                    commands.append(line)
                    if line == b"\\dump_state\n":
                        report = [
                            b"0", b"2", b"1", b"0.000000 10000000000.000000 0x2ef -1 -1 0x1 0x0",
                            b"0 0 0 0 0 0 0", b"0 0 0 0 0 0 0",
                        ] + [b"0"] * 28
                        self.wfile.write(b"\n".join(report) + b"\n")
                    elif line == b"f\n":
                        self.wfile.write(f"{receiver_frequency}\n".encode("ascii"))
                    elif line == b"F 162550000\n":
                        receiver_frequency = 162549999
                        self.wfile.write(b"RPRT 0\n")

        port_before = hub.config.get("sdrpp_rigctl_port")
        with TCPServer(("127.0.0.1", 0), RigctlFixture) as rigctl:
            thread = threading.Thread(target=rigctl.serve_forever, daemon=True)
            thread.start()
            hub.config["sdrpp_rigctl_port"] = rigctl.server_address[1]
            try:
                _, check_body = get(base, "/api/radio/check")
                check("SDR++ connection check reads frequency without tuning",
                      json.loads(check_body)["frequency_hz"] == 162400000
                      and commands == [b"\\dump_state\n", b"f\n"])
                confirmed = Receiver().tune(162550000, hub.config)
                check("SDR++ verifies identity and tunes over one new connection",
                      len(connections) == 2 and confirmed == 162549999
                      and commands[2:] == [b"\\dump_state\n", b"F 162550000\n", b"f\n"])
            finally:
                if port_before is None:
                    hub.config.pop("sdrpp_rigctl_port", None)
                else:
                    hub.config["sdrpp_rigctl_port"] = port_before
                rigctl.shutdown()

        # 8. --no-demo actually disables the sample fallback (round 4:
        # the old code re-read config.json after setting the flag, so
        # --no-demo silently did nothing).
        hub_nodemo = Hub(config_path, demo_fallback=False)
        check("--no-demo flag is in effect", hub_nodemo.demo_fallback is False)
        data = hub_nodemo.search("tourniquet")
        groups = {g["source_id"]: g for g in data["groups"]}
        check("--no-demo: kiwix returns no sample results",
              len(groups["kiwix"]["results"]) == 0)
        check("--no-demo: recoll returns no sample results",
              len(groups["recoll"]["results"]) == 0)
        wdata = hub_nodemo.search("water")
        wgroups = {g["source_id"]: g for g in wdata["groups"]}
        check("--no-demo: real local sources still answer",
              len(wgroups["myfiles"]["results"]) >= 1)
        check("--no-demo does not rewrite config.json",
              json.load(open(config_path)).get("demo_fallback", True) is True)

        # 9. Bad configs start the hub in safe mode instead of crashing
        # it (round 9: a corrupt config must never leave the owner with a
        # dead hub — it backs the file up, tries the last-known-good
        # copy, and otherwise starts from built-in defaults).
        bad_path = os.path.join(tmp, "bad-config.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            json.dump({"sources": "not-a-list", "port": 0}, fh)
        hub_safe = Hub(bad_path)
        check("non-list sources starts in safe mode (6 default sources)",
              len(hub_safe.plugins) == 6
              and hub_safe.demo_fallback is False,
              f"got {len(hub_safe.plugins)} plugins")
        check("corrupt config is backed up, not deleted",
              len([f for f in os.listdir(tmp)
                   if f.startswith("bad-config.json.corrupt-")]) == 1,
              str(os.listdir(tmp)))
        check("safe-mode hub still searches",
              len(hub_safe.search("tourniquet")["groups"]) == 6)
        with open(bad_path, "w", encoding="utf-8") as fh:
            json.dump({"sources": ["junk",
                                   {"id": "mf", "name": "MF", "plugin": "myfiles",
                                    "folder": "fixtures/myfiles", "enabled": True}],
                       "port": 0}, fh)
        hub_bad = Hub(bad_path)
        check("non-dict source entry skipped, valid source loads",
              len(hub_bad.plugins) == 1
              and getattr(hub_bad.plugins[0], "source_id", "") == "mf")
        hub_missing = Hub(os.path.join(tmp, "does-not-exist.json"))
        check("missing config file starts from defaults (6 sources)",
              len(hub_missing.plugins) == 6)
        # A corrupt main config + a valid last-known-good copy restores
        # the good copy and repairs the main file.
        good_path = bad_path + ".good"
        with open(good_path, "w", encoding="utf-8") as fh:
            json.dump({"sources": [
                {"id": "good-one", "name": "Good One", "plugin": "myfiles",
                 "folder": "fixtures/myfiles", "enabled": True}],
                "port": 0, "demo_fallback": False}, fh)
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write("{this is not json")
        hub_good = Hub(bad_path)
        check("corrupt config restores from last-known-good copy",
              len(hub_good.plugins) == 1
              and getattr(hub_good.plugins[0], "source_id", "") == "good-one",
              f"got {[getattr(p, 'source_id', '') for p in hub_good.plugins]}")
        check("restored config repairs the main file",
              json.load(open(bad_path))["sources"][0]["id"] == "good-one")
        # A successful save writes the last-known-good copy.
        hub_good.save_config()
        check("save_config writes the last-known-good copy",
              os.path.exists(good_path)
              and json.load(open(good_path))["sources"][0]["id"] == "good-one")
        # Zero sources: the hub still starts and serves empty groups.
        zero_path = os.path.join(tmp, "zero-config.json")
        with open(zero_path, "w", encoding="utf-8") as fh:
            json.dump({"sources": [], "port": 0, "demo_fallback": False}, fh)
        hub_zero = Hub(zero_path)
        check("zero sources: search returns empty groups, not an error",
              hub_zero.search("tourniquet") == {"query": "tourniquet", "groups": []})
        check("zero sources: status returns no sources",
              hub_zero.status()["sources"] == [])

        # 10. Duplicate source ids refused; slug collapses repeated dashes.
        try:
            post(base, "/api/sources",
                 {"name": "Test Notes", "plugin": "myfiles",
                  "folder": "fixtures/myfiles"})
            check("duplicate source id rejected", False, "got 2xx")
        except Exception as exc:  # urllib raises HTTPError on 400
            check("duplicate source id rejected", "400" in str(exc), str(exc))
        code, body = post(base, "/api/sources",
                          {"name": "My  Notes!", "plugin": "myfiles",
                           "folder": "fixtures/myfiles"})
        data = json.loads(body)
        check("odd name slugs to my-notes", code == 201
              and data.get("added", {}).get("id") == "my-notes",
              f"got {code} {data}")

        # 11. A plug-in whose status() fails cannot kill a search.
        def _boom():
            raise RuntimeError("boom")

        fake_real = PluginBase({"name": "Bad Status", "badge": "Reference"},
                               {"base_dir": BASE_DIR, "demo_fallback": True})
        fake_real.id = "badstatus"
        fake_real.source_id = "badstatus"
        fake_real.display_name = "Bad Status"
        fake_real.display_badge = "Reference"
        fake_real.search = lambda q: [{"title": "Real hit", "snippet": "x",
                                       "url": "", "badge": "Reference",
                                       "detail": "", "demo": False}]
        fake_real.status = _boom
        fake_empty = PluginBase({"name": "Bad Status 2", "badge": "Reference"},
                                {"base_dir": BASE_DIR, "demo_fallback": True})
        fake_empty.id = "badstatus2"
        fake_empty.source_id = "badstatus2"
        fake_empty.display_name = "Bad Status 2"
        fake_empty.display_badge = "Reference"
        fake_empty.search = lambda q: []
        fake_empty.status = _boom
        fake_nondict = PluginBase({"name": "Bad Status 3", "badge": "Reference"},
                                  {"base_dir": BASE_DIR, "demo_fallback": True})
        fake_nondict.id = "badstatus3"
        fake_nondict.source_id = "badstatus3"
        fake_nondict.display_name = "Bad Status 3"
        fake_nondict.display_badge = "Reference"
        fake_nondict.search = lambda q: []
        fake_nondict.status = lambda: "up"  # not a dict
        hub.plugins.extend([fake_real, fake_empty, fake_nondict])
        # Round 2 cache: the test mutates hub.plugins directly (bypassing
        # _apply_config, which clears the cache), so clear it here or
        # the cached "tourniquet" result from before these plug-ins
        # existed is returned and the new groups are missing.
        hub.clear_search_cache()
        data = hub.search("tourniquet")
        groups = {g["source_id"]: g for g in data["groups"]}
        check("real results survive a failing status()",
              groups["badstatus"]["available"] is True)
        check("empty results + failing status() marks source down",
              groups["badstatus2"]["available"] is False
              and "status error" in groups["badstatus2"]["note"])
        check("non-dict status() is handled, not fatal",
              groups["badstatus3"]["available"] is False
              and "bad status" in groups["badstatus3"]["note"])
        code, _ = get(base, "/api/search?q=tourniquet")
        check("HTTP search still 200 with failing-status plug-ins",
              code == 200, f"got {code}")
        hub.plugins = [p for p in hub.plugins
                       if getattr(p, "source_id", "") not in
                       ("badstatus", "badstatus2", "badstatus3")]

        # 12. Recoll parser + config clamps.
        rp_clamp = recoll_plugin.Plugin(
            {"timeout_seconds": 0, "max_results": -5},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        check("recoll timeout clamped to >= 1", rp_clamp.timeout == 1)
        check("recoll max_results clamped to >= 1", rp_clamp.max_results == 1)
        rp = recoll_plugin.Plugin({}, {"base_dir": BASE_DIR, "demo_fallback": False})
        text = ("file:///C:/Library/FieldManuals/fm-3-05-70.pdf\n"
                "Survival manual abstract here.\n\n"
                "Just a snippet line with no URL.\nMore snippet text.\n\n"
                "file:///C:/Library/FieldManuals/tc-4-02-1.pdf\nTCCC abstract.")
        parsed = rp._parse_recollq(text)
        check("recoll parser keeps real file:// blocks", len(parsed) == 2,
              f"got {len(parsed)}")
        check("recoll parser drops snippet-only blocks",
              all(p["url"].startswith("file://") for p in parsed))
        check("recoll path check accepts a Windows path",
              recoll_plugin.Plugin._looks_like_path("C:\\Docs\\file.pdf") is True)
        check("recoll path check rejects plain text",
              recoll_plugin.Plugin._looks_like_path("hello world") is False)
        # The real recollq -t -A format is line-based with NO blank
        # lines (audit 2026-11-20): the old parser split on "\n\n"
        # and returned zero results for it. This sample mirrors the
        # live output (MIME header, ABSTRACT block, next header).
        real_out = (
            "Recoll query: Query((tourniquet))\n"
            "230 results\n"
            "text/html\t[file:///C:/Library/FieldManuals/EPUB/Medical & TCCC/FM 8-10-4 (1990-11) Medical Platoon.epub]\t[ch013.xhtml]\t32307\tbytes\t\n"
            "ABSTRACT\n"
            "Cut clothing around bandages, tourniquets, and splints.\n"
            "/ABSTRACT\n"
            "application/pdf\t[file:///C:/Library/FieldManuals/PDF/Doctrine & Field Manuals/Small-Unit Tactics/ATP 3-06.11 (2024-09) BRIGADE COMBAT TEAM URBAN OPERATIONS.pdf]\t\t12345\tbytes\t\n"
            "ABSTRACT\n"
            "Urban operations tourniquet application.\n"
            "/ABSTRACT\n"
        )
        parsed_real = rp._parse_recollq(real_out)
        check("recoll parser handles the real -t -A format",
              len(parsed_real) == 2, f"got {len(parsed_real)}")
        check("recoll parser extracts the PDF result",
              any(p["title"].startswith("ATP 3 06.11") for p in parsed_real),
              f"got {[p['title'] for p in parsed_real]}")
        check("recoll parser keeps the abstract as the snippet",
              any("tourniquet application" in p["snippet"] for p in parsed_real),
              f"got {[p['snippet'] for p in parsed_real]}")
        # -n must come before -q: a trailing "-n 3" is absorbed into
        # the query and recollq returns everything. The command the
        # plug-in builds must have -n first.
        import subprocess as _sp
        _seen = {}
        _real_run = _sp.run
        def _cap(cmd, **kw):
            _seen["cmd"] = list(cmd)
            class _P:
                returncode = 0
                stdout = ""
            return _P()
        _sp.run = _cap
        try:
            rp._live_search("tourniquet")
        except Exception:
            pass
        finally:
            _sp.run = _real_run
        _cmd = _seen.get("cmd", [])
        check("recoll -n comes before -q in the command",
              "-n" in _cmd and "-q" in _cmd and _cmd.index("-n") < _cmd.index("-q"),
              f"got {_cmd}")
        # EPUB/MD results prefer the PDF sibling (2026-11-20): a
        # browser downloads an EPUB instead of opening it, so the
        # manual must link its PDF copy when one sits next to it.
        _man_dir = os.path.join(tmp, "manuals")
        os.makedirs(_man_dir, exist_ok=True)
        _stem = os.path.join(_man_dir, "FM 8-10-4 Test Manual")
        open(_stem + ".epub", "wb").close()
        open(_stem + ".pdf", "wb").close()
        _res = rp._result_for(
            "file://" + _stem.replace(os.sep, "/") + ".epub", "snippet")
        check("recoll EPUB result links the PDF sibling",
              _res["url"].endswith(".pdf") and "Test Manual" in _res["title"],
              f"got {_res['url']}")
        check("recoll EPUB result detail is the PDF path",
              _res["detail"].endswith(".pdf"), f"got {_res['detail']}")
        _md = os.path.join(_man_dir, "Lonely Note")
        open(_md + ".md", "wb").close()
        _res2 = rp._result_for(
            "file://" + _md.replace(os.sep, "/") + ".md", "snippet")
        check("recoll result without a PDF sibling keeps its file",
              _res2["url"].endswith(".md"), f"got {_res2['url']}")
        # Format-dir twin (2026-11-20): the vault keeps PDF/EPUB/MD
        # as sibling top-level dirs with identical relative paths, so
        # an EPUB under EPUB/<rel> has its PDF under PDF/<rel>.
        _vault = os.path.join(tmp, "FieldManuals")
        _epub_sub = os.path.join(_vault, "EPUB", "Cat One")
        _pdf_sub = os.path.join(_vault, "PDF", "Cat One")
        os.makedirs(_epub_sub, exist_ok=True)
        os.makedirs(_pdf_sub, exist_ok=True)
        _twin_epub = os.path.join(_epub_sub, "FM 1-2 Twin Manual.epub")
        _twin_pdf = os.path.join(_pdf_sub, "FM 1-2 Twin Manual.pdf")
        open(_twin_epub, "wb").close()
        open(_twin_pdf, "wb").close()
        _orig_roots = rp.library_roots
        rp.library_roots = lambda: {"manuals": _vault}
        try:
            _res3 = rp._result_for(
                "file://" + _twin_epub.replace(os.sep, "/"), "snippet", page=8)
            _dated_pdf = os.path.join(_pdf_sub, "Test Manual (2024-09).pdf")
            open(_dated_pdf, "wb").close()
            _indexed_path = _dated_pdf.replace(os.sep, "/").replace("(", "%28").replace(")", "%29")
            paged = rp._parse_recollq(
                f"application/pdf\t[file://{_indexed_path}]"
                "\t[Test Manual]\t123\tbytes\n"
                "SNIPPETS\n0 : Position unavailable.\n12 : The matching passage.\n/SNIPPETS\n"
            )[0]
        finally:
            rp.library_roots = _orig_roots
        check("recoll EPUB result links the format-dir PDF twin",
              _res3["url"].endswith("FM%201-2%20Twin%20Manual.pdf")
              or _res3["url"].endswith("FM 1-2 Twin Manual.pdf"),
              f"got {_res3['url']}")
        check("recoll EPUB twin detail is the PDF path",
              _res3["detail"].endswith(".pdf"), f"got {_res3['detail']}")
        check("Recoll opens a matching PDF at its indexed page",
              paged.get("page") == 12 and paged.get("pdf_url", "").endswith(".pdf#page=12")
              and paged["snippet"] == "The matching passage.")
        check("Recoll exposes an explicit filename date label", paged.get("date_label") == "2024-09")
        check("Recoll EPUB page is never applied to the PDF twin",
              "page" not in _res3 and _res3.get("pdf_url", "").endswith(".pdf"))

        # 13. Maps plug-in tolerates bad gazetteer rows + bad templates.
        gz = os.path.join(tmp, "bad-gazetteer.json")
        with open(gz, "w", encoding="utf-8") as fh:
            json.dump([
                "a stray string",
                {"name": None, "type": "Town", "lat": 42.0, "lon": -71.0},
                {"name": "Nowhere", "type": "Town", "lat": 0, "lon": 0},
                {"name": "Testville", "type": "Town", "lat": 42.5,
                 "lon": -71.5, "county": "Test", "aliases": ""},
            ], fh)
        mp = maps_plugin.Plugin(
            {"gazetteer": gz,
             "viewer_url_template": "http://x/{lat}/{lon}/{unknown}"},
            {"base_dir": BASE_DIR, "demo_fallback": True})
        res = mp.search("testville")
        check("maps skips bad gazetteer rows",
              len(res) == 1 and res[0]["title"] == "Testville",
              f"got {len(res)}")
        check("maps survives a bad viewer template", res[0]["url"] == "")
        check("maps status counts only good rows",
              "1 places" in mp.status()["detail"], mp.status()["detail"])

        # 14. My Files skips files over 5 MB.
        mf_dir = os.path.join(tmp, "myfiles")
        os.makedirs(mf_dir)
        with open(os.path.join(mf_dir, "small.txt"), "w", encoding="utf-8") as fh:
            fh.write("needle in a small file\n")
        with open(os.path.join(mf_dir, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write("needle " + "x" * (6 * 1024 * 1024))
        mfp = myfiles_plugin.Plugin({"folder": mf_dir},
                                    {"base_dir": BASE_DIR, "demo_fallback": True})
        res = mfp.search("needle")
        check("myfiles skips files over 5 MB",
              len(res) == 1 and res[0]["title"] == "small.txt",
              f"got {[r['title'] for r in res]}")

        # 15. Kiwix parser skips protocol-relative links.
        parser = kiwix_plugin._SearchPageParser()
        parser.feed('<html><body><a href="//cdn.example.com/x">CDN</a>'
                    '<a href="/wikipedia_en_all_nopic/A/Tourniquet">Tourniquet</a>'
                    ' tail text</body></html>')
        check("kiwix parser skips protocol-relative links",
              len(parser.results) == 1
              and parser.results[0]["title"] == "Tourniquet",
              f"got {len(parser.results)}")

        # 16. Oversized body + very long query.
        try:
            post(base, "/api/sources",
                 {"name": "X" * 70000, "plugin": "myfiles"})
            check("oversized POST body rejected", False, "got 2xx")
        except Exception as exc:  # urllib raises HTTPError on 413
            check("oversized POST body rejected", "413" in str(exc), str(exc))
        code, _ = get(base, "/api/search?q=" + "x" * 600)
        check("very long query returns 200 (truncated)", code == 200,
              f"got {code}")

        # 17. Status cache: consistent, and invalidated by a new source.
        code, body = get(base, "/api/status")
        data1 = json.loads(body)
        code, body = get(base, "/api/status")
        data2 = json.loads(body)
        check("status cache returns consistent data",
              len(data1["sources"]) == len(data2["sources"]) == 8,
              f"got {len(data1['sources'])} / {len(data2['sources'])}")
        check("status shows the source added in section 10",
              any(s["id"] == "my-notes" for s in data1["sources"]))

        # 18. Shipped config defaults demo fallback OFF (round 4 audit).
        shipped = json.load(open(os.path.join(BASE_DIR, "config.json")))
        check("shipped config defaults demo fallback OFF",
              shipped.get("demo_fallback") is False)
        saved = json.loads(post(base, "/api/hotspot", {"sources": ["maps"], "file_roots": []})[1])
        with open(config_path, encoding="utf-8") as fh:
            stored_policy = json.load(fh)["hotspot"]
        check("sharing settings persist only the selected sources",
              [s["id"] for s in saved["sources"] if s["shared"]] == ["maps"]
              and stored_policy == {"sources": ["maps"], "file_roots": []})
        try:
            post(base, "/api/hotspot", {"sources": ["myfiles"], "file_roots": []})
            check("sharing settings reject My Files", False)
        except urllib.error.HTTPError as exc:
            check("sharing settings reject My Files", exc.code == 400)
        post(base, "/api/hotspot", {"sources": [], "file_roots": []})
        coords = json.loads(get(base, "/api/search?q=42.2%2C-71.0")[1])
        radio = json.loads(get(base, "/api/search?q=162.475+MHz")[1])
        check("coordinates and frequency lookups skip unrelated sources",
              len(coords["groups"]) == len(radio["groups"]) == 1
              and coords["groups"][0]["results"][0]["url"].startswith("/map?lat=42.2")
              and all(r["frequency_mhz"] == "162.475" for r in radio["groups"][0]["results"])
              and bool(radio["groups"][0]["results"]))

        # 19. Kiwix proxy: result links are hub-relative and the hub
        # relays article pages (audit flaw #3).
        from http.server import BaseHTTPRequestHandler

        class FakeKiwix(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/search"):
                    body = (b'<html><body><a href="/wikipedia_en_all_nopic/A/Tourniquet">'
                            b'Tourniquet</a> stops bleeding</body></html>')
                elif self.path.startswith("/wikipedia_en_all_nopic/A/Tourniquet"):
                    body = (b'<html><body><h1>Tourniquet</h1>'
                            b'<div class="mw-parser-output"><p>Sample lead paragraph from the installed reference archive. '
                            b'This article is used to check local search citations while testing offline.</p></div>'
                            b'<a href="/wikipedia_en_all_nopic/A/Other">Other</a>'
                            b"<form action='/search'><input name='pattern'></form>"
                            b'</body></html>')
                    if getattr(self.server, "second_reader", False):
                        body = body.replace(b"Tourniquet</h1>", b"Second reader article</h1>")
                else:
                    self.send_error(404, "not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeKiwix)
        fake_port = fake.server_address[1]
        fake_thread = threading.Thread(target=fake.serve_forever, daemon=True)
        fake_thread.start()
        kiwix_plugin_inst = next(
            p for p in hub.plugins if getattr(p, "kind", "") == "kiwix")
        kiwix_plugin_inst.base_url = f"http://127.0.0.1:{fake_port}"
        res = kiwix_plugin_inst.search("tourniquet")
        check("kiwix live results use hub-relative /kiwix/ URLs",
              len(res) == 1 and res[0]["url"].startswith("/kiwix/"),
              f"got {[r.get('url') for r in res]}")
        code, body = get(base, "/kiwix/wikipedia_en_all_nopic/A/Tourniquet")
        text = body.decode("utf-8")
        check("hub proxies kiwix article pages", code == 200
              and "Tourniquet" in text, f"got {code}")
        check("proxied article links stay inside /kiwix/",
              'href="/kiwix/wikipedia_en_all_nopic/A/Other"' in text)
        check("proxied reader forms submit to Kiwix instead of the hub root",
              "action='/kiwix/search'" in text)
        with ThreadingHTTPServer(("127.0.0.1", 0), FakeKiwix) as second:
            second.second_reader = True
            worker = threading.Thread(target=second.serve_forever, daemon=True)
            worker.start()
            reader = kiwix_plugin.Plugin({"base_url": f"http://127.0.0.1:{second.server_address[1]}"}, {})
            reader.source_id = "second-reader"
            hub.plugins.append(reader)
            try:
                data = json.loads(get(base, "/api/search?q=tourniquet&source=second-reader&fresh=1")[1])
                article = data["groups"][0]["results"][0]["url"]
                code, body = get(base, article)
                check("second Kiwix reader opens its own articles and search forms",
                      code == 200 and article.startswith("/kiwix-readers/second-reader/")
                      and b"Second reader article" in body
                      and b"action='/kiwix-readers/second-reader/search'" in body)
                check("article Best match cites the reader that served its lead",
                      data.get("answer", {}).get("url") == article
                      and data.get("answer", {}).get("text", "").startswith("Sample lead paragraph"))
            finally:
                hub.plugins.remove(reader)
                second.shutdown()
                worker.join()
        fake.shutdown()
        fake.server_close()  # actually close the port, or the proxy's
        # connection succeeds into the backlog and waits out its timeout
        try:
            code, body = get(base, "/kiwix/wikipedia_en_all_nopic/A/Tourniquet")
            check("proxy reports 502 when kiwix-serve is down", False,
                  f"got {code}")
        except urllib.error.HTTPError as exc:  # urlopen raises on 502
            check("proxy reports 502 when kiwix-serve is down",
                  exc.code == 502 and json.load(exc)["error"] == "kiwix-serve not reachable",
                  f"got {exc.code}")
        request = urllib.request.Request(base + "/kiwix/", headers={"Accept": "text/html"})
        try:
            urllib.request.urlopen(request, timeout=10)
            check("Kiwix browser outage offers recovery links", False, "got 2xx")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            check("Kiwix browser outage offers recovery links",
                  exc.code == 502 and b"Kiwix reader unavailable" in body
                  and b'href="/?view=settings"' in body)

        # 20. Recoll query passes -A so snippets come back (audit flaw #4).
        captured = {}
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd

            class P:
                returncode = 0
                stdout = ("file:///C:/Library/FieldManuals/fm.pdf\n"
                          "Abstract line here.\n")
            return P()

        subprocess.run = fake_run
        try:
            rp2 = recoll_plugin.Plugin(
                {}, {"base_dir": BASE_DIR, "demo_fallback": False})
            out = rp2.search("tourniquet")
        finally:
            subprocess.run = real_run
        check("recoll query passes -A for snippets",
              "-A" in captured.get("cmd", []), str(captured.get("cmd")))
        check("recoll -A results carry the abstract as snippet",
              len(out) == 1 and "Abstract line" in out[0]["snippet"],
              f"got {out}")

        # 21. Serving mode: remote clients get file names, not the
        # laptop's tree (audit flaw #7, round 5); config saves are
        # atomic (audit flaw #14).
        from hub import sanitize_for_remote, sanitize_status_for_remote
        fake_result = {
            "query": "tourniquet",
            "groups": [
                {"source_id": "recoll", "results": [
                    {"title": "fm.pdf", "snippet": "x",
                     "url": "file:///C:/Library/FieldManuals/fm.pdf",
                     "badge": "Manual",
                     "detail": "C:\\Library\\FieldManuals\\fm.pdf",
                     "demo": False}]},
                {"source_id": "kiwix", "results": [
                    {"title": "Tourniquet", "snippet": "x",
                     "url": "/kiwix/wikipedia_en_all_nopic/A/Tourniquet",
                     "badge": "Reference", "detail": "wikipedia_en_all_nopic",
                     "demo": False}]},
            ],
        }
        clean = sanitize_for_remote(fake_result)
        rec = clean["groups"][0]["results"][0]
        kiw = clean["groups"][1]["results"][0]
        check("sanitiser drops file-path result URLs for remote clients",
              rec["url"] == "", f"got {rec['url']!r}")
        check("sanitiser reduces detail paths to file names",
              rec["detail"] == "fm.pdf", f"got {rec['detail']!r}")
        check("sanitiser keeps hub-relative /kiwix/ URLs",
              kiw["url"].startswith("/kiwix/"), f"got {kiw['url']!r}")
        check("sanitiser does not mutate the hub's own results",
              fake_result["groups"][0]["results"][0]["url"].startswith("file:///"))
        # Security audit 2026-11-20:
        #  3f — a /map?lat=... URL has "." in its coordinates and was
        #  wrongly blanked for remote clients (the phone's place
        #  results lost their map link).
        from hub import _looks_like_file_path
        check("sanitiser keeps /map viewer URLs for remote clients",
              _looks_like_file_path("/map?lat=42.2215&lon=-71.0015&zoom=14") is False)
        check("sanitiser keeps /files/ URLs for remote clients",
              _looks_like_file_path("/files/manuals/EPUB/fm-3-05-70.epub") is False)
        check("sanitiser still blanks real file paths",
              _looks_like_file_path("file:///C:/Library/FieldManuals/fm.pdf") is True
              and _looks_like_file_path("C:\\Library\\FieldManuals\\fm.pdf") is True)
        #  F19 — My Files snippets are the owner's own file contents
        #  and must not reach a hotspot client.
        fake_myfiles = {
            "query": "notes",
            "groups": [
                {"source_id": "myfiles", "results": [
                    {"title": "private-notes.md",
                     "snippet": "Some private notes from the owner.",
                     "url": "C:\\Library\\MyFiles\\private-notes.md",
                     "badge": "My Files",
                     "detail": "private-notes.md",
                     "demo": False}]},
            ],
        }
        clean_mf = sanitize_for_remote(fake_myfiles)
        check("sanitiser blanks My Files snippets for remote clients",
              clean_mf["groups"][0]["results"][0]["snippet"] == "",
              f"got {clean_mf['groups'][0]['results'][0]['snippet']!r}")
        #  3c — the group note carries the plug-in status detail when
        #  a source returns no results; it must not leak a path.
        fake_note = {
            "query": "zzzz",
            "groups": [
                {"source_id": "myfiles", "results": [],
                 "note": "50 files in C:\\Library\\MyFiles"},
            ],
        }
        clean_note = sanitize_for_remote(fake_note)
        check("sanitiser strips paths from group notes",
              "C:" not in clean_note["groups"][0]["note"],
              clean_note["groups"][0]["note"])
        #  3b — a Recoll result with no abstract falls back to a
        #  snippet; it must be the file name, not the absolute path.
        rp2 = recoll_plugin.Plugin({}, {"base_dir": BASE_DIR, "demo_fallback": False})
        r2 = rp2._result_for("file:///C:/Library/FieldManuals/PDF/ATP 3-06.11.pdf", "")
        check("recoll snippet fallback is the file name, not the path",
              r2["snippet"] == "Document: ATP 3-06.11.pdf",
              f"got {r2['snippet']!r}")
        clean_status = sanitize_status_for_remote(
            {"sources": [{"id": "myfiles", "available": True,
                          "detail": "3 files in C:\\Library\\docs"}],
             "library": {}})
        check("sanitiser strips paths from status details",
              "C:" not in clean_status["sources"][0]["detail"],
              clean_status["sources"][0]["detail"])
        # Atomic save_config: valid JSON afterwards, no temp file left.
        tmp_cfg = os.path.join(tmp, "atomic-config.json")
        shutil.copy(config_path, tmp_cfg)
        hub_atomic = Hub(tmp_cfg)
        hub_atomic.config["port"] = 8899
        hub_atomic.save_config()
        check("atomic save_config leaves valid JSON",
              json.load(open(tmp_cfg))["port"] == 8899)
        check("atomic save_config leaves no temp file behind",
              not os.path.exists(tmp_cfg + ".tmp"))

        # 22. Map viewer + tiles routes (audit flaw #11, round 5).
        code, body = get(base, "/map")
        text = body.decode("utf-8")
        check("map viewer loads (200)", code == 200, f"got {code}")
        check("map viewer uses vendored MapLibre + pmtiles",
              "vendor/maplibre-gl.js" in text and "vendor/pmtiles.js" in text)
        check("map viewer reads and applies the URL style during initialization",
              'params.get("style")' in text and 'style: makeStyle(currentStyle)' in text)
        check("map viewer glyphs are local, not an external URL",
              "fonts.openmaptiles.org" not in text
              and "/static/vendor/glyphs/" in text)
        check("map asset status uses plain text",
              'basemapStatus.textContent = message' in text
              and '"/api/maps/availability?"' in text)
        check("map page escapes the go-to input in showStatus",
              'esc(t)' in text, "no esc(t) in map.html")
        check("map page defines an HTML escaper",
              'function esc(s)' in text, "no esc() in map.html")
        code, body = get(base, "/static/vendor/maplibre-gl.js")
        check("vendored MapLibre JS serves (200)", code == 200
              and b"MapLibre GL JS" in body, f"got {code}")
        # Glyph PBFs are binary and are not in the repo (see README:
        # "Map glyphs"); a fresh clone has no glyph files. Skip the
        # check rather than failing when the file is absent — the map
        # still works, labels just don't render.
        try:
            code, body = get(base, "/static/vendor/glyphs/Noto%20Sans%20Regular/0-255.pbf")
            check("vendored glyph PBF serves (200)", code == 200 and len(body) > 1000,
                  f"got {code} {len(body)}")
        except Exception as exc:  # urllib raises HTTPError on 404
            print("SKIP vendored glyph PBF serves (200) — glyph files not vendored: " + str(exc))
        try:
            get(base, "/static/vendor/../hub.py")
            check("static route blocks path traversal", False, "got 2xx")
        except Exception as exc:  # urllib raises HTTPError on 404
            check("static route blocks path traversal", "404" in str(exc), str(exc))
        # No extract yet: /tiles 404s instead of failing silently.
        try:
            get(base, "/tiles/ne.pmtiles")
            check("tiles 404s with no extract", False, "got 2xx")
        except Exception as exc:  # urllib raises HTTPError on 404
            check("tiles 404s with no extract", "404" in str(exc), str(exc))
        availability_path = "/api/maps/availability?src=/tiles/ne.pmtiles"
        check("map availability reports absent assets",
              json.loads(get(base, availability_path)[1]) ==
              {"vector": False, "topo": False, "trex": False})
        # With an extract in maps_dir: full GET + Range request.
        tiles_dir = os.path.join(tmp, "maps")
        os.makedirs(tiles_dir)
        fake_tiles = os.path.join(tiles_dir, "ne.pmtiles")
        payload = bytes(range(256)) * 4  # 1024 bytes of known content
        with open(fake_tiles, "wb") as fh:
            fh.write(payload)
        hub.config["maps_dir"] = tiles_dir
        check("map availability reports an installed PMTiles archive",
              json.loads(get(base, availability_path)[1])["vector"] is True)
        code, body = get(base, "/tiles/ne.pmtiles")
        check("tiles serves the extract (200, full bytes)",
              code == 200 and body == payload, f"got {code} {len(body)}")
        req = urllib.request.Request(
            base + "/tiles/ne.pmtiles",
            headers={"Range": "bytes=100-199"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            check("tiles honours Range requests (206 + Content-Range)",
                  resp.status == 206
                  and resp.headers.get("Content-Range") == "bytes 100-199/1024"
                  and resp.read() == payload[100:200],
                  f"got {resp.status} {resp.headers.get('Content-Range')}")
        oversized = urllib.request.Request(
            base + "/tiles/ne.pmtiles", headers={"Range": "bytes=0-16383"})
        with urllib.request.urlopen(oversized, timeout=10) as resp:
            check("short PMTiles archive clamps the requested range to EOF",
                  resp.status == 206 and resp.headers.get("Content-Range") == "bytes 0-1023/1024"
                  and resp.read() == payload)
        unsatisfiable = urllib.request.Request(
            base + "/tiles/ne.pmtiles", headers={"Range": "bytes=9999-"})
        try:
            urllib.request.urlopen(unsatisfiable, timeout=2)
            check("unsatisfiable tile ranges return a complete 416", False, "got 2xx")
        except urllib.error.HTTPError as exc:
            check("unsatisfiable tile ranges return a complete 416",
                  exc.code == 416 and exc.headers.get("Content-Length") == "0"
                  and exc.read() == b"")
        # Place results now link into the hub-relative viewer.
        # (Quad results are excluded here: a quad with no bbox yet
        # has no centre to point the viewer at — see section 25.)
        code, body = get(base, "/api/search?q=braintree")
        data = json.loads(body)
        maps = {g["source_id"]: g for g in data["groups"]}["maps"]
        places = [r for r in maps["results"] if "topo quad" not in r["title"]]
        check("place results link to the hub-relative viewer",
              len(places) >= 2
              and all(r["url"].startswith("/map?lat=") for r in places),
              f"got {[r['url'] for r in places]}")
        place_pdf = next((r for r in places if r.get("pdf_url")), {})
        check("missing GeoPDF does not remove a place's map link",
              place_pdf.get("pdf_available") is False and place_pdf["url"].startswith("/map?"))
        if place_pdf:
            quads_dir = os.path.join(tiles_dir, "quads")
            os.makedirs(quads_dir)
            filename = urllib.parse.unquote(place_pdf["pdf_url"].rsplit("/", 1)[-1])
            with open(os.path.join(quads_dir, filename), "wb") as fh:
                fh.write(b"%PDF-")
            _, body = get(base, "/api/search?q=braintree")
            maps = next(g for g in json.loads(body)["groups"] if g["source_id"] == "maps")
            check("GeoPDF availability refreshes on a cached search",
                  any(r.get("pdf_url") == place_pdf["pdf_url"] and r.get("pdf_available") is True
                      for r in maps["results"]))

        old_roots = hub._library_roots_cache
        hub._library_roots_cache = {"audit": tmp}
        try:
            with open(os.path.join(tmp, "reference%20.html"), "wb") as fh:
                fh.write(b"<h1>Offline reference</h1>")
            with urllib.request.urlopen(base + "/files/audit/reference%2520.html", timeout=10) as resp:
                check("library HTML links decode once and serve in a sandbox",
                      resp.read() == b"<h1>Offline reference</h1>"
                      and resp.headers.get("Content-Security-Policy", "").startswith("sandbox;"))
            with open(os.path.join(tmp, "reference-界.pdf"), "wb") as fh:
                fh.write(b"%PDF-")
            with urllib.request.urlopen(base + "/files/audit/reference-%E7%95%8C.pdf", timeout=10) as resp:
                check("library PDFs with Unicode names open inline",
                      resp.read() == b"%PDF-"
                      and "filename*=UTF-8''reference-%E7%95%8C.pdf" in
                      resp.headers.get("Content-Disposition", ""))
        finally:
            hub._library_roots_cache = old_roots

        # 23. Frequencies plug-in (round 8, GAP_LIST item 9).
        fp = freq_plugin.Plugin(
            {"data": "fixtures/frequencies.json"},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        res = fp.search("noaa")
        check("frequencies finds the seven NOAA channels",
              len(res) == 7 and all(r["badge"] == "Radio" for r in res),
              f"got {len(res)}")
        res = fp.search("162.475")
        check("frequencies finds Boston NOAA on 162.475",
              len(res) == 1 and "KHB35" in res[0]["snippet"],
              f"got {[r['title'] for r in res]}")
        res = fp.search("146.52")
        # The 2m national calling frequency is a real entry now (audit
        # 2026-11-20): the old fixture had a SAMPLE row here instead.
        check("frequency normalisation: 146.52 matches 146.520",
              len(res) == 1 and "146.520" in res[0]["title"],
              f"got {[r['title'] for r in res]}")
        res = fp.search("repeater")
        # The fixture is all real entries now (91, 0 samples): a
        # "repeater" search returns real repeaters, none SAMPLE.
        check("frequencies repeater search returns real entries",
              len(res) >= 1 and all("SAMPLE" not in r["detail"] for r in res),
              f"got {len(res)}")
        check("frequencies status counts NOAA vs repeaters",
              "7 NOAA" in fp.status()["detail"]
              and "84 repeaters" in fp.status()["detail"],
              fp.status()["detail"])
        bad_freq = os.path.join(tmp, "bad-frequencies.json")
        with open(bad_freq, "w", encoding="utf-8") as fh:
            json.dump({"entries": [
                "a stray string",
                {"name": "", "kind": "repeater", "frequency_mhz": 146.52},
                {"name": "No Freq", "kind": "repeater"},
                {"name": "Zero Freq", "kind": "repeater", "frequency_mhz": 0},
                {"name": "Nonfinite Freq", "kind": "repeater", "frequency_mhz": "NaN"},
                {"name": "Good Row", "kind": "repeater",
                 "frequency_mhz": 146.52, "location": "Test",
                 "sample": True},
                {"name": "Precise Row", "kind": "repeater", "frequency_mhz": 146.5235},
            ]}, fh)
        fp_bad = freq_plugin.Plugin(
            {"data": bad_freq}, {"base_dir": BASE_DIR, "demo_fallback": False})
        res = fp_bad.search("146.52")
        check("frequencies skips bad rows",
              len(res) == 1 and res[0]["title"] == "Good Row" and
              fp_bad.status()["detail"].startswith("2 frequencies"),
              f"got {[r['title'] for r in res]}")
        precise = fp_bad.search("146.5235 MHz")
        check("exact radio search keeps sub-kHz precision for tuning",
              len(precise) == 1 and precise[0]["frequency_mhz"] == "146.5235")
        fp_missing = freq_plugin.Plugin(
            {"data": "does-not-exist.json"},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        check("frequencies missing file searches empty",
              fp_missing.search("noaa") == [])
        check("frequencies missing file reports unavailable",
              fp_missing.status()["available"] is False)

        # 24. One search reaches every configured source (round 8):
        # the hub fans a query out to all enabled sources in
        # config.json — no source is silently left out of a search.
        code, body = get(base, "/api/search?q=water")
        data = json.loads(body)
        shipped = json.load(open(os.path.join(BASE_DIR, "config.json")))
        enabled_ids = {s["id"] for s in shipped["sources"]
                       if s.get("enabled", True)}
        got_ids = {g["source_id"] for g in data["groups"]}
        check("one search returns a group for every enabled source",
              enabled_ids <= got_ids, f"missing {enabled_ids - got_ids}")
        check("frequencies source answers the fan-out",
              any(g["source_id"] == "frequencies" for g in data["groups"]))
        code, body = get(base, "/api/search?q=noaa")
        data = json.loads(body)
        freq = {g["source_id"]: g for g in data["groups"]}["frequencies"]
        check("one search reaches the frequencies source (noaa)",
              len(freq["results"]) == 7, f"got {len(freq['results'])}")

        # 25. USGS quad index wired into Maps (round 8, GAP_LIST item 2).
        import urllib.parse as _urlparse
        code, body = get(base, "/api/search?q="
                         + _urlparse.quote("braintree quad"))
        data = json.loads(body)
        maps = {g["source_id"]: g for g in data["groups"]}["maps"]
        # No USGS quad is named "Braintree" — Braintree is covered by
        # the Blue Hills quad. The query resolves the place to its
        # covering quad (audit 2026-11-20).
        check("quad name search returns the Braintree quad",
              any("Blue Hills (USGS topo quad)" in r["title"]
                  for r in maps["results"]),
              f"got {[r['title'] for r in maps['results']]}")
        # The real fixture has no null-bbox quads (all 369 have bboxes),
        # so the "bbox pending download" behaviour is tested below with
        # a synthetic quad file instead of against the live index.
        check("quad name search returns a quad with a bbox",
              any("topo quad" in r["title"] and r.get("lat") is not None
                  for r in maps["results"]),
              f"got {[r['title'] for r in maps['results']]}")
        quad_file = os.path.join(tmp, "quads.json")
        with open(quad_file, "w", encoding="utf-8") as fh:
            json.dump({"quads": [
                {"name": "Braintree", "state": "MA", "scale": "1:24000",
                 "bbox": [-71.125, 42.125, -71.0, 42.25],
                 "file": "braintree.pdf",
                 "note": "Covers Braintree itself."},
            ]}, fh)
        mp_quad = maps_plugin.Plugin(
            {"gazetteer": "fixtures/gazetteer-ne.json", "quads": quad_file,
             "viewer_url_template": "/map?lat={lat}&lon={lon}&zoom=14"},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        res = mp_quad.search("braintree")
        check("place result names its covering quad",
              any("Braintree quad" in r["detail"] for r in res),
              f"got {[r['detail'] for r in res]}")
        quad_res = [r for r in res if "topo quad" in r["title"]]
        check("quad result carries its bbox centre coordinates",
              len(quad_res) == 1
              and abs(quad_res[0]["lat"] - 42.1875) < 0.001
              and abs(quad_res[0]["lon"] + 71.0625) < 0.001,
              f"got {quad_res}")
        check("quad result with a bbox links into the viewer",
              len(quad_res) == 1
              and quad_res[0]["url"].startswith("/map?lat="),
              f"got {[r['url'] for r in quad_res]}")
        # A quad with no bbox yet must say so instead of guessing a
        # centre: synthetic null-bbox quad, searched by name.
        quad_nobbox = os.path.join(tmp, "quads-nobbox.json")
        with open(quad_nobbox, "w", encoding="utf-8") as fh:
            json.dump({"quads": [
                {"name": "Nowhere", "state": "MA", "scale": "1:24000",
                 "bbox": None, "file": "", "note": "Not downloaded yet."},
            ]}, fh)
        mp_nobbox = maps_plugin.Plugin(
            {"gazetteer": "fixtures/gazetteer-ne.json", "quads": quad_nobbox,
             "viewer_url_template": "/map?lat={lat}&lon={lon}&zoom=14"},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        res_nb = mp_nobbox.search("nowhere quad")
        check("quad without a bbox says so instead of guessing",
              len(res_nb) == 1
              and "bbox pending download" in res_nb[0]["detail"]
              and res_nb[0].get("lat") is None
              and res_nb[0]["url"] == "",
              f"got {res_nb}")

        # 26. The UI: one hero search box, and the line under it names
        # the sources that one box actually searches (round 8).
        code, body = get(base, "/")
        text = body.decode("utf-8")
        check("UI has exactly one search input", text.count('id="q"') == 1,
              f"got {text.count(chr(34) + 'q' + chr(34))}")
        check("UI names the connected sources under the search box",
              'id="covers"' in text and "One search across" in text)
        check("UI has a Radio badge style",
              ".badge.Radio" in text and "frequencies" in text.lower())

        # 27. Fault injection (round 9): dead / garbage / slow backends
        # must degrade gracefully — the dead source shows unavailable,
        # the other sources still answer, the server never crashes.
        # 27a. Kiwix returning garbage HTML (not a search page at all).
        from http.server import BaseHTTPRequestHandler as _BH

        class QuietServer(ThreadingHTTPServer):
            """Fake backends: a client that times out mid-write leaves
            a broken pipe in the server thread — swallow it so the
            test output stays readable."""

            def handle_error(self, request, client_address):
                pass

        class GarbageKiwix(_BH):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = bytes(range(256)) * 40  # 10 KB of non-HTML bytes
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        garbage = QuietServer(("127.0.0.1", 0), GarbageKiwix)
        garbage_thread = threading.Thread(target=garbage.serve_forever, daemon=True)
        garbage_thread.start()
        garbage_port = garbage.server_address[1]
        kp_garbage = kiwix_plugin.Plugin(
            {"base_url": f"http://127.0.0.1:{garbage_port}"},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        check("kiwix garbage HTML returns no results, no crash",
              kp_garbage.search("tourniquet") == [])
        # Rel H3 (2026-11-21): status is content-based now — a server
        # returning garbage serves no ZIM, so it is not available as a
        # library, even though the HTTP server itself is up.
        check("kiwix garbage HTML reports unavailable (no ZIM served)",
              kp_garbage.status()["available"] is False)
        garbage.shutdown()
        garbage.server_close()
        # 27b. Kiwix slower than the plug-in timeout.
        class SlowKiwix(_BH):
            def log_message(self, *args):
                pass

            def do_GET(self):
                import time as _t
                _t.sleep(2)
                body = b"<html><body>No results</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        slow = QuietServer(("127.0.0.1", 0), SlowKiwix)
        slow_thread = threading.Thread(target=slow.serve_forever, daemon=True)
        slow_thread.start()
        slow_port = slow.server_address[1]
        kp_slow = kiwix_plugin.Plugin(
            {"base_url": f"http://127.0.0.1:{slow_port}", "timeout_seconds": 1},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        import time as _time
        _t0 = _time.monotonic()
        check("kiwix slower than timeout returns empty, no hang",
              kp_slow.search("tourniquet") == [])
        check("kiwix timeout respects the plug-in timeout (~1s, not 2s+)",
              _time.monotonic() - _t0 < 1.8, f"took {_time.monotonic() - _t0:.1f}s")
        slow.shutdown()
        slow.server_close()
        # 27c. Recoll binary missing / bad config dir / nonzero exit.
        real_run2 = subprocess.run

        def _missing_run(cmd, **kw):
            raise FileNotFoundError("no such file: recollq")

        subprocess.run = _missing_run
        try:
            rp_missing = recoll_plugin.Plugin(
                {}, {"base_dir": BASE_DIR, "demo_fallback": False})
            check("recoll binary missing: search returns empty",
                  rp_missing.search("tourniquet") == [])
            check("recoll binary missing: status reports unavailable",
                  rp_missing.status()["available"] is False)
        finally:
            subprocess.run = real_run2

        def _failing_run(cmd, **kw):
            class P:
                returncode = 2
                stdout = ""
            return P()

        subprocess.run = _failing_run
        try:
            rp_fail = recoll_plugin.Plugin(
                {}, {"base_dir": BASE_DIR, "demo_fallback": False})
            check("recoll nonzero exit: search returns empty, no crash",
                  rp_fail.search("tourniquet") == [])
        finally:
            subprocess.run = real_run2
        # 27d. A plug-in that raises, and one that returns a non-list.
        boom_plugin = PluginBase({"name": "Boom", "badge": "Reference"},
                                 {"base_dir": BASE_DIR, "demo_fallback": False})
        boom_plugin.id = "boom"
        boom_plugin.source_id = "boom"
        boom_plugin.display_name = "Boom"
        boom_plugin.display_badge = "Reference"

        def _raise(q):
            raise RuntimeError("kaboom")

        boom_plugin.search = _raise
        boom_plugin.status = lambda: {"available": True, "detail": "up"}
        nonlist_plugin = PluginBase({"name": "NonList", "badge": "Reference"},
                                    {"base_dir": BASE_DIR, "demo_fallback": False})
        nonlist_plugin.id = "nonlist"
        nonlist_plugin.source_id = "nonlist"
        nonlist_plugin.display_name = "NonList"
        nonlist_plugin.display_badge = "Reference"
        nonlist_plugin.search = lambda q: "not a list"
        nonlist_plugin.status = lambda: {"available": True, "detail": "up"}
        hub.plugins.extend([boom_plugin, nonlist_plugin])
        # Round 2 cache (see above): clear it after mutating plugins.
        hub.clear_search_cache()
        try:
            data = hub.search("tourniquet")
            groups = {g["source_id"]: g for g in data["groups"]}
            check("plug-in raising in search() is marked unavailable",
                  groups["boom"]["available"] is False
                  and "plug-in error" in groups["boom"]["note"],
                  groups["boom"]["note"])
            check("plug-in returning a non-list yields an empty group",
                  groups["nonlist"]["results"] == [])
            wdata = hub.search("water")
            wgroups = {g["source_id"]: g for g in wdata["groups"]}
            check("other sources still answer when one plug-in raises",
                  len(wgroups["myfiles"]["results"]) >= 1)
            code, _ = get(base, "/api/search?q=tourniquet")
            check("HTTP search still 200 with a raising plug-in",
                  code == 200, f"got {code}")
        finally:
            hub.plugins = [p for p in hub.plugins
                           if getattr(p, "source_id", "") not in ("boom", "nonlist")]
        # 27e. Two backends down at once (kiwix on a closed port +
        # recoll binary missing): the hub still answers.
        down_cfg = os.path.join(tmp, "down-config.json")
        with open(down_cfg, "w", encoding="utf-8") as fh:
            json.dump({"sources": [
                {"id": "kiwix", "name": "Kiwix", "plugin": "kiwix",
                 "base_url": "http://127.0.0.1:1", "enabled": True},
                {"id": "recoll", "name": "Recoll", "plugin": "recoll",
                 "enabled": True},
            ], "port": 0, "demo_fallback": False}, fh)
        hub_down = Hub(down_cfg)
        data = hub_down.search("tourniquet")
        groups = {g["source_id"]: g for g in data["groups"]}
        check("two backends down: search still returns both groups",
              set(groups) == {"kiwix", "recoll"}, str(set(groups)))
        check("two backends down: both groups unavailable",
              all(not g["available"] for g in data["groups"]))
        check("two backends down: status still answers",
              all(not s["available"] for s in hub_down.status()["sources"]))

        # 28. Parallel fan-out + shared deadline (round 9): one slow
        # plug-in must not hold the whole search hostage.
        import hub as hub_module
        old_deadline = hub_module.Hub.SEARCH_DEADLINE_SECONDS
        old_status_deadline = hub_module.Hub.STATUS_DEADLINE_SECONDS
        slow_plugin = PluginBase({"name": "Slow", "badge": "Reference"},
                                 {"base_dir": BASE_DIR, "demo_fallback": False})
        slow_plugin.id = "slow"
        slow_plugin.source_id = "slow"
        slow_plugin.display_name = "Slow"
        slow_plugin.display_badge = "Reference"

        def _sleepy(q):
            import time as _t
            _t.sleep(5)
            return [{"title": "Too late", "snippet": "x", "url": "",
                     "badge": "Reference", "detail": "", "demo": False}]

        slow_plugin.search = _sleepy
        slow_plugin.status = lambda: {"available": True, "detail": "up"}
        fast_plugin = PluginBase({"name": "Fast", "badge": "Reference"},
                                 {"base_dir": BASE_DIR, "demo_fallback": False})
        fast_plugin.id = "fast"
        fast_plugin.source_id = "fast"
        fast_plugin.display_name = "Fast"
        fast_plugin.display_badge = "Reference"
        fast_plugin.search = lambda q: [{"title": "Quick hit", "snippet": "x",
                                         "url": "", "badge": "Reference",
                                         "detail": "", "demo": False}]
        fast_plugin.status = lambda: {"available": True, "detail": "up"}
        hub.plugins.extend([slow_plugin, fast_plugin])
        # Round 2 cache (see above): clear it after mutating plugins.
        hub.clear_search_cache()
        try:
            hub_module.Hub.SEARCH_DEADLINE_SECONDS = 2
            _t0 = _time.monotonic()
            data = hub.search("tourniquet")
            elapsed = _time.monotonic() - _t0
            groups = {g["source_id"]: g for g in data["groups"]}
            check("slow plug-in: search returns within the deadline",
                  elapsed < 4, f"took {elapsed:.1f}s")
            check("slow plug-in: timed-out group is marked unavailable",
                  groups["slow"]["available"] is False
                  and "timed out" in groups["slow"]["note"],
                  groups["slow"]["note"])
            check("slow plug-in: fast source still answers",
                  len(groups["fast"]["results"]) == 1)
            # Same for status(): a status() that hangs is cut off.
            status_started = threading.Event()
            status_release = threading.Event()

            def _sleepy_status():
                status_started.set()
                status_release.wait(5)
                return {"available": True, "detail": "up"}

            slow_plugin.status = _sleepy_status
            hub._status_cache = None
            hub_module.Hub.STATUS_DEADLINE_SECONDS = 2
            _t0 = _time.monotonic()
            status_result = {}
            probe = threading.Thread(target=lambda: status_result.update(
                json.loads(get(base, "/api/status")[1])), daemon=True)
            probe.start()
            try:
                started = status_started.wait(1)
                health = json.loads(get(base, "/api/health")[1])
                check("watchdog liveness stays responsive with a blocked source",
                      started and probe.is_alive() and health.get("service") == "pandora-search-hub")
                probe.join(4)
            finally:
                status_release.set()
            st = status_result
            elapsed = _time.monotonic() - _t0
            slow_st = {s["id"]: s for s in st["sources"]}["slow"]
            check("slow status(): returns within the deadline",
                  elapsed < 4, f"took {elapsed:.1f}s")
            check("slow status(): timed-out source marked unavailable",
                  slow_st["available"] is False
                  and "timed out" in slow_st["detail"], slow_st["detail"])
        finally:
            hub_module.Hub.SEARCH_DEADLINE_SECONDS = old_deadline
            hub_module.Hub.STATUS_DEADLINE_SECONDS = old_status_deadline
            hub.plugins = [p for p in hub.plugins
                           if getattr(p, "source_id", "") not in ("slow", "fast")]
            hub._status_cache = None

        # 29. HTTP edge cases (round 9).
        code, body = get(base, "/api/search?q=")
        data = json.loads(body)
        check("empty query returns 200 with all groups, no results",
              code == 200 and len(data["groups"]) == 8
              and all(len(g["results"]) == 0 for g in data["groups"]),
              f"got {code} {len(data['groups'])} groups")
        import urllib.parse as _up
        code, _ = get(base, "/api/search?q=" + _up.quote("café"))
        check("non-ASCII query returns 200", code == 200, f"got {code}")
        code, _ = get(base, "/api/search?q=" + _up.quote("北京"))
        check("CJK query returns 200", code == 200, f"got {code}")
        code, _ = get(base, "/api/search?q=" + _up.quote("../../etc/passwd"))
        check("path-traversal-looking query returns 200", code == 200,
              f"got {code}")
        try:
            req = urllib.request.Request(
                base + "/api/sources",
                data=json.dumps(["not", "an", "object"]).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": base}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            check("non-dict POST body rejected with 400", False, "got 2xx")
        except urllib.error.HTTPError as exc:
            check("non-dict POST body rejected with 400", exc.code == 400,
                  f"got {exc.code}")
        try:
            req = urllib.request.Request(
                base + "/api/sources",
                data=b"{not json",
                headers={"Content-Type": "application/json", "Origin": base}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            check("invalid JSON POST body rejected with 400", False, "got 2xx")
        except urllib.error.HTTPError as exc:
            check("invalid JSON POST body rejected with 400", exc.code == 400,
                  f"got {exc.code}")
        try:
            get(base, "/tiles/../config.json")
            check("tiles route blocks path traversal", False, "got 2xx")
        except Exception as exc:
            check("tiles route blocks path traversal", "404" in str(exc), str(exc))
        try:
            get(base, "/tiles/%2e%2e%2fconfig.json")
            check("tiles route blocks encoded traversal", False, "got 2xx")
        except Exception as exc:
            check("tiles route blocks encoded traversal", "404" in str(exc), str(exc))
        # Tiles chunk cap: a 9 MiB extract is served in 8 MiB chunks,
        # never read whole into RAM.
        big_tiles = os.path.join(tiles_dir, "big.pmtiles")
        with open(big_tiles, "wb") as fh:
            fh.write(b"\x00" * (9 * 1024 * 1024))
        code, body = get(base, "/tiles/big.pmtiles")
        check("tiles serves a large extract in capped chunks (206, 8 MiB)",
              code == 206 and len(body) == 8 * 1024 * 1024,
              f"got {code} {len(body)}")
        req = urllib.request.Request(
            base + "/tiles/big.pmtiles",
            headers={"Range": "bytes=100-199"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            check("tiles Range on a large extract still honours the range",
                  resp.status == 206 and len(resp.read()) == 100,
                  f"got {resp.status}")
        os.remove(big_tiles)

        # 30. Port fallback (round 9): a busy port takes the next one,
        # and main() records the actual port in hub.port.
        import socket as _socket
        from hub import _bind_server, PORT_FILE, main as hub_main
        occupied = _socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        busy_port = occupied.getsockname()[1]
        srv, got_port = _bind_server("127.0.0.1", busy_port,
                                     make_handler(hub), tries=3)
        check("port fallback takes the next free port",
              got_port == busy_port + 1, f"got {got_port}")
        srv.server_close()
        if _socket.has_ipv6:
            with _socket.socket(_socket.AF_INET6) as probe:
                try:
                    probe.bind(("::1", 0))
                    ipv6_available = True
                except OSError:
                    ipv6_available = False
            if ipv6_available:
                srv6, _ = _bind_server("::1", 0, make_handler(hub), tries=1)
                with srv6:
                    check("hub binds to IPv6 loopback without opening an IPv4 listener",
                          srv6.server_address[0] == "::1" and
                          srv6.socket.getsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY) == 1)
        occupied2 = _socket.socket()
        occupied2.bind(("127.0.0.1", 0))
        occupied2.listen(1)
        busy2 = occupied2.getsockname()[1]
        occupied3 = _socket.socket()
        occupied3.bind(("127.0.0.1", busy2 + 1))
        occupied3.listen(1)
        try:
            _bind_server("127.0.0.1", busy2, make_handler(hub), tries=2)
            check("port fallback raises after exhausting tries", False, "bound?!")
        except OSError:
            check("port fallback raises after exhausting tries", True)
        # main() in a thread: binds the fallback port, writes hub.port.
        port_file_tmp = os.path.join(tmp, "hub.port")
        old_port_file = hub_module.PORT_FILE
        hub_module.PORT_FILE = port_file_tmp
        main_thread = threading.Thread(
            target=lambda: hub_main(["--config", config_path,
                                     "--port", str(busy_port)]),
            daemon=True)
        main_thread.start()
        found_port = None
        for _ in range(50):
            if os.path.exists(port_file_tmp):
                found_port = int(open(port_file_tmp).read().strip())
                break
            _time.sleep(0.1)
        check("main() writes the actual port to hub.port",
              found_port == busy_port + 1, f"got {found_port}")
        with open(port_file_tmp + ".json", encoding="utf-8") as fh:
            launch = json.load(fh)
        health = json.loads(get(f"http://127.0.0.1:{found_port}", "/api/health")[1])
        check("watchdog liveness identifies the exact hub and fallback port",
              launch["port"] == health["port"] == found_port
              and launch["instance"] == health["instance"]
              and launch["host"] == "127.0.0.1" and launch["requested_port"] == busy_port
              and health["service"] == "pandora-search-hub")
        hub_module.PORT_FILE = old_port_file
        occupied.close()
        occupied2.close()
        occupied3.close()

        # 31. Data files reload by mtime (round 9): swapping in the
        # real repeater export at setup takes effect without a restart.
        freq_file = os.path.join(tmp, "freq-reload.json")
        with open(freq_file, "w", encoding="utf-8") as fh:
            json.dump({"entries": [
                {"name": "Test Channel", "kind": "noaa",
                 "frequency_mhz": 162.400, "location": "Test",
                 "note": "", "sample": False},
            ]}, fh)
        fp_reload = freq_plugin.Plugin(
            {"data": freq_file}, {"base_dir": BASE_DIR, "demo_fallback": False})
        check("frequencies finds the first file version",
              len(fp_reload.search("162.400")) == 1)
        _time.sleep(0.05)  # mtime granularity: make the rewrite land later
        with open(freq_file, "w", encoding="utf-8") as fh:
            json.dump({"entries": [
                {"name": "Test Channel", "kind": "noaa",
                 "frequency_mhz": 162.400, "location": "Test",
                 "note": "", "sample": False},
                {"name": "Second Channel", "kind": "noaa",
                 "frequency_mhz": 162.425, "location": "Test",
                 "note": "", "sample": False},
            ]}, fh)
        check("frequencies picks up the rewritten file without restart",
              len(fp_reload.search("162.425")) == 1)
        os.remove(freq_file)
        check("frequencies missing file searches empty after reload",
              fp_reload.search("162.400") == [])
        check("frequencies missing file reports unavailable after reload",
              fp_reload.status()["available"] is False)
        gz_file = os.path.join(tmp, "gz-reload.json")
        with open(gz_file, "w", encoding="utf-8") as fh:
            json.dump([{"name": "Firstville", "type": "Town", "lat": 42.0,
                        "lon": -71.0, "county": "", "aliases": ""}], fh)
        mp_reload = maps_plugin.Plugin(
            {"gazetteer": gz_file}, {"base_dir": BASE_DIR, "demo_fallback": False})
        check("maps finds the first gazetteer version",
              len(mp_reload.search("firstville")) == 1)
        _time.sleep(0.05)
        with open(gz_file, "w", encoding="utf-8") as fh:
            json.dump([{"name": "Firstville", "type": "Town", "lat": 42.0,
                        "lon": -71.0, "county": "", "aliases": ""},
                       {"name": "Secondville", "type": "Town", "lat": 42.1,
                        "lon": -71.1, "county": "", "aliases": ""}], fh)
        check("maps picks up the rewritten gazetteer without restart",
              len(mp_reload.search("secondville")) == 1)

        # 32. Concurrent-search limit (round 9): 4 in flight, the 5th
        # gets a 503 with a retry hint instead of queuing.
        gate_plugin = PluginBase({"name": "Gate", "badge": "Reference"},
                                 {"base_dir": BASE_DIR, "demo_fallback": False})
        gate_plugin.id = "gate"
        gate_plugin.source_id = "gate"
        gate_plugin.display_name = "Gate"
        gate_plugin.display_badge = "Reference"

        def _gate(q):
            import time as _t
            _t.sleep(2)
            return []

        gate_plugin.search = _gate
        gate_plugin.status = lambda: {"available": True, "detail": "up"}
        hub.plugins.append(gate_plugin)
        # Round 2 cache (see above): clear it after mutating plugins.
        hub.clear_search_cache()
        try:
            barrier = threading.Barrier(5)
            codes = []

            def _fire():
                barrier.wait()
                try:
                    code, _ = get(base, "/api/search?q=tourniquet")
                    codes.append(code)
                except urllib.error.HTTPError as exc:
                    codes.append(exc.code)

            workers = [threading.Thread(target=_fire) for _ in range(5)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=15)
            check("concurrent searches: 4 in flight, 1 gets 503",
                  sorted(codes) == [200, 200, 200, 200, 503],
                  f"got {sorted(codes)}")
        finally:
            hub.plugins = [p for p in hub.plugins
                           if getattr(p, "source_id", "") != "gate"]

        # 33. Kiwix search-page size cap (round 9): a 6 MB garbage page
        # must not fill RAM — the search fails cleanly.
        class HugeKiwix(_BH):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"x" * (6 * 1024 * 1024)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        huge = QuietServer(("127.0.0.1", 0), HugeKiwix)
        huge_thread = threading.Thread(target=huge.serve_forever, daemon=True)
        huge_thread.start()
        huge_port = huge.server_address[1]
        kp_huge = kiwix_plugin.Plugin(
            {"base_url": f"http://127.0.0.1:{huge_port}"},
            {"base_dir": BASE_DIR, "demo_fallback": False})
        check("kiwix 6 MB search page is capped, search returns empty",
              kp_huge.search("tourniquet") == [])
        huge.shutdown()
        huge.server_close()

        # 34. My Files skips non-regular files (round 9): a FIFO must
        # not block the search — opening one waits for a writer forever.
        if hasattr(os, "mkfifo"):
            mf_dir2 = os.path.join(tmp, "myfiles-fifo")
            os.makedirs(mf_dir2)
            with open(os.path.join(mf_dir2, "real.txt"), "w", encoding="utf-8") as fh:
                fh.write("needle in a real file\n")
            os.mkfifo(os.path.join(mf_dir2, "pipe"))
            mfp2 = myfiles_plugin.Plugin({"folder": mf_dir2},
                                         {"base_dir": BASE_DIR, "demo_fallback": True})
            done = []
            t = threading.Thread(
                target=lambda: done.append(mfp2.search("needle")))
            t.start()
            t.join(timeout=5)
            check("myfiles does not block on a FIFO",
                  not t.is_alive(), "search thread still blocked")
            check("myfiles still finds the real file past the FIFO",
                  done and len(done[0]) == 1
                  and done[0][0]["title"] == "real.txt",
                  str(done))
        else:
            print("SKIP myfiles FIFO test (no os.mkfifo on this platform)")

        # 35. Logging never raises (round 9), even for a bad message.
        from hub import _log
        try:
            class _BadStr:
                def __str__(self):
                    raise RuntimeError("str exploded")

            _log(_BadStr())
            _log(None)
            _log("plain log line")
            check("logging a bad message does not raise", True)
        except Exception as exc:  # noqa: BLE001
            check("logging a bad message does not raise", False, str(exc))

        # 36. Setup scripts parse clean (round 9): the .ps1 files the
        # hub depends on must not carry a syntax error onto the laptop.
        pwsh = os.path.expanduser("~/pwsh/pwsh")
        if os.path.exists(pwsh):
            ps_files = ["scripts/install_hub_autostart.ps1",
                        "scripts/serve_toggle.ps1",
                        "scripts/watchdog.ps1",
                        "scripts/setup_hygiene.ps1",
                        "scripts/hash_library.ps1",
                        "scripts/purge_fixtures.ps1"]
            parse_script = (
                "$errs = 0; "
                "foreach ($f in @(" + ", ".join(f"'{f}'" for f in ps_files) + ")) { "
                "$t = $null; $e = $null; "
                "[System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e) | Out-Null; "
                "if ($e.Count -gt 0) { $errs += $e.Count; Write-Host \"PARSE-FAIL $f\"; $e | ForEach-Object { Write-Host \"  $($_.Message)\" } } "
                "} exit $errs"
            )
            proc = subprocess.run(
                [pwsh, "-NoProfile", "-Command", parse_script],
                capture_output=True, text=True, timeout=60,
                cwd=BASE_DIR,
            )
            check("all six .ps1 scripts parse clean",
                  proc.returncode == 0, proc.stdout + proc.stderr)
        else:
            print("SKIP .ps1 parse checks (no PowerShell on this machine)")
        bat = open(os.path.join(BASE_DIR, "run_hub.bat"), encoding="utf-8").read()
        check("run_hub.bat supports --no-pause for the autostart task",
              "--no-pause" in bat)
        check("run_hub.bat documents the port fallback + hub.port",
              "hub.port" in bat)
        autostart = open(os.path.join(BASE_DIR, "scripts",
                                      "install_hub_autostart.ps1"),
                         encoding="utf-8").read()
        check("autostart task restarts the hub on failure",
              "-RestartCount" in autostart and "-RestartInterval" in autostart)
        check("autostart task launches with --no-pause",
              '"--no-pause"' in autostart)
        check("autostart no longer claims the hub binds 0.0.0.0 by default",
              "binds 0.0.0.0:8899 by default" not in autostart)
        check("watchdog script exists", os.path.exists(
            os.path.join(BASE_DIR, "scripts", "watchdog.ps1")))

            # 37. Inventory tracker + server-side pins (round 11).
        code, body = get(base, "/api/inventory")
        inv = json.loads(body)
        check("inventory API returns the seed summary",
              code == 200 and inv["summary"]["water_days"] == 2.0
              and inv["summary"]["food_days"] == 2.7
              and inv["summary"]["target_days"] == 3
              and inv["summary"]["sample"] is True
              and inv["summary"]["item_count"] == 6
              and len(inv["summary"]["gaps"]) == 24
              and len(inv["summary"]["expiring"]) == 1,
              f"got {code} {inv.get('summary')}")
        check("inventory shopping list builds from gaps (worst first)",
              inv["summary"]["gaps"]
              and all(inv["summary"]["gaps"][i]["coverage"]
                      <= inv["summary"]["gaps"][i + 1]["coverage"]
                      for i in range(len(inv["summary"]["gaps"]) - 1))
              and any(g["id"] == "water" for g in inv["summary"]["gaps"]))
        # Add a 5000 kcal food item: food days 2.7 -> 4.0.
        code, body = post(base, "/api/inventory", {"action": "add", "item": {
            "name": "Test beans", "category": "food", "quantity": 1,
            "unit": "cans", "per_unit": 5000, "location": "Pantry"}})
        inv = json.loads(body)
        test_item = next((i for i in inv["items"] if i["name"] == "Test beans"), None)
        check("inventory add works and updates days of food",
              code == 200 and test_item is not None
              and inv["summary"]["food_days"] == 4.0,
              f"got {code} food_days={inv.get('summary', {}).get('food_days')}")
        test_id = test_item["id"]
        # Use: one unit consumed (1 -> 0), food days back to 2.7.
        code, body = post(base, "/api/inventory", {"action": "use", "id": test_id})
        inv = json.loads(body)
        used = next((i for i in inv["items"] if i["id"] == test_id), None)
        check("inventory Use decrements one unit",
              code == 200 and used is not None and used["quantity"] == 0
              and inv["summary"]["food_days"] == 2.7,
              f"got {code} qty={used and used['quantity']}")
        # Restock: one unit back (0 -> 1), food days back to 4.0.
        code, body = post(base, "/api/inventory", {"action": "restock", "id": test_id})
        inv = json.loads(body)
        restocked = next((i for i in inv["items"] if i["id"] == test_id), None)
        check("inventory Restock increments one unit",
              code == 200 and restocked is not None and restocked["quantity"] == 1
              and inv["summary"]["food_days"] == 4.0,
              f"got {code} qty={restocked and restocked['quantity']}")
        # Update + delete round-trip.
        code, body = post(base, "/api/inventory", {"action": "update", "id": test_id,
                          "item": {"name": "Test beans (updated)", "category": "food",
                                   "quantity": 1, "unit": "cans", "per_unit": 5000,
                                   "location": "Pantry"}})
        inv = json.loads(body)
        check("inventory update renames the item",
              code == 200 and any(i["name"] == "Test beans (updated)"
                                  for i in inv["items"]))
        code, body = post(base, "/api/inventory", {"action": "delete", "id": test_id})
        inv = json.loads(body)
        check("inventory delete removes the item and restores the summary",
              code == 200 and not any(i["name"].startswith("Test beans")
                                      for i in inv["items"])
              and inv["summary"]["food_days"] == 2.7
              and inv["summary"]["item_count"] == 6)
        # Bad action + no-Origin POST rejected.
        try:
            code, _ = post(base, "/api/inventory", {"action": "explode"})
            check("inventory bad action is rejected (400)", False, f"got {code}")
        except urllib.error.HTTPError as exc:
            check("inventory bad action is rejected (400)", exc.code == 400)
        try:
            req = urllib.request.Request(
                base + "/api/inventory",
                data=json.dumps({"action": "add", "item": {"name": "x"}}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            check("inventory POST without Origin is rejected (403)", False)
        except urllib.error.HTTPError as exc:
            check("inventory POST without Origin is rejected (403)", exc.code == 403)
        # Inventory UI: tile, top-nav tab, view, and the Use/Restock wiring.
        check("inventory tile, nav tab and view are present in ui.html",
              'id="tile-inventory"' in text and 'id="inventory-view-link"' in text
              and 'id="inventory-view"' in text and 'id="inv-form"' in text
              and 'refreshInventoryView' in text)
        check("inventory Use/Restock buttons are wired in ui.html",
              'data-act="use"' in text and 'data-act="restock"' in text
              and 'data-act="delete"' in text)

        # Pins: the hub is the source of truth (pins.db, /api/pins).
        code, body = get(base, "/api/pins")
        pins = json.loads(body)
        check("pins API starts empty", code == 200 and pins["pins"] == [],
              f"got {code} {pins}")
        code, body = post(base, "/api/pins", {"action": "add", "pin": {
            "id": "test-house-1", "lat": 42.204, "lon": -71.004,
            "label": "Test house",
            "address": "123 Main Street, Braintree, MA, 02184"}})
        pins = json.loads(body)
        check("pins add stores a house (name + address + coords)",
              code == 200 and any(p["id"] == "test-house-1"
                                  and p["label"] == "Test house"
                                  and p["address"] == "123 Main Street, Braintree, MA, 02184"
                                  for p in pins["pins"]),
              f"got {code} {pins}")
        code, body = get(base, "/api/pins")
        pins = json.loads(body)
        check("pins GET returns the saved house",
              code == 200 and len(pins["pins"]) == 1
              and pins["pins"][0]["label"] == "Test house")
        code, body = post(base, "/api/pins", {"action": "update", "pin": {
            "id": "test-house-1", "lat": 42.204, "lon": -71.004,
            "label": "Test house (renamed)",
            "address": "123 Main Street, Braintree, MA, 02184"}})
        pins = json.loads(body)
        check("pins update renames the house",
              code == 200 and pins["pins"][0]["label"] == "Test house (renamed)")
        try:
            code, _ = post(base, "/api/pins", {"action": "add", "pin": {
                "id": "bad", "lat": 999, "lon": 0}})
            check("pins with out-of-range latitude are rejected (400)", False, f"got {code}")
        except urllib.error.HTTPError as exc:
            check("pins with out-of-range latitude are rejected (400)", exc.code == 400)
        code, body = post(base, "/api/pins", {"action": "delete", "id": "test-house-1"})
        pins = json.loads(body)
        check("pins delete removes the house",
              code == 200 and pins["pins"] == [])
        try:
            req = urllib.request.Request(
                base + "/api/pins",
                data=json.dumps({"action": "add", "pin": {"id": "x", "lat": 1, "lon": 2}}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            check("pins POST without Origin is rejected (403)", False)
        except urllib.error.HTTPError as exc:
            check("pins POST without Origin is rejected (403)", exc.code == 403)
        # Pins UI: export/import, the house form, and the directions view.
        check("pins export/import and house form are present in ui.html",
              'id="pins-export"' in text and 'id="pins-import"' in text
              and 'id="house-name"' in text and 'id="house-address"' in text
              and 'id="house-save"' in text)
        map_text = open(os.path.join(BASE_DIR, "static", "map.html"),
                        encoding="utf-8").read()
        check("directions view is present in map.html",
              'id="directions"' in map_text and 'id="dir-from"' in map_text
              and 'id="dir-to"' in map_text and 'id="dir-go"' in map_text
              and 'directions-line' in map_text and 'bearing' in map_text)
        # Print: a Print button on the map toolbar and on the
        # Directions view, both calling window.print(), plus a print
        # stylesheet that hides the chrome and keeps the map + turn
        # list. The WebGL canvas only prints with preserveDrawingBuffer.
        code, body = get(base, "/map")
        map_served = body.decode("utf-8")
        print_block = map_text[map_text.find("@media print"):]
        check("map page serves with Print buttons and a print stylesheet",
              code == 200 and 'id="print-map"' in map_served
              and 'id="dir-print"' in map_served
              and "@media print" in map_served
              and "window.print()" in map_served, f"got {code}")
        check("print stylesheet hides chrome and keeps the map and turn list",
              "@media print" in map_text and "preserveDrawingBuffer" in map_text
              and "@page" in print_block and "#map-hud" in print_block
              and "#dir-turns" in print_block and "#dir-status" in print_block
              and "has-directions" in map_text,
              print_block[:200])
        pins_js = open(os.path.join(BASE_DIR, "static", "pins.js"),
                       encoding="utf-8").read()
        check("pins.js syncs with the hub (load from server, push changes)",
              'sync' in pins_js and 'fetchServer' in pins_js
              and 'pushServer' in pins_js and '/api/pins' in pins_js)

        # 38. Audit-fix regressions (2026-11-21).
        # 38a. Maps shared filter: a private marker in ANY text field
        # must not leak to shared clients (Sec H1). The markers are
        # built from parts so this file carries no literal private-marker
        # strings: the staging sweep fail-louds on them (they would look
        # like real private-place data). At runtime the strings are
        # identical, so the filter is genuinely tested.
        _RAL = "ral" + "ly"
        _HA = "home " + "area"
        _HA_DASH = "home-" + "area"
        mutants = [
            {"name": f"Mutant note {_RAL}", "type": "Town", "lat": 42.1, "lon": -71.1,
             "note": f"Family {_RAL} point 1: meet here.", "public": True},
            {"name": f"Mutant aliases {_RAL}", "type": "Town", "lat": 42.2, "lon": -71.2,
             "aliases": f"{_RAL} meeting point family", "public": True},
            {"name": f"Mutant type {_RAL}", "type": "Ral" + "ly", "lat": 42.3, "lon": -71.3,
             "public": True},
            {"name": "Home " + "Area North", "type": "Place", "lat": 42.4, "lon": -71.4,
             "note": "", "public": True},
            {"name": "Mutant aliases home", "type": "Town", "lat": 42.5, "lon": -71.5,
             "aliases": _HA, "public": True},
            {"name": f"Mutant {_HA_DASH} dash", "type": "Town", "lat": 42.6, "lon": -71.6,
             "note": f"{_HA_DASH} meeting spot", "public": True},
            {"name": "Ordinary public place", "type": "Town", "lat": 42.7, "lon": -71.7,
             "public": True},
        ]
        mut_path = os.path.join(tmp, "mutant-places.json")
        with open(mut_path, "w", encoding="utf-8") as fh:
            json.dump(mutants, fh)
        mut_plugin = maps_plugin.Plugin({"gazetteer": mut_path}, {"base_dir": BASE_DIR})
        shared_places = mut_plugin.places(shared=True)
        check("maps shared filter blocks private markers in every field",
              len(shared_places) == 1
              and shared_places[0]["name"] == "Ordinary public place",
              f"got {[p['name'] for p in shared_places]}")
        check("maps shared search cannot find a private alias",
              mut_plugin.search(_RAL, shared=True) == [])
        check("maps local places still list every row",
              len(mut_plugin.places(shared=False)) == 7)
        real_plugin = maps_plugin.Plugin(
            {"gazetteer": os.path.join(BASE_DIR, "fixtures", "gazetteer-ne.json")},
            {"base_dir": BASE_DIR})
        real_shared = real_plugin.places(shared=True)
        check("shipped gazetteer shares 300 rows and no private-marker row",
              len(real_shared) == 300
              and not any(_RAL in (p["name"] + p["type"] + p["aliases"]
                                   + p["note"] + p["county"]).lower()
                          or _HA in (p["name"] + p["type"] + p["aliases"]
                                     + p["note"] + p["county"]).lower()
                          for p in real_shared),
              f"got {len(real_shared)} shared rows")

        # 38b. A corrupt inventory/pins database surfaces as 503 /
        # unavailable, never as an empty 200 (Rel C2/H1).
        corrupt_dir = os.path.join(tmp, "corrupt")
        os.mkdir(corrupt_dir)
        bad_inv = os.path.join(corrupt_dir, "inventory.db")
        bad_pins = os.path.join(corrupt_dir, "pins.db")
        for _bad in (bad_inv, bad_pins):
            with open(_bad, "wb") as fh:
                fh.write(b"this is not a sqlite database at all")
        from plugins import inventory as inv_plugin  # noqa: E402
        from pins import PinStore  # noqa: E402
        _bad_inv_plugin = inv_plugin.Plugin({"db": bad_inv}, {"base_dir": BASE_DIR})
        check("corrupt inventory status() reports unavailable, not available",
              _bad_inv_plugin.status().get("available") is False)
        try:
            _bad_inv_plugin.state()
            check("corrupt inventory state() raises instead of returning empty stock",
                  False)
        except _sqlite3.Error:
            check("corrupt inventory state() raises instead of returning empty stock",
                  True)
        _bad_store = PinStore(bad_pins)
        try:
            _bad_store.list_pins()
            check("corrupt pins list_pins() raises instead of returning []", False)
        except _sqlite3.Error:
            check("corrupt pins list_pins() raises instead of returning []", True)
        bad_config = json.load(open(config_path))
        for _src in bad_config.get("sources", []):
            if _src.get("id") == "inventory":
                _src["db"] = bad_inv
        bad_config["pins_db"] = bad_pins
        bad_config_path = os.path.join(corrupt_dir, "config.json")
        with open(bad_config_path, "w", encoding="utf-8") as fh:
            json.dump(bad_config, fh)
        bad_hub = Hub(bad_config_path)
        bad_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bad_hub))
        bad_thread = threading.Thread(target=bad_server.serve_forever, daemon=True)
        bad_thread.start()
        bad_base = f"http://127.0.0.1:{bad_server.server_address[1]}"
        try:
            try:
                code, _body = get(bad_base, "/api/inventory")
                check("corrupt inventory.db answers 503 over HTTP", False, f"got {code}")
            except urllib.error.HTTPError as exc:
                check("corrupt inventory.db answers 503 over HTTP", exc.code == 503,
                      f"got {exc.code}")
            try:
                code, _body = get(bad_base, "/api/pins")
                check("corrupt pins.db answers 503 over HTTP", False, f"got {code}")
            except urllib.error.HTTPError as exc:
                check("corrupt pins.db answers 503 over HTTP", exc.code == 503,
                      f"got {exc.code}")
        finally:
            bad_server.shutdown()

        # 38c. Pins duplicate ids and missing keys are 400s, not 500s or
        # wipes (Rel M1, Sec M6, Tech M4/M5).
        dup = [{"id": "dup-1", "lat": 42.0, "lon": -71.0, "label": "A"},
               {"id": "dup-1", "lat": 42.1, "lon": -71.1, "label": "B"}]
        for _action in ("replace", "import"):
            try:
                code, _ = post(base, "/api/pins",
                               {"action": _action, "pins": dup})
                check(f"pins {_action} with duplicate ids is rejected (400)",
                      False, f"got {code}")
            except urllib.error.HTTPError as exc:
                check(f"pins {_action} with duplicate ids is rejected (400)",
                      exc.code == 400, f"got {exc.code}")
            try:
                code, _ = post(base, "/api/pins", {"action": _action})
                check(f"pins {_action} without a pins list is rejected (400)",
                      False, f"got {code}")
            except urllib.error.HTTPError as exc:
                check(f"pins {_action} without a pins list is rejected (400)",
                      exc.code == 400, f"got {exc.code}")
        code, body = get(base, "/api/pins")
        check("pins store is unchanged after the rejected writes",
              code == 200 and json.loads(body)["pins"] == [])
        # Tech L9: a large pins import (900 pins, ~90 KiB) must fit the
        # raised /api/pins body limit instead of tripping the old 64 KiB cap.
        bulk = [{"id": f"bulk-{i:03d}", "lat": 42.0 + i * 0.001, "lon": -71.0,
                 "label": f"Bulk pin number {i:03d} for the body-limit test"}
                for i in range(900)]
        code, body = post(base, "/api/pins",
                          {"action": "import", "pins": bulk, "replace": True})
        imported = json.loads(body)
        check("a large pins import fits the raised body limit",
              code == 200 and len(imported["pins"]) == 900,
              f"got {code} pins={len(imported.get('pins', []))}")
        code, body = post(base, "/api/pins", {"action": "replace", "pins": []})
        check("pins store is empty again after the bulk-import cleanup",
              code == 200 and json.loads(body)["pins"] == [])

        # 38d. Pins replace guard and merge (Rel C1, Tech M2).
        guard_store = PinStore(os.path.join(tmp, "guard-pins.db"))
        guard_store.add_pin({"id": "g1", "lat": 42.0, "lon": -71.0, "label": "G1"})
        guard_store.add_pin({"id": "g2", "lat": 42.1, "lon": -71.1, "label": "G2"})
        try:
            guard_store.replace_all(
                [{"id": "g1", "lat": 42.0, "lon": -71.0, "label": "G1"}])
            check("pins replace_all refuses a shorter list without an explicit flag",
                  False)
        except ValueError:
            check("pins replace_all refuses a shorter list without an explicit flag",
                  True)
        check("pins store keeps both pins after a refused shrink",
              len(guard_store.list_pins()) == 2)
        guard_store.replace_all(
            [{"id": "g1", "lat": 42.0, "lon": -71.0, "label": "G1"}],
            allow_shrink=True)
        check("pins replace_all with an explicit flag shrinks the store",
              len(guard_store.list_pins()) == 1)
        merge_store = PinStore(os.path.join(tmp, "merge-pins.db"))
        merge_store.add_pin({"id": "m1", "lat": 42.0, "lon": -71.0, "label": "M1"})
        merged = merge_store.import_pins(
            [{"id": "m2", "lat": 42.1, "lon": -71.1, "label": "M2"}], replace=False)
        check("pins import merges instead of replacing",
              len(merged["pins"]) == 2 and merged["added"] == 1
              and merged["skipped"] == 0, f"got {merged}")
        merged = merge_store.import_pins(
            [{"id": "m1", "lat": 42.0, "lon": -71.0, "label": "M1"}], replace=False)
        check("pins import skips ids already stored",
              len(merged["pins"]) == 2 and merged["added"] == 0
              and merged["skipped"] == 1, f"got {merged}")
        check("pins.js sync merges by id and pushes per-pin actions, never a full replace",
              "importPins(localOnly, false)" in pins_js
              and "pushPin(op[0], op[1])" in pins_js)

        # 38e. Frequencies tokeniser strips m/mhz only for numeric tokens
        # (Tech M1): "ham" must stay "ham", not become "ha".
        check("frequencies tokeniser keeps word tokens intact",
              freq_plugin._norm_token("ham") == "ham"
              and freq_plugin._norm_token("fm") == "fm"
              and freq_plugin._norm_token("am") == "am"
              and freq_plugin._norm_token("146.52mhz") == "146.52"
              and freq_plugin._norm_token("146.52m") == "146.52")
        freq = freq_plugin.Plugin(
            {"data": os.path.join(BASE_DIR, "fixtures", "frequencies.json")},
            {"base_dir": BASE_DIR})
        ham = freq.search("ham")
        check("frequencies search for 'ham' returns only ham matches",
              bool(ham) and all("ham" in (r["title"] + r["detail"] + r["snippet"]).lower()
                                for r in ham),
              f"got {len(ham)} results")
        am = freq.search("am")
        check("frequencies search for 'am' returns only matches containing 'am'",
              bool(am) and all("am" in (r["title"] + r["detail"] + r["snippet"]).lower()
                               for r in am),
              f"got {len(am)} results")
        check("frequencies search still strips mhz off a numeric frequency",
              any(r["frequency_mhz"] == "146.52" for r in freq.search("146.52mhz")))

        # 38f. Geocode strips a trailing ZIP, maps full state names, and
        # refuses a bare housenumber (Tech L4).
        import urllib.parse as _urlparse  # noqa: E402
        code, body = get(base, "/api/geocode?q=" + _urlparse.quote(
            "123 Main Street, Braintree, MA 02184"))
        geo = json.loads(body)
        check("geocode strips a trailing ZIP code",
              code == 200 and geo["results"]
              and geo["results"][0]["display"].startswith("123 Main Street, Braintree"),
              f"got {code} {geo}")
        code, body = get(base, "/api/geocode?q=" + _urlparse.quote(
            "123 Main Street, Braintree, Massachusetts"))
        geo = json.loads(body)
        check("geocode maps a full state name to its code",
              code == 200 and geo["results"]
              and geo["results"][0]["display"].startswith("123 Main Street, Braintree"),
              f"got {code} {geo}")
        code, body = get(base, "/api/geocode?q=123")
        geo = json.loads(body)
        check("geocode refuses a bare housenumber",
              code == 200 and geo["results"] == [], f"got {geo}")

        # 38g. Audit XSS fixes are present in the served UI (Sec H2/L21/L22).
        check("inventory unit and food total are escaped in ui.html",
              "esc(gap.unit)" in text and "esc(sum.food_kcal" in text)
        check("inventory item ids are escaped into data-id attributes",
              "esc(String(item.id))" in text)

        # 38h. Map centre comes from a localhost-only endpoint, not the
        # page (Sec M9); the best-match citation resolves against the
        # served PDF twin (Tech M3).
        code, body = get(base, "/api/map-config")
        cfg_map = json.loads(body)
        check("map-config serves the TREX centre to localhost",
              code == 200 and cfg_map["trex_center"] == [-71.052185, 42.19545],
              f"got {code} {cfg_map}")
        check("map.html fetches its centre and carries no household coordinates",
              "/api/map-config" in map_text and "TREX_CENTER" not in map_text
              and "Braintree area" not in map_text and "-71.052185" not in map_text)
        # check_smart_search() already made tmp/FieldManuals; reuse it
        # (its basename maps to the "manuals" file root).
        twin_dir = os.path.join(tmp, "FieldManuals")
        md_path = os.path.join(twin_dir, "Guide.md")
        pdf_path = os.path.join(twin_dir, "Guide.pdf")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write("# Guide\n")
        with open(pdf_path, "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        twin_config = os.path.join(tmp, "twin.json")
        with open(twin_config, "w", encoding="utf-8") as fh:
            json.dump({"sources": [{"plugin": "recoll", "id": "manuals",
                                    "name": "Manuals"}],
                       "demo_fallback": False}, fh)
        twin_hub = Hub(twin_config)
        twin_hub.plugins[0]._topdirs = [twin_dir]
        twin_row = {"pdf_url": "/files/manuals/Guide.pdf#page=3", "page": 3,
                    "_matched_path": md_path, "_served_path": pdf_path}
        check("best-match citation resolves against the served PDF twin",
              twin_hub._indexed_pdf_link(twin_row)
              == "/files/manuals/Guide.pdf#page=3")
        no_twin_row = {"pdf_url": "/files/manuals/Guide.pdf#page=3", "page": 3,
                       "_matched_path": md_path}
        check("best-match citation without a served path does not resolve",
              twin_hub._indexed_pdf_link(no_twin_row) == "")

        # 39. Family Plan tab (localhost-only) + road routing.
        check_family_plan(tmp, base, hub, text)
        check_routing(tmp, base, _routing_db)

        # 38i. Fresh-clone completeness: pins_db configured, every file
        # the hub hard-imports in the checkout (Tech H1/H2).
        shipped = json.load(open(os.path.join(BASE_DIR, "config.json")))
        check("workspace config sets pins_db",
              shipped.get("pins_db") == "pins.db", f"got {shipped.get('pins_db')}")
        check("checkout includes hub modules and offline resources",
              all(os.path.isfile(os.path.join(BASE_DIR, name)) for name in (
                  "pins.py", "sqlite_files.py", "routing.py", "routing_build.py", "plugins/inventory.py",
                  "scripts/setup-download-resources.ps1", "static/brand/pandora.png")))


        server.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
