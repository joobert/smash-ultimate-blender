"""Lossless motion-list editing; binary layout follows ultimate-research/motion_lib.

Hashes remain integers in binary documents. YAML labels and unedited fields are
preserved. No Blender dependency, so codecs and matching can be tested separately.
"""
import copy
import os
from pathlib import Path
import struct
import tempfile
import zlib

FLAGS = ('turn', 'loop', 'move', 'fix_trans', 'fix_rot', 'fix_scale',
         'unk_40', 'unk_80', 'unk_100', 'unk_200', 'unk_400', 'unk_800',
         'unk_1000', 'unk_2000')


def hash40(value):
    if isinstance(value, int):
        return value
    if value.startswith('0x'):
        return int(value, 16)
    data = value.encode('utf-8')
    return (len(data) << 32) | zlib.crc32(data)


def decode_binary(data):
    offset = 0

    def read(fmt):
        nonlocal offset
        size = struct.calcsize('<' + fmt)
        if offset + size > len(data):
            raise ValueError('Truncated motion list')
        values = struct.unpack_from('<' + fmt, data, offset)
        offset += size
        return values[0] if len(values) == 1 else values

    if read('Q') != hash40('motion'):
        raise ValueError('Invalid motion_list.bin header')
    doc = {'motion_path': read('Q'), 'list': {}}
    count = read('Q')
    if count > len(data) // 24:
        raise ValueError('Invalid motion count')
    for _ in range(count):
        key, script = read('Q'), read('Q')
        bits, blend, count, size = read('HBBI')
        if count > 3 or size % 8 not in (0, 4):
            raise ValueError('Invalid motion record size')
        names = [read('Q') for _ in range(count)]
        animations = [{'name': name, 'unk': read('B')} for name in names]
        offset = (offset + 3) & ~3
        scripts = [read('Q') for _ in range(size // 8)]
        extra = None
        if size % 8:
            xlu_start, xlu_end, cancel, no_stop = read('BBBB')
            extra = dict(xlu_start=xlu_start, xlu_end=xlu_end,
                         cancel_frame=cancel, no_stop_intp=bool(no_stop))
        if key in doc['list']:
            raise ValueError('Duplicate motion key')
        doc['list'][key] = dict(game_script=script,
            flags={name: bool(bits & (1 << i)) for i, name in enumerate(FLAGS)},
            blend_frames=blend, animations=animations, scripts=scripts, extra=extra,
            _reserved_flags=bits & 0xc000)
    if offset != len(data):
        raise ValueError('Unexpected trailing motion-list data')
    return doc


def encode_binary(doc):
    data = bytearray()

    def write(fmt, *values):
        data.extend(struct.pack('<' + fmt, *values))

    write('QQQ', hash40('motion'), hash40(doc['motion_path']), len(doc['list']))
    seen = set()
    for key, motion in doc['list'].items():
        hashed = hash40(key)
        if hashed in seen:
            raise ValueError('Duplicate motion Hash40')
        seen.add(hashed)
        animations, scripts = motion['animations'], motion['scripts']
        extra = motion.get('extra')
        if len(animations) > 3:
            raise ValueError('Animation count cannot exceed 3')
        bits = motion.get('_reserved_flags', 0)
        if any(not isinstance(value, bool) for value in motion['flags'].values()):
            raise ValueError('Motion flags must be true or false')
        bits |= sum(1 << i for i, name in enumerate(FLAGS) if motion['flags'].get(name, False))
        write('QQHBBI', hash40(key), hash40(motion['game_script']), bits,
              motion['blend_frames'], len(animations), len(scripts) * 8 + (4 if extra is not None else 0))
        for animation in animations:
            write('Q', hash40(animation['name']))
        for animation in animations:
            write('B', animation['unk'])
        data.extend(b'\0' * (-len(data) % 4))
        for script in scripts:
            write('Q', hash40(script))
        if extra is not None:
            if not isinstance(extra['no_stop_intp'], bool):
                raise ValueError('no_stop_intp must be true or false')
            write('BBBB', extra['xlu_start'], extra['xlu_end'], extra['cancel_frame'], int(extra['no_stop_intp']))
    return bytes(data)


# YAML is preferred so an edit lands in the readable file when both exist.
DISCOVERY_ORDER = ('.yml', '.yaml', '.bin')


def discover(animation_path):
    """The one list to read and write, from the nearest directory up to /motion."""
    folder = Path(animation_path).resolve().parent
    for directory in (folder, *folder.parents):
        for suffix in DISCOVERY_ORDER:
            path = directory / ('motion_list' + suffix)
            if path.is_file():
                return [path]
        if directory.name.lower() == 'motion':
            break
    return []


def load(path):
    path = Path(path)
    if path.suffix.lower() == '.bin':
        return decode_binary(path.read_bytes())
    if path.suffix.lower() not in ('.yml', '.yaml'):
        raise ValueError('Choose a .bin, .yml, or .yaml motion list')
    from ...dependencies import yaml
    class UniqueKeyLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            mapping = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise ValueError(f'Duplicate YAML key: {key}')
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    doc = yaml.load(path.read_text(encoding='utf-8-sig'), Loader=UniqueKeyLoader)
    if not isinstance(doc, dict) or not isinstance(doc.get('list'), dict) or 'motion_path' not in doc:
        raise ValueError('Expected a motion_path and list mapping')
    # Validate the complete schema and byte ranges before offering edits.
    encode_binary(doc)
    from . import hash_labels
    hash_labels.harvest(doc)
    return doc


def matching_keys(doc, animation_name):
    wanted = hash40(Path(animation_name).name.lower())
    return [key for key, motion in doc['list'].items()
            if any(hash40(a['name']) == wanted for a in motion['animations'])]


def update(doc, animation_name, *, cancel=None, blend=None, flags=None,
           key='', template=''):
    """Return an edited copy. Creation requires an explicit key and template."""
    result = copy.deepcopy(doc)
    matches = matching_keys(result, animation_name)
    if key:
        matches = [k for k in matches if hash40(k) == hash40(key)]
    if not matches:
        if not key or not template:
            raise ValueError(f'No entry for {animation_name}; supply a new motion key and template')
        if any(hash40(k) == hash40(key) for k in result['list']):
            raise ValueError('Motion key already exists but references a different animation')
        templates = [m for k, m in result['list'].items() if hash40(k) == hash40(template)]
        if len(templates) != 1:
            raise ValueError('Template motion was not found')
        motion = copy.deepcopy(templates[0])
        motion['animations'] = [{'name': Path(animation_name).name.lower(), 'unk': 0}]
        result['list'][key] = motion
        matches = [key]
    if len(matches) != 1:
        raise ValueError('Animation is shared by multiple motions; specify the motion key')
    motion = result['list'][matches[0]]
    for value in (cancel, blend):
        if value is not None and (int(value) != value or not 0 <= value <= 255):
            raise ValueError('Cancel and blend frames must be whole numbers from 0 to 255')
    if cancel is not None:
        if motion.get('extra') is None:
            raise ValueError('This entry has no fighter extra data; cannot set a cancel frame')
        motion['extra']['cancel_frame'] = int(cancel)
    if blend is not None:
        motion['blend_frames'] = int(blend)
    for name, value in (flags or {}).items():
        if name not in FLAGS or not isinstance(value, bool):
            raise ValueError(f'Invalid motion flag: {name}')
        motion['flags'][name] = value
    encode_binary(result)
    return result


def labeled(doc):
    """Copy with resolvable hashes replaced by names; re-encodes identically."""
    from . import hash_labels
    result = dict(doc)
    result['motion_path'] = hash_labels.label(doc['motion_path'])
    result['list'] = {}
    for key, motion in doc['list'].items():
        entry = copy.deepcopy(motion)
        entry['game_script'] = hash_labels.label(entry['game_script'])
        entry['scripts'] = [hash_labels.label(script) for script in entry['scripts']]
        entry['animations'] = [dict(animation, name=hash_labels.label(animation['name']))
                               for animation in entry['animations']]
        result['list'][hash_labels.label(key)] = entry
    if encode_binary(result) != encode_binary(doc):
        raise ValueError('Label substitution changed the motion list; refusing to write')
    return result


def save(path, doc, *, expected=None):
    """Atomic replacement with a one-time original backup and conflict check."""
    path = Path(path)
    encode_binary(doc)
    if path.suffix.lower() == '.bin':
        data = encode_binary(doc)
    else:
        from ...dependencies import yaml
        data = yaml.safe_dump(labeled(doc), sort_keys=False, allow_unicode=True).encode('utf-8')
    original = path.read_bytes()
    if expected is not None and original != expected:
        raise ValueError('Motion list changed on disk; load it again before exporting')
    backup = path.with_name(path.name + '.bak')
    try:
        with backup.open('xb') as stream:
            stream.write(original)
    except FileExistsError:
        pass
    fd, temporary = tempfile.mkstemp(prefix='.motion_list_', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_all(paths):
    """Decode the first path; remember every path's bytes for conflict checks."""
    paths = [Path(path) for path in paths]
    if not paths:
        raise ValueError('No motion_list.yml/.yaml/.bin found beside the animation or up to /motion')
    originals = {path: path.read_bytes() for path in paths}
    return load(paths[0]), paths[0], originals


def save_all(originals, doc):
    """Write every file from one document, or none of them."""
    for path, expected in originals.items():
        if Path(path).read_bytes() != expected:
            raise ValueError(f'{Path(path).name} changed on disk; load it again before exporting')
    for path, expected in originals.items():
        save(path, doc, expected=expected)
