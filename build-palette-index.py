#!/usr/bin/env python3
"""Build the command-palette FTS5 title index (palette.db).

Indexes TITLES ONLY (filenames), not full text — that is Recoll's job.
The palette needs instant "I know the manual name" search over the
19,416 field manuals (+ 369 USGS quad PDFs), and FTS5 in the Python
stdlib does that in <5ms with no server and no new dependency.

Sources (on the laptop):
- C:\\Library\\FieldManuals\\ — 19,416 unzipped manuals (.pdf/.epub/.md).
  Title = filename without extension, underscores/dashes -> spaces.
  kind = "manual", detail = relative path (category/filename).
- C:\\Library\\Maps\\quads\\ — 369 USGS topo quad GeoPDFs.
  Title = quad name from the filename (e.g. "Blue Hills").
  kind = "quad", detail = PDF filename.

Output: palette.db next to this script (the hub reads it read-only
via /api/palette). Rerun this script to rebuild it. The current index is
replaced only after the new database finishes building.

Usage (on the laptop, from the search-hub folder):
    python build-palette-index.py
    python build-palette-index.py --manuals C:\\Library\\FieldManuals --quads C:\\Library\\Maps\\quads
"""

import argparse
import os
import re

from sqlite_files import rebuild_index

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "palette.db")

MANUAL_EXTS = {".pdf", ".epub", ".md", ".txt", ".html"}


def clean_title(filename):
    """Filename -> readable title: strip extension, _/- -> space,
    collapse whitespace, strip a leading date/code prefix."""
    name = os.path.splitext(filename)[0]
    name = name.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def iter_manuals(manuals_dir):
    """Yield (title, kind, detail) for every manual file."""
    for root, _dirs, files in os.walk(manuals_dir):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in MANUAL_EXTS:
                continue
            title = clean_title(fn)
            if not title:
                continue
            rel = os.path.relpath(os.path.join(root, fn), manuals_dir)
            yield (title, "manual", rel.replace(os.sep, "/"))


def iter_quads(quads_dir):
    """Yield (title, kind, detail) for every quad GeoPDF.

    Filename form: MA_Blue_Hills_20150715_TM_geo.pdf -> "Blue Hills".
    """
    try:
        names = os.listdir(quads_dir)
    except OSError:
        return
    for fn in names:
        if not fn.lower().endswith(".pdf"):
            continue
        # Strip the state prefix and the date/suffix: MA_Blue_Hills_2015... -> Blue Hills
        base = os.path.splitext(fn)[0]
        base = re.sub(r"^[A-Z]{2}_", "", base)
        base = re.sub(r"_\d{8}.*$", "", base)
        # Drop a trailing "_geo" left when the filename has no date.
        base = re.sub(r"_geo$", "", base)
        title = clean_title(base)
        if not title:
            continue
        yield (title, "quad", fn)


def build(manuals_dir, quads_dir, db_path=DB_PATH):
    with rebuild_index(db_path) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE palette USING fts5("
            "title, kind, detail, tokenize='porter')"
        )
        rows = 0
        if manuals_dir and os.path.isdir(manuals_dir):
            batch = []
            for row in iter_manuals(manuals_dir):
                batch.append(row)
                if len(batch) >= 1000:
                    conn.executemany(
                        "INSERT INTO palette(title, kind, detail) VALUES (?,?,?)",
                        batch,
                    )
                    rows += len(batch)
                    batch = []
            if batch:
                conn.executemany(
                    "INSERT INTO palette(title, kind, detail) VALUES (?,?,?)",
                    batch,
                )
                rows += len(batch)
        if quads_dir and os.path.isdir(quads_dir):
            batch = list(iter_quads(quads_dir))
            if batch:
                conn.executemany(
                    "INSERT INTO palette(title, kind, detail) VALUES (?,?,?)",
                    batch,
                )
                rows += len(batch)
        conn.execute("INSERT INTO palette(palette) VALUES('optimize')")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the palette FTS5 index.")
    parser.add_argument("--manuals", default=r"C:\Library\FieldManuals")
    parser.add_argument("--quads", default=r"C:\Library\Maps\quads")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args(argv)
    rows = build(args.manuals, args.quads, args.db)
    size = os.path.getsize(args.db)
    print(f"palette.db: {rows} titles, {size} bytes -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
