#!/usr/bin/env python3
"""Open only the manager, check its controls and export its rendered window."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import league_linux as app
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gtk, Gsk

result = {"ok": False}


def capture(application, window):
    try:
        labels = []
        def visit(widget):
            if isinstance(widget, Gtk.Button):
                labels.append(widget.get_label())
            child = widget.get_first_child()
            while child:
                visit(child)
                child = child.get_next_sibling()
        visit(window)
        assert {"Start", "Stop", "Check setup", "Logs", "Build runtime", "Install Riot client"} <= set(labels), labels
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        node = snapshot.to_node()
        if node is not None:
            texture = window.get_renderer().render_texture(node, None)
            path = app.ROOT / "verification/launcher.png"
            path.parent.mkdir(exist_ok=True)
            assert texture.save_to_png(str(path))
            print("Rendered manager window:", path)
        else:
            raise RuntimeError("Window snapshot is empty")
        result["ok"] = True
    except Exception as e:
        print("GUI verification failed:", e, file=sys.stderr)
    finally:
        application.quit()
    return False


app.gui(app.Manager(), on_ready=capture)
raise SystemExit(0 if result["ok"] else 1)
