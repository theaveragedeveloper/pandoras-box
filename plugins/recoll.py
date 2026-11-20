"""Recoll plug-in — full-text search over PDFs and documents.

Expected backend: Recoll installed with an index built over the library
folders (field manuals, Hesperian, USDA, vehicle manuals, own docs).
Indexing is a one-time, plugged-in job — run it once, then the hub just
queries:

    recollindex -c <config_dir>        (build/update the index, plugged in)
    recollq -c <config_dir> -t -q ...  (what this plug-in runs per search)

Query binary: the plug-in prefers ``recollq`` but falls back to
``recoll -t``, which behaves exactly the same way. That fallback
matters on Windows: the Recoll user manual (1.45.0, section 3.4) says
"Depending on the platform, recollq is not always built or installed
by default (as recoll -t works the same)" — so on the 5424 the hub
uses whichever one is present, and reports which in status().

The plug-in shells out to ``recollq`` and parses its output. recollq
prints one block per hit, separated by blank lines; the first line of a
block is the document URL/path and the following lines (with -A) are the
abstract/snippet. The parser is deliberately tolerant — recollq output
varies a little between versions.

Badge: paths containing medical / hesperian / wikimed get "Medical",
paths in a manuals folder get "Manual", everything else "Reference".
Tune with cfg "medical_markers" and "manual_markers".

If recollq is missing or the index is empty and demo_fallback is on,
matching rows from fixtures/demo_recoll.json are returned, marked demo.
"""

import json
import os
import shutil
import subprocess

from plugins.base import PluginBase


class Plugin(PluginBase):
    id = "recoll"
    name = "Field Manuals & Documents"
    badge = "Manual"
    kind = "recoll"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.config_dir = self.data_path("config_dir", "")
        try:
            self.timeout = int(cfg.get("timeout_seconds", 20))
        except (TypeError, ValueError):
            self.timeout = 20
        # A zero/negative timeout makes subprocess.run fail immediately
        # on every search; clamp to at least 1 second.
        self.timeout = max(1, self.timeout)
        # Query binary: recollq if present, else "recoll -t" (same
        # behaviour per the Recoll manual 1.45.0 §3.4). On Windows
        # recollq is not always installed; recoll -t covers that case.
        self.query_bin = "recollq" if shutil.which("recollq") else "recoll"
        self.medical_markers = [m.lower() for m in cfg.get("medical_markers", ["medical", "hesperian", "wikimed", "tccc", "doctor", "dentist"])]
        self.manual_markers = [m.lower() for m in cfg.get("manual_markers", ["manual", "field-manual", "fm-", "tc-", "trex", "army"])]

    # -- live backend ----------------------------------------------------
    def _load_topdirs(self):
        """Recoll topdirs from recoll.conf — the folders Recoll indexes.

        Read once and cached. The topdirs line is space-separated
        forward-slash paths (e.g. C:/Library/FieldManuals
        C:/Library/MyFiles). Used to turn a result's absolute file path
        into a hub-relative /files/ URL (audit 2026-11-20: browsers
        block file:// links from http:// pages, so a manual PDF result
        was unopenable). Returns [] when the config cannot be read.
        """
        if hasattr(self, "_topdirs"):
            return self._topdirs
        self._topdirs = []
        if not self.config_dir:
            return self._topdirs
        conf = os.path.join(self.config_dir, "recoll.conf")
        try:
            with open(conf, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("topdirs"):
                        _, _, rest = line.partition("=")
                        for p in rest.strip().split():
                            p = p.replace("/", os.sep)
                            if os.path.isabs(p):
                                self._topdirs.append(os.path.normpath(p))
                        break
        except OSError:
            self._topdirs = []
        return self._topdirs

    def library_roots(self):
        """{rootname: dir} for the hub's /files/ route.

        Root names are short: FieldManuals -> "manuals", MyFiles ->
        "myfiles", anything else -> the folder name lowercased.
        """
        roots = {}
        for d in self._load_topdirs():
            base = os.path.basename(d.rstrip(os.sep)).lower()
            if base == "fieldmanuals":
                name = "manuals"
            elif base == "myfiles":
                name = "myfiles"
            else:
                name = base or "files"
            roots[name] = d
        return roots

    def _hub_url(self, path):
        """Absolute file path -> hub-relative /files/ URL, or "".

        Matches the path against the Recoll topdirs; the relative part
        becomes the URL. Returns "" when the path is under no known
        root (the caller then keeps the file:// URL as a fallback).
        """
        import urllib.parse
        norm = os.path.normpath(path)
        for name, root in self.library_roots().items():
            root_n = os.path.normpath(root)
            # Windows paths are case-insensitive; compare lowercased.
            if os.name == "nt":
                if norm.lower().startswith(root_n.lower() + os.sep):
                    rel = os.path.relpath(norm, root_n)
                    return "/files/" + name + "/" + urllib.parse.quote(
                        rel.replace(os.sep, "/")
                    )
            else:
                if norm.startswith(root_n + os.sep):
                    rel = os.path.relpath(norm, root_n)
                    return "/files/" + name + "/" + urllib.parse.quote(
                        rel.replace(os.sep, "/")
                    )
        return ""

    def _live_search(self, query):
        # -A asks recollq for the abstract/snippet lines the parser
        # turns into result snippets; without it the parser only ever
        # sees the document URL and falls back to "Document: <path>".
        # -n MUST come before -q: recollq absorbs a trailing "-n 3"
        # into the query (it then returns all matches instead of 3).
        # The plugin still caps with out[:max_results], but putting -n
        # first keeps recollq from doing the extra work.
        cmd = [self.query_bin, "-t", "-A", "-n", str(self.max_results), "-q", query]
        if self.config_dir:
            cmd = [self.query_bin, "-c", self.config_dir, "-t", "-A", "-n", str(self.max_results), "-q", query]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            # A failed run (bad config dir, no index, bad query syntax)
            # must not be parsed as results — fall back to demo data.
            raise OSError(f"{self.query_bin} exited {proc.returncode}")
        return self._parse_recollq(proc.stdout)

    @staticmethod
    def _looks_like_path(url):
        """True if a recollq block's first line is a document URL/path."""
        if url.startswith(("file://", "/", "\\\\")):
            return True
        # Windows drive-letter path, e.g. C:\... or C:/...
        return (
            len(url) >= 3 and url[0].isalpha() and url[1] == ":"
            and url[2] in "\\/"
        )

    def _parse_recollq(self, text):
        """Parse recollq -t -A output into result dicts.

        The -t -A format is line-based, NOT blank-line separated
        (audit 2026-11-20: the old parser split on "\\n\\n" and got
        zero results because this output has no blank lines):
            Recoll query: Query(...)
            N results
            <mimetype>\\t[<file-url>]\\t[<subdoc>]\\t<size>\\tbytes\\t
            ABSTRACT
            <abstract lines>
            /ABSTRACT
            <mimetype>\\t[<file-url>]\\t...
            ...
        A result header is a line containing "\\t[" whose first bracket
        holds a file:// URL. The snippet is the lines between ABSTRACT
        and /ABSTRACT. The old blank-line block format is still
        accepted as a fallback (a block whose first line is a path).
        """
        out = []
        lines = text.splitlines()
        i = 0
        n = len(lines)
        while i < n and len(out) < self.max_results:
            ln = lines[i].strip()
            # Skip the "Recoll query:" / "N results" header lines.
            if ln.startswith("Recoll query:") or ln.endswith(" results"):
                i += 1
                continue
            url = self._header_url(ln)
            if url is None:
                # Not a result header. Try the old block format: a
                # bare path line on its own (blank-line separated).
                if self._looks_like_path(ln):
                    # Collect the block until a blank line or a new
                    # header.
                    block = [ln]
                    j = i + 1
                    while j < n and lines[j].strip() and self._header_url(lines[j].strip()) is None:
                        block.append(lines[j].strip())
                        j += 1
                    snippet = " ".join(block[1:])[:300]
                    out.append(self._result_for(url=ln, snippet=snippet))
                    i = j
                    continue
                i += 1
                continue
            # A result header: gather the ABSTRACT lines that follow.
            snippet_parts = []
            j = i + 1
            if j < n and lines[j].strip() == "ABSTRACT":
                j += 1
                while j < n and lines[j].strip() != "/ABSTRACT":
                    snippet_parts.append(lines[j].strip())
                    j += 1
                # j now points at "/ABSTRACT" (or past the end).
                i = j + 1
            else:
                i = j
            snippet = " ".join(snippet_parts)[:300]
            out.append(self._result_for(url=url, snippet=snippet))
        return out[: self.max_results]

    @staticmethod
    def _header_url(line):
        """File URL from a recollq -t result header line, or None.

        Header lines look like:
            text/html\\t[file:///C:/Library/...epub]\\t[ch005.xhtml]\\t5895\\tbytes\\t
        The file URL is the first bracketed token. Returns None when
        the line is not a result header.
        """
        if "\t[" not in line:
            return None
        start = line.find("[")
        end = line.find("]", start)
        if start < 0 or end < 0:
            return None
        cand = line[start + 1 : end]
        if cand.startswith("file://") or Plugin._looks_like_path(cand):
            return cand
        return None

    def _result_for(self, url, snippet):
        """One result dict from a parsed file URL + snippet."""
        path = url.replace("file:///", "").replace("file://", "")
        title = os.path.basename(path)
        title = title.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
        low = path.lower()
        if any(m in low for m in self.medical_markers):
            badge = "Medical"
        elif any(m in low for m in self.manual_markers):
            badge = "Manual"
        else:
            badge = "Reference"
        # Hub-relative URL (audit 2026-11-20): a file:// link is dead
        # in the browser (blocked from http:// pages), so serve the
        # file through the hub's /files/ route. Falls back to the raw
        # file:// URL when the path is under no known library root.
        hub_url = self._hub_url(path)
        # Snippet fallback must not leak the absolute path to remote
        # clients (audit 2026-11-20, 3b): use the file name only.
        snippet = snippet or f"Document: {os.path.basename(path)}"
        return {
            "title": title or url,
            "snippet": snippet,
            "url": hub_url or url,
            "badge": badge,
            "detail": path,
            "demo": False,
        }

    # -- demo fallback ---------------------------------------------------
    def _demo_search(self, query):
        path = os.path.join(self.ctx.get("base_dir", ""), "fixtures", "demo_recoll.json")
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            return []
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            # Empty query in, empty out (round 9): all() over an empty
            # token list would otherwise match every demo row.
            return []
        out = []
        for row in rows:
            hay = (row.get("title", "") + " " + row.get("snippet", "")).lower()
            if all(t in hay for t in tokens):
                out.append(
                    {
                        "title": row["title"],
                        "snippet": row["snippet"],
                        "url": row.get("url", ""),
                        "badge": row.get("badge", "Manual"),
                        "detail": row.get("path", "") + " (sample)",
                        "demo": True,
                    }
                )
        return out[: self.max_results]

    # -- contract --------------------------------------------------------
    def search(self, query):
        try:
            results = self._live_search(query)
            if results:
                return results
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        if self.demo_fallback():
            return self._demo_search(query)
        return []

    def status(self):
        try:
            proc = subprocess.run(
                [self.query_bin, "-h"], capture_output=True, text=True, timeout=5
            )
            if proc.returncode not in (0, 1):
                raise OSError(f"{self.query_bin} not usable")
            if self.query_bin == "recoll":
                return {"available": True, "detail": "recoll present (using recoll -t; recollq not found)"}
            return {"available": True, "detail": "recollq present"}
        except (OSError, subprocess.SubprocessError, ValueError):
            if self.demo_fallback():
                return {
                    "available": False,
                    "detail": "Recoll not installed — showing sample data",
                }
            return {"available": False, "detail": "recollq / recoll not found"}
