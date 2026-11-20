"""My Files plug-in — plain-text search over the owner's own documents.

Walks a folder (cfg "folder", default fixtures/myfiles) and searches
readable text files (.txt, .md, .html) for the query tokens. For each
file with a hit it returns the file name, the first matching line as the
snippet, and the file path. PDFs and other binary files are matched by
file name only — Recoll is the tool that indexes their contents, so this
plug-in stays deliberately dumb and fast.

This is a real implementation, not a stub: point "folder" at the real
docs folder (family emergency plan, own notes, CHIRP exports, etc.) in
config.json and it just works. Binary-safe: files that fail to decode
as text are skipped instead of crashing the search.
"""

import os
import urllib.parse

from plugins.base import PluginBase

TEXT_EXTS = {".txt", ".md", ".markdown", ".html", ".htm", ".csv"}

# Files over this size are skipped: reading a huge log on battery for
# one query is not worth it; Recoll indexes big files instead.
MAX_FILE_BYTES = 5 * 1024 * 1024


def _quote_rel(rel):
    """Relative path -> URL-safe path (forward slashes, quoted)."""
    return urllib.parse.quote(rel.replace(os.sep, "/"))


class Plugin(PluginBase):
    id = "myfiles"
    name = "My Files"
    badge = "My Files"
    kind = "myfiles"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.folder = self.data_path("folder", "fixtures/myfiles")

    def search(self, query):
        tokens = [t for t in query.lower().split() if t]
        if not tokens or not os.path.isdir(self.folder):
            return []
        out = []
        for root, dirs, files in os.walk(self.folder):
            dirs.sort()  # deterministic order, not filesystem order
            for fname in sorted(files):
                if len(out) >= self.max_results:
                    return out
                path = os.path.join(root, fname)
                if not os.path.isfile(path):
                    # Skip FIFOs, sockets, devices: opening one would
                    # block the search (a FIFO waits for a writer).
                    # A broken symlink also lands here — it has no
                    # content to search.
                    continue
                rel = os.path.relpath(path, self.folder)
                ext = os.path.splitext(fname)[1].lower()
                if ext in TEXT_EXTS:
                    try:
                        if os.path.getsize(path) > MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                    snippet = self._search_text(path, tokens)
                    if snippet is None:
                        continue
                else:
                    # Binary / PDF: file-name match only.
                    if not all(t in fname.lower() for t in tokens):
                        continue
                    snippet = f"File name matches — contents indexed by Recoll."
                out.append(
                    {
                        "title": fname,
                        "snippet": snippet,
                        "url": "/files/myfiles/" + _quote_rel(rel),
                        "badge": "My Files",
                        "detail": rel,
                        "demo": False,
                    }
                )
        return out

    @staticmethod
    def _search_text(path, tokens):
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                for line in fh:
                    low = line.lower()
                    if all(t in low for t in tokens):
                        return line.strip()[:300]
        except (OSError, UnicodeDecodeError):
            return None
        return None

    def status(self):
        if os.path.isdir(self.folder):
            count = sum(len(files) for _, _, files in os.walk(self.folder))
            return {"available": True, "detail": f"{count} files in {self.folder}"}
        return {"available": False, "detail": f"folder not found: {self.folder}"}
