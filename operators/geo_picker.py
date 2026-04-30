"""Modal operator for picking a scene location and converting it to LLA."""

import lichtfeld as lf
import lichtfeld.selection as sel
from lfs_plugins.types import Operator, Event

# Module-level callback — set by the panel before invoking
_pick_callback = None
_pick_cancelled = False


def set_pick_callback(callback) -> None:
    global _pick_callback, _pick_cancelled
    _pick_callback = callback
    _pick_cancelled = False


def clear_pick_callback() -> None:
    global _pick_callback, _pick_cancelled
    _pick_callback = None
    _pick_cancelled = True


def was_pick_cancelled() -> bool:
    global _pick_cancelled
    if _pick_cancelled:
        _pick_cancelled = False
        return True
    return False


class GEO_OT_pick_location(Operator):
    """Modal operator: left-click picks a world position, ESC/right-click cancels."""

    label       = "Pick Geo Location"
    description = "Click on the model to get its geographic coordinates"
    options     = {"BLOCKING"}

    def invoke(self, context, event: Event) -> set:
        return {"RUNNING_MODAL"}

    def modal(self, context, event: Event) -> set:
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            result = sel.pick_at_screen(event.mouse_region_x, event.mouse_region_y)
            if result is not None and _pick_callback is not None:
                _pick_callback(result.world_position)
            return {"RUNNING_MODAL"}

        if event.type in {"RIGHTMOUSE", "ESC"}:
            clear_pick_callback()
            return {"CANCELLED"}

        return {"RUNNING_MODAL"}

    def cancel(self, context) -> None:
        clear_pick_callback()
