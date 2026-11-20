"""Plug-in registry: loads plug-ins named in config.json.

Each entry in config.json ``"sources"`` looks like::

    {"id": "kiwix", "name": "Wikipedia & Reference", "plugin": "kiwix",
     "badge": "Reference", "enabled": true, "base_url": "...", ...}

``plugin`` is the module name under this package. The module must define
a class named ``Plugin`` subclassing :class:`plugins.base.PluginBase`.

Unknown plug-in modules are skipped (logged to stderr) rather than
crashing the hub — a bad source must never take the whole hub down.
"""

import importlib
import sys


def load_plugins(config, ctx):
    """Instantiate one plug-in per enabled source entry. Returns a list."""
    plugins = []
    for entry in config.get("sources", []):
        if not isinstance(entry, dict):
            print(f"[hub] skipping non-object source entry: {entry!r}",
                  file=sys.stderr)
            continue
        module_name = entry.get("plugin", "")
        if not isinstance(module_name, str) or not module_name.isidentifier():
            print(
                f"[hub] skipping source '{entry.get('id', '?')}': "
                f"bad plugin name {module_name!r}",
                file=sys.stderr,
            )
            continue
        try:
            module = importlib.import_module("plugins." + module_name)
        except Exception as exc:  # noqa: BLE001 - a bad source must not kill the hub
            print(
                f"[hub] skipping source '{entry.get('id', '?')}': "
                f"cannot load plugin '{module_name}': {exc}",
                file=sys.stderr,
            )
            continue
        try:
            plugin = module.Plugin(entry, ctx)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[hub] skipping source '{entry.get('id', '?')}': "
                f"plugin init failed: {exc}",
                file=sys.stderr,
            )
            continue
        plugin.source_id = entry.get("id", plugin.id)
        plugin.display_name = entry.get("name", plugin.name)
        plugin.display_badge = entry.get("badge", plugin.badge)
        plugins.append(plugin)
    return plugins
