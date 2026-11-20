"""Base class for Search Hub plug-ins.

A plug-in is one searchable source. Each plug-in module in this package
defines a class named ``Plugin`` that subclasses :class:`PluginBase`.

Contract:
- ``__init__(self, cfg, ctx)`` — cfg is the source entry from config.json,
  ctx is a small dict of hub-wide helpers (base_dir, demo_fallback, ...).
- ``search(self, query)`` -> list of result dicts. Each result dict has:
    title   (str)  — headline shown in the UI
    snippet (str)  — one or two lines shown under the title
    url     (str)  — where the result opens (kiwix-serve URL, file path,
                     map viewer link, ...). May be "" if there is none.
    badge   (str)  — one of Medical / Manual / Reference / My Files /
                     Place / Radio. Defaults to the plug-in's own badge.
    detail  (str)  — optional small line, e.g. the ZIM name or file path.
- ``status(self)`` -> dict with at least ``available`` (bool) and
  ``detail`` (str, e.g. "kiwix-serve not reachable" or "3,214 places").

Rules for plug-in authors:
- Never raise out of ``search()`` for a backend problem. Catch the error
  and return an empty list; report the problem in ``status()``. The hub
  shows an "unavailable" group instead of failing the whole search.
- Never do network I/O outside the local machine. Everything here is
  offline by design.
- Keep it fast and light: the hub runs on battery. Cap results
  (``max_results`` in cfg, default 10) and set short timeouts.
"""

import os


class PluginBase:
    """One searchable source. Subclass this; set id / name / badge."""

    id = "base"
    name = "Base"
    badge = "Reference"
    kind = "base"

    def __init__(self, cfg, ctx):
        self.cfg = cfg or {}
        self.ctx = ctx or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        try:
            self.max_results = int(self.cfg.get("max_results", 10))
        except (TypeError, ValueError):
            self.max_results = 10
        # A bad max_results must never empty a source out or slice
        # weirdly (a negative value makes result[:-n] drop the tail):
        # clamp to a sane range.
        self.max_results = max(1, min(100, self.max_results))

    # -- helpers ---------------------------------------------------------
    def data_path(self, key, default=""):
        """Resolve a path from cfg, relative to the hub base dir."""
        p = self.cfg.get(key, default)
        if not p:
            return ""
        if os.path.isabs(p):
            return p
        return os.path.join(self.ctx.get("base_dir", ""), p)

    def demo_fallback(self):
        return bool(self.ctx.get("demo_fallback", True))

    # -- contract --------------------------------------------------------
    def search(self, query):
        """Return a list of result dicts for query (may be empty)."""
        return []

    def status(self):
        """Return {"available": bool, "detail": str} for the sources rail."""
        return {"available": True, "detail": ""}

    def describe(self):
        return {
            "id": self.id,
            "name": self.cfg.get("name", self.name),
            "badge": self.cfg.get("badge", self.badge),
            "kind": self.kind,
            "enabled": self.enabled,
        }
