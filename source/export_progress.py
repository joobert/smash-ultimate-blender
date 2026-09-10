"""Cursor progress shared by synchronous and modal exporters."""
from functools import wraps
import bpy


class ExportProgress:
    depth = 0

    def __init__(self, context):
        self.context = context

    def __enter__(self):
        if not self.depth:
            self.context.window_manager.progress_begin(0, 100)
        type(self).depth += 1
        return self

    def __exit__(self, *exc):
        type(self).depth -= 1
        if not self.depth:
            self.context.window_manager.progress_end()


def export_progress(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with ExportProgress(bpy.context):
            result = function(*args, **kwargs)
            bpy.context.window_manager.progress_update(100)
            return result
    return wrapped
