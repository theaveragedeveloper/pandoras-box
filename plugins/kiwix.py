"""Kiwix plug-in — searches ZIMs through a running kiwix-serve.

Expected backend (started separately, plugged in):
    kiwix-serve --port 8080 --library library.xml
    (or: kiwix-serve --port 8080 wikipedia_en_all_nopic.zim wikimed_en_all.zim ...)

The hub queries kiwix-serve's built-in search page:
    GET {base_url}/search?pattern=<query>

and parses the result links out of the returned HTML. It does not depend
on the exact page layout: any link whose href points at an article inside
a ZIM is treated as a result, and the text following it as the snippet.

Badge: results from a WikiMed / medical ZIM get "Medical", everything
else "Reference". Add ZIM names to cfg "medical_zims" to tune this.

If kiwix-serve is not reachable and demo_fallback is on, the plug-in
returns matching rows from fixtures/demo_kiwix.json, marked demo=True,
so the UI can be tried before the library exists.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from plugins.base import PluginBase


def reader_origin(url):
    """Validate a local reader URL and return its origin components."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Use a local Kiwix reader URL")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Use a local Kiwix reader URL")
    port = parsed.port
    if port == 0:
        raise ValueError("Use a valid Kiwix reader port")
    return parsed.scheme, parsed.hostname, port or (443 if parsed.scheme == "https" else 80)


def open_reader(request, *, timeout):
    origin = reader_origin(request.full_url)

    class ReaderRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if reader_origin(newurl) != origin:
                raise urllib.error.URLError("Kiwix redirected outside its configured origin")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), ReaderRedirect()
    ).open(request, timeout=timeout)

# Query expansion (query_expand.py): Kiwix matches words literally, so
# "how to give stitches" only finds sewing. Expanding the query with
# the right concept ("suture") pulls the real article in as well.
# Fail-open: if the module is missing, search just runs the original
# query exactly as before.
try:
    from query_expand import expand_query
except ImportError:  # noqa: BLE001 - expansion is optional
    def expand_query(query):
        return [query]

# Semantic re-ranker (rerank.py, Part 2): reorders merged results by
# MiniLM similarity to the query. Fail-open: if the model is missing
# or slow, rerank() returns the list unchanged.
try:
    from rerank import rerank as _rerank_results
except ImportError:  # noqa: BLE001 - re-ranking is optional
    def _rerank_results(query, results):
        return results


class _SearchPageParser(HTMLParser):
    """Collect (href, link_text, following_text) triples from a page."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._in_link = False
        self._in_any_link = False
        self._link_href = ""
        self._link_text = []
        self._after_link = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._in_any_link = True
            href = dict(attrs).get("href", "")
            # Article links look like /<zim>/<namespace>/<Title>.
            # Skip protocol-relative links (//cdn/...) — they are not
            # articles and would otherwise be collected as results.
            if href.startswith("/") and not href.startswith("//") and href.count("/") >= 2:
                self._in_link = True
                self._link_href = href
                self._link_text = []
                self._after_link = []

    def handle_endtag(self, tag):
        if tag != "a":
            return
        self._in_any_link = False
        if self._in_link:
            self._in_link = False
            text = "".join(self._link_text).strip()
            if text:
                self.results.append(
                    {
                        "href": self._link_href,
                        "title": text,
                        "snippet": "",
                        "_tail": self._after_link,
                    }
                )

    def handle_data(self, data):
        if self._in_link:
            self._link_text.append(data)
        elif not self._in_any_link and self.results and len(self.results[-1].get("_tail", [])) < 4:
            self.results[-1]["_tail"].append(data)


class Plugin(PluginBase):
    id = "kiwix"
    name = "Wikipedia & Reference"
    badge = "Reference"
    kind = "kiwix"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.base_url = cfg.get("base_url", "http://127.0.0.1:8080").rstrip("/")
        try:
            self.timeout = float(cfg.get("timeout_seconds", 5))
        except (TypeError, ValueError):
            self.timeout = 5
        # A zero/negative timeout makes urlopen fail immediately on
        # every search; clamp to at least 1 second.
        self.timeout = max(1.0, self.timeout)
        self.medical_zims = [z.lower() for z in cfg.get("medical_zims", ["wikimed"])]

    # -- live backend ----------------------------------------------------
    # Cap the search page (round 9): a runaway kiwix-serve (or a huge
    # ZIM result set) must not fill the laptop's RAM. 5 MB is far above
    # any real search page; over it, the search fails cleanly instead
    # of the hub ballooning.
    MAX_SEARCH_PAGE_BYTES = 5 * 1024 * 1024

    def _live_search(self, query):
        url = self.base_url + "/search?pattern=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "search-hub/1.0"})
        with open_reader(req, timeout=self.timeout) as resp:
            raw = resp.read(self.MAX_SEARCH_PAGE_BYTES + 1)
        if len(raw) > self.MAX_SEARCH_PAGE_BYTES:
            raise ValueError("kiwix search page too large")
        html = raw.decode("utf-8", errors="replace")
        parser = _SearchPageParser()
        parser.feed(html)
        out = []
        for r in parser.results[: self.max_results]:
            zim = r["href"].strip("/").split("/", 1)[0]
            snippet = " ".join(t.strip() for t in r.get("_tail", []) if t.strip())
            out.append(
                {
                    "title": r["title"],
                    "snippet": snippet or f"From {zim}.",
                    # Hub-relative URL: the hub proxies /kiwix/* to
                    # kiwix-serve, so the link works from any device
                    # (the phone on the hotspot included). A 127.0.0.1
                    # URL would point at the phone itself — that was
                    # audit flaw #3 (round 4).
                    "url": "/kiwix" + r["href"],
                    "badge": (
                        "Medical"
                        if any(m in zim.lower() for m in self.medical_zims)
                        else "Reference"
                    ),
                    "detail": zim,
                    "demo": False,
                }
            )
        return out

    # -- demo fallback ---------------------------------------------------
    def _demo_search(self, query):
        path = os.path.join(self.ctx.get("base_dir", ""), "fixtures", "demo_kiwix.json")
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            return []
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            # Empty query in, empty out: without this, all() over an
            # empty token list matches every demo row (round 9).
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
                        "badge": row.get("badge", "Reference"),
                        "detail": row.get("zim", "") + " (sample)",
                        "demo": True,
                    }
                )
        return out[: self.max_results]

    # -- contract --------------------------------------------------------
    def search(self, query, *, shared=False):
        # Kiwix is a shareable kind (public reference material); shared
        # searches run normally and the hub only routes them when the
        # source is shared. The flag exists for the hub's shared=
        # pass-through contract.
        # Query expansion (Part 1): run the original query plus each
        # expanded variant (e.g. "suture" for "how to give stitches").
        # Results are merged ROUND-ROBIN across variants, deduped by
        # URL, and capped at max_results. Round-robin matters: the
        # original query alone can fill all 8 slots with the wrong
        # sense of a word (sewing for "stitches"), which would leave
        # no room for the expansion's results. Interleaving guarantees
        # each variant's top hit is visible. A failed variant is
        # skipped; only if every variant fails does the demo fallback
        # (or an empty list) apply.
        variants = expand_query(query)
        per_variant = []
        last_exc = None
        for variant in variants:
            try:
                per_variant.append(self._live_search(variant))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
                last_exc = exc
                continue
        seen = set()
        out = []
        idx = 0
        while len(out) < self.max_results:
            added = False
            for res_list in per_variant:
                if idx < len(res_list):
                    r = res_list[idx]
                    if r["url"] not in seen:
                        seen.add(r["url"])
                        out.append(r)
                        added = True
                        if len(out) >= self.max_results:
                            break
            if not added:
                break
            idx += 1
        if out:
            # Semantic re-rank (Part 2): reorder by MiniLM similarity
            # to the query. Fail-open — rerank() returns the list
            # unchanged when the model is missing or too slow.
            try:
                out = _rerank_results(query, out)
            except Exception:  # noqa: BLE001 - re-rank never breaks search
                pass
            return out[: self.max_results]
        if last_exc is not None and self.demo_fallback():
            return self._demo_search(query)
        return []

    def _count_zims(self):
        """Number of ZIMs the reader serves, or None if unreachable.

        Tries the OPDS catalog first (/catalog/v2/entries, machine-
        readable totalResults), then counts the library links on the
        reader root page (one link per library). Returns 0 when the
        reader answers but serves no library.
        """
        try:
            req = urllib.request.Request(
                self.base_url + "/catalog/v2/entries?count=1",
                headers={"User-Agent": "search-hub/1.0"},
            )
            with open_reader(req, timeout=self.timeout) as resp:
                raw = resp.read(65536)
            text = raw.decode("utf-8", errors="replace")
            total = re.search(r"<(?:opensearch:)?totalResults[^>]*>(\d+)</", text)
            if total:
                return int(total.group(1))
            if "<entry" in text:
                return text.count("<entry")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            pass
        try:
            req = urllib.request.Request(
                self.base_url + "/", headers={"User-Agent": "search-hub/1.0"}
            )
            with open_reader(req, timeout=self.timeout) as resp:
                raw = resp.read(65536)
            html = raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        libs = set()
        for href in re.findall(r'href="(/[^"?#]*)"', html):
            first = href.strip("/").split("/", 1)[0]
            if first and first not in ("search", "catalog", "viewer", "skin",
                                       "static", "content", "favicon.ico",
                                       "robots.txt"):
                libs.add(first)
        return len(libs)

    def status(self, *, shared=False):
        """Content-checked status (Rel H3 / known F20): kiwix-serve
        answering is not enough — at least one ZIM must be loaded.

        Distinguishes "up, no ZIMs loaded" (available False) from "up
        with N ZIMs". The 5 s status cache lives in the hub. Shared
        clients get no detail (the hub blanks it anyway).
        """
        count = self._count_zims()
        if count is None:
            if self.demo_fallback():
                return {
                    "available": False,
                    "detail": "kiwix-serve not running — showing sample data",
                }
            return {"available": False, "detail": "kiwix-serve not reachable"}
        if count == 0:
            detail = "kiwix-serve up, no ZIMs loaded"
            if self.demo_fallback():
                detail += " — showing sample data"
            return {"available": False, "detail": "" if shared else detail}
        detail = f"kiwix-serve up with {count} ZIM(s)"
        return {"available": True, "detail": "" if shared else detail}
