"""Finger sliders parented to the Smash hand bones.

Most controls are 1D sliders on the hand box. Curl is a 2D knob (Y = all
fingers together, X = progressive cascade Index↔Pinky). The thumb is a
square knob inside a square pad. Extra hands (HandL2, etc.) get the same
controls. Bones are named BL_* so they stay visible in the animation rig
and can be skipped on export.
"""

import math
import re

import bpy
from bpy.types import Operator
from mathutils import Quaternion, Vector

from ..blender_compat import (
    assign_bone_to_collection,
    ensure_bone_collection,
    isolate_bone_in_collection,
    set_pose_bone_select,
)
from .create_animation_rig import (
    _FINGER_BONE,
    _LEFT_COLOR,
    _RIGHT_COLOR,
    _assign_shape,
    _edit_bone,
    _estimate_character_scale,
    _set_bone_color,
    _set_collection_visible,
    _set_named_bone_fcurves_muted,
    _suffix_groups,
    _widget_object,
    canonical_bone_name,
    bone_name_suffix,
)

CONTROL_PREFIX = "BL_"
PAD_PREFIX = "BL_Pad_"
SLIDER_COLLECTION = "Finger Sliders"
CIRCLE_COLLECTION = "Finger Circles"
STANDARD_COLLECTION = "Standard Bones"

_HAND_RE = re.compile(r"^Hand([LR])(\d*)$")
# Finger{L|R}{optional extra-hand digits}{digit 1-5}{joint 0-3}.
# Joint 0 is the metacarpal (FingerR10). Extra hands: FingerL211.
_FINGER_RE = re.compile(r"^Finger([LR])(\d*)([1-5])([0-3])$")
_SLIDER_KNOB_RE = re.compile(r"^BL_(Curl|Spread|Thumb|Index|Middle|Ring|Pinky)_([LR]\d*)$")

FINGERS = (
    ("Thumb", 5),
    ("Index", 1),
    ("Middle", 2),
    ("Ring", 3),
    ("Pinky", 4),
)
ALL_SEGMENTS = (0, 1, 2, 3)
SEGMENTS = (1, 2, 3)
SEGMENT_WEIGHTS = {0: 0.35, 1: 0.8, 2: 1.0, 3: 1.0}
THUMB_DIGIT = 5
SPREAD_FACTORS = {1: 1.0, 2: 0.35, 3: -0.35, 4: -1.0, THUMB_DIGIT: 0.8}

MAX_CURL_DEGREES = 90.0
MAX_SPREAD_DEGREES = 12.0
MAX_SIDE_DEGREES = 60.0

# 1D sliders, except Thumb which is a 2D square-in-square pad.
SLIDER_KINDS = (
    ("Curl", "curl"),
    ("Spread", "spread"),
    ("Thumb", "thumb"),
    ("Index", "index"),
    ("Middle", "middle"),
    ("Ring", "ring"),
    ("Pinky", "pinky"),
)


def slider_bone_name(kind, side, suffix="", digits=""):
    return f"{CONTROL_PREFIX}{kind}_{side}{digits}{suffix}"


def pad_bone_name(kind, side, suffix="", digits=""):
    return f"{PAD_PREFIX}{kind}_{side}{digits}{suffix}"


def finger_bone_name(side, digit, segment, suffix="", digits=""):
    return f"Finger{side}{digits}{digit}{segment}{suffix}"


def parse_finger_bone(name):
    base = canonical_bone_name(name)
    match = _FINGER_RE.match(base)
    if not match:
        return None
    return {
        "side": match.group(1),
        "digits": match.group(2) or "",
        "digit": int(match.group(3)),
        "segment": int(match.group(4)),
        "suffix": bone_name_suffix(name),
    }


def _armature_bones(armature):
    if getattr(armature, "type", None) == "ARMATURE":
        return armature.data.bones
    return armature.bones


def _finger_chain(armature, side, digit, suffix="", digits=""):
    """Proximal-to-distal joints that exist, including metacarpals (segment 0)."""
    bones = _armature_bones(armature)
    chain = []
    for segment in ALL_SEGMENTS:
        name = finger_bone_name(side, digit, segment, suffix, digits=digits)
        bone = bones.get(name)
        if bone is not None:
            chain.append((segment, bone, name))
    return chain


def is_finger_circle_bone(name):
    """Smash Finger* bones that get circle widgets, including FingerL1 roots."""
    return _FINGER_BONE.match(canonical_bone_name(name)) is not None


def iter_hand_slots(armature_obj):
    """Yield (side, digits, suffix) for every Smash hand, including extra arms."""
    slots = set()
    for pose_bone in armature_obj.pose.bones:
        suffix = bone_name_suffix(pose_bone.name)
        base = canonical_bone_name(pose_bone.name)
        hand = _HAND_RE.match(base)
        if hand:
            slots.add((hand.group(1), hand.group(2) or "", suffix))
            continue
        parsed = parse_finger_bone(pose_bone.name)
        if parsed is not None:
            slots.add((parsed["side"], parsed["digits"], parsed["suffix"]))
    return sorted(slots)


def is_anim_control_bone(name):
    return canonical_bone_name(name).startswith(CONTROL_PREFIX)


def is_finger_pad_bone(name):
    return canonical_bone_name(name).startswith(PAD_PREFIX)


def is_finger_slider_bone(name):
    return bool(_SLIDER_KNOB_RE.match(canonical_bone_name(name)))


def is_finger_control_bone(name):
    return is_finger_slider_bone(name) or is_finger_pad_bone(name)


def is_thumb_slider_bone(name):
    return canonical_bone_name(name).startswith("BL_Thumb_")


def _slider_side(name):
    base = canonical_bone_name(name)
    if base.endswith("_R") or "_R_" in f"{base}_":
        return "R"
    tail = base.rsplit("_", 1)[-1]
    if tail.startswith("R"):
        return "R"
    return "L"


def has_any_finger_bones(armature_obj, suffix="", side=None, digits=None):
    for bone in armature_obj.data.bones:
        parsed = parse_finger_bone(bone.name)
        if parsed is None:
            continue
        if suffix and parsed["suffix"] != suffix:
            continue
        if side is not None and parsed["side"] != side:
            continue
        if digits is not None and parsed["digits"] != digits:
            continue
        return True
    return False


def has_finger_sliders(armature_obj):
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return False
    return any(is_finger_slider_bone(bone.name) for bone in armature_obj.data.bones)


def has_finger_slider_constraints(armature_obj):
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return False
    for _pose_bone, _constraint in _iter_finger_slider_constraints(armature_obj):
        return True
    return False


def _rest_head(armature, name):
    bone = armature.bones.get(name)
    if bone is not None:
        return bone.head_local.copy()
    edit = getattr(armature, "edit_bones", None)
    if edit is not None:
        bone = edit.get(name)
        if bone is not None:
            return bone.head.copy()
    return None


def _rest_tail(armature, name):
    bone = armature.bones.get(name)
    if bone is not None:
        return bone.tail_local.copy()
    edit = getattr(armature, "edit_bones", None)
    if edit is not None:
        bone = edit.get(name)
        if bone is not None:
            return bone.tail.copy()
    return None


def _finger_head(armature, side, digit, suffix="", digits=""):
    """Knuckle/head used for layout: first phalange, else the metacarpal."""
    for segment in (1, 0, 2, 3):
        head = _rest_head(armature, finger_bone_name(side, digit, segment, suffix, digits=digits))
        if head is not None:
            return head
    return None


def _hand_across_vector(armature, side, suffix, digits=""):
    """Index knuckle -> pinky knuckle. Used so sliders sit over the real fingers."""
    index = _finger_head(armature, side, 1, suffix, digits=digits)
    pinky = _finger_head(armature, side, 4, suffix, digits=digits)
    if index is not None and pinky is not None:
        across = pinky - index
        if across.length > 1e-6:
            return across.normalized()
    thumb = _finger_head(armature, side, 5, suffix, digits=digits)
    if thumb is not None and index is not None:
        across = index - thumb
        if across.length > 1e-6:
            return across.normalized()
    middle = _finger_head(armature, side, 2, suffix, digits=digits)
    if index is not None and middle is not None:
        across = middle - index
        if across.length > 1e-6:
            return across.normalized()
    return None


def _finger_bend_axis(armature, side, digit, across, suffix, up=None, digits=""):
    chain = _finger_chain(armature, side, digit, suffix, digits=digits)
    phalanges = [bone for segment, bone, _name in chain if segment >= 1]
    root = phalanges[0] if phalanges else (chain[0][1] if chain else None)
    along = None
    if root is not None:
        along = root.tail_local - root.head_local
        if along.length < 1e-6:
            along = None
        else:
            along = along.normalized()

    # Thumb does not share the other fingers' hinge. Fold it toward the palm
    # (down the dorsal axis of the hand box) instead of auto-detecting a
    # sideways opposition axis from the rest pose.
    if digit == THUMB_DIGIT and along is not None and up is not None:
        hinge = along.cross(-up)
        if hinge.length > 1e-6:
            return hinge.normalized()

    if len(phalanges) >= 3:
        first = phalanges[1].head_local - phalanges[0].head_local
        second = phalanges[2].head_local - phalanges[1].head_local
        if first.length > 1e-6 and second.length > 1e-6:
            normal = first.normalized().cross(second.normalized())
            if normal.length > 0.087:
                return normal.normalized()
    elif len(phalanges) >= 2:
        first = phalanges[1].head_local - phalanges[0].head_local
        if first.length > 1e-6 and along is not None:
            normal = along.cross(first.normalized())
            if normal.length > 0.087:
                return normal.normalized()

    if digit == THUMB_DIGIT and root is not None and across is not None and along is not None:
        palm_normal = across.cross(along)
        if palm_normal.length > 1e-6:
            return palm_normal.normalized()
    return across


def _axis_from_vector(bone, vector, exclude=None):
    local = bone.matrix_local.to_3x3().inverted() @ vector
    components = {0: local.x, 1: local.y, 2: local.z}
    if exclude is not None:
        components.pop(exclude, None)
    axis = max(components, key=lambda i: abs(components[i]))
    return axis, (1.0 if components[axis] >= 0.0 else -1.0)


def _hand_slider_frame(armature, side, suffix, digits=""):
    """Return (origin, along, across, up, hand_length) in armature space.

    `across` always runs index -> pinky so slider order matches the real fingers.
    `up` is flipped onto the dorsal side of the hand without reversing `across`.
    """
    hand_name = f"Hand{side}{digits}{suffix}"
    head = _rest_head(armature, hand_name)
    tail = _rest_tail(armature, hand_name)
    if head is None or tail is None:
        return None

    along = tail - head
    if along.length < 1e-6:
        along = Vector((0.0, 1.0, 0.0))
    else:
        along = along.normalized()

    across = _hand_across_vector(armature, side, suffix, digits=digits)
    if across is None:
        hand = armature.bones.get(hand_name)
        across = Vector(hand.matrix_local.to_3x3().col[0]).normalized() if hand else Vector((1.0, 0.0, 0.0))
    across = across - across.project(along)
    if across.length < 1e-6:
        across = Vector((1.0, 0.0, 0.0))
    else:
        across = across.normalized()

    up = along.cross(across)
    if up.length < 1e-6:
        up = Vector((0.0, 0.0, 1.0))
    else:
        up = up.normalized()
    # Sit sliders on the back of the hand, but keep index -> pinky as +across.
    if up.dot(Vector((0.0, 0.0, 1.0))) < 0.0:
        up = -up
    return head, along, across, up, max((tail - head).length, 0.05)


def _slider_positions(armature, side, suffix, origin, along, across, up, box_size, digits=""):
    """Curl/Spread at the wrist, 1-4 evenly across the box, thumb on the radial side."""
    top = origin + up * (box_size * 0.58)
    half_w = box_size * 0.42
    wrist_along = along * (box_size * -0.32)
    finger_along = along * (box_size * 0.40)

    positions = {
        "Curl": top + wrist_along + across * (-half_w * 0.55),
        "Spread": top + wrist_along + across * (half_w * 0.55),
    }
    finger_kinds = ("Index", "Middle", "Ring", "Pinky")
    for index, kind in enumerate(finger_kinds):
        x = (index / (len(finger_kinds) - 1) - 0.5) * 2.0 * half_w
        positions[kind] = top + finger_along + across * x

    def across_of(digit):
        head = _finger_head(armature, side, digit, suffix, digits=digits)
        if head is None:
            return None
        return (head - origin).dot(across)

    index_x = across_of(1)
    pinky_x = across_of(4)
    thumb_x = across_of(5)
    if thumb_x is None:
        if index_x is not None and pinky_x is not None:
            thumb_x = index_x - (pinky_x - index_x) * 0.4
        else:
            thumb_x = -half_w
    side_sign = 1.0 if thumb_x >= 0.0 else -1.0
    positions["Thumb"] = (
        origin
        + up * (box_size * 0.12)
        + along * (box_size * 0.22)
        + across * (side_sign * box_size * 0.58)
    )
    return positions


def _remove_slider_edit_bones(armature, suffix=""):
    to_remove = []
    for bone in armature.edit_bones:
        if is_finger_control_bone(bone.name):
            if suffix and not bone.name.endswith(suffix):
                continue
            to_remove.append(bone)
    for bone in to_remove:
        armature.edit_bones.remove(bone)


FINGER_CON_PREFIX = "SUB Finger"


def _clear_finger_drivers(armature_obj, suffix=""):
    removed = 0
    for pose_bone in list(armature_obj.pose.bones):
        parsed = parse_finger_bone(pose_bone.name)
        if parsed is None:
            continue
        if suffix and parsed["suffix"] != suffix:
            continue
        data_path = f'pose.bones["{pose_bone.name}"].rotation_euler'
        for axis in range(3):
            try:
                if armature_obj.driver_remove(data_path, axis):
                    removed += 1
            except Exception:
                pass
        for constraint in list(pose_bone.constraints):
            if constraint.name.startswith(FINGER_CON_PREFIX):
                pose_bone.constraints.remove(constraint)
                removed += 1
    return removed


def _add_transform_driver(armature_obj, data_path, axis, expression, bone_vars):
    """bone_vars: list of (var_name, slider_bone_name)."""
    armature_obj.animation_data_create()
    try:
        armature_obj.driver_remove(data_path, axis)
    except Exception:
        pass
    fcurve = armature_obj.driver_add(data_path, axis)
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    driver.use_self = False
    for var_name, bone_name in bone_vars:
        var = driver.variables.new()
        var.name = var_name
        var.type = "TRANSFORMS"
        target = var.targets[0]
        target.id = armature_obj
        target.bone_target = bone_name
        target.transform_type = "LOC_Y"
        target.transform_space = "LOCAL_SPACE"
    driver.expression = expression
    return fcurve


def _configure_pad_pose(pose_bone, color):
    pose_bone.lock_location = (True, True, True)
    pose_bone.lock_rotation = (True, True, True)
    pose_bone.lock_scale = (True, True, True)
    pose_bone.lock_rotation_w = True
    pose_bone.rotation_mode = "XYZ"
    pose_bone.location = (0.0, 0.0, 0.0)
    pose_bone.bone.hide_select = True
    for constraint in list(pose_bone.constraints):
        if constraint.type == "LIMIT_LOCATION":
            pose_bone.constraints.remove(constraint)
    _set_bone_color(pose_bone, color)


def _configure_slider_pose(pose_bone, half_travel, color):
    pose_bone.lock_location = (True, False, True)
    pose_bone.lock_rotation = (True, True, True)
    pose_bone.lock_scale = (True, True, True)
    pose_bone.lock_rotation_w = True
    pose_bone.rotation_mode = "XYZ"
    pose_bone.location = (0.0, 0.0, 0.0)
    pose_bone.bone.hide_select = False

    for constraint in list(pose_bone.constraints):
        if constraint.type == "LIMIT_LOCATION":
            pose_bone.constraints.remove(constraint)

    limit = pose_bone.constraints.new("LIMIT_LOCATION")
    limit.owner_space = "LOCAL"
    limit.use_min_x = True
    limit.use_max_x = True
    limit.min_x = 0.0
    limit.max_x = 0.0
    limit.use_min_z = True
    limit.use_max_z = True
    limit.min_z = 0.0
    limit.max_z = 0.0
    limit.use_min_y = True
    limit.use_max_y = True
    limit.min_y = -half_travel
    limit.max_y = half_travel
    limit.use_transform_limit = True
    limit.name = "Slider Range"
    _set_bone_color(pose_bone, color)


def _configure_curl_slider_pose(pose_bone, half_travel, color):
    """Curl is 2D: Y = all fingers together, X = progressive cascade."""
    pose_bone.lock_location = (False, False, True)
    pose_bone.lock_rotation = (True, True, True)
    pose_bone.lock_scale = (True, True, True)
    pose_bone.lock_rotation_w = True
    pose_bone.rotation_mode = "XYZ"
    pose_bone.location = (0.0, 0.0, 0.0)
    pose_bone.bone.hide_select = False

    for constraint in list(pose_bone.constraints):
        if constraint.type == "LIMIT_LOCATION":
            pose_bone.constraints.remove(constraint)

    limit = pose_bone.constraints.new("LIMIT_LOCATION")
    limit.owner_space = "LOCAL"
    limit.use_min_x = True
    limit.use_max_x = True
    limit.min_x = -half_travel
    limit.max_x = half_travel
    limit.use_min_y = True
    limit.use_max_y = True
    limit.min_y = -half_travel
    limit.max_y = half_travel
    limit.use_min_z = True
    limit.use_max_z = True
    limit.min_z = 0.0
    limit.max_z = 0.0
    limit.use_transform_limit = True
    limit.name = "Curl Pad Range"
    _set_bone_color(pose_bone, color)


def _configure_thumb_knob_pose(pose_bone, half_travel, color):
    pose_bone.lock_location = (False, False, True)
    pose_bone.lock_rotation = (True, True, True)
    pose_bone.lock_scale = (True, True, True)
    pose_bone.lock_rotation_w = True
    pose_bone.rotation_mode = "XYZ"
    pose_bone.location = (0.0, 0.0, 0.0)
    pose_bone.bone.hide_select = False

    for constraint in list(pose_bone.constraints):
        if constraint.type == "LIMIT_LOCATION":
            pose_bone.constraints.remove(constraint)

    limit = pose_bone.constraints.new("LIMIT_LOCATION")
    limit.owner_space = "LOCAL"
    limit.use_min_x = True
    limit.use_max_x = True
    limit.min_x = -half_travel
    limit.max_x = half_travel
    limit.use_min_y = True
    limit.use_max_y = True
    limit.min_y = -half_travel
    limit.max_y = half_travel
    limit.use_min_z = True
    limit.use_max_z = True
    limit.min_z = 0.0
    limit.max_z = 0.0
    limit.use_transform_limit = True
    limit.name = "Pad Range"
    _set_bone_color(pose_bone, color)


def _set_transform_rot_range(constraint, axis, min_r, max_r):
    letter = "xyz"[axis]
    for attr, value in ((f"to_min_{letter}_rot", min_r), (f"to_max_{letter}_rot", max_r)):
        if hasattr(constraint, attr):
            setattr(constraint, attr, value)


def _map_transform_from_axis(constraint, from_axis, to_axis):
    from_letter = "XYZ"[int(from_axis)]
    for i, letter in enumerate("xyz"):
        attr = f"map_to_{letter}_from"
        if hasattr(constraint, attr):
            setattr(constraint, attr, from_letter if i == to_axis else "X")


def _ensure_finger_constraint(
    pose_bone,
    name,
    target,
    subtarget,
    half,
    rot_axis,
    rot_at_max,
    from_axis=1,
    from_min=None,
    from_max=None,
    rot_min=None,
    rot_max=None,
):
    constraint = next((c for c in pose_bone.constraints if c.name == name), None)
    if constraint is None:
        constraint = pose_bone.constraints.new("TRANSFORM")
    constraint.name = name
    constraint.target = target
    constraint.subtarget = subtarget
    constraint.target_space = "LOCAL"
    constraint.owner_space = "LOCAL"
    constraint.map_from = "LOCATION"
    constraint.map_to = "ROTATION"

    axis_min = -half if from_min is None else float(from_min)
    axis_max = half if from_max is None else float(from_max)
    constraint.from_min_x = axis_min if from_axis == 0 else 0.0
    constraint.from_max_x = axis_max if from_axis == 0 else 0.0
    constraint.from_min_y = axis_min if from_axis == 1 else 0.0
    constraint.from_max_y = axis_max if from_axis == 1 else 0.0
    constraint.from_min_z = 0.0
    constraint.from_max_z = 0.0
    _map_transform_from_axis(constraint, from_axis, rot_axis)

    if rot_min is None and rot_max is None:
        rmin, rmax = -float(rot_at_max), float(rot_at_max)
    else:
        rmin = -float(rot_at_max) if rot_min is None else float(rot_min)
        rmax = float(rot_at_max) if rot_max is None else float(rot_max)
    for axis in range(3):
        if axis == rot_axis:
            _set_transform_rot_range(constraint, axis, rmin, rmax)
        else:
            _set_transform_rot_range(constraint, axis, 0.0, 0.0)
    if hasattr(constraint, "mix_mode_rot"):
        for mode in ("AFTER", "ADD", "AFTER_FULL", "AFTER_ORIGINAL", "AFTER_SPLIT"):
            try:
                constraint.mix_mode_rot = mode
                break
            except (TypeError, ValueError):
                continue
    elif hasattr(constraint, "mix_mode"):
        for mode in ("AFTER_ORIGINAL", "ADD", "AFTER_FULL", "BEFORE_ORIGINAL"):
            try:
                constraint.mix_mode = mode
                break
            except (TypeError, ValueError):
                continue
    if hasattr(constraint, "to_euler_order"):
        try:
            constraint.to_euler_order = "AUTO"
        except (TypeError, ValueError):
            pass
    constraint.mute = False
    return constraint


# Index → Pinky. Horizontal Curl X cascades through these in order.
_CURL_CASCADE_DIGITS = (1, 2, 3, 4)


def _cascade_window(finger_index, count, half, positive):
    """X window where this finger curls during a progressive sweep."""
    count = max(int(count), 1)
    t0 = finger_index / count
    t1 = (finger_index + 1) / count
    overlap = 0.22 / count
    t0 = max(0.0, t0 - overlap)
    t1 = min(1.0, t1 + overlap)
    if positive:
        return (t0 * half, t1 * half)
    return (-t1 * half, -t0 * half)


def _cascade_rot_range(weight):
    weight = float(weight)
    if weight >= 0.0:
        return 0.0, weight
    return weight, 0.0


def _curl_axis_for_finger(bone, digit, bend_vector):
    if digit == THUMB_DIGIT:
        local = Vector((0.0, 0.0, 1.0))
        if bend_vector is not None:
            try:
                local = bone.matrix_local.to_3x3().inverted() @ bend_vector
            except ValueError:
                pass
        return 2, (1.0 if local.z >= 0.0 else -1.0)
    if bend_vector is not None:
        return _axis_from_vector(bone, bend_vector, exclude=1)
    return 2, 1.0


def _has_foreign_finger_children(bone, digit):
    """True if this bone parents another finger's chain (official FingerR10, etc.)."""
    for child in getattr(bone, "children", []) or []:
        parsed = parse_finger_bone(child.name)
        if parsed is not None and parsed["digit"] != digit:
            return True
        if parsed is None and _FINGER_BONE.match(canonical_bone_name(child.name)):
            if _has_foreign_finger_children(child, digit):
                return True
        elif parsed is not None and _has_foreign_finger_children(child, digit):
            return True
    return False


def _clear_finger_drive_props(pose_bone):
    for key in (
        "sub_finger_curl_axis",
        "sub_finger_curl_weight",
        "sub_finger_curl_cascade_weight",
        "sub_finger_spread_axis",
        "sub_finger_spread_weight",
        "sub_finger_side_axis",
        "sub_finger_side_weight",
    ):
        if key in pose_bone:
            del pose_bone[key]


def _drive_fingers(armature_obj, side, suffix, half_travel, digits=""):
    curl_name = slider_bone_name("Curl", side, suffix, digits=digits)
    spread_name = slider_bone_name("Spread", side, suffix, digits=digits)
    if curl_name not in armature_obj.pose.bones:
        return

    frame = _hand_slider_frame(armature_obj.data, side, suffix, digits=digits)
    across = frame[2] if frame is not None else _hand_across_vector(
        armature_obj.data, side, suffix, digits=digits
    )
    up = frame[3] if frame is not None else None
    max_curl = math.radians(MAX_CURL_DEGREES)
    max_spread = math.radians(MAX_SPREAD_DEGREES)
    max_side = math.radians(MAX_SIDE_DEGREES)
    half = max(half_travel, 1e-6)
    cascade_count = len(_CURL_CASCADE_DIGITS)
    cascade_index = {digit: i for i, digit in enumerate(_CURL_CASCADE_DIGITS)}

    for label, digit in FINGERS:
        offset_name = slider_bone_name(label, side, suffix, digits=digits)
        bend_vector = _finger_bend_axis(
            armature_obj.data, side, digit, across, suffix, up=up, digits=digits
        )
        chain = _finger_chain(armature_obj.data, side, digit, suffix, digits=digits)
        driveable = [
            (segment, bone, name)
            for segment, bone, name in chain
            if segment != 0 and not _has_foreign_finger_children(bone, digit)
        ]
        spread_segment = None
        if digit != THUMB_DIGIT and driveable:
            # Same as the original rig: spread on joint 1 when it exists.
            spread_segment = 1 if any(seg == 1 for seg, _bone, _name in driveable) else driveable[0][0]
        for segment, bone, bone_name in chain:
            pose_bone = armature_obj.pose.bones.get(bone_name)
            if pose_bone is None:
                continue
            data_path = f'pose.bones["{bone_name}"].rotation_euler'
            for stale in range(3):
                try:
                    armature_obj.driver_remove(data_path, stale)
                except Exception:
                    pass
            for constraint in list(pose_bone.constraints):
                if constraint.name.startswith(FINGER_CON_PREFIX):
                    pose_bone.constraints.remove(constraint)
            # Official metacarpals (FingerR10/20/30/40) keep their Smash animation.
            # Driving them with the phalange sliders pulls Mario's fists off-pose.
            if segment == 0 or _has_foreign_finger_children(bone, digit):
                _clear_finger_drive_props(pose_bone)
                continue
            curl_axis, curl_sign = _curl_axis_for_finger(bone, digit, bend_vector)

            weight = SEGMENT_WEIGHTS.get(segment, 1.0) * max_curl * curl_sign
            pose_bone["sub_finger_curl_axis"] = curl_axis
            pose_bone["sub_finger_curl_weight"] = weight
            if digit == THUMB_DIGIT:
                if offset_name not in armature_obj.pose.bones:
                    continue
                _ensure_finger_constraint(
                    pose_bone,
                    f"{FINGER_CON_PREFIX} Thumb",
                    armature_obj,
                    offset_name,
                    half,
                    curl_axis,
                    weight,
                    from_axis=1,
                )
            else:
                # Y: all fingers curl together
                _ensure_finger_constraint(
                    pose_bone,
                    f"{FINGER_CON_PREFIX} Curl",
                    armature_obj,
                    curl_name,
                    half,
                    curl_axis,
                    weight,
                    from_axis=1,
                )
                # X: progressive cascade (right = Index→Pinky, left = Pinky→Index)
                if digit in cascade_index:
                    pose_bone["sub_finger_curl_cascade_weight"] = weight
                    idx = cascade_index[digit]
                    rev = cascade_count - 1 - idx
                    rmin, rmax = _cascade_rot_range(weight)
                    pos_min, pos_max = _cascade_window(idx, cascade_count, half, True)
                    neg_min, neg_max = _cascade_window(rev, cascade_count, half, False)
                    _ensure_finger_constraint(
                        pose_bone,
                        f"{FINGER_CON_PREFIX} Curl Cascade+",
                        armature_obj,
                        curl_name,
                        half,
                        curl_axis,
                        weight,
                        from_axis=0,
                        from_min=pos_min,
                        from_max=pos_max,
                        rot_min=rmin,
                        rot_max=rmax,
                    )
                    _ensure_finger_constraint(
                        pose_bone,
                        f"{FINGER_CON_PREFIX} Curl Cascade-",
                        armature_obj,
                        curl_name,
                        half,
                        curl_axis,
                        weight,
                        from_axis=0,
                        from_min=neg_min,
                        from_max=neg_max,
                        rot_min=rmin,
                        rot_max=rmax,
                    )
                if offset_name in armature_obj.pose.bones:
                    _ensure_finger_constraint(
                        pose_bone,
                        f"{FINGER_CON_PREFIX} Offset",
                        armature_obj,
                        offset_name,
                        half,
                        curl_axis,
                        weight,
                        from_axis=1,
                    )

            along = bone.tail_local - bone.head_local
            if along.length <= 1e-6 or bend_vector is None:
                continue
            side_vector = bend_vector.cross(along.normalized())
            if side_vector.length <= 1e-6:
                continue

            if digit == THUMB_DIGIT:
                side_axis, side_sign = _axis_from_vector(
                    bone, side_vector.normalized(), exclude=curl_axis
                )
                side_weight = SEGMENT_WEIGHTS.get(segment, 1.0) * max_side * side_sign
                pose_bone["sub_finger_side_axis"] = side_axis
                pose_bone["sub_finger_side_weight"] = side_weight
                if offset_name in armature_obj.pose.bones:
                    _ensure_finger_constraint(
                        pose_bone,
                        f"{FINGER_CON_PREFIX} Side",
                        armature_obj,
                        offset_name,
                        half,
                        side_axis,
                        side_weight,
                        from_axis=0,
                    )
                continue

            if segment != spread_segment:
                continue
            spread_axis, spread_sign = _axis_from_vector(
                bone, side_vector.normalized(), exclude=curl_axis
            )
            factor = SPREAD_FACTORS.get(digit, 0.0)
            if factor == 0.0 or spread_name not in armature_obj.pose.bones:
                continue
            spread_weight = max_spread * factor * spread_sign
            pose_bone["sub_finger_spread_axis"] = spread_axis
            pose_bone["sub_finger_spread_weight"] = spread_weight
            _ensure_finger_constraint(
                pose_bone,
                f"{FINGER_CON_PREFIX} Spread",
                armature_obj,
                spread_name,
                half,
                spread_axis,
                spread_weight,
                from_axis=1,
            )


def _new_control_edit_bone(armature, name, head, direction, length, parent, roll=None, up=None):
    bone = armature.edit_bones.new(name)
    bone.head = head
    bone.tail = head + direction.normalized() * length
    bone.use_deform = False
    bone.use_connect = False
    bone.parent = parent
    if roll is not None:
        bone.roll = roll
    elif up is not None:
        try:
            bone.align_roll(up)
        except (TypeError, ValueError, RuntimeError):
            bone.roll = parent.roll if parent is not None else 0.0
    return bone


def build_finger_sliders(context, armature_obj):
    """Create sliders on each Hand box, with a 2D pad only on the thumb."""
    created = 0
    char_scale = _estimate_character_scale(armature_obj)
    box_size = char_scale * 0.12
    half_travel = box_size * 0.18
    bone_length = max(box_size * 0.08, 0.015)
    slots = list(iter_hand_slots(armature_obj))

    bpy.ops.object.mode_set(mode="EDIT")
    try:
        suffixes = {suffix for _side, _digits, suffix in slots} or set(_suffix_groups(armature_obj).keys())
        for suffix in suffixes:
            _remove_slider_edit_bones(armature_obj.data, suffix)
        for side, digits, suffix in slots:
            if not has_any_finger_bones(armature_obj, suffix, side=side, digits=digits):
                continue
            hand = _edit_bone(armature_obj.data, f"Hand{side}{digits}", suffix)
            if hand is None:
                continue
            frame = _hand_slider_frame(armature_obj.data, side, suffix, digits=digits)
            if frame is None:
                continue
            origin, along, across, up, _hand_len = frame
            positions = _slider_positions(
                armature_obj.data, side, suffix, origin, along, across, up, box_size, digits=digits
            )
            hand_dir = hand.tail - hand.head
            if hand_dir.length < 1e-6:
                hand_dir = along.copy()
            else:
                hand_dir = hand_dir.normalized()
            for kind, _role in SLIDER_KINDS:
                if kind not in positions:
                    continue
                if kind not in ("Curl", "Spread"):
                    digit = next(d for label, d in FINGERS if label == kind)
                    if not _finger_chain(armature_obj.data, side, digit, suffix, digits=digits):
                        continue
                control_name = slider_bone_name(kind, side, suffix, digits=digits)
                pad_name = pad_bone_name(kind, side, suffix, digits=digits)
                for old_name in (pad_name, control_name):
                    existing = armature_obj.data.edit_bones.get(old_name)
                    if existing is not None:
                        armature_obj.data.edit_bones.remove(existing)
                pos = positions[kind]
                if kind == "Thumb":
                    pad = _new_control_edit_bone(
                        armature_obj.data, pad_name, pos, hand_dir, bone_length, hand, roll=hand.roll
                    )
                    _new_control_edit_bone(
                        armature_obj.data, control_name, pos, hand_dir, bone_length, pad, roll=hand.roll
                    )
                else:
                    _new_control_edit_bone(
                        armature_obj.data, control_name, pos, along, bone_length, hand, up=up
                    )
                created += 1
    finally:
        bpy.ops.object.mode_set(mode="POSE")

    slider_widget = _widget_object(context, "slider")
    pad_widget = _widget_object(context, "pad")
    knob_widget = _widget_object(context, "knob")
    collection = ensure_bone_collection(armature_obj.data, SLIDER_COLLECTION)
    _set_collection_visible(armature_obj.data, SLIDER_COLLECTION, True)

    slider_scale = box_size * 0.22
    knob_scale = half_travel * 0.32
    pad_scale = half_travel + knob_scale
    for pose_bone in armature_obj.pose.bones:
        if is_finger_pad_bone(pose_bone.name):
            color = "THEME02"
            _configure_pad_pose(pose_bone, color)
            _assign_shape(pose_bone, pad_widget, pad_scale, color, False, armature_obj)
            if hasattr(pose_bone, "custom_shape_wire_width"):
                pose_bone.custom_shape_wire_width = 1.5
            if collection is not None:
                assign_bone_to_collection(collection, armature_obj.data.bones[pose_bone.name])
            continue
        if not is_finger_slider_bone(pose_bone.name):
            continue
        side = _slider_side(pose_bone.name)
        color = _LEFT_COLOR if side == "L" else _RIGHT_COLOR
        base = canonical_bone_name(pose_bone.name)
        if "Curl" in base:
            color = "THEME09"
        elif "Spread" in base:
            color = "THEME04"
        elif "Thumb" in base:
            color = "THEME02"
        if is_thumb_slider_bone(pose_bone.name):
            _configure_thumb_knob_pose(pose_bone, half_travel, color)
            _assign_shape(pose_bone, knob_widget, knob_scale, color, False, armature_obj)
            if hasattr(pose_bone, "custom_shape_wire_width"):
                pose_bone.custom_shape_wire_width = 2.5
        elif "Curl" in base:
            # 2D pad: Y = uniform curl, X = progressive cascade
            _configure_curl_slider_pose(pose_bone, half_travel, color)
            _assign_shape(pose_bone, knob_widget, knob_scale * 1.15, color, False, armature_obj)
            if hasattr(pose_bone, "custom_shape_wire_width"):
                pose_bone.custom_shape_wire_width = 2.5
        else:
            _configure_slider_pose(pose_bone, half_travel, color)
            _assign_shape(pose_bone, slider_widget, slider_scale, color, False, armature_obj)
        if collection is not None:
            assign_bone_to_collection(collection, armature_obj.data.bones[pose_bone.name])

    for side, digits, suffix in iter_hand_slots(armature_obj):
        if not has_any_finger_bones(armature_obj, suffix, side=side, digits=digits):
            continue
        _drive_fingers(armature_obj, side, suffix, half_travel, digits=digits)

    armature_obj.data["sub_finger_slider_travel"] = half_travel
    set_finger_slider_mode(armature_obj, True)
    return created


FINGER_SLIDER_USE_KEY = "sub_use_finger_sliders"
_HELD_FINGER_MUTE_KEY = "sub_held_finger_fcurves"


def finger_sliders_are_enabled(armature_obj):
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return False
    if not has_finger_sliders(armature_obj):
        return False
    return bool(armature_obj.data.get(FINGER_SLIDER_USE_KEY, True))


def _set_finger_constraint_mute(armature_obj, mute):
    for _pose_bone, constraint in _iter_finger_slider_constraints(armature_obj):
        constraint.mute = bool(mute)


def _heal_finger_circle_disable(armature_obj):
    """Undo lock/mute leftovers from when the switch used to disable circles."""
    names = []
    for pose_bone in armature_obj.pose.bones:
        if parse_finger_bone(pose_bone.name) is None:
            continue
        names.append(pose_bone.name)
        pose_bone.lock_rotation = (False, False, False)
        pose_bone.lock_rotation_w = False
        bone = armature_obj.data.bones.get(pose_bone.name)
        if bone is not None:
            bone.hide_select = False
    if names and armature_obj.data.get(_HELD_FINGER_MUTE_KEY):
        _set_named_bone_fcurves_muted(
            armature_obj, names, mute=False, held_key=_HELD_FINGER_MUTE_KEY
        )
    _set_finger_constraint_mute(armature_obj, False)


def _isolate_bones_in_collection(armature_obj, collection_name, predicate):
    collection = ensure_bone_collection(armature_obj.data, collection_name)
    if collection is None:
        return None
    for bone in armature_obj.data.bones:
        if predicate(bone.name):
            isolate_bone_in_collection(collection, bone)
    return collection


def _restore_finger_circles_to_standard(armature_obj):
    standard = None
    collections = getattr(armature_obj.data, "collections", None)
    if collections is not None:
        standard = collections.get(STANDARD_COLLECTION) if hasattr(collections, "get") else (
            collections[STANDARD_COLLECTION] if STANDARD_COLLECTION in collections else None
        )
    for bone in armature_obj.data.bones:
        if not is_finger_circle_bone(bone.name):
            continue
        bone.hide = False
        bone.hide_select = False
        if standard is not None:
            isolate_bone_in_collection(standard, bone)
    _set_collection_visible(armature_obj.data, CIRCLE_COLLECTION, True)


def set_finger_slider_mode(armature_obj, use_sliders, context=None):
    """Show sliders or finger circles. Both stay live; this is visibility only."""
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return False
    if not has_finger_sliders(armature_obj):
        return False
    use_sliders = bool(use_sliders)
    armature_obj.data[FINGER_SLIDER_USE_KEY] = use_sliders
    _heal_finger_circle_disable(armature_obj)

    # Pose Mode in Blender 5 ignores Bone.hide; a bone stays visible if it is
    # still in Standard Bones. Isolate each set into its own collection.
    _isolate_bones_in_collection(armature_obj, SLIDER_COLLECTION, is_finger_control_bone)
    _isolate_bones_in_collection(armature_obj, CIRCLE_COLLECTION, is_finger_circle_bone)

    for bone in armature_obj.data.bones:
        if is_finger_control_bone(bone.name):
            bone.hide = not use_sliders
            if not use_sliders:
                set_pose_bone_select(armature_obj.pose.bones.get(bone.name), False)
        elif is_finger_circle_bone(bone.name):
            bone.hide = use_sliders
            if use_sliders:
                set_pose_bone_select(armature_obj.pose.bones.get(bone.name), False)

    _set_collection_visible(armature_obj.data, SLIDER_COLLECTION, use_sliders)
    _set_collection_visible(armature_obj.data, CIRCLE_COLLECTION, not use_sliders)
    armature_obj.update_tag()
    if context is not None:
        try:
            context.view_layer.update()
        except Exception:
            pass
    return True


def remove_finger_sliders(context, armature_obj):
    """Remove slider bones and the finger rotation drivers they feed."""
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return 0
    removed_drivers = 0
    for suffix in list(_suffix_groups(armature_obj).keys()):
        removed_drivers += _clear_finger_drivers(armature_obj, suffix)

    prev_mode = armature_obj.mode
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.mode_set(mode="EDIT")
    removed_bones = 0
    try:
        names = [b.name for b in armature_obj.data.edit_bones if is_finger_control_bone(b.name)]
        for name in names:
            bone = armature_obj.data.edit_bones.get(name)
            if bone is not None:
                armature_obj.data.edit_bones.remove(bone)
                removed_bones += 1
    finally:
        target = prev_mode if prev_mode != "EDIT" else "OBJECT"
        if target not in {"OBJECT", "POSE", "EDIT"}:
            target = "OBJECT"
        bpy.ops.object.mode_set(mode="POSE" if target == "EDIT" else target)

    if "sub_finger_slider_travel" in armature_obj.data:
        del armature_obj.data["sub_finger_slider_travel"]
    if FINGER_SLIDER_USE_KEY in armature_obj.data:
        del armature_obj.data[FINGER_SLIDER_USE_KEY]
    _heal_finger_circle_disable(armature_obj)
    _restore_finger_circles_to_standard(armature_obj)
    return removed_bones + removed_drivers


def _iter_finger_slider_constraints(armature_obj):
    for pose_bone in armature_obj.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.name.startswith(FINGER_CON_PREFIX):
                yield pose_bone, constraint


def _local_xyz_euler(pose_bone):
    return pose_bone.matrix_basis.to_euler("XYZ")


def _set_local_xyz_euler(pose_bone, euler):
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.rotation_quaternion = euler.to_quaternion()
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        quat = euler.to_quaternion()
        pose_bone.rotation_axis_angle = (quat.angle, quat.axis.x, quat.axis.y, quat.axis.z)
    else:
        pose_bone.rotation_euler = euler


def _key_pose_rotation(pose_bone, frame):
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=pose_bone.name)
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        pose_bone.keyframe_insert("rotation_axis_angle", frame=frame, group=pose_bone.name)
    else:
        pose_bone.keyframe_insert("rotation_euler", frame=frame, group=pose_bone.name)
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=pose_bone.name)


def _axis_quat(axis_index, angle):
    axis = Vector((0.0, 0.0, 0.0))
    axis[int(axis_index)] = 1.0
    return Quaternion(axis, float(angle))


def _slider_loc_axis(constraint):
    if abs(float(constraint.from_max_x) - float(constraint.from_min_x)) > 1e-8:
        return 0
    return 1


def _constraint_from_range(constraint, loc_axis):
    if loc_axis == 0:
        return float(constraint.from_min_x), float(constraint.from_max_x)
    return float(constraint.from_min_y), float(constraint.from_max_y)


def _constraint_amount(slider, constraint, half):
    """Map slider location through the constraint's from-range.

    Symmetric Curl/Spread/Side use -1..1. Cascade windows use 0..1 within their slice.
    """
    loc_axis = _slider_loc_axis(constraint)
    loc = float(slider.location[loc_axis])
    from_min, from_max = _constraint_from_range(constraint, loc_axis)
    span = from_max - from_min
    if abs(span) < 1e-8:
        return 0.0
    # Cascade: one-sided window mapped 0→1
    if "Cascade" in (constraint.name or ""):
        t = (loc - from_min) / span
        return max(0.0, min(1.0, t))
    # Symmetric controls centered on travel half
    if half > 1e-8 and abs(from_min + half) < 1e-5 and abs(from_max - half) < 1e-5:
        return max(-1.0, min(1.0, loc / half))
    t = (loc - from_min) / span
    return max(-1.0, min(1.0, t * 2.0 - 1.0))


def _slider_constraint_quat(armature_obj, pose_bone, constraint, half):
    slider = armature_obj.pose.bones.get(constraint.subtarget)
    if slider is None or half <= 0.0:
        return Quaternion()
    amount = _constraint_amount(slider, constraint, half)
    if "Cascade" in (constraint.name or ""):
        axis = pose_bone.get("sub_finger_curl_axis")
        weight = pose_bone.get("sub_finger_curl_cascade_weight")
        if weight is None:
            weight = pose_bone.get("sub_finger_curl_weight")
    elif _slider_loc_axis(constraint) == 0 and "Side" in (constraint.name or ""):
        axis = pose_bone.get("sub_finger_side_axis")
        weight = pose_bone.get("sub_finger_side_weight")
    elif "Spread" in constraint.name:
        axis = pose_bone.get("sub_finger_spread_axis")
        weight = pose_bone.get("sub_finger_spread_weight")
    else:
        axis = pose_bone.get("sub_finger_curl_axis")
        weight = pose_bone.get("sub_finger_curl_weight")
    if axis is None or not weight:
        return Quaternion()
    return _axis_quat(axis, amount * float(weight))


def _finger_mix_is_add(pose_bone):
    for constraint in pose_bone.constraints:
        if not constraint.name.startswith(FINGER_CON_PREFIX):
            continue
        mode = str(
            getattr(constraint, "mix_mode_rot", None)
            or getattr(constraint, "mix_mode", "")
            or ""
        ).upper()
        return "ADD" in mode and "AFTER" not in mode
    return False


def _subtract_slider_constraints(armature_obj, pose_bone, captured_quat, half):
    """Undo TRANSFORM contributions so AFTER/ADD does not double the original curl."""
    if _finger_mix_is_add(pose_bone):
        euler = captured_quat.to_euler("XYZ")
        for constraint in pose_bone.constraints:
            if not constraint.name.startswith(FINGER_CON_PREFIX):
                continue
            slider = armature_obj.pose.bones.get(constraint.subtarget)
            if slider is None or half <= 0.0:
                continue
            amount = _constraint_amount(slider, constraint, half)
            if "Cascade" in (constraint.name or ""):
                axis = pose_bone.get("sub_finger_curl_axis")
                weight = pose_bone.get("sub_finger_curl_cascade_weight")
                if weight is None:
                    weight = pose_bone.get("sub_finger_curl_weight")
            elif _slider_loc_axis(constraint) == 0 and "Side" in (constraint.name or ""):
                axis = pose_bone.get("sub_finger_side_axis")
                weight = pose_bone.get("sub_finger_side_weight")
            elif "Spread" in constraint.name:
                axis = pose_bone.get("sub_finger_spread_axis")
                weight = pose_bone.get("sub_finger_spread_weight")
            else:
                axis = pose_bone.get("sub_finger_curl_axis")
                weight = pose_bone.get("sub_finger_curl_weight")
            if axis is None or not weight:
                continue
            euler[int(axis)] -= amount * float(weight)
        return euler.to_quaternion()

    residual = captured_quat.copy()
    for constraint in reversed(list(pose_bone.constraints)):
        if not constraint.name.startswith(FINGER_CON_PREFIX):
            continue
        residual = residual @ _slider_constraint_quat(
            armature_obj, pose_bone, constraint, half
        ).inverted()
    return residual


def _finger_pose_bones(armature_obj):
    return [pb for pb in armature_obj.pose.bones if parse_finger_bone(pb.name) is not None]


def _bone_chain_depth(pose_bone):
    depth = 0
    parent = pose_bone.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def is_finger_match_fcurve_path(path):
    """True for Smash Finger* keys (including metacarpals) and BL_* slider keys."""
    match = re.search(r'pose\.bones\[["\']([^"\']+)["\']', path or "")
    if not match:
        return False
    name = match.group(1)
    return is_finger_slider_bone(name) or parse_finger_bone(name) is not None or is_finger_circle_bone(name)


def bake_finger_slider_keys(
    context,
    armature_obj,
    progress=None,
    progress_start=0.0,
    progress_end=1.0,
):
    """Capture the posed fingers, remove slider constraints, then key that pose.

    Must not nla.bake while constraints are still on: IK bake already wrote the
    visual pose, and a second visual bake would add the sliders again.
    """
    scene = context.scene
    start, end = int(scene.frame_start), int(scene.frame_end)
    original = scene.frame_current
    fingers = _finger_pose_bones(armature_obj)
    if not fingers:
        return 0

    if context.mode != "POSE":
        bpy.ops.object.mode_set(mode="POSE")

    keyed = _bake_finger_visual_matrices(
        context,
        armature_obj,
        fingers,
        start,
        end,
        original,
        progress,
        progress_start,
        progress_end,
    )
    scene.frame_set(original)
    if progress is not None:
        progress.update(progress_end)
    return keyed


def _clear_finger_slider_constraints(armature_obj):
    for pose_bone, constraint in list(_iter_finger_slider_constraints(armature_obj)):
        pose_bone.constraints.remove(constraint)


def _bake_finger_visual_matrices(
    context,
    armature_obj,
    fingers,
    start,
    end,
    original,
    progress,
    progress_start,
    progress_end,
):
    """Sample visual matrices with sliders live, then key unconstrained local poses."""
    from . import pose_math
    from ..anim.fcurve_bulk import PoseKeyWriter

    scene = context.scene
    ordered = sorted(fingers, key=_bone_chain_depth)
    # Parents above the fingers keep their pose through the bake, so their
    # sampled matrices anchor the replay without needing an evaluation.
    sampled = ordered + [armature_obj.pose.bones[name]
                         for name in pose_math.reference_names(ordered)
                         if name in armature_obj.pose.bones]
    frames = []
    total = max(1, end - start + 1)
    keyed = 0
    try:
        for index, frame in enumerate(range(start, end + 1)):
            scene.frame_set(frame)
            context.view_layer.update()
            frames.append({pb.name: pb.matrix.copy() for pb in sampled})
            if progress is not None:
                factor = progress_start + ((index + 0.5) / total) * (progress_end - progress_start)
                progress.update(factor)
        _clear_finger_slider_constraints(armature_obj)
        context.view_layer.update()

        if pose_math.can_replay(ordered):
            # Every target matrix is known, so the local poses are arithmetic
            # and the keys go out one channel at a time instead of one bone
            # per frame. No depsgraph evaluation in this pass at all.
            writer = PoseKeyWriter(armature_obj)
            for index, (frame, visuals) in enumerate(zip(range(start, end + 1), frames)):
                for pose_bone in ordered:
                    parent = pose_bone.parent
                    basis = pose_math.basis_from_world(
                        pose_bone, visuals[pose_bone.name],
                        visuals.get(parent.name) if parent is not None else None)
                    writer.stash_matrix_basis(pose_bone, frame, basis)
                    keyed += 1
                if progress is not None:
                    factor = progress_start + ((index + 1) / total) * (progress_end - progress_start)
                    progress.update(factor)
            writer.flush()
            return keyed

        depths = {}
        for pose_bone in ordered:
            depths.setdefault(_bone_chain_depth(pose_bone), []).append(pose_bone)
        for index, (frame, visuals) in enumerate(zip(range(start, end + 1), frames)):
            scene.frame_set(frame)
            context.view_layer.update()
            for depth in sorted(depths):
                for pose_bone in depths[depth]:
                    matrix = visuals.get(pose_bone.name)
                    if matrix is not None:
                        pose_bone.matrix = matrix
                context.view_layer.update()
                for pose_bone in depths[depth]:
                    if pose_bone.name not in visuals:
                        continue
                    pose_bone.keyframe_insert("location", frame=frame, group=pose_bone.name)
                    pose_bone.keyframe_insert("scale", frame=frame, group=pose_bone.name)
                    _key_pose_rotation(pose_bone, frame)
                    keyed += 1
            if progress is not None:
                factor = progress_start + ((index + 1) / total) * (progress_end - progress_start)
                progress.update(factor)
    finally:
        scene.frame_set(original)
    return keyed


class SUB_OP_bake_finger_sliders(Operator):
    bl_idname = "sub.bake_finger_sliders"
    bl_label = "Bake Finger Sliders"
    bl_description = (
        "Write the posed fingers to quaternion keyframes so they export. "
        "Live sliders only drive the viewport"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        from .create_animation_rig import find_target_armature
        arm = find_target_armature(context)
        return arm is not None and has_finger_sliders(arm)

    def execute(self, context):
        from .create_animation_rig import ProgressCursor, find_target_armature, _activate_armature
        armature_obj = find_target_armature(context)
        if armature_obj is None:
            self.report({"ERROR"}, "Select a Smash Ultimate armature.")
            return {"CANCELLED"}
        _activate_armature(context, armature_obj)
        if context.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")
        with ProgressCursor(context) as progress:
            keyed = bake_finger_slider_keys(context, armature_obj, progress=progress)
        self.report(
            {"INFO"},
            f"Baked {keyed} finger keyframe(s). These are real quaternion keys, so they will export.",
        )
        return {"FINISHED"}


class SUB_OP_toggle_finger_sliders(Operator):
    bl_idname = "sub.toggle_finger_sliders"
    bl_label = "Switch Finger Sliders"
    bl_description = "Show finger sliders on the hands, or the Smash finger circle bones"
    bl_options = {"REGISTER", "UNDO"}

    set_enabled: bpy.props.BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    enable_sliders: bpy.props.BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        from .create_animation_rig import find_target_armature
        arm = find_target_armature(context)
        return arm is not None and has_finger_sliders(arm)

    def execute(self, context):
        from .create_animation_rig import find_target_armature
        armature_obj = find_target_armature(context)
        if armature_obj is None:
            self.report({"ERROR"}, "Select a Smash Ultimate armature.")
            return {"CANCELLED"}
        if self.set_enabled:
            use_sliders = bool(self.enable_sliders)
        else:
            use_sliders = not finger_sliders_are_enabled(armature_obj)
        set_finger_slider_mode(armature_obj, use_sliders, context)
        if context.view_layer is not None:
            context.view_layer.update()
        mode = "sliders" if use_sliders else "finger circles"
        self.report({"INFO"}, f"Switched fingers to {mode}.")
        return {"FINISHED"}
