#!/usr/bin/env python3
"""Minimal GTK3 XDND source: a button that drags file:// URIs, like dragon-drop."""
import sys
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk

uris = ['file://' + p for p in sys.argv[1:]] or ['file:///tmp/drop-me.txt']
window = Gtk.Window(title='drag-source')
window.move(0, 0)
button = Gtk.Button(label='drag me')
button.set_size_request(160, 80)
button.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.COPY)
button.drag_source_add_uri_targets()
button.connect('drag-data-get', lambda w, ctx, data, info, t: data.set_uris(uris))
button.connect('drag-end', lambda *a: print('drag-end', flush=True))
window.add(button)
window.connect('destroy', Gtk.main_quit)
window.show_all()
Gtk.main()
