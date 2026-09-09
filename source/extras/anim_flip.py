"""
Studio SB anim_flip convention, shared by Mirror Animation and Idle Pose Library.

The reference Maya .anim script does all of the following in Smash / Maya space:
  1. Negate translateZ
  2. Negate rotateX / rotateY (Smash quaternion X/Y)
  3. Leave translateX/Y, rotateZ, and scale unchanged
  4. Swap L/R bone-name suffixes, including numbered ones (FingerL11)

Idle Pose Library Mirrored is the reference result: flip real nuanmb TRS,
then apply with the Smash→Blender importer (apply_smash_node_to_bone).

Mirror Animation uses that same apply path. Imported actions keep a Smash
TRS cache so the values match Idle Pose. Without a cache it recovers only
animated bones via the inverse of that importer, never helper/IK bones.
"""

import json
import math
import os
import re

from mathutils import Matrix, Quaternion, Vector

from ..model.import_model import get_blender_transform

# Smash / nuanmb quaternion storage is (x, y, z, w).
_LR_SUFFIX = re.compile(r'^(.*)([LR])(\d*)$')
_POSE_BONE_PATH_DOUBLE = re.compile(r'^(pose\.bones\[")([^"]+)("\].*)$')
_POSE_BONE_PATH_SINGLE = re.compile(r"^(pose\.bones\[')([^']+)('\].*)$")
_IK_HELPER = re.compile(r'(HandIK|FootIK|ArmIK|KneeIK)[LR]?$', re.IGNORECASE)
SMASH_POSE_CACHE_KEY = "sub_smash_pose_cache"
SMASH_ANIM_SOURCE_KEY = "sub_anim_source_path"


def flip_smash_translation(translation):
    """Negate Maya / Smash translateZ."""
    flipped = list(translation)
    flipped[2] = -flipped[2]
    return flipped


def flip_smash_rotation_xyzw(rotation):
    """Negate Smash quaternion X and Y (equivalent to Maya rotateX / rotateY)."""
    flipped = list(rotation)
    flipped[0] = -flipped[0]
    flipped[1] = -flipped[1]
    return flipped


def _root_basis_matrices():
    y_up_to_z_up = Matrix.Rotation(math.radians(90), 4, 'X')
    x_major_to_y_major = Matrix.Rotation(math.radians(-90), 4, 'Z')
    return y_up_to_z_up, x_major_to_y_major


def _smash_matrix_from_trs(translation, rotation_xyzw, scale):
    tm = Matrix.Translation(Vector(translation))
    rotation = Quaternion((
        rotation_xyzw[3],
        rotation_xyzw[0],
        rotation_xyzw[1],
        rotation_xyzw[2],
    ))
    rm = rotation.to_matrix().to_4x4()
    sm = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))
    return tm @ rm @ sm


def _import_smash_from_blender_rel(blender_rel):
    """Inverse of get_blender_transform(raw).transposed() used by import / idle."""
    p = Matrix((
        (0.0, -1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))
    return p.inverted() @ blender_rel @ p


def smash_trs_from_pose_bone(bone):
    """Inverse of apply_smash_node_to_bone. Fallback when no import cache exists."""
    if bone.parent is None:
        y_up_to_z_up, x_major_to_y_major = _root_basis_matrices()
        raw = y_up_to_z_up.inverted() @ bone.matrix @ x_major_to_y_major.inverted()
    else:
        blender_rel = bone.parent.matrix.inverted() @ bone.matrix
        raw = _import_smash_from_blender_rel(blender_rel)
    translation, quat, scale = raw.decompose()
    return (
        [translation.x, translation.y, translation.z],
        [quat.x, quat.y, quat.z, quat.w],
        [scale.x, scale.y, scale.z],
    )


def store_smash_pose_cache(action, cache):
    if action is None:
        return
    action[SMASH_POSE_CACHE_KEY] = json.dumps(cache)


def rebuild_smash_pose_cache(action):
    """Rebuild the Smash TRS cache by re-reading the action's source .nuanmb."""
    source_path = action.get(SMASH_ANIM_SOURCE_KEY, "")
    if not source_path or not os.path.isfile(source_path):
        return None
    try:
        from ...dependencies import ssbh_data_py
        anim_data = ssbh_data_py.anim_data.read_anim(source_path)
    except Exception:
        return None

    transform_group = next(
        (group for group in anim_data.groups if group.group_type.name == 'Transform'),
        None,
    )
    if transform_group is None:
        return None

    try:
        start_frame = int(round(action.frame_range[0]))
    except (AttributeError, TypeError, ValueError):
        start_frame = 1

    cache = {}
    for node in transform_group.nodes:
        if not node.tracks:
            continue
        for index, value in enumerate(node.tracks[0].values):
            cache.setdefault(str(start_frame + index), {})[node.name] = {
                "translation": list(value.translation),
                "rotation": list(value.rotation),
                "scale": list(value.scale),
            }
    return cache or None


def load_smash_pose_cache(action):
    if action is None:
        return None
    if SMASH_POSE_CACHE_KEY not in action:
        # Older actions embedded this table; newly imported ones rebuild it from
        # the source file the first time a flip actually needs it.
        cache = rebuild_smash_pose_cache(action)
        if cache is not None:
            store_smash_pose_cache(action, cache)
        return cache
    try:
        cache = json.loads(action[SMASH_POSE_CACHE_KEY])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return cache if isinstance(cache, dict) else None


def smash_pose_data_from_cache(cache, frame):
    """Hold-last-value lookup so one-frame Smash tracks still apply later."""
    if not cache:
        return None
    pose_data = {}
    frame = int(round(frame))
    for frame_key in sorted(cache, key=lambda key: int(key)):
        pose_data.update(cache[frame_key])
        if int(frame_key) >= frame:
            break
    pose_data = {
        name: data
        for name, data in pose_data.items()
        if not is_untouchable_mirror_bone(name)
    }
    return pose_data or None


def smash_pose_data_from_armature(armature, bone_filter=None):
    pose_data = {}
    for bone in armature.pose.bones:
        if is_untouchable_mirror_bone(bone.name):
            continue
        if bone_filter is not None and bone.name not in bone_filter:
            continue
        translation, rotation, scale = smash_trs_from_pose_bone(bone)
        pose_data[bone.name] = {
            "translation": translation,
            "rotation": rotation,
            "scale": scale,
            "flags": {
                "override_translation": True,
                "override_rotation": True,
                "override_scale": True,
            },
        }
    return pose_data


def apply_smash_node_to_bone(bone, node_data):
    """Apply Smash-space TRS the same way animation import / idle poses do."""
    raw_matrix = _smash_matrix_from_trs(
        node_data["translation"],
        node_data["rotation"],
        node_data.get("scale", (1.0, 1.0, 1.0)),
    )

    if bone.parent is None:
        y_up_to_z_up, x_major_to_y_major = _root_basis_matrices()
        bone.matrix = y_up_to_z_up @ raw_matrix @ x_major_to_y_major
    else:
        bone.matrix = bone.parent.matrix @ get_blender_transform(raw_matrix).transposed()


def _hierarchy_order(armature):
    roots = [bone for bone in armature.pose.bones if bone.parent is None]
    ordered = []
    for root in roots:
        ordered.append(root)
        ordered.extend(root.children_recursive)
    return ordered


def apply_smash_pose_data(armature, pose_data, target_bones=None, skip_bones=None):
    skip_bones = skip_bones or set()
    applied = []
    for bone in _hierarchy_order(armature):
        if is_untouchable_mirror_bone(bone.name):
            continue
        if bone.name not in pose_data or bone.name in skip_bones:
            continue
        if target_bones is not None and bone.name not in target_bones:
            continue
        apply_smash_node_to_bone(bone, pose_data[bone.name])
        applied.append(bone)
    return applied


def keyframe_pose_bones(bones, frame):
    for bone in bones:
        bone.keyframe_insert(data_path="location", frame=frame, group=bone.name)
        if bone.rotation_mode == 'QUATERNION':
            bone.keyframe_insert(data_path="rotation_quaternion", frame=frame, group=bone.name)
        else:
            bone.keyframe_insert(data_path="rotation_euler", frame=frame, group=bone.name)
        bone.keyframe_insert(data_path="scale", frame=frame, group=bone.name)


def swap_lr_bone_name(name):
    """
    Swap a Smash Ultimate L/R suffix the way Studio SB anim_flip does.

    ArmL → ArmR, FingerL11 → FingerR11, ClavicleR → ClavicleL.
    Names without an uppercase L/R suffix (Hip, Trans, Null) are unchanged.
    """
    match = _LR_SUFFIX.match(name)
    if not match:
        return name
    prefix, side, digits = match.group(1), match.group(2), match.group(3)
    new_side = 'R' if side == 'L' else 'L'
    return f'{prefix}{new_side}{digits}'


def _split_pose_path(name):
    """Split pose.bones[\"ArmL\"] into (prefix, bone_name, suffix)."""
    match = _POSE_BONE_PATH_DOUBLE.match(name)
    if match:
        return match.group(1), match.group(2), match.group(3)
    match = _POSE_BONE_PATH_SINGLE.match(name)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return '', name, ''


def _difference(a, b):
    common_prefix = os.path.commonprefix((a, b))
    common_suffix = os.path.commonprefix((a[::-1], b[::-1]))[::-1]
    return a[len(common_prefix):len(a) - len(common_suffix)], b[len(common_prefix):len(b) - len(common_suffix)]


def _lower_tuple(words):
    return tuple(word.lower() for word in words)


def create_mirror_map(names, patterns=None):
    """
    Map each name to its L/R counterpart. Unpaired (center) names map to themselves
    so they still receive anim_flip channel negation.

    Accepts either raw bone names or fcurve prefixes like pose.bones[\"ArmL\"].
    """
    names = list(names)
    wrappers = {name: _split_pose_path(name) for name in names}
    unique_bones = {bone for _prefix, bone, _suffix in wrappers.values()}
    bone_map = {}

    # Pass 1: Studio SB suffix swap (R <-> L, R11 <-> L11)
    for bone in unique_bones:
        swapped = swap_lr_bone_name(bone)
        if swapped != bone and swapped in unique_bones:
            bone_map[bone] = swapped

    # Pass 2: leftover Left/Right word pairs
    if patterns is None:
        patterns = (
            ('l', 'r'),
            ('left', 'right'),
            ('L', 'R'),
            ('Left', 'Right'),
        )
    norm_patterns = tuple(_lower_tuple(_difference(*pattern)) for pattern in patterns)
    rpatterns = tuple(pattern[::-1] for pattern in norm_patterns)

    unmatched = [bone for bone in unique_bones if bone not in bone_map]
    for left_name in unmatched:
        for other in unique_bones:
            if left_name == other:
                continue
            if _lower_tuple(_difference(left_name, other)) in (*norm_patterns, *rpatterns):
                bone_map[left_name] = other
                break

    for bone in unique_bones:
        if bone not in bone_map:
            bone_map[bone] = bone

    mirror_map = {}
    for name in names:
        prefix, bone, suffix = wrappers[name]
        target = bone_map.get(bone, bone)
        mirror_map[name] = f'{prefix}{target}{suffix}'
    return mirror_map


def extract_bone_name_from_path(path):
    """Extract Hip from pose.bones[\"Hip\"] or a full data_path."""
    _prefix, bone, _suffix = _split_pose_path(path)
    if bone and bone != path:
        return bone
    if 'pose.bones[' in path:
        if '"' in path:
            return path.split('"')[1]
        if "'" in path:
            return path.split("'")[1]
    return ""


def is_untouchable_mirror_bone(bone_name):
    """Swing, helper, and addon IK bones are never mirrored."""
    if not bone_name:
        return False
    return (
        bone_name.startswith('S_')
        or bone_name.startswith('H_')
        or bool(_IK_HELPER.search(bone_name))
    )


# Vanilla fighter body / system bones shared by a normal Smash Ultimate armature.
_STANDARD_SMASH_BONES = frozenset({
    'Trans', 'Rot', 'Throw', 'Have',
    'Hip', 'HipN', 'Waist', 'Bust', 'Belly',
    'Neck', 'Head', 'Face',
    'ClavicleC', 'ClavicleL', 'ClavicleR',
    'ShoulderL', 'ShoulderR',
    'ArmL', 'ArmR', 'ElbowL', 'ElbowR',
    'WristL', 'WristR', 'HandL', 'HandR',
    'LegL', 'LegR', 'KneeL', 'KneeR',
    'FootL', 'FootR', 'ToeL', 'ToeR', 'HeelL', 'HeelR',
    'EyeL', 'EyeR',
})
_STANDARD_SMASH_PATTERN = re.compile(
    r'^(Finger[LR]\d+|'
    r'(Arm|Shoulder|Elbow|Wrist|Hand|Leg|Knee|Foot|Toe|Heel)[LR]\d*|'
    r'Clavicle[CLR]\d*)$'
)
_STANDARD_SMASH_SUFFIXES = ('_null', '_eff', '_offset')
_STANDARD_FACE_KEYWORDS = ('brow', 'lip', 'eye', 'nose', 'cheek', 'jaw', 'mouth', 'tooth', 'tongue')


def is_standard_smash_bone(bone_name):
    """True for a normal Smash fighter bone, including swing/helper/system extras."""
    if not bone_name:
        return False
    if is_untouchable_mirror_bone(bone_name):
        return True
    base = bone_name
    for suffix in _STANDARD_SMASH_SUFFIXES:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    if base in _STANDARD_SMASH_BONES or _STANDARD_SMASH_PATTERN.match(base):
        return True
    if base == 'Face' or base.startswith('Face'):
        return True
    lower = base.lower()
    return any(keyword in lower for keyword in _STANDARD_FACE_KEYWORDS)


def find_custom_mirror_bones(armature):
    """Bones on this armature that are not part of a normal Smash skeleton."""
    if armature is None or getattr(armature, 'type', None) != 'ARMATURE':
        return []
    return sorted(
        bone.name
        for bone in armature.pose.bones
        if not is_standard_smash_bone(bone.name)
    )


def collect_unchecked_custom_mirror_bones(armature, custom_items):
    """
    Custom bones to skip. Empty list means the user has not scanned yet,
    so extra bones keep the previous default (they still get mirrored).
    """
    if not custom_items:
        return set()
    included = {item.name for item in custom_items if getattr(item, 'include', False)}
    return {
        name
        for name in find_custom_mirror_bones(armature)
        if name not in included
    }


def should_exclude_bone_from_mirroring(bone_name, armature=None, include_fingers=True):
    """Optional UX filters. Hip / Trans / root are never excluded."""
    if not bone_name:
        return False

    if is_untouchable_mirror_bone(bone_name):
        return True

    bone_name_lower = bone_name.lower()
    if bone_name_lower in ('hip', 'hipn', 'trans', 'root', 'pelvis'):
        return False

    facial_keywords = ('brow', 'lip', 'eye', 'nose', 'cheek', 'jaw', 'mouth')
    if any(keyword in bone_name_lower for keyword in facial_keywords):
        return True

    if not include_fingers and bone_name.startswith('Finger'):
        return True

    if bone_name == 'Face':
        return True

    if armature is not None and getattr(armature, 'type', None) == 'ARMATURE':
        pose_bones = getattr(getattr(armature, 'pose', None), 'bones', None)
        if pose_bones is not None and bone_name in pose_bones:
            bone = pose_bones[bone_name]
            if bone.parent and bone.parent.name == 'Face':
                return True

    return False


def collect_excluded_bone_names(armature, include_fingers=True):
    if armature is None or getattr(armature, 'type', None) != 'ARMATURE':
        return set()
    return {
        bone.name
        for bone in armature.pose.bones
        if should_exclude_bone_from_mirroring(bone.name, armature, include_fingers)
    }


def mirror_smash_pose_data(pose_data, excluded_bones=None, source_bones=None, in_place=False):
    """
    Apply anim_flip to Smash-space pose dicts used by the idle pose library.

    Each value is a dict with translation [x,y,z], rotation [x,y,z,w],
    optional scale, and flags. Hip / Trans are flipped like every other bone.
    """
    excluded_bones = excluded_bones or set()
    mirrored = {}
    for bone_name, data in pose_data.items():
        if source_bones is not None and bone_name not in source_bones:
            continue
        if is_untouchable_mirror_bone(bone_name):
            continue
        if bone_name in excluded_bones:
            continue
        target_name = bone_name if in_place else swap_lr_bone_name(bone_name)
        flipped = dict(data)
        if "translation" in flipped:
            flipped["translation"] = flip_smash_translation(flipped["translation"])
        if "rotation" in flipped:
            flipped["rotation"] = flip_smash_rotation_xyzw(flipped["rotation"])
        mirrored[target_name] = flipped
    return mirrored


def mirror_evaluated_pose(
    armature,
    excluded_bones=None,
    target_bones=None,
    source_bones=None,
    pose_data=None,
    bone_filter=None,
    in_place=False,
):
    """
    Flip Smash TRS (cached nuanmb or recovered importer inverse), then apply
    with the same function Idle Pose Library Mirrored uses.
    """
    excluded_bones = excluded_bones or set()
    if pose_data is None:
        pose_data = smash_pose_data_from_armature(armature, bone_filter=bone_filter)
    pose_data = mirror_smash_pose_data(
        pose_data,
        excluded_bones=excluded_bones,
        source_bones=source_bones,
        in_place=in_place,
    )
    return apply_smash_pose_data(
        armature,
        pose_data,
        target_bones=target_bones,
        skip_bones=excluded_bones,
    )
