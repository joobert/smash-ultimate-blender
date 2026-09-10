"""
Compatibility layer for bpy.types.Action F-Curves on Blender 4 and 5.

Blender 4.0–4.3 expose the legacy `action.fcurves` / `action.groups` API.
Blender 4.4 introduced layered Actions (layers, strips, slots, channelbags)
and kept `action.fcurves` as a first-slot wrapper.
Blender 5.0 removed that wrapper. Code that used `action.fcurves.new(...)`
must now go through a channelbag.

These helpers feature-detect the available API so one codebase works on both.
"""

import re

import bpy

from ..blender_compat import (
    assign_action,
    id_type_for_id_data,
    slot_display_name,
    slot_id_type,
    uses_legacy_action_fcurves,
)

_POSE_BONE_NAME = re.compile(r'^pose\.bones\[["\']([^"\']+)["\']')
_DUP_SUFFIX = re.compile(r'(?:\.\d{3})+$')


def _get_or_create_first_slot(action: bpy.types.Action, id_type: str = 'OBJECT', slot_name: str | None = None):
    matches = [slot for slot in action.slots if slot_id_type(slot) == id_type]
    if slot_name:
        for slot in matches:
            if slot_display_name(slot) == slot_name:
                return slot
        return action.slots.new(id_type, name=slot_name)
    # Prefer a named slot over Blender's generic / legacy placeholders so
    # keyframes land on the same slot assign_action() binds to the ID.
    for slot in matches:
        name = slot_display_name(slot)
        if name not in {"Slot", "Legacy Slot", ""}:
            return slot
    if matches:
        return matches[0]
    return action.slots.new(id_type, name=slot_name or "Slot")


def _get_or_create_channelbag(
    action: bpy.types.Action,
    id_type: str = 'OBJECT',
    ensure: bool = True,
    slot_name: str | None = None,
):
    if not ensure:
        if len(action.slots) == 0 or len(action.layers) == 0 or len(action.layers[0].strips) == 0:
            return None
        matches = [slot for slot in action.slots if slot_id_type(slot) == id_type]
        slot = None
        if slot_name:
            for candidate in matches:
                if slot_display_name(candidate) == slot_name:
                    slot = candidate
                    break
        else:
            for candidate in matches:
                name = slot_display_name(candidate)
                if name not in {"Slot", "Legacy Slot", ""}:
                    slot = candidate
                    break
            if slot is None and matches:
                slot = matches[0]
        if slot is None:
            slot = action.slots[0]
        strip = action.layers[0].strips[0]
        return strip.channelbag(slot, ensure=False)

    slot = _get_or_create_first_slot(action, id_type, slot_name=slot_name)

    if len(action.layers) == 0:
        layer = action.layers.new("Layer")
    else:
        layer = action.layers[0]

    if len(layer.strips) == 0:
        strip = layer.strips.new(type='KEYFRAME')
    else:
        strip = layer.strips[0]

    return strip.channelbag(slot, ensure=True)


def get_fcurves(action: bpy.types.Action, id_type: str = 'OBJECT'):
    """
    Equivalent of the old `action.fcurves` for the action's first slot.
    This is read-only: it does NOT create a layer/strip/slot/channelbag if
    none exist yet. Use new_fcurve() to create F-Curves.
    """
    if action is None:
        return []
    if uses_legacy_action_fcurves(action):
        return action.fcurves
    channelbag = _get_or_create_channelbag(action, id_type, ensure=False)
    return channelbag.fcurves if channelbag is not None else []


def _iter_channelbags(action: bpy.types.Action):
    """Yield existing channelbags for a layered action (Blender 5+)."""
    if action is None or uses_legacy_action_fcurves(action):
        return
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                channelbag = strip.channelbag(slot, ensure=False)
                if channelbag is not None:
                    yield channelbag


def _find_fcurve_in_channelbag(channelbag, fcurve):
    """Return the channelbag fcurve matching fcurve, if any."""
    if channelbag is None or fcurve is None:
        return None
    for fc in channelbag.fcurves:
        if fc == fcurve:
            return fc
        if fc.data_path == fcurve.data_path and fc.array_index == fcurve.array_index:
            return fc
    return None


def find_fcurve(action: bpy.types.Action, data_path: str, index: int = 0, id_type: str = 'OBJECT'):
    """Equivalent of the old `action.fcurves.find(...)`."""
    if action is None:
        return None
    if uses_legacy_action_fcurves(action):
        return action.fcurves.find(data_path, index=index)
    for channelbag in _iter_channelbags(action):
        found = channelbag.fcurves.find(data_path, index=index)
        if found is not None:
            return found
    channelbag = _get_or_create_channelbag(action, id_type, ensure=False)
    if channelbag is None:
        return None
    return channelbag.fcurves.find(data_path, index=index)


def get_all_action_fcurves(action: bpy.types.Action, id_type: str = 'OBJECT'):
    """Return every fcurve on an action across all layered slots."""
    if action is None:
        return []
    if uses_legacy_action_fcurves(action):
        return list(get_fcurves(action))

    fcurves = []
    seen = set()
    for channelbag in _iter_channelbags(action):
        for fc in channelbag.fcurves:
            key = (fc.data_path, fc.array_index)
            if key not in seen:
                seen.add(key)
                fcurves.append(fc)
    if not fcurves:
        for slot_type in ('ARMATURE', 'OBJECT', id_type):
            for fc in get_fcurves(action, id_type=slot_type):
                key = (fc.data_path, fc.array_index)
                if key not in seen:
                    seen.add(key)
                    fcurves.append(fc)
    return fcurves


def _key_lies_on_neighbors(prev, curr, nxt, threshold):
    interp = getattr(curr, "interpolation", "BEZIER") or "BEZIER"
    prev_val = prev.co[1]
    curr_val = curr.co[1]
    next_val = nxt.co[1]
    if interp == "CONSTANT":
        return (
            abs(curr_val - prev_val) <= threshold
            and abs(curr_val - next_val) <= threshold
        )
    span = nxt.co[0] - prev.co[0]
    if abs(span) < 1e-12:
        return abs(curr_val - prev_val) <= threshold
    t = (curr.co[0] - prev.co[0]) / span
    expected = prev_val + (next_val - prev_val) * t
    return abs(curr_val - expected) <= threshold


def clean_fcurve_redundant_keys(fcurve, threshold=1e-4):
    """Remove interior keys that sit on the interpolation between their neighbors."""
    if fcurve is None or getattr(fcurve, "lock", False):
        return 0
    points = getattr(fcurve, "keyframe_points", None)
    if points is None or len(points) < 3:
        return 0
    removed = 0
    index = 1
    while index < len(points) - 1:
        prev = points[index - 1]
        curr = points[index]
        nxt = points[index + 1]
        if _key_lies_on_neighbors(prev, curr, nxt, threshold):
            try:
                points.remove(curr, fast=True)
            except TypeError:
                points.remove(curr)
            removed += 1
            continue
        index += 1
    if removed:
        try:
            fcurve.update()
        except Exception:
            pass
    return removed


def clean_redundant_keys_on_id(id_data, threshold=1e-4, skip_data_path=None):
    """Clean baked-interpolation keys on every fcurve of this ID's action."""
    if id_data is None:
        return 0
    try:
        action = id_data.animation_data.action
    except AttributeError:
        return 0
    if action is None:
        return 0
    removed = 0
    for fcurve in get_all_action_fcurves(action, id_type=id_type_for_id_data(id_data)):
        if skip_data_path is not None and skip_data_path(fcurve.data_path or ""):
            continue
        removed += clean_fcurve_redundant_keys(fcurve, threshold)
    return removed


VISIBILITY_KEYFRAME_TYPE = 'EXTREME'
VISIBILITY_HANDLE_TYPE = 'VECTOR'
MATERIAL_KEYFRAME_TYPE = 'JITTER'
MATERIAL_HANDLE_TYPE = 'VECTOR'
FK_KEYFRAME_TYPE = 'BREAKDOWN'
FK_HANDLE_TYPE = 'FREE'
IK_KEYFRAME_TYPE = 'GENERATED'
IK_HANDLE_TYPE = 'FREE'
_VIS_VALUE_PATH = re.compile(
    r'^sub_anim_properties\.vis_track_entries\[\d+\]\.value$'
)
_MAT_PATH = re.compile(r'^sub_anim_properties\.mat_tracks\[\d+\]')
_IK_FK_PATH = re.compile(r'^sub_use_ik_(arms|legs)$')
_EYE_CTRL_BONE = 'BL_EyeLook'
_EYE_OPT_PREFIX = 'BL_Eye_'

VISIBILITY_GROUP = 'Visibility'
IK_FK_GROUP = 'FK to IK Swap'
# Blank-looking Dope Sheet row between pose animation and Visibility / IK switch.
DOPESHEET_SPACER_GROUP = '\u2003'
DOPESHEET_SPACER_KEY = 'sub_dopesheet_spacer'
DOPESHEET_SPACER_PATH = f'["{DOPESHEET_SPACER_KEY}"]'

# Dope Sheet keyframe type colors we temporarily apply (restored on unregister).
_VIS_COLOR = (1.0, 0.78, 0.86)
_VIS_COLOR_SEL = (1.0, 0.88, 0.93)
_IK_COLOR = (1.0, 0.12, 0.10)
_IK_COLOR_SEL = (1.0, 0.38, 0.32)
_EYE_COLOR = (0.28, 0.82, 0.38)
_EYE_COLOR_SEL = (0.48, 0.94, 0.55)
_THEME_BACKUP = None
_THEME_COLOR_KEYS = (
    'keyframe_extreme',
    'keyframe_extreme_selected',
    'keyframe_extreme_sel',
    'keyframe_generated',
    'keyframe_generated_selected',
    'keyframe_generated_sel',
    'keyframe_jitter',
    'keyframe_jitter_selected',
    'keyframe_jitter_sel',
)


def is_visibility_fcurve(fcurve):
    return fcurve is not None and bool(_VIS_VALUE_PATH.match(fcurve.data_path or ''))


def is_material_fcurve(fcurve):
    return fcurve is not None and bool(_MAT_PATH.match(fcurve.data_path or ''))


def is_ik_fk_fcurve(fcurve):
    return fcurve is not None and bool(_IK_FK_PATH.match(fcurve.data_path or ''))


def _canonical_fcurve_bone_name(name):
    if not name:
        return ''
    base = name
    while True:
        stripped = _DUP_SUFFIX.sub('', base)
        if stripped == base:
            return base
        base = stripped


def is_eye_control_bone_name(name):
    base = _canonical_fcurve_bone_name(name)
    return base == _EYE_CTRL_BONE or base.startswith(_EYE_OPT_PREFIX)


def is_eye_control_fcurve(fcurve):
    if fcurve is None:
        return False
    match = _POSE_BONE_NAME.match(fcurve.data_path or '')
    return bool(match and is_eye_control_bone_name(match.group(1)))


def _style_keyframe(keyframe, key_type=None, handle=None, interpolation=None):
    if interpolation is not None and getattr(keyframe, 'interpolation', None) != interpolation:
        try:
            keyframe.interpolation = interpolation
        except (AttributeError, TypeError, RuntimeError):
            pass
    if key_type is not None and getattr(keyframe, 'type', None) != key_type:
        try:
            keyframe.type = key_type
        except (AttributeError, TypeError, RuntimeError):
            pass
    if handle is not None:
        for attr in ('handle_left_type', 'handle_right_type'):
            if getattr(keyframe, attr, None) == handle:
                continue
            try:
                setattr(keyframe, attr, handle)
            except (AttributeError, TypeError, RuntimeError):
                pass


def _style_fcurve_keys(fcurve, key_type=None, handle=None, interpolation=None, key_type_for_value=None):
    if fcurve is None:
        return
    for keyframe in fcurve.keyframe_points:
        this_type = key_type
        this_handle = handle
        if key_type_for_value is not None:
            this_type, this_handle = key_type_for_value(keyframe.co[1])
        _style_keyframe(
            keyframe,
            key_type=this_type,
            handle=this_handle,
            interpolation=interpolation,
        )


def style_visibility_fcurve(fcurve):
    if not is_visibility_fcurve(fcurve):
        return
    _style_fcurve_keys(
        fcurve,
        key_type=VISIBILITY_KEYFRAME_TYPE,
        handle=VISIBILITY_HANDLE_TYPE,
    )


def style_material_fcurve(fcurve):
    if not is_material_fcurve(fcurve):
        return
    _style_fcurve_keys(
        fcurve,
        key_type=MATERIAL_KEYFRAME_TYPE,
        handle=MATERIAL_HANDLE_TYPE,
    )


def style_eye_control_fcurve(fcurve):
    if not is_eye_control_fcurve(fcurve):
        return
    _style_fcurve_keys(
        fcurve,
        key_type=MATERIAL_KEYFRAME_TYPE,
        handle=MATERIAL_HANDLE_TYPE,
    )


def style_eye_control_action(action):
    if action is None:
        return
    for fcurve in get_all_action_fcurves(action, id_type='OBJECT'):
        style_eye_control_fcurve(fcurve)
    apply_dopesheet_key_colors()


def style_ik_fk_fcurve(fcurve):
    if fcurve is None:
        return
    def _ik_fk_style(value):
        if value >= 0.5:
            return IK_KEYFRAME_TYPE, IK_HANDLE_TYPE
        return FK_KEYFRAME_TYPE, FK_HANDLE_TYPE
    _style_fcurve_keys(
        fcurve,
        interpolation='LINEAR',
        key_type_for_value=_ik_fk_style,
    )
    apply_dopesheet_key_colors()


def _iter_action_groups(action):
    if action is None:
        return
    if uses_legacy_action_fcurves(action):
        groups = getattr(action, 'groups', None)
        if groups is not None:
            yield groups
        return
    for channelbag in _iter_channelbags(action):
        groups = getattr(channelbag, 'groups', None)
        if groups is not None:
            yield groups


def _move_action_group(groups, name, to_index):
    names = [group.name for group in groups]
    if name not in names:
        return
    from_index = names.index(name)
    to_index = max(0, min(int(to_index), len(names) - 1))
    if from_index == to_index or not hasattr(groups, 'move'):
        return
    try:
        groups.move(from_index, to_index)
    except (AttributeError, TypeError, RuntimeError):
        pass


def order_armature_channel_groups(action):
    """Keep a blank row, then Visibility, then the IK/FK switch."""
    for groups in _iter_action_groups(action):
        names = [group.name for group in groups]
        if DOPESHEET_SPACER_GROUP in names:
            _move_action_group(groups, DOPESHEET_SPACER_GROUP, 0)
        names = [group.name for group in groups]
        if VISIBILITY_GROUP in names:
            vis_index = 1 if DOPESHEET_SPACER_GROUP in names else 0
            _move_action_group(groups, VISIBILITY_GROUP, vis_index)
        names = [group.name for group in groups]
        if IK_FK_GROUP in names and VISIBILITY_GROUP in names:
            _move_action_group(groups, IK_FK_GROUP, names.index(VISIBILITY_GROUP) + 1)


def ensure_dopesheet_visibility_spacer(action, armature_data=None):
    """Insert an empty Dope Sheet group above Visibility so pose keys sit apart."""
    if action is None:
        return
    has_vis = any(is_visibility_fcurve(fc) for fc in get_all_action_fcurves(action, id_type='ARMATURE'))
    if not has_vis:
        return
    if armature_data is not None:
        try:
            armature_data[DOPESHEET_SPACER_KEY] = 0.0
        except Exception:
            pass
    fcurve = find_fcurve(action, DOPESHEET_SPACER_PATH, index=0, id_type='ARMATURE')
    if fcurve is None:
        try:
            fcurve = new_fcurve(
                action,
                DOPESHEET_SPACER_PATH,
                index=0,
                action_group=DOPESHEET_SPACER_GROUP,
                id_type='ARMATURE',
            )
        except (AttributeError, TypeError, RuntimeError):
            return
    fcurve.mute = True
    try:
        fcurve.lock = True
    except (AttributeError, TypeError, RuntimeError):
        pass
    try:
        fcurve.hide = False
    except (AttributeError, TypeError, RuntimeError):
        pass
    group = getattr(fcurve, 'group', None)
    if group is not None:
        try:
            group.show_expanded = False
        except (AttributeError, TypeError, RuntimeError):
            pass
        try:
            group.lock = True
        except (AttributeError, TypeError, RuntimeError):
            pass
    order_armature_channel_groups(action)


def style_visibility_action(action, *, create_spacer=False):
    if action is None:
        return
    for fcurve in get_all_action_fcurves(action, id_type='ARMATURE'):
        style_visibility_fcurve(fcurve)
        style_material_fcurve(fcurve)
        if is_ik_fk_fcurve(fcurve):
            style_ik_fk_fcurve(fcurve)
    # Creating F-Curves here during depsgraph eval (SAP sync) crashes Blender 5
    # layered actions. Only insert the spacer from operators.
    if create_spacer:
        ensure_dopesheet_visibility_spacer(action)


def _dopesheet_theme():
    try:
        themes = bpy.context.preferences.themes
    except Exception:
        return None
    if not themes:
        return None
    theme = themes[0]
    common = getattr(theme, 'common', None)
    anim = getattr(common, 'anim', None) if common is not None else None
    if anim is not None:
        return anim
    return getattr(theme, 'dopesheet_editor', None) or getattr(theme, 'dopesheet', None)


def _copy_theme_color(theme, name):
    value = getattr(theme, name, None)
    if value is None:
        return None
    try:
        return tuple(value)
    except TypeError:
        return None


def _set_theme_color(theme, name, color):
    if getattr(theme, name, None) is None:
        return False
    try:
        current = getattr(theme, name)
        if hasattr(current, '__len__') and len(current) >= 3:
            current[0] = color[0]
            current[1] = color[1]
            current[2] = color[2]
            return True
    except Exception:
        pass
    try:
        setattr(theme, name, color)
        return True
    except (AttributeError, TypeError, RuntimeError):
        return False


def apply_dopesheet_key_colors():
    """Light pink for Extreme (visibility), red for Generated (IK), green for Jitter (eye look)."""
    global _THEME_BACKUP
    theme = _dopesheet_theme()
    if theme is None:
        return
    if _THEME_BACKUP is None:
        _THEME_BACKUP = {
            name: _copy_theme_color(theme, name) for name in _THEME_COLOR_KEYS
        }
    color_map = {
        'keyframe_extreme': _VIS_COLOR,
        'keyframe_extreme_selected': _VIS_COLOR_SEL,
        'keyframe_extreme_sel': _VIS_COLOR_SEL,
        'keyframe_generated': _IK_COLOR,
        'keyframe_generated_selected': _IK_COLOR_SEL,
        'keyframe_generated_sel': _IK_COLOR_SEL,
        'keyframe_jitter': _EYE_COLOR,
        'keyframe_jitter_selected': _EYE_COLOR_SEL,
        'keyframe_jitter_sel': _EYE_COLOR_SEL,
    }
    for name, color in color_map.items():
        current = _copy_theme_color(theme, name)
        if current is not None and len(current) >= 3 and len(color) >= 3:
            if all(abs(float(current[i]) - float(color[i])) < 1e-5 for i in range(3)):
                continue
        _set_theme_color(theme, name, color)


def restore_dopesheet_key_colors():
    global _THEME_BACKUP
    theme = _dopesheet_theme()
    if theme is None or _THEME_BACKUP is None:
        _THEME_BACKUP = None
        return
    for name, color in _THEME_BACKUP.items():
        if color is None:
            continue
        _set_theme_color(theme, name, color)
    _THEME_BACKUP = None


def _fcurve_resolves(path_resolve, fcurve):
    data_path = fcurve.data_path
    if fcurve.array_index:
        data_path = f"{data_path}[{fcurve.array_index}]"
    path_resolve(data_path)


def bone_names_from_action(action):
    """Extract pose bone names referenced by an action's fcurves."""
    names = set()
    for fc in get_all_action_fcurves(action):
        match = _POSE_BONE_NAME.match(fc.data_path)
        if match:
            names.add(match.group(1))
    return names


def action_matches_armature(action, armature):
    """True when the action animates at least one bone on the armature."""
    if action is None or armature is None:
        return False
    fcurves = get_all_action_fcurves(action)
    if not fcurves:
        return False
    armature_bones = {bone.name for bone in armature.data.bones}
    if bone_names_from_action(action) & armature_bones:
        return True
    path_resolve = armature.path_resolve
    for fc in fcurves:
        try:
            _fcurve_resolves(path_resolve, fc)
            return True
        except ValueError:
            continue
    return False


def action_matches_path_resolve(action, path_resolve):
    """True when at least one fcurve on the action resolves via path_resolve."""
    if action is None or path_resolve is None:
        return False
    fcurves = get_all_action_fcurves(action)
    if not fcurves:
        return False
    for fc in fcurves:
        try:
            _fcurve_resolves(path_resolve, fc)
            return True
        except ValueError:
            continue
    return False


def action_matches_id(action, armature_or_path_resolve):
    """Accept an armature object or a path_resolve callable."""
    if isinstance(armature_or_path_resolve, bpy.types.Object):
        return action_matches_armature(action, armature_or_path_resolve)
    return action_matches_path_resolve(action, armature_or_path_resolve)


def get_or_create_fcurves(action: bpy.types.Action, id_type: str = 'OBJECT', slot_name: str | None = None):
    """
    Like get_fcurves(), but ensures the layer/strip/slot/channelbag exist
    on Blender 5. On Blender 4 this is simply action.fcurves.
    """
    if action is None:
        return []
    if uses_legacy_action_fcurves(action):
        return action.fcurves
    channelbag = _get_or_create_channelbag(action, id_type, ensure=True, slot_name=slot_name)
    return channelbag.fcurves


def new_fcurve(
    action: bpy.types.Action,
    data_path: str,
    index: int = 0,
    action_group: str = '',
    id_type: str = 'OBJECT',
    slot_name: str | None = None,
) -> bpy.types.FCurve:
    """Equivalent of the old `action.fcurves.new(...)`."""
    if uses_legacy_action_fcurves(action):
        return action.fcurves.new(data_path, index=index, action_group=action_group)
    channelbag = _get_or_create_channelbag(action, id_type, ensure=True, slot_name=slot_name)
    return channelbag.fcurves.new(data_path, index=index, group_name=action_group)


def _ensure_accepts_group_name():
    """Whether Action.fcurve_ensure_for_datablock takes a group_name argument.

    Added alongside the method on Blender 5; on 4.x the method exists but has
    only datablock/data_path/index, so passing group_name raises TypeError.
    """
    function = bpy.types.Action.bl_rna.functions.get('fcurve_ensure_for_datablock')
    if function is None:
        return False
    return 'group_name' in function.parameters.keys()


def _assign_fcurve_group(action, fcurve, name):
    """Put an F-curve in a named channel group, on legacy or slotted actions."""
    if not name or fcurve is None or getattr(fcurve, 'group', None) is not None:
        return
    holders = list(_iter_channelbags(action))
    for holder in holders:
        if _find_fcurve_in_channelbag(holder, fcurve) is None:
            continue
        groups = getattr(holder, 'groups', None)
        if groups is None:
            return
        fcurve.group = groups.get(name) or groups.new(name)
        return
    groups = getattr(action, 'groups', None)
    if groups is not None:
        fcurve.group = groups.get(name) or groups.new(name)


def ensure_fcurve_for_datablock(
    action: bpy.types.Action,
    id_data,
    data_path: str,
    index: int = 0,
    action_group: str = '',
    id_type: str | None = None,
) -> bpy.types.FCurve:
    """
    Create/find an F-Curve bound to id_data's assigned action slot.

    On Blender 5 this uses Action.fcurve_ensure_for_datablock so keys land on
    the same slot the ID is evaluating, not a leftover Legacy Slot.
    """
    if action is None:
        raise ValueError("action is required")
    ensure = getattr(action, "fcurve_ensure_for_datablock", None)
    if ensure is not None and id_data is not None:
        # Action must already be assigned to id_data for this API.
        anim = getattr(id_data, "animation_data", None)
        if anim is not None and anim.action != action:
            assign_action(anim, action)
        if _ensure_accepts_group_name():
            return ensure(
                datablock=id_data,
                data_path=data_path,
                index=index,
                group_name=action_group,
            )
        # Blender 4.x exposes this method without group_name; group the curve
        # afterwards so channels still land under their bone in the dope sheet.
        fcurve = ensure(datablock=id_data, data_path=data_path, index=index)
        _assign_fcurve_group(action, fcurve, action_group)
        return fcurve
    if id_type is None:
        id_type = id_type_for_id_data(id_data) if id_data is not None else "OBJECT"
    slot_name = getattr(id_data, "name", None) if id_data is not None else None
    return new_fcurve(
        action,
        data_path,
        index=index,
        action_group=action_group,
        id_type=id_type,
        slot_name=slot_name,
    )


def get_fcurves_for_assigned_slot(id_data) -> list:
    """F-Curves from the channelbag currently driving id_data, if any."""
    anim = getattr(id_data, "animation_data", None) if id_data is not None else None
    if anim is None or anim.action is None:
        return []
    try:
        from bpy_extras import anim_utils

        channelbag = anim_utils.animdata_get_channelbag_for_assigned_slot(anim)
        if channelbag is not None:
            return list(channelbag.fcurves)
    except Exception:
        pass
    return list(get_all_action_fcurves(anim.action, id_type=id_type_for_id_data(id_data)))


def remove_fcurve(action: bpy.types.Action, fcurve: bpy.types.FCurve, id_type: str = 'OBJECT'):
    """Equivalent of the old `action.fcurves.remove(fcurve)`."""
    if action is None or fcurve is None:
        return
    if uses_legacy_action_fcurves(action):
        action.fcurves.remove(fcurve)
        return
    for channelbag in _iter_channelbags(action):
        match = _find_fcurve_in_channelbag(channelbag, fcurve)
        if match is not None:
            channelbag.fcurves.remove(match)
            return
    channelbag = _get_or_create_channelbag(action, id_type, ensure=False)
    if channelbag is not None:
        match = _find_fcurve_in_channelbag(channelbag, fcurve)
        if match is not None:
            channelbag.fcurves.remove(match)


def get_id_action_fcurves(id_data):
    """
    Return the mutable F-Curve collection for id_data.animation_data.action,
    or None if the ID has no action. Safe on Blender 4 and 5.
    """
    try:
        action = id_data.animation_data.action
    except AttributeError:
        return None
    if action is None:
        return None
    return get_or_create_fcurves(action, id_type=id_type_for_id_data(id_data))


def _action_valid_for_armature(action, armature):
    return action_matches_armature(action, armature)


def _is_bake_backup_action(action):
    name = action.name
    return "SAP Data" in name or "_old" in name


def action_has_pose_fcurves(action):
    """True when the action contains at least one pose-bone fcurve."""
    if action is None:
        return False
    for fc in get_all_action_fcurves(action):
        if fc.data_path.startswith('pose.bones['):
            return True
    return False


def sap_armature_name_for_action(bone_action_name):
    """
    Return the armature object name encoded in a SAP Data action name, if any.

    Import creates SAP actions named ``{armature} {anim_stem} SAP Data``.
    """
    sap_suffix = f" {bone_action_name} SAP Data"
    for sap_action in bpy.data.actions:
        if sap_action.name.endswith(sap_suffix):
            return sap_action.name[:-len(sap_suffix)]
    return None


def action_frame_range_safe(action):
    """Return a usable (start, end) frame range for baking."""
    if action is None:
        return 1, 1
    try:
        start, end = action.frame_range
        start, end = int(start), int(end)
        if end > start:
            return start, end
    except (AttributeError, TypeError, ValueError):
        pass

    frames = []
    for fc in get_all_action_fcurves(action):
        for kp in fc.keyframe_points:
            frames.append(kp.co[0])
    if frames:
        return int(min(frames)), int(max(frames))
    return 1, 1


def _expand_bake_armatures(armatures):
    """Include armatures referenced by SAP Data import pairs."""
    expanded = []
    seen = set()

    def add_armature(ob):
        if ob and getattr(ob, 'type', None) == 'ARMATURE' and ob.name not in seen:
            seen.add(ob.name)
            expanded.append(ob)

    for ob in armatures:
        add_armature(ob)

    for action in bpy.data.actions:
        if _is_bake_backup_action(action) or not action_has_pose_fcurves(action):
            continue
        armature_name = sap_armature_name_for_action(action.name)
        if not armature_name:
            continue
        add_armature(bpy.data.objects.get(armature_name))

    return expanded


def collect_actions_for_armatures(armatures):
    """
    Return actions that resolve on any of the given armatures and are eligible
    for retarget baking (skips SAP Data and _old backups).
    """
    armatures = _expand_bake_armatures(armatures)
    if not armatures:
        return []

    armature_names = {ob.name for ob in armatures}
    results = []
    seen = set()
    for action in bpy.data.actions:
        name = action.name
        if name in seen or _is_bake_backup_action(action):
            continue
        if any(_action_valid_for_armature(action, ob) for ob in armatures):
            seen.add(name)
            results.append(action)
            continue
        if not action_has_pose_fcurves(action):
            continue
        sap_armature = sap_armature_name_for_action(name)
        if sap_armature and sap_armature in armature_names:
            seen.add(name)
            results.append(action)
    return results


def collect_actions_for_bake(action_armature, bake_armature, extra_armatures=()):
    """Collect pose actions for a constrained/visual bake pair."""
    armatures = [action_armature, bake_armature, *extra_armatures]
    return collect_actions_for_armatures(armatures)
