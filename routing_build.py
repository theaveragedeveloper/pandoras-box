"""Shared helpers for the optional OSM routing-index builders."""

from collections import defaultdict
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import osmium

from sqlite_files import publish_index


def build_routing_file(builder, output, resume=False):
    """Build under the destination directory and publish only a complete graph."""
    target = Path(output).absolute()
    prefix = f".{target.name}.building-"
    with TemporaryDirectory(prefix=prefix, dir=target.parent) as folder:
        staging = Path(folder, target.name)
        if resume:
            candidates = sorted(entry / target.name for entry in target.parent.iterdir()
                                if entry.name.startswith(prefix) and (entry / target.name).is_file())
            if len(candidates) > 1:
                raise ValueError("More than one interrupted routing build was found.")
            previous = candidates[0] if candidates else target
            if not previous.is_file():
                raise ValueError("No interrupted routing build was found.")
            shutil.copyfile(previous, staging)
        counts = builder(str(staging))
        if not counts["nodes"] or not counts["edges"]:
            raise ValueError("Routing index has no usable roads.")
        publish_index(staging, target)
        return counts


class TurnRestrictions(osmium.SimpleHandler):
    """Resolve OSM via-node car restrictions to directed graph transitions."""

    def __init__(self, inputs):
        super().__init__()
        self.rules = set()
        self.vias = defaultdict(set)
        self.arrivals = defaultdict(set)
        self.departures = defaultdict(set)
        for path in inputs:
            self.apply_file(path, locations=False)

    def relation(self, relation):
        tags = relation.tags
        if tags.get("type") != "restriction":
            return
        specific = tags.get("restriction:motorcar") or tags.get("restriction:motor_vehicle")
        restriction = specific or tags.get("restriction", "")
        exceptions = (item.strip() for item in tags.get("except", "").split(";"))
        if not specific and any(item in {"vehicle", "motor_vehicle", "motorcar"} for item in exceptions):
            return
        if not restriction.startswith(("no_", "only_")):
            return
        vias = [member for member in relation.members if member.role == "via"]
        if len(vias) != 1 or vias[0].type != "n":
            return
        via = vias[0].ref
        incoming = [m.ref for m in relation.members if m.type == "w" and m.role == "from"]
        outgoing = [m.ref for m in relation.members if m.type == "w" and m.role == "to"]
        for first in incoming:
            for last in outgoing:
                self.rules.add((first, via, last, restriction))
                self.vias[first].add(via)
                self.vias[last].add(via)

    def record(self, way_id, start, end):
        via_nodes = self.vias.get(way_id)
        if via_nodes:
            if end in via_nodes:
                self.arrivals[way_id, end].add(start)
            if start in via_nodes:
                self.departures[way_id, start].add(end)

    def write(self, conn):
        conn.execute(
            "CREATE TABLE turn_restrictions (incoming INTEGER NOT NULL, "
            "via INTEGER NOT NULL, outgoing INTEGER, kind TEXT NOT NULL)")
        rows = set()
        for first, via, last, restriction in self.rules:
            only = restriction.startswith("only_")
            for incoming in self.arrivals[first, via]:
                destinations = self.departures[last, via]
                if first == last:
                    # Both directions can belong to the same OSM way.
                    u_turn = restriction.endswith("u_turn")
                    destinations = {node for node in destinations if (node == incoming) == u_turn}
                if not destinations and only:
                    # A permitted exit may have been clipped out of the extract.
                    destinations = {None}
                for outgoing in destinations:
                    rows.add((incoming, via, outgoing, "only" if only else "no"))
        conn.executemany("INSERT INTO turn_restrictions VALUES (?,?,?,?)", rows)
        conn.execute("INSERT INTO meta VALUES ('turn_restrictions', 'via-node')")
        conn.commit()
