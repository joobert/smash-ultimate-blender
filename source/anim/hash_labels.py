"""Resolve hash40 integers to readable names using the bundled ParamLabels.csv.

No Blender dependency, so it can be tested alongside motion_list.py.
"""
from pathlib import Path
import zlib

_CACHE = None


def csv_path():
    return Path(__file__).resolve().parents[2] / 'dependencies' / 'pyprc' / 'ParamLabels.csv'


def _hash40(text):
    data = text.encode('utf-8')
    return (len(data) << 32) | zlib.crc32(data)


def labels():
    """Hash-to-name map, parsed once. A missing file yields an empty map."""
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        try:
            text = csv_path().read_text(encoding='utf-8-sig')
        except OSError:
            return _CACHE
        for line in text.splitlines():
            key, separator, name = line.partition(',')
            if not separator or not key.startswith('0x'):
                continue
            try:
                _CACHE[int(key, 16)] = name.strip()
            except ValueError:
                continue
    return _CACHE


def label(value):
    """Readable name for a hash40 integer; strings pass through unchanged."""
    if isinstance(value, str):
        return value
    return labels().get(value, '0x%x' % value)


def harvest(doc):
    """Record names a YAML document spells out, which the CSV may not list."""
    known = labels()
    for key, motion in (doc.get('list') or {}).items():
        for name in (key, *(a.get('name') for a in motion.get('animations') or [])):
            if isinstance(name, str):
                known[_hash40(name)] = name
