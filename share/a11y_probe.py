#!/usr/bin/env python3
"""Report one session's accessibility tree as JSON, for `niri-harness`.

This runs as a separate process, against the session's own D-Bus address, for
two reasons. It keeps PyGObject out of the harness proper, so a machine without
the accessibility stack simply falls back to OCR rather than failing to start;
and it can be given a hard deadline, because an application that is wedged can
leave a tree walk blocked on a reply that never comes.

Coordinates come back in the *window's* frame, never the screen's. That is not
a limitation of this code: a Wayland client is not told where it is on screen,
so the screen coordinates AT-SPI reports for one are all zero. The compositor
is the only thing that knows where the window sits, and the harness adds that
offset itself.
"""

from __future__ import annotations

import json
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# Roles a caller can plausibly aim a pointer at. A tree is mostly anonymous
# boxes; offering those as targets would bury the handful of real controls.
INTERESTING = {
    "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "menu", "combo box", "entry", "text",
    "password text", "link", "list item", "page tab", "tree item", "table cell",
    "label", "heading", "slider", "spin button", "toggle", "switch", "button",
}


def collect(node, out, window, frame, active, deadline, depth=0):
    if time.monotonic() > deadline or depth > 64:
        return
    try:
        role = node.get_role_name()
        name = (node.get_name() or "").strip()
        states = node.get_state_set()
        showing = states.contains(Atspi.StateType.SHOWING)
        # A node that is not showing is in a collapsed menu or a background
        # tab. It has geometry and a name, and clicking it would hit whatever
        # is actually drawn there instead.
        if showing and name and role in INTERESTING:
            component = node.get_component_iface()
            if component is not None:
                extents = component.get_extents(Atspi.CoordType.WINDOW)
                if extents.width > 0 and extents.height > 0:
                    out.append(
                        {
                            "text": name,
                            "role": role,
                            "x": extents.x,
                            "y": extents.y,
                            "width": extents.width,
                            "height": extents.height,
                            "window": window,
                            # Two windows of one application routinely share a
                            # title - two unsaved documents, say - so the title
                            # alone cannot tell their controls apart. This can.
                            "frame": frame,
                            # Which of several frames is the one the caller is
                            # acting on. The window manager's notion of focus
                            # lives on the frame, not on the widgets inside it:
                            # measured against GTK4, not one node in a live
                            # window reports FOCUSED, so asking them is asking
                            # the wrong thing.
                            "active": active,
                            # GTK marks a usable widget SENSITIVE and never sets
                            # ENABLED at all, so testing ENABLED alone reports
                            # every control in a healthy GTK window as disabled.
                            "enabled": (
                                states.contains(Atspi.StateType.SENSITIVE)
                                or states.contains(Atspi.StateType.ENABLED)
                            ),
                            "focused": states.contains(Atspi.StateType.FOCUSED),
                        }
                    )
        for index in range(node.get_child_count()):
            child = node.get_child_at_index(index)
            if child is not None:
                collect(child, out, window, frame, active, deadline, depth + 1)
    except Exception:
        # One unreachable node must not cost the rest of the tree; an
        # application can and does disappear mid-walk.
        return


def main() -> int:
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    deadline = time.monotonic() + budget
    Atspi.init()
    desktop = Atspi.get_desktop(0)
    out: list[dict] = []
    frames = 0
    count = desktop.get_child_count()
    for index in range(max(0, count)):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        for frame_index in range(app.get_child_count()):
            frame = app.get_child_at_index(frame_index)
            if frame is None:
                continue
            try:
                title = frame.get_name() or ""
                active = frame.get_state_set().contains(Atspi.StateType.ACTIVE)
            except Exception:
                continue
            frames += 1
            collect(frame, out, title, frames, active, deadline)
    json.dump({"applications": max(0, count), "nodes": out}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
