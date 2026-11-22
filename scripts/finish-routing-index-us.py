"""Finish a US routing index from a DB that already has edges_raw + nodes.

Recovery path for a build that was killed during the collapse phase
(the time_s-ordered collapse does random PK inserts and is far too slow
at US scale: ~100M rows into a B-tree that does not fit in cache).

The collapse here orders by (a, b, time_s) instead, so the PK inserts
land sequentially (each leaf page is written once, stays in cache)
instead of randomly. Within one (a, b) pair the fastest edge comes
first and (name <> '') prefers a named edge on a time tie, so
INSERT OR IGNORE keeps exactly the edge the original collapse kept.

Usage (on the laptop):
    python finish-routing-index-us.py C:\\Library\\geocode\\routing-us.db

Expects the DB to contain edges_raw, nodes, junction, names (all
complete) and an empty edges table. Writes meta, runs components +
CSR export, ANALYZEs and VACUUMs. The timings recorded in meta are
the combined totals of the killed runs plus this finish run.
"""

import importlib.util
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "build_routing_index_us",
    os.path.join(_HERE, "build-routing-index-us.py"))
_BUILD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BUILD)
compute_components = _BUILD.compute_components
export_csr = _BUILD.export_csr

# Timings (seconds) of the killed runs this finish completes, from
# their logs (US build, 2026-11-22). Kept so meta total_s is honest.
PASS1_S = 4342
JUNCTIONS_S = 2670
WALK_S = 4352
JUNCTIONS = 20953411
CHAINS = 100351508


def main():
    if len(sys.argv) != 2:
        print("usage: finish-routing-index-us.py <routing db>", file=sys.stderr)
        return 2
    db = sys.argv[1]
    overall = time.time()
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-262144")
        # The killed collapse left edges empty (its transaction never
        # committed); make sure before refilling it.
        conn.execute("DELETE FROM edges")
        conn.commit()
        n_raw = conn.execute(
            "SELECT COUNT(*) FROM edges_raw").fetchone()[0]
        print(f"edges_raw: {n_raw:,} rows; collapsing in (a, b) order...",
              flush=True)
        started = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO edges (a, b, name, length_m, time_s)"
            " SELECT a, b, name, length_m, time_s FROM edges_raw"
            " WHERE a <> b ORDER BY a, b, time_s ASC, (name <> '') DESC")
        conn.execute("DROP TABLE edges_raw")
        counts = {}
        for table in ("nodes", "edges"):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"collapse done in {time.time() - started:.0f}s: "
              f"{counts['nodes']:,} nodes, {counts['edges']:,} edges",
              flush=True)
        ncomp = compute_components(conn)
        export_csr(conn)
        conn.execute("DROP TABLE names")
        conn.execute("DROP TABLE junction")
        conn.execute("ANALYZE")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("coverage", "United States (CONUS, Alaska, Hawaii)"))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("sources", r"C:\Library\geocode\us-latest.osm.pbf"))
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
            ("junctions", str(JUNCTIONS)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("chains", str(CHAINS)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("components", str(ncomp)))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("bend_degrees", "30"))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("contracted", "1"))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("excluded_highway", "service"))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("resumed", "1"))
        finish_s = round(time.time() - overall)
        total_s = PASS1_S + JUNCTIONS_S + WALK_S + finish_s
        for key, val in (("pass1_s", PASS1_S), ("junctions_s", JUNCTIONS_S),
                         ("walk_s", WALK_S), ("finish_s", finish_s),
                         ("total_s", total_s)):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (key, str(val)))
        conn.commit()
        print("vacuuming...", flush=True)
        conn.execute("VACUUM")
        conn.commit()
        print(f"done: {counts['nodes']:,} nodes, {counts['edges']:,} "
              f"directed edges, {ncomp:,} components -> {db} "
              f"(finish {finish_s}s, combined total {total_s}s)",
              flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
