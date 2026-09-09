"""Filesystem discovery shared by importers."""
import os


def walk_import_folders(directory):
    """Follow directory symlinks/junctions, visiting each target only once."""
    visited = set()
    for root, dirs, files in os.walk(directory, followlinks=True):
        target = os.path.normcase(os.path.realpath(root))
        if target in visited:
            dirs.clear()
            continue
        visited.add(target)
        dirs.sort(key=str.casefold)
        yield root, dirs, files
