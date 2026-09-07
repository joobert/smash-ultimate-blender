"""Smash Export Doctor: one preflight pass over a model or animation export.

``core`` holds the result model, the check registry and the runner.
``checks`` holds every check and every fix.
``ui`` holds the Blender panel, operators and the preflight hook the exporters
call.
"""

from . import core
from . import checks
from . import ui

from .ui import preflight


def register():
    ui.register()


def unregister():
    ui.unregister()
