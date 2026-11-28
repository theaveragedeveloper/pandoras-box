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
import re
import shutil
import subprocess
import time
from urllib.parse import unquote

from plugins.base import PluginBase
from query_expand import expand_query, search_words

MANUAL_ID = re.compile(r"(FM|ATP|ADP|ADRP|TC|TM|JP|DA PAM)[ _-]*(\d+(?:[.\-]\d+)+[A-Z]?)",
                       re.IGNORECASE)


def _recoll_literal(query):
    """Escape a user query for recollq -q as literal terms (Sec L24).

    Recoll parses its own query language, so a raw user query could
    inject operators (filename:, mime:, dir:, OR, *, quotes, parens)
    and broaden or narrow the search beyond plain full-text. Recoll
    operators are intentionally NOT exposed: every whitespace-
    separated word is wrapped in double quotes (with \\ and " escaped),
    so it is matched as a literal term. The MANUAL_ID path below is
    the one exception — it builds a validated filename: query itself
    and passes literal=False.
    """
    words = (query or "").split()
    if not words:
        return '""'
    return " ".join(
        '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'
        for word in words
    )


def _dedupe_results(results):
    """Drop repeat hits for the same served document.

    Recoll indexes the same manual three times (PDF/EPUB/MD) and the
    PDF-twin swap collapses all three to the same PDF URL, so one
    document can occupy three of the eight slots. Keep one hit per
    URL, preferring the copy that carries a page anchor: only a
    directly matched PDF has a page number, and the Best match box
    needs it to cite a manual passage, so an EPUB/MD copy ranked
    first must not displace the PDF copy that has the page.
    """
    def _rank(row):
        score = 0
        page = row.get("page")
        if type(page) is int and page > 0:
            score += 2
        matched = row.get("_matched_path") or ""
        if matched.lower().endswith(".pdf"):
            score += 1
        return score

    best = {}
    order = []
    for row in results:
        url = row.get("url")
        if url not in best:
            best[url] = row
            order.append(url)
        elif _rank(row) > _rank(best[url]):
            best[url] = row
    return [best[url] for url in order]


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

    def _live_search(self, query, *, timeout=None, literal=True):
        # -A -p 3 asks Recoll for up to three matching snippets with
        # page numbers when the file and index support them.
        # -n MUST come before -q: recollq absorbs a trailing "-n 3"
        # into the query (it then returns all matches instead of 3).
        # The plugin still caps with out[:max_results], but putting -n
        # first keeps recollq from doing the extra work.
        # Sec L24: user queries are passed as quoted literal terms
        # (see _recoll_literal); only the validated MANUAL_ID query
        # below passes literal=False.
        q = _recoll_literal(query) if literal else query
        cmd = [self.query_bin, "-t", "-A", "-p", "3", "-n", str(self.max_results), "-q", q]
        if self.config_dir:
            cmd[1:1] = ["-c", self.config_dir]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=self.timeout if timeout is None else timeout,
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
        and /ABSTRACT, or a numbered match in a SNIPPETS block. The old
        blank-line block format is still
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
            # A result header: gather the snippet or legacy abstract.
            snippet_parts = []
            j = i + 1
            page = None
            marker = lines[j].strip() if j < n else ""
            if marker in {"ABSTRACT", "SNIPPETS"}:
                j += 1
                while j < n and lines[j].strip() != "/" + marker and self._header_url(lines[j].strip()) is None:
                    snippet_parts.append(lines[j].strip())
                    j += 1
                i = j + 1 if j < n and lines[j].strip() == "/" + marker else j
            else:
                i = j
            if marker == "SNIPPETS":
                matches = [
                    match for part in snippet_parts
                    if (match := re.fullmatch(r"(-?[0-9]{1,6})\s*:\s*(.+)", part))
                ]
                if matches:
                    best = next((match for match in matches if int(match[1]) > 0), matches[0])
                    page = int(best[1]) or None
                    snippet_parts = [best[2]]
            snippet = " ".join(snippet_parts)[:300]
            out.append(self._result_for(url=url, snippet=snippet, page=page))
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

    def _pdf_twin(self, path):
        """PDF twin of an EPUB/MD result, or None.

        The vault keeps formats in sibling top-level dirs under the
        library root (FieldManuals/PDF, EPUB, MD) with identical
        relative paths, so an EPUB at <root>/EPUB/<rel>.epub has its
        PDF at <root>/PDF/<rel>.pdf. Returns the PDF path when it
        exists on disk, else None.
        """
        norm = os.path.normpath(path)
        for root in self.library_roots().values():
            root_n = os.path.normpath(root)
            if os.name == "nt":
                under = norm.lower().startswith(root_n.lower() + os.sep)
            else:
                under = norm.startswith(root_n + os.sep)
            if not under:
                continue
            rel = os.path.relpath(norm, root_n)
            parts = rel.split(os.sep)
            if len(parts) < 2 or parts[0].lower() not in ("epub", "md"):
                continue
            twin_rel = os.path.join(
                "PDF", *parts[1:-1],
                os.path.splitext(parts[-1])[0] + ".pdf")
            twin = os.path.join(root_n, twin_rel)
            if os.path.isfile(twin):
                return twin
        return None

    def _result_for(self, url, snippet, page=None):
        """One result dict from a parsed file URL + snippet."""
        # Strip file:// but keep a leading "/" for POSIX paths and a
        # drive letter for Windows paths (the old replace() dropped
        # the leading "/" from file:///home/...).
        if url.startswith("file://"):
            path = url[len("file://"):]
            if len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            if "%" in path and not os.path.isfile(path):
                decoded = unquote(path)
                if os.path.isfile(decoded):
                    path = decoded
        else:
            path = url
        title = os.path.basename(path)
        label = re.search(r"\(((?:19|20)[0-9]{2}(?:-(?:0[1-9]|1[0-2]))?)\)", title)
        title = title.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
        low = path.lower()
        matched_path = path
        matched_pdf = low.endswith(".pdf")
        # Prefer the PDF copy (2026-11-20): every manual exists as
        # PDF/EPUB/MD, and browsers cannot display EPUBs — clicking
        # one downloads it and nothing opens. When the matched file
        # is not a PDF, try a same-directory sibling first, then the
        # format-dir twin (FieldManuals keeps PDF/EPUB/MD as sibling
        # top-level dirs with identical relative paths), and link
        # the PDF so it opens in the browser's viewer.
        if not low.endswith(".pdf"):
            pdf_path = os.path.splitext(path)[0] + ".pdf"
            if not os.path.isfile(pdf_path):
                pdf_path = self._pdf_twin(path) or ""
            if pdf_path and os.path.isfile(pdf_path):
                path = pdf_path
                low = path.lower()
        if any(m in low for m in self.medical_markers):
            badge = "Medical"
        elif any(m in low for m in self.manual_markers):
            badge = "Manual"
        else:
            badge = "Reference"
        # Hub-relative URL (audit 2026-11-20): a file:// link is dead
        # in the browser (blocked from http:// pages), so serve the
        # file through the hub's /files/ route. Falls back to a
        # file:// URL rebuilt from the (possibly PDF-swapped) path.
        hub_url = self._hub_url(path)
        if hub_url:
            final_url = hub_url
        else:
            p = path.replace(os.sep, "/")
            final_url = "file://" + p if p.startswith("/") else "file:///" + p
        # Snippet fallback must not leak the absolute path to remote
        # clients (audit 2026-11-20, 3b): use the file name only.
        snippet = snippet or f"Document: {os.path.basename(path)}"
        result = {
            "title": title or url,
            "snippet": snippet,
            "url": final_url,
            "badge": badge,
            "detail": path,
            "demo": False,
            "_matched_path": matched_path,
            # Tech M3: the final served PDF path, after the PDF-twin
            # resolution above — the hub compares citations against
            # this, not _matched_path, so EPUB/MD matches with a PDF
            # twin can still verify.
            "_served_path": path,
        }
        if hub_url and low.endswith(".pdf"):
            if matched_pdf and page is not None and page > 0:
                result["page"] = page
            result["pdf_url"] = hub_url + (f"#page={result['page']}" if "page" in result else "")
        if label:
            result["date_label"] = label[1]
        return result

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
    def search(self, query, *, shared=False):
        # Recoll is a shareable kind: shared searches run normally and
        # the hub filters results to shared roots (shared_manual), so
        # shared=True changes nothing here. The flag exists for the
        # hub's shared= pass-through contract.
        manual = MANUAL_ID.fullmatch(query.strip())
        if manual:
            # The query syntax contains only a validated publication ID.
            try:
                matched = self._live_search(f'filename:"{manual[1]} {manual[2]}"', literal=False)
            except (OSError, subprocess.SubprocessError, ValueError):
                matched = []
            exact = []
            for result in matched:
                filename = os.path.basename(result.get("_matched_path", ""))
                identifier = MANUAL_ID.match(filename)
                if (identifier and tuple(part.casefold() for part in identifier.groups())
                        == tuple(part.casefold() for part in manual.groups())):
                    exact.append({**result, "exact": True})
            if exact:
                return _dedupe_results(exact)
        try:
            started = time.monotonic()
            results = _dedupe_results(self._live_search(query))
            variants = expand_query(query)
            remaining = min(2.0, self.timeout - (time.monotonic() - started))
            if not manual and len(variants) > 1 and remaining > 0:
                expansion = variants[1]
                terms = set(search_words(expansion))
                if not any(terms <= set(search_words(row["title"] + " " + row["snippet"]))
                           for row in results[:3]):
                    try:
                        extra = self._live_search(expansion, timeout=remaining)
                    except (OSError, subprocess.SubprocessError, ValueError):
                        extra = []
                    seen = {row["url"] for row in results}
                    additions = []
                    for row in extra:
                        if row["url"] not in seen:
                            additions.append(row)
                            seen.add(row["url"])
                    results = (results[:1] + additions[:2] + results[1:] + additions[2:])[:self.max_results]
            if results:
                return results
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        if self.demo_fallback():
            return self._demo_search(query)
        return []

    def _index_dir(self):
        """Directory holding the Recoll index (xapiandb lives under it).

        recoll.conf 'dbdir' wins when set; otherwise the config dir
        itself (Recoll's default). "" when no config dir is configured
        — the probe query below is the only check then.
        """
        if not self.config_dir:
            return ""
        conf = os.path.join(self.config_dir, "recoll.conf")
        try:
            with open(conf, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("dbdir"):
                        _, _, rest = line.partition("=")
                        dbdir = rest.strip().strip('"').strip("'")
                        if dbdir:
                            return dbdir.replace("/", os.sep)
                        break
        except OSError:
            pass
        return self.config_dir

    @staticmethod
    def _nonempty_dir(path):
        """True when path is a directory containing at least one entry."""
        try:
            with os.scandir(path) as entries:
                return any(True for _ in entries)
        except OSError:
            return False

    def _probe_status(self):
        """Content check (Rel H3 / known F20): (available, detail).

        The binary existing is not enough — the index must exist and
        be non-empty. Probes, in order: (1) a non-empty xapiandb under
        the configured index dir; (2) a cheap one-result probe query
        ("manual" — the library is field manuals). A present binary
        with a missing/empty index reports available False.
        """
        if shutil.which(self.query_bin) is None:
            if self.demo_fallback():
                return False, "Recoll not installed — showing sample data"
            return False, "recollq / recoll not found"
        index_dir = self._index_dir()
        if index_dir and self._nonempty_dir(os.path.join(index_dir, "xapiandb")):
            return True, "recollq present; index found"
        try:
            probe = self._live_search("manual", timeout=5)
        except (OSError, subprocess.SubprocessError, ValueError):
            probe = []
        if probe:
            return True, "recollq present; index responding"
        if self.demo_fallback():
            return False, "Recoll index missing or empty — showing sample data"
        return False, "Recoll index missing or empty"

    def status(self, *, shared=False):
        # Shared clients get no detail (the hub blanks it anyway);
        # availability is the same content-checked probe either way.
        available, detail = self._probe_status()
        return {"available": available, "detail": "" if shared else detail}
