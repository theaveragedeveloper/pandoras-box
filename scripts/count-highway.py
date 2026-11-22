"""Count drivable ways by highway class (and their node refs) in a PBF.

One quick pass, to size the routing-index composition (how much of the
graph is service/track/driveway vs through-roads).
"""
import collections
import sys
import time

import osmium

SPEEDS_KMH = {
    "motorway": 105, "motorway_link": 60,
    "trunk": 90, "trunk_link": 60,
    "primary": 80, "primary_link": 50,
    "secondary": 70, "secondary_link": 50,
    "tertiary": 60, "tertiary_link": 40,
    "unclassified": 50, "residential": 40,
    "living_street": 20, "service": 30,
    "road": 40, "track": 20,
}


class CountHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways = collections.Counter()
        self.refs = collections.Counter()
        self.started = time.time()

    def way(self, w):
        highway = w.tags.get("highway")
        if highway not in SPEEDS_KMH:
            return
        if w.tags.get("access") in ("no", "private"):
            return
        if w.tags.get("motor_vehicle") == "no":
            return
        if (w.tags.get("area") or "").strip().lower() == "yes":
            return
        n = len(w.nodes)
        self.ways[highway] += 1
        self.refs[highway] += max(0, n - 1)


handler = CountHandler()
handler.apply_file(sys.argv[1], locations=False)
total_ways = sum(handler.ways.values())
total_refs = sum(handler.refs.values())
print(f"drivable ways: {total_ways:,}, segments: {total_refs:,}")
for cls in sorted(handler.ways, key=lambda c: -handler.ways[c]):
    print(f"  {cls:15s} ways {handler.ways[cls]:>10,} "
          f"({100.0 * handler.ways[cls] / total_ways:5.1f}%)  "
          f"segments {handler.refs[cls]:>12,} "
          f"({100.0 * handler.refs[cls] / total_refs:5.1f}%)")
print(f"({time.time() - handler.started:.0f}s)")
