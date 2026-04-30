"""
geo_register_pluggin - A LichtFeld Studio plugin.
"""

import lichtfeld as lf
from .panels.main_panel import MainPanel, _geo_draw_handler
from .operators.geo_picker import GEO_OT_pick_location

_classes = [MainPanel, GEO_OT_pick_location]


def on_load():
    """Called when plugin is loaded."""
    for cls in _classes:
        lf.register_class(cls)

    # Self-register for startup so the user doesn't have to reload manually each session.
    try:
        from lfs_plugins.settings import SettingsManager
        prefs = SettingsManager.instance().get("geo_register_pluggin")
        if not prefs.get("load_on_startup", False):
            prefs.set("load_on_startup", True)
            lf.log.info("geo_register_pluggin: enabled load on startup")
    except Exception as exc:
        lf.log.warn(f"geo_register_pluggin: could not set load_on_startup: {exc}")

    lf.add_draw_handler("geo_register_overlay", _geo_draw_handler, "POST_VIEW")
    lf.log.info("geo_register_pluggin plugin loaded")


def on_unload():
    """Called when plugin is unloaded."""
    lf.remove_draw_handler("geo_register_overlay")
    for cls in reversed(_classes):
        lf.unregister_class(cls)
    lf.log.info("geo_register_pluggin plugin unloaded")
