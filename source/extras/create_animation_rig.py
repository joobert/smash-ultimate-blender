import contextlib
import math
import re

import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator
from mathutils import Vector

from . import fk_to_ik
from .ik_leg_placement import place_leg_ik_edit_bones
from ..blender_compat import (
    assign_action,
    assign_bone_to_collection,
    ensure_action_slot,
    ensure_bone_collection,
)
from ..anim.fcurve_compat import (
    clean_redundant_keys_on_id,
    ensure_dopesheet_visibility_spacer,
    find_fcurve,
    get_all_action_fcurves,
    new_fcurve,
    remove_fcurve,
    style_ik_fk_fcurve,
)

WIDGET_COLLECTION = "SUB_AnimRig_Widgets"
WIDGET_PREFIX = "SUB_WGT_"
ARMATURE_FLAG = "sub_animation_rig"
SMUSH_ARMATURE_PREFIX = "smush_blender_import"
IK_FK_GROUP = "FK to IK Swap"

_DUP_SUFFIX = re.compile(r'(?:\.\d{3})+$')
_SIDE_BONE = re.compile(
    r'^(Clavicle|Shoulder|Arm|Elbow|Wrist|Hand|Leg|Knee|Foot|Toe|Heel)([LR])(\d*)$'
)
_FINGER_BONE = re.compile(r'^Finger([LR])(\d+)$')
_IK_BONE = re.compile(r'^(Foot|Hand|Knee|Arm)IK([LR])(\d*)$')
_CUSTOM_SIDE = re.compile(r'([LR])\d*$')

_CENTER_BONES = {
    'Trans': ('crosshair', 'THEME09', 0.55, False),
    'Hip': ('box_waist', 'THEME09', 0.34, True),
    'HipN': ('circle', 'THEME09', 0.22, True),
    'Waist': ('circle', 'THEME09', 0.24, True),
    'Bust': ('circle', 'THEME09', 0.22, True),
    'Belly': ('circle', 'THEME09', 0.18, True),
    'ClavicleC': ('square', 'THEME06', 0.11, True),
    'LegC': ('square', 'THEME06', 0.13, True),
    'Neck': ('circle', 'THEME09', 0.11, True),
    'Head': ('circle', 'THEME09', 0.16, True),
    'Face': ('circle', 'THEME09', 0.10, True),
    'Jaw': ('bone_arrow', 'THEME09', 0.08, False),
    'Throw': ('diamond', 'THEME02', 0.14, False),
    'Have': ('sphere', 'THEME02', 0.08, False),
}

_SIDE_WIDGETS = {
    'Clavicle': ('circle', 0.11, True),
    'Shoulder': ('box', 0.13, True),
    'Arm': ('box', 0.10, True),
    'Elbow': ('circle', 0.08, True),
    'Wrist': ('circle', 0.07, True),
    'Hand': ('box', 0.12, False),
    'Leg': ('box', 0.14, True),
    'Knee': ('circle', 0.10, True),
    'Foot': ('foot', 0.18, False),
    'Toe': ('box_flat', 0.08, False),
    'Heel': ('box_flat', 0.09, False),
}

_IK_WIDGETS = {
    'Foot': ('foot', 0.22, False),
    'Hand': ('box', 0.16, False),
    'Knee': ('arrow', 0.14, False),
    'Arm': ('arrow', 0.14, False),
}

_HIDE_COLLECTIONS = (
    "Helper Bones",
    '"Exo" Helper Bones',
    "Swing Bones",
    "Null Swing Bones",
)

_LEFT_COLOR = 'THEME03'
_RIGHT_COLOR = 'THEME01'
_IK_COLOR = 'THEME04'
_CENTER_FALLBACK = 'THEME09'
_TRANS_AIM_BONE = 'BL_TransAim'


def canonical_bone_name(name):
    """Strip Blender duplicate suffixes (.001, .002) from a bone name."""
    if not name:
        return name
    return _DUP_SUFFIX.sub('', name)


def _connected_toe_side(bone):
    """Return L/R for a connected toe descendant rooted at ToeL/ToeR."""
    candidate = bone
    while candidate is not None:
        base = canonical_bone_name(candidate.name).lower()
        if base in {'toel', 'toer'}:
            return base[-1].upper() if 'toe' in canonical_bone_name(bone.name).lower() else None
        if not getattr(candidate, 'use_connect', False):
            break
        candidate = candidate.parent
    return None


def bone_name_suffix(name):
    base = canonical_bone_name(name)
    if not name or name == base:
        return ''
    return name[len(base):]


def find_target_armature(context):
    """Prefer the active/selected armature, including smush_blender_import.001 copies."""
    obj = getattr(context, 'object', None)
    if obj is not None:
        owner = obj.get('sub_floor_owner')
        if isinstance(owner, bpy.types.Object) and owner.type == 'ARMATURE':
            return owner
        if obj.type == 'ARMATURE':
            return obj
        if obj.type == 'MESH':
            arm = obj.find_armature()
            if arm is not None:
                return arm

    for selected in getattr(context, 'selected_objects', []) or []:
        if selected.type == 'ARMATURE':
            return selected

    smush = [
        obj for obj in context.scene.objects
        if obj.type == 'ARMATURE' and SMUSH_ARMATURE_PREFIX in obj.name.lower()
    ]
    if not smush:
        return None
    exact = [obj for obj in smush if obj.name.lower() == SMUSH_ARMATURE_PREFIX]
    if exact:
        return exact[0]
    smush.sort(key=lambda obj: obj.name)
    return smush[0]


def _pose_bone(armature_obj, base_name, suffix=''):
    return armature_obj.pose.bones.get(base_name + suffix)


def _edit_bone(armature, base_name, suffix=''):
    return armature.edit_bones.get(base_name + suffix)


def _estimate_character_scale(armature_obj):
    pose_bones = armature_obj.pose.bones
    hip = None
    head = None
    for pb in pose_bones:
        base = canonical_bone_name(pb.name)
        if base == 'Hip' and hip is None:
            hip = pb
        elif base == 'Head' and head is None:
            head = pb
    if hip is not None and head is not None:
        distance = (head.head - hip.head).length
        if distance > 0.05:
            return distance

    heads = [pb.head for pb in pose_bones]
    if not heads:
        return 1.0
    zs = [h.z for h in heads]
    height = max(zs) - min(zs)
    return height if height > 0.05 else 1.0


def _layer_collection_by_name(layer, name):
    if layer.name == name:
        return layer
    for child in layer.children:
        found = _layer_collection_by_name(child, name)
        if found is not None:
            return found
    return None


def _ensure_widget_collection(context):
    collection = bpy.data.collections.get(WIDGET_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(WIDGET_COLLECTION)
    scene_col = context.scene.collection
    if collection.name not in scene_col.children:
        try:
            scene_col.children.link(collection)
        except RuntimeError:
            pass
    collection.hide_viewport = True
    collection.hide_render = True
    layer = _layer_collection_by_name(context.view_layer.layer_collection, WIDGET_COLLECTION)
    if layer is not None:
        # Custom shapes must stay in the view layer. Excluding the collection
        # leaves a null object base and crashes Blender 5.2 while posing.
        layer.exclude = False
        layer.hide_viewport = True
    return collection


def _heal_widget_view_layer(context):
    if bpy.data.collections.get(WIDGET_COLLECTION) is None:
        return
    _ensure_widget_collection(context)


def _new_widget_mesh(name, verts, edges):
    mesh = bpy.data.meshes.get(name)
    if mesh is None:
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(verts, edges, [])
        mesh.update()
        return mesh
    if len(mesh.vertices) == 0:
        mesh.from_pydata(verts, edges, [])
        mesh.update()
    return mesh


def _ensure_widget_object(context, widget_id, verts, edges):
    name = WIDGET_PREFIX + widget_id
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.type == 'MESH':
        return obj

    mesh = _new_widget_mesh(name + "_mesh", verts, edges)
    obj = bpy.data.objects.new(name, mesh)
    obj.use_fake_user = True
    collection = _ensure_widget_collection(context)
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    obj.hide_viewport = True
    obj.hide_render = True
    obj.hide_select = True
    try:
        obj.hide_set(True)
    except RuntimeError:
        pass
    return obj


def _circle_geometry(radius=1.0, segments=32, plane='XZ'):
    verts = []
    for i in range(segments):
        angle = (2.0 * math.pi * i) / segments
        x = math.cos(angle) * radius
        y = math.sin(angle) * radius
        if plane == 'XZ':
            verts.append((x, 0.0, y))
        elif plane == 'XY':
            verts.append((x, y, 0.0))
        else:
            verts.append((0.0, x, y))
    edges = [(i, (i + 1) % segments) for i in range(segments)]
    return verts, edges


def _box_geometry(size_x=1.0, size_y=1.0, size_z=1.0):
    hx, hy, hz = size_x * 0.5, size_y * 0.5, size_z * 0.5
    verts = [
        (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
        (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
    ]
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    return verts, edges


def _arrow_geometry():
    verts = [
        (0.0, -0.6, 0.0),
        (0.0, 0.35, 0.0),
        (0.28, 0.05, 0.0),
        (-0.28, 0.05, 0.0),
        (0.0, 0.05, 0.28),
        (0.0, 0.05, -0.28),
        (0.0, 0.7, 0.0),
    ]
    edges = [
        (0, 1),
        (1, 2), (1, 3), (1, 4), (1, 5),
        (2, 6), (3, 6), (4, 6), (5, 6),
        (2, 4), (4, 3), (3, 5), (5, 2),
    ]
    return verts, edges


def _bone_arrow_geometry():
    """Arrow from the bone head (y=0) to the tail (y=1)."""
    verts = [
        (0.0, 0.0, 0.0),
        (0.0, 0.62, 0.0),
        (0.18, 0.48, 0.0),
        (-0.18, 0.48, 0.0),
        (0.0, 0.48, 0.18),
        (0.0, 0.48, -0.18),
        (0.0, 1.0, 0.0),
    ]
    edges = [
        (0, 1),
        (1, 2), (1, 3), (1, 4), (1, 5),
        (2, 6), (3, 6), (4, 6), (5, 6),
        (2, 4), (4, 3), (3, 5), (5, 2),
    ]
    return verts, edges


def _diamond_geometry():
    verts = [
        (0.0, 1.0, 0.0),
        (0.7, 0.0, 0.0),
        (0.0, 0.0, 0.7),
        (-0.7, 0.0, 0.0),
        (0.0, 0.0, -0.7),
        (0.0, -1.0, 0.0),
    ]
    edges = [
        (0, 1), (0, 2), (0, 3), (0, 4),
        (5, 1), (5, 2), (5, 3), (5, 4),
        (1, 2), (2, 3), (3, 4), (4, 1),
    ]
    return verts, edges


def _sphere_geometry(segments=16):
    verts = []
    edges = []
    for plane in ('XY', 'XZ', 'YZ'):
        start = len(verts)
        c_verts, c_edges = _circle_geometry(1.0, segments, plane)
        verts.extend(c_verts)
        edges.extend((a + start, b + start) for a, b in c_edges)
    return verts, edges


def _crosshair_geometry(segments=32):
    verts, edges = _circle_geometry(1.0, segments, 'XZ')
    inner, _ = _circle_geometry(0.72, max(12, segments // 2), 'XZ')
    offset = len(verts)
    verts.extend(inner)
    edges.extend((a + offset, b + offset) for a, b in _circle_geometry(0.72, max(12, segments // 2), 'XZ')[1])
    cross_start = len(verts)
    verts.extend([
        (-1.15, 0.0, 0.0), (1.15, 0.0, 0.0),
        (0.0, 0.0, -1.15), (0.0, 0.0, 1.15),
        (0.0, -0.2, 0.0), (0.0, 0.2, 0.0),
    ])
    edges.extend([
        (cross_start, cross_start + 1),
        (cross_start + 2, cross_start + 3),
        (cross_start + 4, cross_start + 5),
    ])
    return verts, edges


def _trans_orient_geometry():
    """Flat ground arrow showing Trans yaw (drawn red via BL_TransAim)."""
    # Along +Z on the XZ plane — with world_flat this matches Smash forward (-Y).
    verts = [
        (0.0, 0.0, -0.15),
        (0.0, 0.0, 1.65),
        (0.0, 0.0, 1.65),
        (0.32, 0.0, 1.22),
        (0.0, 0.0, 1.65),
        (-0.32, 0.0, 1.22),
        # Parallel stroke so the line reads clearly at a distance
        (0.05, 0.0, -0.15),
        (0.05, 0.0, 1.45),
        (-0.05, 0.0, -0.15),
        (-0.05, 0.0, 1.45),
    ]
    edges = [
        (0, 1),
        (2, 3),
        (4, 5),
        (6, 7),
        (8, 9),
    ]
    return verts, edges


def _foot_geometry():
    verts, edges = _box_geometry(0.85, 1.35, 0.28)
    # Forward chevron so the animator can see which way the foot faces.
    arrow_start = len(verts)
    verts.extend([
        (0.0, 0.72, 0.0),
        (0.22, 0.42, 0.0),
        (-0.22, 0.42, 0.0),
        (0.0, 0.95, 0.0),
    ])
    edges.extend([
        (arrow_start, arrow_start + 3),
        (arrow_start + 1, arrow_start + 3),
        (arrow_start + 2, arrow_start + 3),
        (arrow_start + 1, arrow_start + 2),
    ])
    return verts, edges


def _box_waist_geometry():
    return _box_geometry(1.25, 0.45, 1.05)


def _box_flat_geometry():
    return _box_geometry(1.0, 1.0, 0.35)


def _square_xy_geometry(size=1.0):
    s = float(size)
    verts = [
        (-s, -s, 0.0),
        (s, -s, 0.0),
        (s, s, 0.0),
        (-s, s, 0.0),
    ]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
    return verts, edges


def _square_xz_geometry(size=1.0):
    s = float(size)
    verts = [
        (-s, 0.0, -s),
        (s, 0.0, -s),
        (s, 0.0, s),
        (-s, 0.0, s),
    ]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
    return verts, edges


def _slider_geometry():
    verts, edges = _circle_geometry(0.42, 16, 'XZ')
    start = len(verts)
    verts.extend([
        (0.0, -1.2, 0.0),
        (0.0, 1.2, 0.0),
        (0.2, 0.88, 0.0),
        (-0.2, 0.88, 0.0),
        (0.2, -0.88, 0.0),
        (-0.2, -0.88, 0.0),
    ])
    edges.extend([
        (start, start + 1),
        (start + 1, start + 2),
        (start + 1, start + 3),
        (start, start + 4),
        (start, start + 5),
    ])
    return verts, edges


_LETTER_STROKES = {
    'A': (((0.0, 0.0), (0.5, 1.0), (1.0, 0.0)), ((0.2, 0.4), (0.8, 0.4))),
    'E': (((1.0, 1.0), (0.0, 1.0), (0.0, 0.0), (1.0, 0.0)), ((0.0, 0.5), (0.7, 0.5))),
    'I': (((0.15, 1.0), (0.85, 1.0)), ((0.5, 1.0), (0.5, 0.0)), ((0.15, 0.0), (0.85, 0.0))),
    'N': (((0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)),),
    'R': (((0.0, 0.0), (0.0, 1.0), (0.75, 1.0), (1.0, 0.75), (0.75, 0.5), (0.0, 0.5)), ((0.55, 0.5), (1.0, 0.0))),
    'T': (((0.0, 1.0), (1.0, 1.0)), ((0.5, 1.0), (0.5, 0.0))),
    'V': (((0.0, 1.0), (0.5, 0.0), (1.0, 1.0)),),
    'X': (((0.0, 1.0), (1.0, 0.0)), ((1.0, 1.0), (0.0, 0.0))),
    'Y': (((0.0, 1.0), (0.5, 0.5), (1.0, 1.0)), ((0.5, 0.5), (0.5, 0.0))),
}


def _append_letter_strokes(verts, edges, letter, origin_x, origin_z, size_x, size_z):
    for stroke in _LETTER_STROKES.get(letter, ()):
        start = len(verts)
        for px, py in stroke:
            verts.append((origin_x + px * size_x, 0.0, origin_z + py * size_z))
        for i in range(start, len(verts) - 1):
            edges.append((i, i + 1))


def _labeled_slider_geometry(label):
    verts, edges = _circle_geometry(0.16, 12, 'XZ')
    start = len(verts)
    verts.extend([
        (0.0, -0.45, 0.0),
        (0.0, 0.45, 0.0),
        (0.07, 0.32, 0.0),
        (-0.07, 0.32, 0.0),
        (0.07, -0.32, 0.0),
        (-0.07, -0.32, 0.0),
    ])
    edges.extend([
        (start, start + 1),
        (start + 1, start + 2),
        (start + 1, start + 3),
        (start, start + 4),
        (start, start + 5),
    ])
    text = (label or "").upper()
    if not text:
        return verts, edges
    char_w = 0.09
    char_h = 0.12
    gap = 0.025
    origin_x = 0.26
    origin_z = -char_h * 0.5
    for index, char in enumerate(text):
        if char == ' ':
            continue
        _append_letter_strokes(
            verts, edges, char,
            origin_x + index * (char_w + gap),
            origin_z, char_w, char_h,
        )
    return verts, edges


def labeled_slider_widget(context, label):
    widget_id = "slider_lbl_v2_" + "".join(ch if ch.isalnum() else "_" for ch in (label or "slider"))
    verts, edges = _labeled_slider_geometry(label)
    return _ensure_widget_object(context, widget_id, verts, edges)


class ProgressCursor:
    """Status-bar progress plus a changing mouse cursor, matching Bulk IK."""

    def __init__(self, context):
        self.context = context
        self._active = False

    def __enter__(self):
        try:
            self.context.window_manager.progress_begin(0, 100)
            self.context.window.cursor_modal_set("WAIT")
            self._active = True
        except Exception:
            self._active = False
        return self

    def update(self, factor):
        if not self._active:
            return
        factor = max(0.0, min(1.0, float(factor)))
        try:
            self.context.window_manager.progress_update(factor * 100.0)
        except Exception:
            pass
        try:
            window = self.context.window
            if factor < 0.25:
                window.cursor_modal_set("WAIT")
            elif factor < 0.5:
                window.cursor_modal_set("CROSSHAIR")
            elif factor < 0.75:
                window.cursor_modal_set("MOVE_X")
            else:
                window.cursor_modal_set("MOVE_Y")
        except Exception:
            pass

    def __exit__(self, exc_type, exc, tb):
        if not self._active:
            return False
        try:
            self.context.window_manager.progress_end()
        except Exception:
            pass
        try:
            self.context.window.cursor_modal_restore()
        except Exception:
            pass
        return False


_WIDGET_BUILDERS = {
    'circle': lambda: _circle_geometry(),
    'box': lambda: _box_geometry(),
    'box_waist': _box_waist_geometry,
    'box_flat': _box_flat_geometry,
    'arrow': _arrow_geometry,
    'bone_arrow': _bone_arrow_geometry,
    'diamond': _diamond_geometry,
    'sphere': _sphere_geometry,
    'crosshair': _crosshair_geometry,
    'trans_orient': _trans_orient_geometry,
    'foot': _foot_geometry,
    'slider': _slider_geometry,
    'square': lambda: _square_xz_geometry(1.0),
    'pad': lambda: _square_xy_geometry(1.0),
    'knob': lambda: _square_xy_geometry(1.0),
}


def _widget_object(context, widget_id):
    builder = _WIDGET_BUILDERS[widget_id]
    verts, edges = builder()
    return _ensure_widget_object(context, widget_id, verts, edges)


def _set_bone_color(pose_bone, palette):
    try:
        pose_bone.color.palette = palette
    except (AttributeError, TypeError):
        pass
    try:
        pose_bone.bone.color.palette = palette
    except (AttributeError, TypeError):
        pass


def _shape_rotation_world_up(pose_bone, armature_obj):
    """Rotate an XZ widget so it lies on the ground no matter how Trans is oriented."""
    bone_mat = pose_bone.bone.matrix_local.to_3x3()
    arm_mat = armature_obj.matrix_world.to_3x3()
    world_up = Vector((0.0, 0.0, 1.0))
    try:
        up_in_arm = arm_mat.inverted() @ world_up
        up_in_bone = bone_mat.inverted() @ up_in_arm
    except ValueError:
        return (0.0, 0.0, 0.0)
    if up_in_bone.length < 1e-6:
        return (0.0, 0.0, 0.0)
    up_in_bone.normalize()
    rotation = Vector((0.0, 1.0, 0.0)).rotation_difference(up_in_bone)
    return rotation.to_euler()


def _assign_shape(pose_bone, widget, scale, color, center_on_bone, armature_obj=None, world_flat=False, rotation_euler=None):
    pose_bone.custom_shape = widget
    pose_bone.use_custom_shape_bone_size = False
    if isinstance(scale, (int, float)):
        scale_xyz = (float(scale), float(scale), float(scale))
    else:
        scale_xyz = tuple(float(v) for v in scale)
    if hasattr(pose_bone, 'custom_shape_scale_xyz'):
        pose_bone.custom_shape_scale_xyz = scale_xyz
    if center_on_bone and hasattr(pose_bone, 'custom_shape_translation'):
        pose_bone.custom_shape_translation = (0.0, pose_bone.bone.length * 0.5, 0.0)
    elif hasattr(pose_bone, 'custom_shape_translation'):
        pose_bone.custom_shape_translation = (0.0, 0.0, 0.0)
    if hasattr(pose_bone, 'custom_shape_rotation_euler'):
        if rotation_euler is not None:
            pose_bone.custom_shape_rotation_euler = rotation_euler
        elif world_flat and armature_obj is not None:
            pose_bone.custom_shape_rotation_euler = _shape_rotation_world_up(pose_bone, armature_obj)
        else:
            pose_bone.custom_shape_rotation_euler = (0.0, 0.0, 0.0)
    pose_bone.bone.show_wire = True
    if hasattr(pose_bone, 'custom_shape_wire_width'):
        pose_bone.custom_shape_wire_width = 2.0
    if color:
        _set_bone_color(pose_bone, color)


def _should_hide_bone(base_name):
    if base_name.startswith('H_') or base_name.startswith('S_'):
        return True
    return base_name.endswith(('_eff', '_null', '_offset')) or base_name == 'Rot'


def _ensure_trans_aim_bone(context, armature_obj):
    """Visual-only red orientation arrow parented to Trans."""
    if armature_obj.pose.bones.get(_TRANS_AIM_BONE) is not None:
        return True

    prev_mode = armature_obj.mode
    _activate_armature(context, armature_obj)
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        armature = armature_obj.data
        trans = (
            _edit_bone(armature, 'Trans', '')
            or next(
                (b for b in armature.edit_bones if canonical_bone_name(b.name) == 'Trans'),
                None,
            )
        )
        if trans is None:
            return False
        if armature.edit_bones.get(_TRANS_AIM_BONE) is not None:
            return True
        aim = armature.edit_bones.new(_TRANS_AIM_BONE)
        aim.head = trans.head.copy()
        direction = trans.tail - trans.head
        if direction.length < 1e-6:
            direction = Vector((0.0, 1.0, 0.0))
        else:
            direction.normalize()
        aim.tail = aim.head + direction * max(trans.length * 0.05, 0.001)
        aim.roll = trans.roll
        aim.parent = trans
        aim.use_connect = False
        aim.use_deform = False
    finally:
        try:
            bpy.ops.object.mode_set(mode='POSE' if prev_mode == 'EDIT' else prev_mode)
        except Exception:
            bpy.ops.object.mode_set(mode='POSE')
    return armature_obj.pose.bones.get(_TRANS_AIM_BONE) is not None


def _classify_bone(base_name):
    if base_name == 'BL_EyeLook':
        return ('box', 'THEME03', 0.18, True)
    if base_name == _TRANS_AIM_BONE:
        return ('trans_orient', _RIGHT_COLOR, 0.55, False)
    if base_name.startswith('BL_') or _should_hide_bone(base_name):
        return None

    ik_match = _IK_BONE.match(base_name)
    if ik_match:
        kind, side = ik_match.group(1), ik_match.group(2)
        widget, mul, center = _IK_WIDGETS[kind]
        color = _IK_COLOR
        return widget, color, mul, center

    if base_name in _CENTER_BONES:
        return _CENTER_BONES[base_name]

    side_match = _SIDE_BONE.match(base_name)
    if side_match:
        kind, side, _digits = side_match.group(1), side_match.group(2), side_match.group(3)
        widget, mul, center = _SIDE_WIDGETS[kind]
        color = _LEFT_COLOR if side == 'L' else _RIGHT_COLOR
        return widget, color, mul, center

    finger_match = _FINGER_BONE.match(base_name)
    if finger_match:
        side = finger_match.group(1)
        color = _LEFT_COLOR if side == 'L' else _RIGHT_COLOR
        digit = finger_match.group(2)
        mul = 0.055 if digit.endswith('0') else 0.038
        return 'circle', color, mul, True

    if base_name.startswith('Eye'):
        return 'sphere', _CENTER_FALLBACK, 0.05, False

    return None


def _custom_bone_color(base_name):
    match = _CUSTOM_SIDE.search(base_name)
    if match:
        return _LEFT_COLOR if match.group(1) == 'L' else _RIGHT_COLOR
    return _CENTER_FALLBACK


def _suffix_groups(armature_obj):
    groups = {}
    for pose_bone in armature_obj.pose.bones:
        suffix = bone_name_suffix(pose_bone.name)
        groups.setdefault(suffix, []).append(pose_bone)
    return groups


def _looks_like_smash_armature(armature_obj):
    bases = {canonical_bone_name(pb.name) for pb in armature_obj.pose.bones}
    return 'Trans' in bases or 'Hip' in bases


def _set_collection_visible(armature_data, name, visible):
    collections = getattr(armature_data, 'collections_all', None) or getattr(
        armature_data, 'collections', None
    )
    if collections is None:
        return
    collection = None
    if hasattr(collections, 'get'):
        collection = collections.get(name)
    if collection is None:
        try:
            collection = collections[name]
        except (KeyError, TypeError, IndexError):
            for candidate in collections:
                if getattr(candidate, 'name', None) == name:
                    collection = candidate
                    break
    if collection is None:
        return
    if hasattr(collection, 'is_solo') and not visible:
        collection.is_solo = False
    if hasattr(collection, 'is_visible'):
        collection.is_visible = visible



def _hide_clutter(armature_obj):
    for name in _HIDE_COLLECTIONS:
        _set_collection_visible(armature_obj.data, name, False)
    for bone in armature_obj.data.bones:
        if _should_hide_bone(canonical_bone_name(bone.name)):
            bone.hide = True


def _restore_clutter(armature_obj):
    for name in _HIDE_COLLECTIONS:
        _set_collection_visible(armature_obj.data, name, True)
    for bone in armature_obj.data.bones:
        if _should_hide_bone(canonical_bone_name(bone.name)):
            bone.hide = False


def _apply_shapes(context, armature_obj):
    char_scale = _estimate_character_scale(armature_obj)
    _ensure_trans_aim_bone(context, armature_obj)
    widgets = {widget_id: _widget_object(context, widget_id) for widget_id in _WIDGET_BUILDERS}
    shaped = 0
    for pose_bone in armature_obj.pose.bones:
        base = canonical_bone_name(pose_bone.name)
        classification = _classify_bone(base)
        if classification is None:
            if base.startswith('BL_') or _should_hide_bone(base):
                continue
            length = max(pose_bone.length, 0.01)
            mul = max(0.045, min(0.14, (length / max(char_scale, 1e-6)) * 0.45))
            classification = ('circle', _custom_bone_color(base), mul, True)
        widget_id, color, mul, center = classification
        world_flat = base in {'Trans', _TRANS_AIM_BONE}
        scale = char_scale * mul
        if widget_id == 'bone_arrow':
            thickness = char_scale * mul
            scale = (
                thickness,
                max(pose_bone.bone.length, thickness * 1.6),
                thickness,
            )
            center = False
        _assign_shape(
            pose_bone,
            widgets[widget_id],
            scale,
            color,
            center,
            armature_obj,
            world_flat,
        )
        if base == _TRANS_AIM_BONE:
            try:
                pose_bone.bone.hide_select = True
            except Exception:
                pass
            # Keep locked so animators do not key the helper by accident
            try:
                pose_bone.lock_location = (True, True, True)
                pose_bone.lock_rotation = (True, True, True)
                pose_bone.lock_scale = (True, True, True)
            except Exception:
                pass
        shaped += 1
    return shaped


def apply_eye_look_shape(context, armature_obj):
    """Give BL_EyeLook the same wireframe box style as the rest of the rig."""
    pose_bone = armature_obj.pose.bones.get('BL_EyeLook')
    if pose_bone is None:
        return False
    char_scale = _estimate_character_scale(armature_obj)
    widget = _widget_object(context, 'box')
    _assign_shape(
        pose_bone,
        widget,
        char_scale * 0.18,
        'THEME03',
        True,
        armature_obj,
    )
    pose_bone.lock_location = (False, False, False)
    pose_bone.lock_rotation = (True, True, True)
    pose_bone.lock_rotation_w = True
    pose_bone.bone.hide = False
    collection = ensure_bone_collection(armature_obj.data, 'Eye Look')
    _set_collection_visible(armature_obj.data, 'Eye Look', True)
    if collection is not None:
        assign_bone_to_collection(collection, armature_obj.data.bones[pose_bone.name])
    return True


def apply_eye_option_shapes(context, armature_obj, ssp=None):
    """Invert X / Invert Y sliders on top of the eye box, like finger sliders on the hand."""
    from .eye_rig import EYE_OPTION_BONES, EYE_TOGGLE_TRAVEL_KEY, EYE_OPT_INVERT_X, EYE_OPT_INVERT_Y
    char_scale = _estimate_character_scale(armature_obj)
    box_scale = char_scale * 0.18
    travel = max(box_scale * 0.20, 0.008)
    armature_obj.data[EYE_TOGGLE_TRAVEL_KEY] = travel
    collection = ensure_bone_collection(armature_obj.data, 'Eye Look')
    _set_collection_visible(armature_obj.data, 'Eye Look', True)
    starts = {
        EYE_OPT_INVERT_X: bool(getattr(ssp, 'eye_look_invert_x', False)) if ssp else False,
        EYE_OPT_INVERT_Y: bool(getattr(ssp, 'eye_look_invert_y', False)) if ssp else False,
    }
    slider_scale = box_scale * 0.50
    for name, color, label in EYE_OPTION_BONES:
        pose_bone = armature_obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        pose_bone.lock_location = (True, False, True)
        pose_bone.lock_rotation = (True, True, True)
        pose_bone.lock_scale = (True, True, True)
        pose_bone.lock_rotation_w = True
        pose_bone.rotation_mode = 'XYZ'
        for constraint in list(pose_bone.constraints):
            if constraint.type == 'LIMIT_LOCATION':
                pose_bone.constraints.remove(constraint)
        limit = pose_bone.constraints.new('LIMIT_LOCATION')
        limit.owner_space = 'LOCAL'
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
        limit.min_y = 0.0
        limit.max_y = travel
        limit.use_transform_limit = True
        limit.name = 'Toggle Range'
        pose_bone.location.y = travel if starts.get(name, False) else 0.0
        pose_bone.bone.hide = False
        widget = labeled_slider_widget(context, label)
        _assign_shape(
            pose_bone,
            widget,
            slider_scale,
            color,
            False,
            armature_obj,
        )
        if collection is not None:
            assign_bone_to_collection(collection, armature_obj.data.bones[pose_bone.name])
    return True


def _ik_limb_kind(name):
    match = _IK_BONE.match(canonical_bone_name(name))
    if match is None:
        return None
    if match.group(1) in {'Hand', 'Arm'}:
        return 'ARMS'
    if match.group(1) in {'Foot', 'Knee'}:
        return 'LEGS'
    return None


def armature_has_animation_rig(armature_obj):
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return False
    return bool(armature_obj.data.get(ARMATURE_FLAG))


def armature_has_ik(armature_obj, limbs='BOTH'):
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return False
    from .ik_channels import chains
    if any(chains(armature_obj, limbs)):
        return True
    for pose_bone in armature_obj.pose.bones:
        kind = _ik_limb_kind(pose_bone.name)
        if kind is None:
            continue
        if limbs == 'BOTH' or kind == limbs:
            return True
    return False


def armature_ik_is_enabled(armature_obj, limbs='BOTH'):
    props = getattr(armature_obj, "data", None)
    if props is None:
        return False
    if limbs == 'ARMS':
        return float(getattr(props, "sub_use_ik_arms", 0.0) or 0.0) > 0.5
    if limbs == 'LEGS':
        return float(getattr(props, "sub_use_ik_legs", 0.0) or 0.0) > 0.5
    return (
        float(getattr(props, "sub_use_ik_arms", 0.0) or 0.0) > 0.5
        or float(getattr(props, "sub_use_ik_legs", 0.0) or 0.0) > 0.5
    )


def _iter_limb_ik_constraints(armature_obj, limbs='BOTH'):
    if armature_obj is not None and armature_obj.data.get("sub_independent_ik"):
        from .ik_channels import outputs
        for pb, con, _kind in outputs(armature_obj, limbs):
            yield pb, con
        return
    if armature_obj is None:
        return
    for pose_bone in armature_obj.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.type not in {'IK', 'COPY_ROTATION'}:
                continue
            kind = _ik_limb_kind(constraint.subtarget or '')
            if kind is None:
                continue
            if limbs != 'BOTH' and kind != limbs:
                continue
            yield pose_bone, constraint


def _parent_keep_matrix(edit_bone, parent):
    matrix = edit_bone.matrix.copy()
    edit_bone.parent = parent
    edit_bone.use_connect = False
    edit_bone.use_deform = False
    edit_bone.matrix = matrix


def _clear_ik_locks(pose_bone):
    if pose_bone is None:
        return
    pose_bone.lock_ik_x = False
    pose_bone.lock_ik_y = False
    pose_bone.lock_ik_z = False
    pose_bone.use_ik_limit_x = False
    pose_bone.use_ik_limit_y = False
    pose_bone.use_ik_limit_z = False


def _configure_hinge_ik(mid_pose, parent_pose, pole_pose):
    """
    Smash T-pose limbs are nearly straight, so Blender's IK solver will not
    choose an elbow/knee bend when the target slides along the chain (global X
    on a T-pose arm). Lock twist and allow a wide hinge so it can fold.
    """
    _clear_ik_locks(parent_pose)
    if mid_pose is None:
        return
    _clear_ik_locks(mid_pose)
    mid_pose.lock_ik_y = True
    mid_pose.ik_stiffness_y = 0.85

    pole_dir = Vector((0.0, 1.0, 0.0))
    if pole_pose is not None:
        offset = pole_pose.head - mid_pose.head
        if offset.length > 0.001:
            pole_dir = offset.normalized()

    axes = mid_pose.matrix.to_3x3()
    bone_y = Vector(axes.col[1]).normalized()
    hinge = bone_y.cross(pole_dir)
    if hinge.length < 0.05:
        hinge = Vector(axes.col[2]).copy()
    else:
        hinge.normalize()
    bone_x = Vector(axes.col[0])
    bone_z = Vector(axes.col[2])
    use_x = abs(hinge.dot(bone_x)) >= abs(hinge.dot(bone_z))
    limit = math.radians(175.0)
    if use_x:
        mid_pose.lock_ik_x = False
        mid_pose.lock_ik_z = True
        mid_pose.use_ik_limit_x = True
        mid_pose.ik_min_x = -limit
        mid_pose.ik_max_x = limit
    else:
        mid_pose.lock_ik_z = False
        mid_pose.lock_ik_x = True
        mid_pose.use_ik_limit_z = True
        mid_pose.ik_min_z = -limit
        mid_pose.ik_max_z = limit


def _configure_arm_ik_bend(arm_pose, shoulder_pose):
    """Straight T-pose arms will twist instead of folding unless twist is locked.

    Leave local X/Z free so the elbow can hinge toward the pole. iTaSC is not
    used; the working Create Arm IK operator uses Blender's legacy 2-bone solver.
    """
    for pose_bone in (shoulder_pose, arm_pose):
        _clear_ik_locks(pose_bone)
        if pose_bone is None:
            continue
        pose_bone.lock_ik_y = True
        pose_bone.ik_stiffness_y = 0.9
        pose_bone.ik_stiffness_x = 0.0
        pose_bone.ik_stiffness_z = 0.0
        pose_bone.ik_stretch = 0.0


def _use_legacy_ik_solver(armature):
    """The 2-bone + pole setup needs the legacy solver. iTaSC keeps a T-pose arm straight."""
    if not hasattr(armature, 'ik_solver'):
        return
    for name in ('LEGACY', 'STANDARD'):
        try:
            armature.ik_solver = name
            return
        except (TypeError, ValueError):
            continue


def _setup_ik_constraint(constraint, target, subtarget, pole_subtarget, pole_angle=0.0):
    constraint.target = target
    constraint.subtarget = subtarget
    constraint.pole_target = target
    constraint.pole_subtarget = pole_subtarget
    constraint.chain_count = 2
    constraint.pole_angle = pole_angle
    constraint.use_tail = True
    constraint.use_stretch = False
    constraint.weight = 1.0
    if hasattr(constraint, 'orient_weight'):
        constraint.orient_weight = 1.0
    constraint.iterations = 500


def _align_ik_to_source(ik_bone, source_bone, scale=1.5):
    """Keep HandIK/FootIK aligned with the actual hand/foot instead of pointing world-up."""
    ik_bone.head = source_bone.head.copy()
    direction = source_bone.tail - source_bone.head
    length = direction.length * scale
    if length < 0.05:
        length = max(source_bone.length, 0.15) * scale
    if direction.length < 0.001:
        direction = Vector((0.0, 1.0, 0.0))
    else:
        direction.normalize()
    ik_bone.tail = ik_bone.head + direction * length
    ik_bone.roll = source_bone.roll
    ik_bone.use_deform = False
    ik_bone.use_connect = False


def _arm_ik_pole_angle(side):
    """Smash left-arm rest rolls need -90; the right arm matches at 0.

    Same values as Create Arm IK Bones. A T-pose arm is straight, so there is
    no elbow bend to measure a pole angle from.
    """
    return math.radians(-90.0) if side == 'L' else 0.0


def _place_arm_pole(armature, side, suffix, arm, hand, ik_scale_factor, digits=''):
    name = f'ArmIK{side}{digits}{suffix}'
    arm_ik = armature.edit_bones.get(name)
    if arm_ik is None:
        arm_ik = armature.edit_bones.new(name)
    shoulder = _edit_bone(armature, f'Shoulder{side}{digits}', suffix)
    elbow = arm.head.copy()
    chain_start = shoulder.head if shoulder is not None else arm.head
    chain = hand.head - chain_start
    chain_dir = chain.normalized() if chain.length > 0.001 else Vector((1.0, 0.0, 0.0))
    # Smash characters face -Y; +Y is behind the back, where elbows should fold.
    char_back = Vector((0.0, 1.0, 0.0))
    pole_dir = char_back - char_back.project(chain_dir)
    if pole_dir.length < 0.01:
        pole_dir = Vector((0.0, 0.0, 1.0)) - Vector((0.0, 0.0, 1.0)).project(chain_dir)
    if pole_dir.length < 0.001:
        pole_dir = Vector((0.0, 1.0, 0.0))
    else:
        pole_dir.normalize()
    # Keep the pole far behind the elbow so a straight T-pose still has a
    # preferred bend direction. The older arm IK tools used a world Y of 4.
    pole_dist = max(chain.length if chain.length > 0.001 else arm.length, 1.0) * 1.5
    pole_len = max(arm.length * 0.2, 0.15) * ik_scale_factor
    arm_ik.head = elbow + pole_dir * pole_dist
    arm_ik.tail = arm_ik.head + pole_dir * pole_len
    arm_ik.use_deform = False
    arm_ik.use_connect = False
    return arm_ik


def _ensure_ik_controls(armature_obj, suffix):
    """Create Smash IK targets/poles for one suffix group when they are missing."""
    armature = armature_obj.data
    created = False
    ik_scale_factor = 1.5

    bpy.ops.object.mode_set(mode='EDIT')
    for bone in armature.edit_bones:
        bone.select = False
        bone.select_head = False
        bone.select_tail = False

    trans = _edit_bone(armature, 'Trans', suffix) or _edit_bone(armature, 'Trans', '')

    for side in ('L', 'R'):
        if (
            _edit_bone(armature, f'FootIK{side}', suffix) is None
            and _edit_bone(armature, f'KneeIK{side}', suffix) is None
        ):
            leg = _edit_bone(armature, f'Leg{side}', suffix)
            knee = _edit_bone(armature, f'Knee{side}', suffix)
            foot = _edit_bone(armature, f'Foot{side}', suffix)
            if leg and knee and foot:
                knee_ik, foot_ik = place_leg_ik_edit_bones(
                    armature, side, leg, knee, foot, ik_scale_factor
                )
                if suffix:
                    desired_knee = f'KneeIK{side}{suffix}'
                    desired_foot = f'FootIK{side}{suffix}'
                    if armature.edit_bones.get(desired_knee) is None:
                        knee_ik.name = desired_knee
                    if armature.edit_bones.get(desired_foot) is None:
                        foot_ik.name = desired_foot
                created = True

        foot = _edit_bone(armature, f'Foot{side}', suffix)
        foot_ik = _edit_bone(armature, f'FootIK{side}', suffix)
        if foot is not None and foot_ik is not None:
            _align_ik_to_source(foot_ik, foot, ik_scale_factor)

        arm = _edit_bone(armature, f'Arm{side}', suffix)
        hand = _edit_bone(armature, f'Hand{side}', suffix)
        if arm is not None and hand is not None:
            if _edit_bone(armature, f'ArmIK{side}', suffix) is None:
                created = True
            _place_arm_pole(armature, side, suffix, arm, hand, ik_scale_factor)
            hand_ik = _edit_bone(armature, f'HandIK{side}', suffix)
            if hand_ik is None:
                hand_ik = armature.edit_bones.new(f'HandIK{side}{suffix}')
                created = True
            _align_ik_to_source(hand_ik, hand, ik_scale_factor)

        for base in (f'FootIK{side}', f'KneeIK{side}', f'HandIK{side}', f'ArmIK{side}'):
            ik_bone = _edit_bone(armature, base, suffix)
            if ik_bone is None:
                continue
            _parent_keep_matrix(ik_bone, trans)

    bpy.ops.object.mode_set(mode='POSE')

    fk_matrices = {}
    for side in ('L', 'R'):
        hand_pose = _pose_bone(armature_obj, f'Hand{side}', suffix)
        foot_pose = _pose_bone(armature_obj, f'Foot{side}', suffix)
        if hand_pose:
            fk_matrices[f'Hand{side}'] = hand_pose.matrix.copy()
        if foot_pose:
            fk_matrices[f'Foot{side}'] = foot_pose.matrix.copy()

    for side in ('L', 'R'):
        knee_pose = _pose_bone(armature_obj, f'Knee{side}', suffix)
        foot_pose = _pose_bone(armature_obj, f'Foot{side}', suffix)
        arm_pose = _pose_bone(armature_obj, f'Arm{side}', suffix)
        hand_pose = _pose_bone(armature_obj, f'Hand{side}', suffix)
        foot_ik = _pose_bone(armature_obj, f'FootIK{side}', suffix)
        knee_ik = _pose_bone(armature_obj, f'KneeIK{side}', suffix)
        hand_ik = _pose_bone(armature_obj, f'HandIK{side}', suffix)
        arm_ik = _pose_bone(armature_obj, f'ArmIK{side}', suffix)

        if knee_pose and foot_ik and knee_ik:
            ik_con = next(
                (c for c in knee_pose.constraints if c.type == 'IK' and c.subtarget == foot_ik.name),
                None,
            )
            if ik_con is None:
                ik_con = knee_pose.constraints.new('IK')
            _setup_ik_constraint(ik_con, armature_obj, foot_ik.name, knee_ik.name, 0.0)
            _configure_hinge_ik(
                knee_pose,
                _pose_bone(armature_obj, f'Leg{side}', suffix),
                knee_ik,
            )
        if foot_pose and foot_ik:
            if not any(c.type == 'COPY_ROTATION' and c.subtarget == foot_ik.name for c in foot_pose.constraints):
                constraint = foot_pose.constraints.new('COPY_ROTATION')
                constraint.target = armature_obj
                constraint.subtarget = foot_ik.name
        if arm_pose and hand_ik and arm_ik:
            ik_con = next(
                (c for c in arm_pose.constraints if c.type == 'IK' and c.subtarget == hand_ik.name),
                None,
            )
            if ik_con is None:
                ik_con = arm_pose.constraints.new('IK')
            _setup_ik_constraint(
                ik_con, armature_obj, hand_ik.name, arm_ik.name, _arm_ik_pole_angle(side)
            )
            _configure_arm_ik_bend(
                arm_pose,
                _pose_bone(armature_obj, f'Shoulder{side}', suffix),
            )
        if hand_pose and hand_ik:
            if not any(c.type == 'COPY_ROTATION' and c.subtarget == hand_ik.name for c in hand_pose.constraints):
                constraint = hand_pose.constraints.new('COPY_ROTATION')
                constraint.target = armature_obj
                constraint.subtarget = hand_ik.name

    _use_legacy_ik_solver(armature)

    for side in ('L', 'R'):
        hand_ik = _pose_bone(armature_obj, f'HandIK{side}', suffix)
        foot_ik = _pose_bone(armature_obj, f'FootIK{side}', suffix)
        if hand_ik is not None and f'Hand{side}' in fk_matrices:
            hand_ik.matrix = fk_matrices[f'Hand{side}']
        if foot_ik is not None and f'Foot{side}' in fk_matrices:
            foot_ik.matrix = fk_matrices[f'Foot{side}']

    ik_collection = ensure_bone_collection(armature, "IK Bones")
    for bone in armature.bones:
        base = canonical_bone_name(bone.name)
        if _IK_BONE.match(base):
            assign_bone_to_collection(ik_collection, bone)
            try:
                bone.color.palette = 'THEME01'
            except (AttributeError, TypeError):
                pass
    return created


def _extra_arm_indices(armature_obj, suffix):
    found = []
    seen = set()
    pattern = re.compile(r'^Arm([LR])(\d+)$')
    for pose_bone in armature_obj.pose.bones:
        if bone_name_suffix(pose_bone.name) != suffix:
            continue
        match = pattern.match(canonical_bone_name(pose_bone.name))
        if not match:
            continue
        side, digits = match.group(1), match.group(2)
        key = (side, digits)
        if key in seen:
            continue
        if _pose_bone(armature_obj, f'Hand{side}{digits}', suffix) is None:
            continue
        seen.add(key)
        found.append(key)
    return found


def _ensure_extra_arm_ik(armature_obj):
    """Create HandIK/ArmIK for extra Smash arms (ArmL2, 4-arm characters, etc.)."""
    chains = []
    for suffix in _suffix_groups(armature_obj):
        for side, digits in _extra_arm_indices(armature_obj, suffix):
            chains.append((side, digits, suffix))
    if not chains:
        return False

    armature = armature_obj.data
    created = False
    ik_scale_factor = 1.5
    prev_mode = armature_obj.mode
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        for bone in armature.edit_bones:
            bone.select = False
            bone.select_head = False
            bone.select_tail = False
        for side, digits, suffix in chains:
            trans = _edit_bone(armature, 'Trans', suffix) or _edit_bone(armature, 'Trans', '')
            arm = _edit_bone(armature, f'Arm{side}{digits}', suffix)
            hand = _edit_bone(armature, f'Hand{side}{digits}', suffix)
            if arm is None or hand is None:
                continue
            if _edit_bone(armature, f'ArmIK{side}{digits}', suffix) is None:
                created = True
            _place_arm_pole(armature, side, suffix, arm, hand, ik_scale_factor, digits=digits)
            hand_ik = _edit_bone(armature, f'HandIK{side}{digits}', suffix)
            if hand_ik is None:
                hand_ik = armature.edit_bones.new(f'HandIK{side}{digits}{suffix}')
                created = True
            _align_ik_to_source(hand_ik, hand, ik_scale_factor)
            for base in (f'HandIK{side}{digits}', f'ArmIK{side}{digits}'):
                ik_bone = _edit_bone(armature, base, suffix)
                if ik_bone is not None and trans is not None:
                    _parent_keep_matrix(ik_bone, trans)
    finally:
        bpy.ops.object.mode_set(mode='POSE')

    fk_matrices = {}
    for side, digits, suffix in chains:
        hand_pose = _pose_bone(armature_obj, f'Hand{side}{digits}', suffix)
        if hand_pose is not None:
            fk_matrices[(side, digits, suffix)] = hand_pose.matrix.copy()

    for side, digits, suffix in chains:
        arm_pose = _pose_bone(armature_obj, f'Arm{side}{digits}', suffix)
        hand_pose = _pose_bone(armature_obj, f'Hand{side}{digits}', suffix)
        hand_ik = _pose_bone(armature_obj, f'HandIK{side}{digits}', suffix)
        arm_ik = _pose_bone(armature_obj, f'ArmIK{side}{digits}', suffix)
        if arm_pose and hand_ik and arm_ik:
            ik_con = next(
                (c for c in arm_pose.constraints if c.type == 'IK' and c.subtarget == hand_ik.name),
                None,
            )
            if ik_con is None:
                ik_con = arm_pose.constraints.new('IK')
            _setup_ik_constraint(
                ik_con, armature_obj, hand_ik.name, arm_ik.name, _arm_ik_pole_angle(side)
            )
            _configure_arm_ik_bend(
                arm_pose,
                _pose_bone(armature_obj, f'Shoulder{side}{digits}', suffix),
            )
        if hand_pose and hand_ik:
            if not any(c.type == 'COPY_ROTATION' and c.subtarget == hand_ik.name for c in hand_pose.constraints):
                constraint = hand_pose.constraints.new('COPY_ROTATION')
                constraint.target = armature_obj
                constraint.subtarget = hand_ik.name
        if hand_ik is not None and (side, digits, suffix) in fk_matrices:
            hand_ik.matrix = fk_matrices[(side, digits, suffix)]

    _use_legacy_ik_solver(armature)
    ik_collection = ensure_bone_collection(armature, "IK Bones")
    for bone in armature.bones:
        if _IK_BONE.match(canonical_bone_name(bone.name)):
            assign_bone_to_collection(ik_collection, bone)
            try:
                bone.color.palette = 'THEME01'
            except (AttributeError, TypeError):
                pass
    if prev_mode in {'OBJECT', 'POSE', 'EDIT'} and armature_obj.mode != prev_mode:
        try:
            bpy.ops.object.mode_set(mode=prev_mode if prev_mode != 'EDIT' else 'POSE')
        except Exception:
            pass
    return created


def _set_ik_bone_visibility(armature_obj, visible, limbs='BOTH'):
    from ..blender_compat import is_pose_bone_selected, set_pose_bone_select
    selected = [
        pose_bone.name
        for pose_bone in armature_obj.pose.bones
        if is_pose_bone_selected(pose_bone)
    ]
    active = armature_obj.data.bones.active
    active_name = active.name if active is not None else None
    any_ik_visible = False
    for bone in armature_obj.data.bones:
        kind = _ik_limb_kind(bone.name)
        if kind is None:
            continue
        if limbs == 'BOTH' or kind == limbs:
            bone.hide = not visible
        if not bone.hide:
            any_ik_visible = True
    _set_collection_visible(armature_obj.data, "IK Bones", any_ik_visible)
    for name in selected:
        pose_bone = armature_obj.pose.bones.get(name)
        if pose_bone is not None:
            set_pose_bone_select(pose_bone, True)
    if active_name:
        bone = armature_obj.data.bones.get(active_name)
        if bone is not None:
            armature_obj.data.bones.active = bone


def _ik_fk_chain_bones(armature_obj):
    names = []
    seen = set()
    for pose_bone in armature_obj.pose.bones:
        base = canonical_bone_name(pose_bone.name)
        match = _SIDE_BONE.match(base)
        if not match:
            continue
        if match.group(1) not in {'Shoulder', 'Arm', 'Hand', 'Leg', 'Knee', 'Foot'}:
            continue
        if pose_bone.name in seen:
            continue
        seen.add(pose_bone.name)
        names.append(pose_bone.name)
    return names


def _ik_control_bone_names(armature_obj, limbs='BOTH'):
    names = []
    for bone in armature_obj.pose.bones:
        match = _IK_BONE.match(canonical_bone_name(bone.name))
        if match is None:
            continue
        part = match.group(1)
        kind = 'ARMS' if part in {'Hand', 'Arm'} else 'LEGS'
        if limbs != 'BOTH' and kind != limbs:
            continue
        names.append(bone.name)
    return names


_POSE_FCURVE_BONE = re.compile(r'^pose\.bones\[["\']([^"\']+)["\']')
# Only location/rotation/scale — never constraint props (pole_angle, influence, …)
_POSE_TRANSFORM_FCURVE = re.compile(
    r'^pose\.bones\[["\']([^"\']+)["\']\]\.'
    r'(location|rotation_quaternion|rotation_euler|rotation_axis_angle|scale)'
    r'(?:$|\[)'
)
_HELD_FK_MUTE_KEY = "sub_ik_held_fk_fcurves"
_HELD_IK_MUTE_KEY = "sub_ik_held_ik_fcurves"
_IK_DRIVEN_FK_PARTS = {
    # Shoulder is in the Arm IK chain (chain_count=2 on Arm) — must mute/bake with arms.
    "Shoulder", "Arm", "Elbow", "Wrist", "Hand",
    "Leg", "Knee", "Foot", "Heel", "Toe",
}
_IK_DRIVEN_FK_ARMS = {"Shoulder", "Arm", "Elbow", "Wrist", "Hand"}


def _ik_driven_fk_bone_names(armature_obj, limbs='BOTH'):
    """Smash bones whose FK keys should rest while IK is solving."""
    names = []
    for pose_bone in armature_obj.pose.bones:
        match = _SIDE_BONE.match(canonical_bone_name(pose_bone.name))
        toe_side = _connected_toe_side(pose_bone.bone)
        if match and match.group(1) in _IK_DRIVEN_FK_PARTS:
            part = match.group(1)
            kind = 'ARMS' if part in _IK_DRIVEN_FK_ARMS else 'LEGS'
        elif toe_side:
            kind = 'LEGS'
        else:
            continue
        if limbs != 'BOTH' and kind != limbs:
            continue
        names.append(pose_bone.name)
    return names


def _fcurve_id(fcurve):
    return f"{fcurve.data_path}|{fcurve.array_index}"


def _is_pose_transform_fcurve(data_path):
    """True for bone transform channels only (not constraints like pole_angle)."""
    return bool(_POSE_TRANSFORM_FCURVE.match(data_path or ""))


def _iter_armature_actions(armature_obj):
    """Active action plus every NLA strip action (Animation Layers inclusive)."""
    actions = []
    seen = set()

    def _take(action):
        if action is None:
            return
        try:
            key = action.as_pointer()
        except (AttributeError, ReferenceError):
            return
        if key in seen:
            return
        seen.add(key)
        actions.append(action)

    anim = getattr(armature_obj, "animation_data", None)
    if anim is not None:
        _take(getattr(anim, "action", None))
        for track in getattr(anim, "nla_tracks", []) or []:
            for strip in getattr(track, "strips", []) or []:
                _take(getattr(strip, "action", None))

    # Animation Layers often leaves action=None; still mute the strip that drives the view.
    try:
        from . import anim_layers_compat
        driving, _slot = anim_layers_compat.viewport_driving_action(armature_obj)
        _take(driving)
    except Exception:
        pass
    return actions


def _bone_ik_fk_role(bone_name, armature_obj=None):
    """Return ('fk'|'ik', 'ARMS'|'LEGS') or None for bones involved in the switch."""
    base = canonical_bone_name(bone_name)
    ik_match = _IK_BONE.match(base)
    if ik_match:
        part = ik_match.group(1)
        return 'ik', ('ARMS' if part in {'Hand', 'Arm'} else 'LEGS')
    fk_match = _SIDE_BONE.match(base)
    if fk_match and fk_match.group(1) in _IK_DRIVEN_FK_PARTS:
        part = fk_match.group(1)
        return 'fk', ('ARMS' if part in _IK_DRIVEN_FK_ARMS else 'LEGS')
    bone = armature_obj.data.bones.get(bone_name) if armature_obj is not None else None
    if bone is not None and _connected_toe_side(bone):
        return 'fk', 'LEGS'
    return None


_IK_FK_APPLYING = False
_IK_VIS_CACHE = {}
_IK_FK_MUTE_SYNC_PAUSED = False
_IK_FK_FRAME_SYNC_BUSY = False
# Frames for Switch IK/FK to ease sub_use_ik_* from old mode to new mode.
IK_FK_BLEND_FRAMES = 5


def pause_ik_fk_mute_sync(paused=True):
    """Skip FK/IK mute sync (e.g. while FK→IK matching needs live FK evaluation)."""
    global _IK_FK_MUTE_SYNC_PAUSED
    _IK_FK_MUTE_SYNC_PAUSED = bool(paused)


@contextlib.contextmanager
def _disable_autokey(context=None):
    """Prevent Autokey from baking neutralized FK rest into the action."""
    if context is None:
        context = bpy.context
    tool_settings = getattr(context, "tool_settings", None)
    if tool_settings is None:
        yield
        return
    prev = bool(getattr(tool_settings, "use_keyframe_insert_auto", False))
    if prev:
        tool_settings.use_keyframe_insert_auto = False
    try:
        yield
    finally:
        if prev:
            try:
                tool_settings.use_keyframe_insert_auto = True
            except (AttributeError, RuntimeError):
                pass


def _ik_switch_factor(props, name, default=0.0):
    return float(getattr(props, name, default) or 0.0)


def _effective_limb_ik_factor(armature_obj, kind):
    """IK blend for a limb, or 0 if that limb has no IK controls on the rig.

    Prevents Legs-only IK from muting/neutralizing arm FK (which stretches the
    whole upper body when Arm IK bones are absent).
    """
    if armature_obj is None:
        return 0.0
    if kind == 'ARMS':
        if not armature_has_ik(armature_obj, 'ARMS'):
            return 0.0
        return _ik_switch_factor(armature_obj.data, "sub_use_ik_arms", 0.0)
    if kind == 'LEGS':
        if not armature_has_ik(armature_obj, 'LEGS'):
            return 0.0
        return _ik_switch_factor(armature_obj.data, "sub_use_ik_legs", 0.0)
    return 0.0


def _sync_ik_props_to_present_limbs(armature_obj):
    """Keep arm/leg switches off when that limb has no IK bones.

    Only writes when a missing limb still has a non-zero switch — never
    rewrite animated props every frame.
    """
    global _IK_FK_APPLYING
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    props = armature_obj.data
    has_arms = armature_has_ik(armature_obj, 'ARMS')
    has_legs = armature_has_ik(armature_obj, 'LEGS')
    fix_arms = (not has_arms) and _ik_switch_factor(props, "sub_use_ik_arms") != 0.0
    fix_legs = (not has_legs) and _ik_switch_factor(props, "sub_use_ik_legs") != 0.0
    if not fix_arms and not fix_legs:
        return
    _IK_FK_APPLYING = True
    try:
        if fix_arms:
            props.sub_use_ik_arms = 0.0
        if fix_legs:
            props.sub_use_ik_legs = 0.0
        arms_on = has_arms and _ik_switch_factor(props, "sub_use_ik_arms") > 0.5
        legs_on = has_legs and _ik_switch_factor(props, "sub_use_ik_legs") > 0.5
        props.sub_use_ik = 1.0 if (arms_on and legs_on) else 0.0
    finally:
        _IK_FK_APPLYING = False


def _enable_ik_props_for_present_limbs(armature_obj):
    """Turn IK on only for limbs that actually have IK bones."""
    global _IK_FK_APPLYING
    if armature_obj is None:
        return
    props = _ik_fk_props(armature_obj)
    has_arms = armature_has_ik(armature_obj, 'ARMS')
    has_legs = armature_has_ik(armature_obj, 'LEGS')
    _IK_FK_APPLYING = True
    try:
        props.sub_use_ik_arms = 1.0 if has_arms else 0.0
        props.sub_use_ik_legs = 1.0 if has_legs else 0.0
        props.sub_use_ik = 1.0 if (has_arms and has_legs) else 0.0
    finally:
        _IK_FK_APPLYING = False


def finalize_ik_controls(armature_obj, context=None):
    """Create independent solver chains, keeping FK active without switch keys."""
    from . import ik_channels
    if armature_obj is None or not armature_has_ik(armature_obj):
        return
    context = context or bpy.context
    ik_channels.ensure(armature_obj, context)
    armature_obj.data[ARMATURE_FLAG] = True


def _ik_mode_bucket(factor):
    """'fk', 'ik', or 'blend'."""
    if factor <= 1e-4:
        return 'fk'
    if factor >= 1.0 - 1e-4:
        return 'ik'
    return 'blend'


# Fully IK / fully FK thresholds — mid values must keep blending via influence.
_IK_ON_EPSILON = 1.0 - 1e-4
_FK_ON_EPSILON = 1e-4


def _want_mute_ik_fk_channel(channel, factor):
    """Mute inactive channel only at the ends so mid-blend can interpolate."""
    if channel == 'fk':
        # FK keys stay live until fully IK (rest-hold then locks them out).
        return factor >= _IK_ON_EPSILON
    if channel == 'ik':
        return factor <= _FK_ON_EPSILON
    return False


_CONSTRAINT_INFLUENCE_FCURVE = re.compile(
    r'^pose\.bones\[["\'][^"\']+["\']\]\.constraints\[["\'][^"\']+["\']\]\.influence$'
)
_FK_REST_HOLD_KEY = "sub_fk_rest_hold_limbs"


def _strip_competing_ik_influence_keys(armature_obj):
    """Remove keyed constraint.influence that fights the switch drivers.

    Legacy 'Toggle IK Influence' keys influence directly; those keys keep IK
    pulling while the switch props (and UI) say FK — classic FK+IK fight.
    """
    if armature_obj is None:
        return
    for action in _iter_armature_actions(armature_obj):
        for fcurve in list(get_all_action_fcurves(action, id_type='OBJECT')):
            path = fcurve.data_path or ""
            if not _CONSTRAINT_INFLUENCE_FCURVE.match(path):
                continue
            try:
                remove_fcurve(action, fcurve, id_type='OBJECT')
            except Exception:
                try:
                    fcurve.mute = True
                except Exception:
                    pass


def _remove_fk_rest_hold_drivers(pose_bone):
    """Remove rest-hold drivers so FK action keys can drive the bone again."""
    if pose_bone is None:
        return
    for prop in (
        "location",
        "scale",
        "rotation_quaternion",
        "rotation_euler",
        "rotation_axis_angle",
    ):
        try:
            pose_bone.driver_remove(prop)
        except (AttributeError, TypeError, RuntimeError):
            pass


def _install_fk_rest_hold_drivers(pose_bone):
    """Force local rest on a bone. Drivers override action keys — hard FK off."""
    if pose_bone is None:
        return
    _remove_fk_rest_hold_drivers(pose_bone)

    def _const(prop, index, value):
        try:
            fcurve = pose_bone.driver_add(prop, index)
        except (AttributeError, TypeError, RuntimeError):
            return
        driver = getattr(fcurve, "driver", None)
        if driver is None:
            return
        driver.type = 'SCRIPTED'
        driver.expression = repr(float(value))
        # No variables — pure constant rest.

    for i in range(3):
        _const("location", i, 0.0)
        _const("scale", i, 1.0)

    mode = pose_bone.rotation_mode
    if mode == 'QUATERNION':
        for i, value in enumerate((1.0, 0.0, 0.0, 0.0)):
            _const("rotation_quaternion", i, value)
    elif mode == 'AXIS_ANGLE':
        for i, value in enumerate((0.0, 0.0, 1.0, 0.0)):
            _const("rotation_axis_angle", i, value)
    else:
        for i in range(3):
            _const("rotation_euler", i, 0.0)


def _fk_rest_hold_token(arms_hold, legs_hold):
    parts = []
    if arms_hold:
        parts.append("ARMS")
    if legs_hold:
        parts.append("LEGS")
    return ",".join(parts)


def _set_fk_rest_hold(armature_obj, limbs, hold):
    """Hard-disable (hold=True) or re-enable FK transforms for limb FK bones.

    When hold is True, constant rest drivers override FK action keys so they
    cannot feed the IK solver. When False, drivers are removed and keys play.
    """
    if armature_obj is None:
        return
    for name in _ik_driven_fk_bone_names(armature_obj, limbs=limbs):
        pose_bone = armature_obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        if hold:
            _install_fk_rest_hold_drivers(pose_bone)
        else:
            _remove_fk_rest_hold_drivers(pose_bone)


def _sync_fk_rest_hold(armature_obj, force=False):
    if armature_obj.data.get("sub_independent_ik"):
        return
    """Keep FK rest-hold drivers in sync with the IK switch (true on/off)."""
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    if _IK_FK_MUTE_SYNC_PAUSED and not force:
        return
    # Only lock FK out at full IK so mid-blend influence can interpolate.
    arms_hold = (
        armature_has_ik(armature_obj, 'ARMS')
        and _effective_limb_ik_factor(armature_obj, 'ARMS') >= _IK_ON_EPSILON
    )
    legs_hold = (
        armature_has_ik(armature_obj, 'LEGS')
        and _effective_limb_ik_factor(armature_obj, 'LEGS') >= _IK_ON_EPSILON
    )
    token = _fk_rest_hold_token(arms_hold, legs_hold)
    data = armature_obj.data
    if not force and data.get(_FK_REST_HOLD_KEY) == token:
        return
    if armature_has_ik(armature_obj, 'ARMS'):
        _set_fk_rest_hold(armature_obj, 'ARMS', hold=arms_hold)
    if armature_has_ik(armature_obj, 'LEGS'):
        _set_fk_rest_hold(armature_obj, 'LEGS', hold=legs_hold)
    data[_FK_REST_HOLD_KEY] = token


def _clear_fk_rest_hold(armature_obj):
    """Remove all FK rest-hold drivers (bake / match / remove IK)."""
    if armature_obj is None:
        return
    for name in _ik_driven_fk_bone_names(armature_obj, limbs='BOTH'):
        _remove_fk_rest_hold_drivers(armature_obj.pose.bones.get(name))
    if _FK_REST_HOLD_KEY in armature_obj.data:
        del armature_obj.data[_FK_REST_HOLD_KEY]


def _sync_ik_fk_fcurve_mutes(armature_obj):
    if armature_obj.data.get("sub_independent_ik"):
        return
    """Mute inactive FK/IK transform keys per limb (arms vs legs independently).

    FK keys are never deleted. Limbs without IK controls are never muted by this.
    """
    if _IK_FK_MUTE_SYNC_PAUSED:
        return
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    arms_f = _effective_limb_ik_factor(armature_obj, 'ARMS')
    legs_f = _effective_limb_ik_factor(armature_obj, 'LEGS')
    for action in _iter_armature_actions(armature_obj):
        for fcurve in get_all_action_fcurves(action, id_type='OBJECT'):
            path = fcurve.data_path or ""
            match = _POSE_FCURVE_BONE.match(path)
            if match is None:
                continue
            role = _bone_ik_fk_role(match.group(1), armature_obj)
            if role is None:
                continue
            # Never mute constraint / custom props (pole_angle, influence, …)
            if not _is_pose_transform_fcurve(path):
                if fcurve.mute:
                    fcurve.mute = False
                continue
            channel, limbs = role
            limb_f = arms_f if limbs == 'ARMS' else legs_f
            # No IK for this limb → leave FK animation alone
            if channel == 'fk' and limb_f <= 0.0 and not armature_has_ik(armature_obj, limbs):
                if fcurve.mute:
                    fcurve.mute = False
                continue
            want_mute = _want_mute_ik_fk_channel(channel, limb_f)
            if bool(fcurve.mute) != want_mute:
                fcurve.mute = want_mute


def _enforce_ik_fk_channel_separation(armature_obj):
    """Re-apply per-limb mutes + FK rest hold."""
    if _IK_FK_MUTE_SYNC_PAUSED:
        return
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    _sync_ik_props_to_present_limbs(armature_obj)
    _sync_ik_fk_fcurve_mutes(armature_obj)
    _sync_fk_rest_hold(armature_obj)


@persistent
def _sync_ik_fk_visibility(scene, depsgraph=None):
    """Sync IK visibility / mutes / FK rest-hold only when the mode changes.

    Walking every fcurve every frame was tanking FPS (~20) in FK mode on Smash
    actions. Influence still blends via drivers without this handler.
    """
    global _IK_FK_FRAME_SYNC_BUSY
    if _IK_FK_MUTE_SYNC_PAUSED or _IK_FK_FRAME_SYNC_BUSY:
        return
    cache = _IK_VIS_CACHE
    _IK_FK_FRAME_SYNC_BUSY = True
    try:
        for obj in scene.objects:
            if obj.type != 'ARMATURE':
                continue
            if not armature_has_ik(obj):
                continue
            if obj.data.get("sub_independent_ik"):
                continue
            data = obj.data
            if not data.get(ARMATURE_FLAG):
                data[ARMATURE_FLAG] = True

            arms_f = _effective_limb_ik_factor(obj, 'ARMS')
            legs_f = _effective_limb_ik_factor(obj, 'LEGS')
            state = (_ik_mode_bucket(arms_f), _ik_mode_bucket(legs_f))
            key = obj.name
            prev = cache.get(key)

            if prev == state:
                continue

            cache[key] = state
            if prev is None:
                _strip_competing_ik_influence_keys(obj)
                _ensure_ik_influence_drivers(obj)

            if armature_has_ik(obj, 'ARMS'):
                _set_ik_bone_visibility(obj, arms_f > _FK_ON_EPSILON, 'ARMS')
            if armature_has_ik(obj, 'LEGS'):
                _set_ik_bone_visibility(obj, legs_f > _FK_ON_EPSILON, 'LEGS')

            _sync_all_limb_ik_constraint_mutes(obj)
            _sync_ik_fk_fcurve_mutes(obj)
            _sync_fk_rest_hold(obj, force=True)
            data["sub_ik_fk_mute_healed"] = True
    finally:
        _IK_FK_FRAME_SYNC_BUSY = False


def _apply_ik_fk_state(armature_obj, enabled, limbs='BOTH'):
    """Apply IK vs FK as a hard switch for the requested limbs."""
    del enabled  # limb on/off comes from the switch props (already set by caller)
    _strip_competing_ik_influence_keys(armature_obj)
    # Sync ALL limbs from their props so Arms-only toggles cannot leave Legs live.
    _sync_all_limb_ik_constraint_mutes(armature_obj)
    arms_f = _effective_limb_ik_factor(armature_obj, 'ARMS')
    legs_f = _effective_limb_ik_factor(armature_obj, 'LEGS')
    _IK_VIS_CACHE[armature_obj.name] = (_ik_mode_bucket(arms_f), _ik_mode_bucket(legs_f))
    _sync_ik_fk_fcurve_mutes(armature_obj)
    # Hard switch: IK on → FK transforms forced to rest (keys ignored).
    # FK on → rest drivers removed so keys drive again.
    _sync_fk_rest_hold(armature_obj, force=True)
    armature_obj.update_tag()


def unmute_all_ik_fk_fcurves(armature_obj):
    """Clear IK/FK mute state so bake can write and play FK keys."""
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    _clear_fk_rest_hold(armature_obj)
    for action in _iter_armature_actions(armature_obj):
        for fcurve in get_all_action_fcurves(action, id_type='OBJECT'):
            match = _POSE_FCURVE_BONE.match(fcurve.data_path or "")
            if match is None:
                continue
            if _bone_ik_fk_role(match.group(1), armature_obj) is None:
                continue
            if fcurve.mute:
                fcurve.mute = False
    for key in (_HELD_FK_MUTE_KEY, _HELD_IK_MUTE_KEY):
        if key in armature_obj.data:
            del armature_obj.data[key]


def _neutralize_fk_pose_for_ik(armature_obj, limbs='BOTH'):
    if armature_obj.data.get("sub_independent_ik"):
        return
    """Clear residual FK pose (used only when rest drivers are not yet installed)."""
    for name in _ik_driven_fk_bone_names(armature_obj, limbs=limbs):
        pose_bone = armature_obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        try:
            pose_bone.matrix_basis.identity()
        except Exception:
            pose_bone.location = (0.0, 0.0, 0.0)
            pose_bone.scale = (1.0, 1.0, 1.0)
            if pose_bone.rotation_mode == 'QUATERNION':
                pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            elif pose_bone.rotation_mode == 'AXIS_ANGLE':
                pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            else:
                pose_bone.rotation_euler = (0.0, 0.0, 0.0)


def _set_named_bone_fcurves_muted(armature_obj, bone_names, mute, held_key):
    """Mute or unmute transform fcurves for the given bones (all actions / NLA strips).

    Used temporarily by FK→IK matching; normal play uses `_sync_ik_fk_fcurve_mutes`.
    Constraint channels (pole_angle) are never muted.
    """
    names = set(bone_names)
    for action in _iter_armature_actions(armature_obj):
        held = [str(item) for item in armature_obj.data.get(held_key, [])]
        held_set = set(held)
        fcurves = get_all_action_fcurves(action, id_type='OBJECT')
        if mute:
            for fcurve in fcurves:
                path = fcurve.data_path or ""
                match = _POSE_TRANSFORM_FCURVE.match(path)
                if match is None or match.group(1) not in names:
                    continue
                curve_id = _fcurve_id(fcurve)
                if not fcurve.mute:
                    fcurve.mute = True
                    if curve_id not in held_set:
                        held.append(curve_id)
                        held_set.add(curve_id)
            armature_obj.data[held_key] = held
        else:
            for fcurve in fcurves:
                path = fcurve.data_path or ""
                match = _POSE_TRANSFORM_FCURVE.match(path)
                if match is None or match.group(1) not in names:
                    continue
                fcurve.mute = False
            if held_key in armature_obj.data:
                del armature_obj.data[held_key]


def _set_ik_driven_fcurves_muted(armature_obj, mute, limbs='BOTH'):
    """Temporarily mute/unmute limb FK curves (FK→IK match), without deleting keys."""
    _set_named_bone_fcurves_muted(
        armature_obj,
        _ik_driven_fk_bone_names(armature_obj, limbs=limbs),
        mute,
        _HELD_FK_MUTE_KEY,
    )


def _set_ik_control_fcurves_muted(armature_obj, mute, limbs='BOTH'):
    """Temporarily mute/unmute IK control curves (FK→IK match), without deleting keys."""
    _set_named_bone_fcurves_muted(
        armature_obj,
        _ik_control_bone_names(armature_obj, limbs=limbs),
        mute,
        _HELD_IK_MUTE_KEY,
    )


_IK_INFLUENCE_DRIVER_VAR = "use_ik"


def _constraint_influence_path(pose_bone, constraint):
    bone = pose_bone.name.replace('"', '\\"')
    name = constraint.name.replace('"', '\\"')
    return f'pose.bones["{bone}"].constraints["{name}"].influence'


def _find_object_driver(armature_obj, data_path):
    anim = getattr(armature_obj, "animation_data", None)
    if anim is None:
        return None
    for fcurve in anim.drivers:
        if fcurve.data_path == data_path and int(getattr(fcurve, "array_index", 0) or 0) == 0:
            return fcurve
    return None


def _limb_switch_prop(kind):
    if kind == 'ARMS':
        return "sub_use_ik_arms"
    if kind == 'LEGS':
        return "sub_use_ik_legs"
    return None


def _ensure_constraint_influence_driver(armature_obj, pose_bone, constraint, prop_name):
    path = _constraint_influence_path(pose_bone, constraint)
    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()
    fcurve = _find_object_driver(armature_obj, path)
    if fcurve is None:
        try:
            added = constraint.driver_add("influence")
        except (AttributeError, TypeError, RuntimeError):
            return
        fcurve = added[0] if isinstance(added, (list, tuple)) else added
    driver = getattr(fcurve, "driver", None)
    if driver is None:
        return
    driver.type = 'SCRIPTED'
    driver.expression = _IK_INFLUENCE_DRIVER_VAR
    var = None
    for existing in driver.variables:
        if existing.name == _IK_INFLUENCE_DRIVER_VAR:
            var = existing
            break
    if var is None:
        while driver.variables:
            driver.variables.remove(driver.variables[0])
        var = driver.variables.new()
        var.name = _IK_INFLUENCE_DRIVER_VAR
    var.type = 'SINGLE_PROP'
    target = var.targets[0]
    try:
        target.id_type = 'ARMATURE'
    except (AttributeError, TypeError):
        pass
    target.id = armature_obj.data
    target.data_path = prop_name


def _ensure_ik_influence_drivers(armature_obj):
    if armature_obj.data.get("sub_independent_ik"):
        from .ik_channels import wire
        wire(armature_obj)
        return
    """Drive IK influence from the keyed switch so playback actually changes mode."""
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    _strip_competing_ik_influence_keys(armature_obj)
    for pose_bone, constraint in _iter_limb_ik_constraints(armature_obj):
        kind = _ik_limb_kind(constraint.subtarget or "")
        prop_name = _limb_switch_prop(kind)
        if not prop_name:
            continue
        # Mute is owned by _set_limb_ik_constraints / _apply_ik_fk_state.
        _ensure_constraint_influence_driver(armature_obj, pose_bone, constraint, prop_name)


def _remove_ik_influence_drivers(armature_obj):
    if armature_obj is None:
        return
    for pose_bone, constraint in list(_iter_limb_ik_constraints(armature_obj)):
        try:
            constraint.driver_remove("influence")
        except (AttributeError, TypeError, RuntimeError):
            path = _constraint_influence_path(pose_bone, constraint)
            try:
                armature_obj.driver_remove(path, 0)
            except (AttributeError, TypeError, RuntimeError):
                pass


def _set_limb_ik_constraints(armature_obj, enabled, limbs='BOTH'):
    if armature_obj.data.get("sub_independent_ik"):
        return
    """Enable/disable IK constraints for a limb set (blend-safe)."""
    _ensure_ik_influence_drivers(armature_obj)
    for _pose_bone, constraint in _iter_limb_ik_constraints(armature_obj, limbs=limbs):
        # Only hard-mute at full FK; while blending, influence driver eases.
        constraint.mute = not enabled
        if enabled:
            constraint.mute = False
        else:
            constraint.mute = True
            constraint.influence = 0.0


def _sync_all_limb_ik_constraint_mutes(armature_obj):
    if armature_obj.data.get("sub_independent_ik"):
        return
    """Mute IK constraints only when fully FK; leave them live while blending."""
    if armature_obj is None:
        return
    _ensure_ik_influence_drivers(armature_obj)
    for kind in ('ARMS', 'LEGS'):
        if not armature_has_ik(armature_obj, kind):
            continue
        factor = _effective_limb_ik_factor(armature_obj, kind)
        fully_fk = factor <= _FK_ON_EPSILON
        for _pose_bone, constraint in _iter_limb_ik_constraints(armature_obj, limbs=kind):
            # During 0→1 blend, constraints stay unmuted so influence can ease.
            constraint.mute = fully_fk
            if fully_fk:
                constraint.influence = 0.0
        _set_ik_bone_visibility(armature_obj, factor > _FK_ON_EPSILON, kind)


def _object_for_armature_data(armature_data, context=None):
    if armature_data is None:
        return None
    if context is not None:
        view_layer = getattr(context, "view_layer", None)
        if view_layer is not None:
            for obj in view_layer.objects:
                if obj.type == 'ARMATURE' and obj.data == armature_data:
                    return obj
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.data == armature_data:
            return obj
    return None


def _ik_fk_props(armature_obj):
    return armature_obj.data


def _update_use_ik_limb(armature_obj, enabled, limbs):
    global _IK_FK_APPLYING
    if _IK_FK_APPLYING or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    if not armature_has_ik(armature_obj, limbs):
        return
    _IK_FK_APPLYING = True
    try:
        _apply_ik_fk_state(armature_obj, enabled, limbs=limbs)
    finally:
        _IK_FK_APPLYING = False


def _update_use_ik_arms(self, context):
    obj = _object_for_armature_data(self, context)
    if obj is None:
        return
    _update_use_ik_limb(obj, float(self.sub_use_ik_arms) > 0.5, 'ARMS')


def _update_use_ik_legs(self, context):
    obj = _object_for_armature_data(self, context)
    if obj is None:
        return
    _update_use_ik_limb(obj, float(self.sub_use_ik_legs) > 0.5, 'LEGS')


def _update_use_ik(self, context):
    global _IK_FK_APPLYING
    obj = _object_for_armature_data(self, context)
    if _IK_FK_APPLYING or obj is None:
        return
    if not armature_has_ik(obj):
        return
    enabled = float(self.sub_use_ik) > 0.5
    _IK_FK_APPLYING = True
    try:
        self.sub_use_ik_arms = 1.0 if enabled else 0.0
        self.sub_use_ik_legs = 1.0 if enabled else 0.0
        _apply_ik_fk_state(obj, enabled, limbs='BOTH')
    finally:
        _IK_FK_APPLYING = False


def _set_ik_enabled(context, armature_obj, enabled, limbs='BOTH'):
    """Toggle IK vs FK without copying or keying one onto the other."""
    global _IK_FK_APPLYING
    from ..blender_compat import is_pose_bone_selected, set_pose_bone_select
    selected = [
        pose_bone.name
        for pose_bone in armature_obj.pose.bones
        if is_pose_bone_selected(pose_bone)
    ]
    active = armature_obj.data.bones.active
    active_name = active.name if active is not None else None
    _activate_armature(context, armature_obj)
    if context.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')
    props = _ik_fk_props(armature_obj)
    _IK_FK_APPLYING = True
    try:
        if limbs in {'ARMS', 'BOTH'} and armature_has_ik(armature_obj, 'ARMS'):
            props.sub_use_ik_arms = 1.0 if enabled else 0.0
        if limbs in {'LEGS', 'BOTH'} and armature_has_ik(armature_obj, 'LEGS'):
            props.sub_use_ik_legs = 1.0 if enabled else 0.0
        _sync_ik_props_to_present_limbs(armature_obj)
        props.sub_use_ik = 1.0 if (
            _effective_limb_ik_factor(armature_obj, 'ARMS') > 0.5
            and _effective_limb_ik_factor(armature_obj, 'LEGS') > 0.5
        ) else 0.0
        _apply_ik_fk_state(armature_obj, enabled, limbs=limbs)
    finally:
        _IK_FK_APPLYING = False
    # Switching to FK: force a re-eval so unmuted FK keys restore the pose
    # (bones may still be at the neutralized rest left over from IK mode).
    if not enabled:
        frame = context.scene.frame_current
        try:
            context.scene.frame_set(frame)
        except Exception:
            context.view_layer.update()
    else:
        context.view_layer.update()
    for name in selected:
        pose_bone = armature_obj.pose.bones.get(name)
        if pose_bone is not None:
            set_pose_bone_select(pose_bone, True)
    if active_name:
        bone = armature_obj.data.bones.get(active_name)
        if bone is not None:
            armature_obj.data.bones.active = bone


def _iter_action_group_collections(action):
    if action is None:
        return
    groups = getattr(action, "groups", None)
    if getattr(action, "fcurves", None) is not None and groups is not None:
        yield groups
        return
    layers = getattr(action, "layers", None)
    slots = getattr(action, "slots", None)
    if not layers or not slots:
        return
    for layer in layers:
        strips = getattr(layer, "strips", None)
        if not strips:
            continue
        for strip in strips:
            for slot in slots:
                try:
                    bag = strip.channelbag(slot, ensure=False)
                except (AttributeError, TypeError, RuntimeError):
                    continue
                if bag is None:
                    continue
                bag_groups = getattr(bag, "groups", None)
                if bag_groups is not None:
                    yield bag_groups


def _place_ik_fk_group_after_visibility(action):
    ensure_dopesheet_visibility_spacer(action)


def _ensure_fcurve_in_group(action, fcurve, group_name):
    if fcurve is None:
        return
    current = getattr(fcurve, "group", None)
    if current is not None and current.name == group_name:
        return
    for groups in _iter_action_group_collections(action):
        group = None
        if hasattr(groups, "get"):
            group = groups.get(group_name)
        if group is None:
            group = next((item for item in groups if item.name == group_name), None)
        if group is None:
            try:
                group = groups.new(group_name)
            except (AttributeError, TypeError, RuntimeError):
                continue
        try:
            fcurve.group = group
            return
        except (AttributeError, TypeError, RuntimeError):
            continue


def _style_ik_fk_fcurve(fcurve):
    style_ik_fk_fcurve(fcurve)


def _ensure_armature_sap_action(armature_obj):
    data = armature_obj.data
    if data.animation_data is None:
        data.animation_data_create()
    action = data.animation_data.action
    if action is None:
        bone_action = None
        if armature_obj.animation_data is not None:
            bone_action = armature_obj.animation_data.action
        if bone_action is not None:
            sap_name = f"{armature_obj.name} {bone_action.name} SAP Data"
        else:
            sap_name = f"{armature_obj.name} SAP Data"
        action = bpy.data.actions.get(sap_name)
        if action is None:
            action = bpy.data.actions.new(sap_name)
        ensure_action_slot(action, data)
        assign_action(data.animation_data, action)
    else:
        ensure_action_slot(action, data)
    return action, data


def _bool_switch_fcurve(armature_obj, data_path):
    action, _data = _ensure_armature_sap_action(armature_obj)
    fcurve = find_fcurve(action, data_path, index=0, id_type='ARMATURE')
    if fcurve is None:
        fcurve = find_fcurve(action, data_path, index=0)
    if fcurve is None:
        fcurve = new_fcurve(action, data_path, index=0, action_group=IK_FK_GROUP, id_type='ARMATURE')
    _ensure_fcurve_in_group(action, fcurve, IK_FK_GROUP)
    try:
        fcurve.extrapolation = 'CONSTANT'
    except (AttributeError, TypeError, RuntimeError):
        pass
    return action, fcurve


def _set_bool_switch_key(fcurve, frame, value):
    value = 1.0 if float(value) >= 0.5 else 0.0
    existing = None
    for keyframe in fcurve.keyframe_points:
        if abs(keyframe.co[0] - frame) < 0.001:
            existing = keyframe
            break
    if existing is None:
        try:
            existing = fcurve.keyframe_points.insert(frame, value)
        except (AttributeError, TypeError, RuntimeError):
            existing = None
        if existing is None:
            for keyframe in fcurve.keyframe_points:
                if abs(keyframe.co[0] - frame) < 0.001:
                    existing = keyframe
                    break
    if existing is None:
        return
    existing.co[1] = value
    # Linear on every switch key so playback eases between opposite switches.
    for keyframe in fcurve.keyframe_points:
        keyframe.interpolation = 'LINEAR'
        keyframe.handle_left_type = 'AUTO'
        keyframe.handle_right_type = 'AUTO'
    try:
        existing.type = 'GENERATED' if value >= 0.5 else 'BREAKDOWN'
    except (AttributeError, TypeError, RuntimeError):
        pass
    try:
        fcurve.update()
    except Exception:
        pass


def _key_bool_prop(armature_obj, data_path, frame, old_value=None, new_value=None, blend_frames=None):
    """Key only the new IK/FK value at this frame.

    Interpolation comes from the previous switch key on the curve (another
    frame), not from auto-inserting a paired key a few frames away.
    """
    del old_value, blend_frames  # kept for call-site compatibility
    if new_value is None:
        new_value = float(getattr(armature_obj.data, data_path) or 0.0) >= 0.5
    new_f = 1.0 if new_value else 0.0
    action, fcurve = _bool_switch_fcurve(armature_obj, data_path)
    _set_bool_switch_key(fcurve, float(frame), new_f)
    try:
        fcurve.extrapolation = 'CONSTANT'
    except (AttributeError, TypeError, RuntimeError):
        pass
    try:
        fcurve.update()
    except Exception:
        pass
    _style_ik_fk_fcurve(fcurve)
    _place_ik_fk_group_after_visibility(action)


def _key_use_ik(armature_obj, frame, limbs='BOTH', old_arms=None, old_legs=None, enabled=None):
    if limbs in {'ARMS', 'BOTH'} and armature_has_ik(armature_obj, 'ARMS'):
        _key_bool_prop(
            armature_obj,
            "sub_use_ik_arms",
            frame,
            new_value=enabled,
        )
    if limbs in {'LEGS', 'BOTH'} and armature_has_ik(armature_obj, 'LEGS'):
        _key_bool_prop(
            armature_obj,
            "sub_use_ik_legs",
            frame,
            new_value=enabled,
        )


def _match_ik_to_fk_for_blend(armature_obj, context, limbs='BOTH'):
    """Snap IK controls + pole_angle to the current FK pose at this frame."""
    if armature_obj is None:
        return

    from . import anim_layers_compat

    # bpy Operators cannot be constructed with __new__; borrow methods onto a plain object.
    class _FkToIkMatchHelper:
        pass

    op_cls = fk_to_ik.SUB_OP_fk_to_ik_transfer
    for name, func in op_cls.__dict__.items():
        if isinstance(func, (classmethod, staticmethod, property)):
            continue
        if callable(func) and not name.startswith('bl_'):
            setattr(_FkToIkMatchHelper, name, func)

    helper = _FkToIkMatchHelper()
    helper.cleanup_mode = limbs
    helper.entire_animation = False
    helper.auto_keyframe = True
    helper.custom_leg_bone_l = "LegL"
    helper.custom_knee_bone_l = "KneeL"
    helper.custom_foot_bone_l = "FootL"
    helper.custom_leg_bone_r = "LegR"
    helper.custom_knee_bone_r = "KneeR"
    helper.custom_foot_bone_r = "FootR"
    helper._knee_pole_signs = {}
    helper._arm_pole_signs = {}
    helper._collect_keys = False

    was_paused = _IK_FK_MUTE_SYNC_PAUSED
    prev_active = getattr(context.view_layer.objects, "active", None)
    pause_ik_fk_mute_sync(True)
    try:
        with _disable_autokey(context):
            # Pure FK evaluation: rest-hold off, FK keys live, stale IK keys frozen.
            _clear_fk_rest_hold(armature_obj)
            _set_ik_driven_fcurves_muted(armature_obj, False, limbs=limbs)
            _set_ik_control_fcurves_muted(armature_obj, True, limbs=limbs)
            for _pb, constraint in _iter_limb_ik_constraints(armature_obj, limbs=limbs):
                constraint.mute = True
            context.view_layer.objects.active = armature_obj
            frame = context.scene.frame_current
            try:
                context.scene.frame_set(frame)
            except Exception:
                context.view_layer.update()
            # Animation Layers: key into the strip that actually drives the view.
            with anim_layers_compat.bind_driving_action_for_bake(armature_obj, context):
                helper.process_frame(context)
    finally:
        if prev_active is not None:
            try:
                context.view_layer.objects.active = prev_active
            except (ReferenceError, RuntimeError):
                pass
        pause_ik_fk_mute_sync(was_paused)
        # Do NOT sync mutes/rest-hold here — props are still FK and would mute
        # the IK control keys we just wrote before the caller enables IK.


def _activate_armature(context, armature_obj):
    if (
        getattr(context.view_layer.objects, "active", None) is armature_obj
        and context.mode == "POSE"
    ):
        armature_obj.show_in_front = True
        return
    if context.mode not in {'OBJECT', 'POSE'}:
        bpy.ops.object.mode_set(mode='OBJECT')
    try:
        armature_obj.hide_set(False)
    except RuntimeError:
        pass
    armature_obj.select_set(True)
    context.view_layer.objects.active = armature_obj
    armature_obj.show_in_front = True


def _key_pose_from_matrix(pose_bone, matrix, frame):
    pose_bone.matrix = matrix
    pose_bone.keyframe_insert("location", frame=frame, group=pose_bone.name)
    pose_bone.keyframe_insert("scale", frame=frame, group=pose_bone.name)
    pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=pose_bone.name)
    if pose_bone.rotation_mode not in {"QUATERNION", "AXIS_ANGLE"}:
        pose_bone.keyframe_insert("rotation_euler", frame=frame, group=pose_bone.name)


def bake_ik_visual_keys(context, armature_obj):
    """Bake IK-driven limb bones to visual keys, then mute the IK constraints."""
    from .apply_ik_animation import (
        bake_ik_driven_fk_visual,
        collect_fk_bone_names,
        present_ik_limbs,
    )

    limbs = present_ik_limbs(armature_obj)
    if not limbs:
        return 0
    names = collect_fk_bone_names(armature_obj, limbs=limbs)
    if not names or not list(_iter_limb_ik_constraints(armature_obj, limbs=limbs)):
        return 0
    pause_ik_fk_mute_sync(True)
    try:
        keyed = bake_ik_driven_fk_visual(
            context,
            armature_obj,
            names,
            context.scene.frame_start,
            context.scene.frame_end,
            limbs=limbs,
            clear_constraints=False,
        )
        for _pose_bone, constraint in _iter_limb_ik_constraints(armature_obj, limbs=limbs):
            constraint.mute = True
            constraint.influence = 0.0
        unmute_all_ik_fk_fcurves(armature_obj)
        _set_ik_bone_visibility(armature_obj, False, limbs=limbs)
        return keyed
    finally:
        pause_ik_fk_mute_sync(False)


def _remove_ik_fk_switch_keys(armature_obj, limbs='BOTH'):
    """Delete the IK/FK switch channels after the pose has been baked."""
    if armature_obj is None:
        return 0
    paths = {"sub_use_ik_arms", "sub_use_ik_legs", "sub_use_ik"} if limbs == "BOTH" else {_limb_switch_prop(limbs)}
    removed = 0
    for id_data, id_type in ((armature_obj.data, "ARMATURE"), (armature_obj, "OBJECT")):
        anim = getattr(id_data, "animation_data", None)
        action = getattr(anim, "action", None) if anim else None
        if action is None:
            continue
        for fcurve in list(get_all_action_fcurves(action, id_type=id_type)):
            if (fcurve.data_path or "") not in paths:
                continue
            remove_fcurve(action, fcurve, id_type=id_type)
            removed += 1
    return removed


def strip_animation_rig(context, armature_obj):
    """Remove extra animation-rig bones and control shapes. IK constraints stay unless already muted."""
    from . import finger_sliders
    from . import eye_rig
    _remove_ik_influence_drivers(armature_obj)
    _remove_ik_fk_switch_keys(armature_obj)
    if finger_sliders.has_finger_slider_constraints(armature_obj):
        finger_sliders.bake_finger_slider_keys(context, armature_obj)
    finger_sliders.remove_finger_sliders(context, armature_obj)
    eye_rig.remove_eye_look_control_bone(armature_obj)

    cleared = 0
    for pose_bone in armature_obj.pose.bones:
        widget = pose_bone.custom_shape
        if widget is None or not widget.name.startswith(WIDGET_PREFIX):
            continue
        pose_bone.custom_shape = None
        pose_bone.bone.show_wire = False
        if hasattr(pose_bone, 'custom_shape_translation'):
            pose_bone.custom_shape_translation = (0.0, 0.0, 0.0)
        if hasattr(pose_bone, 'use_custom_shape_bone_size'):
            pose_bone.use_custom_shape_bone_size = True
        cleared += 1

    if armature_obj.data.bones.get(_TRANS_AIM_BONE) is not None:
        prev_mode = armature_obj.mode
        _activate_armature(context, armature_obj)
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            aim = armature_obj.data.edit_bones.get(_TRANS_AIM_BONE)
            if aim is not None:
                armature_obj.data.edit_bones.remove(aim)
        finally:
            try:
                bpy.ops.object.mode_set(mode='POSE' if prev_mode == 'EDIT' else prev_mode)
            except Exception:
                bpy.ops.object.mode_set(mode='POSE')

    _restore_clutter(armature_obj)
    if ARMATURE_FLAG in armature_obj.data:
        del armature_obj.data[ARMATURE_FLAG]
    return cleared


class SUB_OP_create_animation_rig(Operator):
    bl_idname = "sub.create_animation_rig"
    bl_label = "Create Animation Rig"
    bl_description = (
        "Add animator control shapes to a Smash Ultimate armature "
        "(smush_blender_import, including .001 copies)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    stage: bpy.props.EnumProperty(
        items=(
            ('MAIN', "Main", ""),
            ('IK', "IK", ""),
            ('EYE', "Eye", ""),
        ),
        default='MAIN',
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    dialog_mouse_x: bpy.props.IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    dialog_mouse_y: bpy.props.IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    setup_ik: bpy.props.BoolProperty(
        name="Create IK Controls",
        description="Create foot/hand IK targets and knee/elbow pole controls",
        default=True,
    )
    ik_limbs: bpy.props.EnumProperty(name="IK Limbs", items=(('LEGS', "Legs", ""), ('ARMS', "Arms", ""), ('BOTH', "Arms and Legs", "")), default='BOTH')
    setup_eye_look: bpy.props.BoolProperty(
        name="Add Eye Look Control",
        description="Add the BL_EyeLook bone in front of the head and set up CustomVector31 so posing it aims the eyes",
        default=True,
    )
    setup_finger_sliders: bpy.props.BoolProperty(
        name="Add Finger Sliders",
        description="Add finger sliders on each hand, including extra hands. The thumb is a 2D pad. Turn off to pose Smash finger bones only",
        default=True,
    )
    hide_helpers: bpy.props.BoolProperty(
        name="Hide Helper / Swing Bones",
        description="Hide helper, swing, and extra system bones so only animation controls stay visible",
        default=True,
    )
    match_position: bpy.props.BoolProperty(
        name="Match IK to FK Position",
        description="After creating IK, match the controls to the current pose or loaded animation",
        default=True,
    )
    ik_entire_animation: bpy.props.BoolProperty(
        name="Entire Animation",
        description="Match IK controls on every frame in the scene range",
        default=True,
    )
    ik_auto_keyframe: bpy.props.BoolProperty(
        name="Auto Keyframe",
        description="Keyframe the IK controls while matching the loaded animation",
        default=True,
    )
    remove_knee_frames: bpy.props.BoolProperty(
        name="Delete Knee/Leg FK Keys",
        description="Permanently delete Knee/Leg keys after matching. Leave off to keep them muted while IK is on so switching to FK restores the original anim",
        default=False,
    )
    remove_arm_frames: bpy.props.BoolProperty(
        name="Delete Arm FK Keys",
        description="Permanently delete Arm keys after matching. Leave off to keep them muted while IK is on so switching to FK restores the original anim",
        default=False,
    )
    eye_pupil_from_scale: bpy.props.BoolProperty(
        name="Pupil Size From Bone Scale",
        description="Drive CustomVector31 X/Y from the eye control bone's scale. Shrink the bone to shrink the pupil",
        default=True,
    )
    eye_measure_from_mesh: bpy.props.BoolProperty(
        name="Measure Pupil Centre From Mesh",
        description="Sample the eye meshes' UVs now and use that as the pupil scale pivot",
        default=True,
    )
    eye_look_live_preview: bpy.props.BoolProperty(
        name="Live Preview",
        description=(
            "Start Solid Texture and Material eye look preview. "
            "On by default. Bake still has to run to create the keyframes export reads"
        ),
        default=True,
    )
    eye_material_anim: bpy.props.EnumProperty(
        name="Existing Eye Material Anim",
        description="What to do with EyeL/EyeR CustomVector31 keys already on this animation",
        items=(
            ('MATCH', "Match Material Anim",
             "Copy the look onto the eye bone, clean baked interpolation keys, and delete EyeL/EyeR CustomVector31 keyframes"),
            ('REPLACE', "Replace Material Anim",
             "Delete EyeL/EyeR CustomVector31 keyframes and add an empty eye bone with no animation"),
        ),
        default='MATCH',
    )

    @classmethod
    def poll(cls, context):
        return find_target_armature(context) is not None

    def invoke(self, context, event):
        if self.stage == 'MAIN' and event is not None:
            self.dialog_mouse_x = event.mouse_x
            self.dialog_mouse_y = event.mouse_y
        else:
            self._warp_cursor(context, self.dialog_mouse_x, self.dialog_mouse_y)
        result = context.window_manager.invoke_props_dialog(self, width=380)
        self._warp_cursor_to_confirm(context)
        return result

    def _warp_cursor(self, context, mouse_x, mouse_y):
        if not mouse_x and not mouse_y:
            return
        try:
            context.window.cursor_warp(mouse_x, mouse_y)
        except Exception:
            pass

    def _warp_cursor_to_confirm(self, context):
        """Leave the dialog where it is, but put the mouse on OK so each step is a click."""
        if not self.dialog_mouse_x and not self.dialog_mouse_y:
            return
        scale = getattr(context.preferences.system, "ui_scale", 1.0) or 1.0
        try:
            dpi = float(context.preferences.system.dpi) / 72.0
        except Exception:
            dpi = 1.0
        row = int(20 * dpi * scale)
        rows = {
            'MAIN': 6,
            'IK': 8,
            'EYE': 10,
        }.get(self.stage, 6)
        offset = int(row * (rows - 0.35))
        self._warp_cursor(context, self.dialog_mouse_x, self.dialog_mouse_y - offset)

    def _kwargs(self):
        return {
            "stage": self.stage,
            "dialog_mouse_x": self.dialog_mouse_x,
            "dialog_mouse_y": self.dialog_mouse_y,
            "setup_ik": self.setup_ik,
            "ik_limbs": self.ik_limbs,
            "setup_eye_look": self.setup_eye_look,
            "setup_finger_sliders": self.setup_finger_sliders,
            "hide_helpers": self.hide_helpers,
            "match_position": self.match_position,
            "ik_entire_animation": self.ik_entire_animation,
            "ik_auto_keyframe": self.ik_auto_keyframe,
            "remove_knee_frames": self.remove_knee_frames,
            "remove_arm_frames": self.remove_arm_frames,
            "eye_pupil_from_scale": self.eye_pupil_from_scale,
            "eye_measure_from_mesh": self.eye_measure_from_mesh,
            "eye_look_live_preview": self.eye_look_live_preview,
            "eye_material_anim": self.eye_material_anim,
        }

    def execute(self, context):
        nxt = self._next_stage()
        if nxt is not None:
            kwargs = self._kwargs()
            kwargs["stage"] = nxt
            return bpy.ops.sub.create_animation_rig("INVOKE_DEFAULT", **kwargs)
        return self._build(context)

    def _pending_stages(self):
        stages = []
        if self.setup_ik:
            stages.append('IK')
        if self.setup_eye_look:
            stages.append('EYE')
        return stages

    def _next_stage(self):
        pending = self._pending_stages()
        if self.stage == 'MAIN':
            return pending[0] if pending else None
        if self.stage in pending:
            index = pending.index(self.stage) + 1
            if index < len(pending):
                return pending[index]
        return None

    def draw(self, context):
        layout = self.layout
        if self.stage == 'MAIN':
            layout.label(text="What should the animation rig include?")
            layout.prop(self, "setup_ik")
            if self.setup_ik:
                layout.prop(self, "ik_limbs")
            layout.prop(self, "setup_eye_look")
            layout.prop(self, "setup_finger_sliders")
            layout.prop(self, "hide_helpers")
            return
        if self.stage == 'IK':
            layout.label(text="IK Options")
            layout.prop(self, "match_position")
            col = layout.column()
            col.enabled = self.match_position
            col.prop(self, "ik_entire_animation")
            colk = col.column()
            colk.enabled = self.match_position and self.ik_entire_animation
            colk.prop(self, "ik_auto_keyframe")
            layout.label(text="Original FK animation is preserved.")
            return
        if self.stage == 'EYE':
            layout.label(text="Eye Look Options")
            layout.prop(self, "eye_look_live_preview")
            layout.prop(self, "eye_pupil_from_scale")
            layout.prop(self, "eye_measure_from_mesh")
            from .eye_rig import armature_has_eye_material_keys
            if armature_has_eye_material_keys(find_target_armature(context)):
                layout.separator()
                layout.label(text="This animation has EyeL/EyeR CustomVector31 keys.")
                layout.prop(self, "eye_material_anim", expand=True)
            layout.label(text="Invert X / Invert Y are sliders next to the eye bone.")
            return

    def _build(self, context):
        armature_obj = find_target_armature(context)
        if armature_obj is None or armature_obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Select a Smash Ultimate armature (smush_blender_import).")
            return {'CANCELLED'}
        if not _looks_like_smash_armature(armature_obj):
            self.report(
                {'ERROR'},
                "This armature does not look like a Smash Ultimate rig (missing Trans/Hip).",
            )
            return {'CANCELLED'}

        _activate_armature(context, armature_obj)
        cleaned = 0
        with ProgressCursor(context) as progress:
            progress.update(0.05)
            ik_created = False
            if self.setup_ik:
                from .ik_channels import create_controls
                ik_created = bool(create_controls(context, armature_obj, self.ik_limbs))

            if context.mode != 'POSE':
                bpy.ops.object.mode_set(mode='POSE')
            progress.update(0.2)

            ssp = getattr(context.scene, 'sub_scene_properties', None)
            eye_added = False
            if self.setup_eye_look:
                from . import eye_rig
                if ssp is not None:
                    ssp.eye_look_mode = 'OFFSET'
                    ssp.eye_look_pupil_from_scale = self.eye_pupil_from_scale
                    if self.eye_measure_from_mesh:
                        centre = eye_rig.eye_uv_centre(armature_obj)
                        ssp.eye_pupil_centre = (centre.x, centre.y)
                        ssp.eye_pupil_centre_auto = False
                try:
                    eye_rig.setup_eye_cv31_tracks(armature_obj, context)
                except Exception:
                    pass
                ok, _message = eye_rig.add_eye_look_control_bone(
                    armature_obj,
                    include_invert_sliders=True,
                    material_anim=self.eye_material_anim,
                )
                eye_added = ok
                if context.mode != 'POSE':
                    bpy.ops.object.mode_set(mode='POSE')
            progress.update(0.4)

            shaped = _apply_shapes(context, armature_obj)
            if eye_added:
                apply_eye_look_shape(context, armature_obj)
                apply_eye_option_shapes(context, armature_obj, ssp)
                if ssp is not None:
                    ssp.eye_look_live_preview = self.eye_look_live_preview
                    eye_rig.ensure_eye_live_preview(context.scene)
            progress.update(0.55)

            slider_count = 0
            if self.setup_finger_sliders:
                from . import finger_sliders
                slider_count = finger_sliders.build_finger_sliders(context, armature_obj)
                progress.update(0.7)

            if self.hide_helpers:
                _hide_clutter(armature_obj)

            armature_obj.data[ARMATURE_FLAG] = True
            progress.update(0.92)

            if self.setup_ik and (ik_created or armature_has_ik(armature_obj)):
                if self.match_position:
                    bpy.ops.sub.fk_to_ik_transfer(
                        'EXEC_DEFAULT',
                        cleanup_mode=self.ik_limbs,
                        entire_animation=self.ik_entire_animation,
                        auto_keyframe=self.ik_auto_keyframe,
                        remove_knee_frames=self.remove_knee_frames,
                        remove_arm_frames=self.remove_arm_frames,
                        show_progress=False,
                    )
                finalize_ik_controls(armature_obj, context)
            cleaned = 0
            if ssp is not None and ssp.clean_keyframes_after_rig:
                from .finger_sliders import is_finger_match_fcurve_path
                # Official metacarpals (Finger*10/20/...) look "redundant" to the
                # cleaner but they hold the fist pose. Never strip Finger* keys.
                cleaned += clean_redundant_keys_on_id(
                    armature_obj, skip_data_path=is_finger_match_fcurve_path
                )
                cleaned += clean_redundant_keys_on_id(armature_obj.data)
            progress.update(1.0)

        extra = " IK controls added." if ik_created else ""
        if slider_count:
            extra += f" {slider_count} finger sliders on the hand boxes."
        if eye_added:
            extra += " Eye look control added."
        if cleaned:
            extra += f" Cleaned {cleaned} redundant keys."
        self.report({'INFO'}, f"Animation rig created on {armature_obj.name} ({shaped} control shapes).{extra}")
        return {'FINISHED'}


class SUB_OP_remove_animation_rig(Operator):
    bl_idname = "sub.remove_animation_rig"
    bl_label = "Remove Animation Rig"
    bl_description = "Remove animation control shapes from the selected Smash Ultimate armature"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        armature = find_target_armature(context)
        return armature is not None and armature.type == 'ARMATURE'

    def execute(self, context):
        armature_obj = find_target_armature(context)
        if armature_obj is None:
            self.report({'ERROR'}, "Select a Smash Ultimate armature.")
            return {'CANCELLED'}

        _activate_armature(context, armature_obj)
        cleared = strip_animation_rig(context, armature_obj)
        self.report({'INFO'}, f"Removed animation rig shapes from {armature_obj.name} ({cleared} bones).")
        return {'FINISHED'}


class SUB_OP_bake_and_remove_rig(Operator):
    bl_idname = "sub.bake_and_remove_rig"
    bl_label = "Bake and Remove Rig"
    bl_description = (
        "Bake selected animation-rig extras to Smash bones / CustomVector31, then remove the extra controls"
    )
    bl_options = {'REGISTER', 'UNDO'}

    bake_fingers: bpy.props.BoolProperty(
        name="Fingers",
        description="Bake finger slider poses onto the Smash finger bones",
        default=True,
    )
    bake_eyes: bpy.props.BoolProperty(
        name="Eyes",
        description="Bake the eye look control to EyeL/EyeR CustomVector31 keyframes",
        default=True,
    )
    bake_ik: bpy.props.BoolProperty(
        name="IK",
        description="Bake IK limb poses onto the Smash arm/leg bones and mute IK",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return armature_has_animation_rig(find_target_armature(context))

    def invoke(self, context, event):
        armature_obj = find_target_armature(context)
        if armature_obj is None:
            return {'CANCELLED'}
        from .finger_sliders import has_finger_sliders
        from .eye_rig import EYE_CTRL_BONE
        self.bake_fingers = has_finger_sliders(armature_obj)
        self.bake_eyes = armature_obj.pose.bones.get(EYE_CTRL_BONE) is not None
        self.bake_ik = armature_has_ik(armature_obj)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Bake:")
        layout.prop(self, "bake_fingers")
        layout.prop(self, "bake_eyes")
        layout.prop(self, "bake_ik")
        layout.separator()
        layout.label(text="Then the extra rig controls will be removed.")

    def execute(self, context):
        armature_obj = find_target_armature(context)
        if armature_obj is None:
            self.report({'ERROR'}, "Select a Smash Ultimate armature.")
            return {'CANCELLED'}
        _activate_armature(context, armature_obj)
        if context.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')

        parts = []
        with ProgressCursor(context) as progress:
            steps = int(self.bake_fingers) + int(self.bake_eyes) + int(self.bake_ik) + 1
            done = 0
            if self.bake_eyes:
                from . import eye_rig
                baked = eye_rig.bake_eye_look_keys(
                    context,
                    armature_obj,
                    progress=progress,
                    progress_start=done / steps,
                    progress_end=(done + 1) / steps,
                )
                parts.append(f"{baked} eye keys")
                done += 1
            if self.bake_fingers:
                from . import finger_sliders
                keyed = finger_sliders.bake_finger_slider_keys(
                    context,
                    armature_obj,
                    progress=progress,
                    progress_start=done / steps,
                    progress_end=(done + 1) / steps,
                )
                parts.append(f"{keyed} finger keys")
                done += 1
            if self.bake_ik:
                progress.update(done / steps)
                bpy.ops.sub.apply_ik_animation()
                parts.append("IK (IK Tools bake)")
                done += 1
                if context.mode not in {'POSE', 'OBJECT'}:
                    bpy.ops.object.mode_set(mode='POSE')
            progress.update(done / steps)
            cleared = strip_animation_rig(context, armature_obj)
            progress.update(1.0)

        baked = ", ".join(parts) if parts else "nothing baked"
        self.report(
            {'INFO'},
            f"Baked {baked} and removed animation rig from {armature_obj.name} ({cleared} shapes).",
        )
        return {'FINISHED'}


def key_ik_stretch(context, armature_obj, kind, enabled):
    """Store stretch in the same SAP action as the FK/IK switches."""
    path = 'sub_ik_stretch_' + kind.lower()
    _key_bool_prop(armature_obj, path, context.scene.frame_current, new_value=enabled)
    _action, curve = _bool_switch_fcurve(armature_obj, path)
    for point in curve.keyframe_points:
        point.interpolation = 'CONSTANT'
    curve.update()
    setattr(armature_obj.data, path, enabled)
    armature_obj.update_tag()
    context.view_layer.update()


class SUB_OP_key_ik_stretch(Operator):
    bl_idname = 'sub.key_ik_stretch'
    bl_label = 'Toggle and Key IK Stretch'
    bl_description = 'Toggle IK Stretch and key its state at the current frame'
    bl_options = {'REGISTER', 'UNDO'}

    limbs: bpy.props.EnumProperty(items=(('ARMS', 'Arms', ''), ('LEGS', 'Legs', '')))

    @classmethod
    def poll(cls, context):
        obj = find_target_armature(context)
        return obj is not None and armature_has_ik(obj)

    def execute(self, context):
        obj = find_target_armature(context)
        path = 'sub_ik_stretch_' + self.limbs.lower()
        key_ik_stretch(context, obj, self.limbs, not getattr(obj.data, path))
        return {'FINISHED'}


class SUB_OP_anim_rig_toggle_ik_fk(Operator):
    bl_idname = "sub.anim_rig_toggle_ik_fk"
    bl_label = "Switch IK/FK"
    bl_description = (
        "Key the new IK/FK mode at the current frame and apply it. "
        "Playback interpolates between this key and any earlier opposite switch key"
    )
    bl_options = {'REGISTER', 'UNDO'}

    limbs: bpy.props.EnumProperty(
        name="Limbs",
        items=(
            ('ARMS', "Arms", "Hands, elbows, and extra arms such as HandIKL2"),
            ('LEGS', "Legs", "Feet and knees"),
            ('BOTH', "Both", "Arms and legs"),
        ),
        default='BOTH',
        options={'SKIP_SAVE'},
    )
    set_enabled: bpy.props.BoolProperty(
        name="Set Enabled",
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    enable_ik: bpy.props.BoolProperty(
        default=True,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        armature = find_target_armature(context)
        return armature is not None and armature_has_ik(armature)

    def execute(self, context):
        armature_obj = find_target_armature(context)
        if armature_obj is None:
            self.report({'ERROR'}, "Select a Smash Ultimate armature.")
            return {'CANCELLED'}
        if not armature_has_ik(armature_obj, self.limbs):
            self.report({'ERROR'}, "This armature has no IK controls for that selection.")
            return {'CANCELLED'}

        if self.set_enabled:
            enable_ik = bool(self.enable_ik)
        else:
            enable_ik = not armature_ik_is_enabled(armature_obj, self.limbs)
        frame = context.scene.frame_current

        # Snap IK to FK before leaving FK so the first IK key matches the pose.
        from .ik_channels import ensure
        ensure(armature_obj, context, self.limbs)

        # One key at this frame for the destination mode (no auto +N pair).
        _key_use_ik(
            armature_obj,
            frame,
            limbs=self.limbs,
            enabled=enable_ik,
        )

        _strip_competing_ik_influence_keys(armature_obj)
        # Apply via the shared path (mute/unmute + drivers + safe neutralize).
        _set_ik_enabled(context, armature_obj, enable_ik, limbs=self.limbs)
        context.view_layer.update()

        label = {'ARMS': 'arms', 'LEGS': 'legs', 'BOTH': 'arms and legs'}[self.limbs]
        mode = "IK" if enable_ik else "FK"
        self.report(
            {'INFO'},
            f"Keyed {label} to {mode} at frame {frame}.",
        )
        return {'FINISHED'}


_last_pose_tool_bone = None
_pose_tool_msgbus = object()
_pose_tool_busy = False
_pose_tool_defer_depth = 0


@contextlib.contextmanager
def defer_pose_tool_updates():
    """Refresh the interactive pose tool once after a synchronous rig edit."""
    global _pose_tool_defer_depth
    _pose_tool_defer_depth += 1
    try:
        yield
    finally:
        _pose_tool_defer_depth -= 1
        if _pose_tool_defer_depth == 0:
            _schedule_pose_tool()


def _tool_id_for_pose_bone(pose_bone):
    """Prefer select-box for normal bones; only special controls force a tool.

    Auto-switching every widget bone to Rotate stole Select Box and broke
    click-drag multi-select in Pose Mode.
    """
    from .eye_rig import EYE_CTRL_BONE, EYE_OPT_INVERT_X, EYE_OPT_INVERT_Y
    from .finger_sliders import is_finger_pad_bone, is_finger_slider_bone, is_thumb_slider_bone
    name = canonical_bone_name(pose_bone.name)
    if is_finger_pad_bone(pose_bone.name):
        return None
    if is_finger_slider_bone(pose_bone.name) or is_thumb_slider_bone(pose_bone.name) or name in {EYE_OPT_INVERT_X, EYE_OPT_INVERT_Y}:
        return "builtin.move"
    if _IK_BONE.match(name):
        return "builtin.transform"
    if name == EYE_CTRL_BONE:
        return "builtin.move"
    # Normal Smash bones keep whatever tool the user has (usually Select Box)
    return None


def _is_transforming(context):
    def _is_transform_op(name):
        name = name or ""
        return name.startswith("TRANSFORM_OT_")

    window = getattr(context, "window", None)
    modal = getattr(window, "modal_operators", None) if window else None
    if modal:
        try:
            for operator in modal:
                if _is_transform_op(getattr(operator, "bl_idname", "") or ""):
                    return True
        except Exception:
            pass
    operator = getattr(context, "active_operator", None)
    return _is_transform_op(getattr(operator, "bl_idname", "") or "")


def _pose_armature(context):
    obj = getattr(context, "object", None)
    if obj is not None and getattr(obj, "type", None) == "ARMATURE":
        return obj
    return find_target_armature(context)


def _active_pose_bone(context):
    pose_bone = getattr(context, "active_pose_bone", None)
    if pose_bone is not None:
        return pose_bone
    armature_obj = _pose_armature(context)
    if armature_obj is None:
        return None
    active = getattr(armature_obj.data.bones, "active", None)
    if active is not None:
        found = armature_obj.pose.bones.get(active.name)
        if found is not None:
            return found
    from ..blender_compat import is_pose_bone_selected
    for bone in armature_obj.pose.bones:
        if is_pose_bone_selected(bone):
            return bone
    return None


def _set_local_orientation(context):
    try:
        slot = context.scene.transform_orientation_slots[0]
        if slot.type != "LOCAL":
            slot.type = "LOCAL"
    except Exception:
        pass


def _file_browser_open(context):
    """True while a file selector is open (browse folder/file)."""
    wm = getattr(context, "window_manager", None)
    if wm is None:
        return False
    try:
        for window in wm.windows:
            screen = getattr(window, "screen", None)
            if screen is None:
                continue
            for area in screen.areas:
                if area.type == "FILE_BROWSER":
                    return True
    except Exception:
        pass
    return False


def _set_view3d_tool(context, tool_id):
    global _pose_tool_busy
    if _pose_tool_busy:
        return False
    if _file_browser_open(context):
        return False
    window = context.window
    if window is None or window.screen is None:
        return False
    area = next((item for item in window.screen.areas if item.type == "VIEW_3D"), None)
    if area is None:
        return False
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        return False
    _pose_tool_busy = True
    try:
        with context.temp_override(
            window=window,
            screen=window.screen,
            area=area,
            region=region,
            workspace=context.workspace,
        ):
            bpy.ops.wm.tool_set_by_id(name=tool_id, space_type="VIEW_3D")
        return True
    except Exception:
        try:
            with context.temp_override(
                window=window,
                screen=window.screen,
                area=area,
                region=region,
            ):
                bpy.ops.wm.tool_set_by_id(name=tool_id)
            return True
        except Exception:
            return False
    finally:
        _pose_tool_busy = False


def _apply_pose_tool(context):
    global _last_pose_tool_bone
    if _pose_tool_defer_depth or _pose_tool_busy or _file_browser_open(context):
        return
    if getattr(context, "mode", None) != "POSE":
        _last_pose_tool_bone = None
        return
    if _is_transforming(context):
        return
    pose_bone = _active_pose_bone(context)
    if pose_bone is None:
        # Nothing selected — restore Select Box so click-drag multi-select works
        if _last_pose_tool_bone != ("", ""):
            _set_view3d_tool(context, "builtin.select_box")
            _last_pose_tool_bone = ("", "")
        return
    key = (pose_bone.id_data.as_pointer(), pose_bone.name)
    if key == _last_pose_tool_bone:
        return
    tool_id = _tool_id_for_pose_bone(pose_bone)
    if not tool_id:
        _last_pose_tool_bone = key
        return
    if tool_id == "builtin.move":
        _set_local_orientation(context)
    if _set_view3d_tool(context, tool_id):
        _last_pose_tool_bone = key


def _pose_tool_apply_soon():
    try:
        context = bpy.context
        if context is not None:
            _apply_pose_tool(context)
    except Exception:
        pass
    return None


def _schedule_pose_tool():
    if _pose_tool_defer_depth:
        return
    try:
        context = bpy.context
        if context is not None and not _pose_tool_busy and not _file_browser_open(context):
            _apply_pose_tool(context)
    except Exception:
        pass
    if bpy.app.timers.is_registered(_pose_tool_apply_soon):
        return
    bpy.app.timers.register(_pose_tool_apply_soon, first_interval=0.0)


def _pose_tool_on_rna(*_args):
    _schedule_pose_tool()


def _pose_tool_timer():
    try:
        context = bpy.context
        if context is not None and getattr(context, "mode", None) == "POSE":
            _apply_pose_tool(context)
    except Exception:
        pass
    return 0.25


def _subscribe_pose_tool_msgbus():
    bpy.msgbus.clear_by_owner(_pose_tool_msgbus)
    keys = (
        (bpy.types.PoseBone, "select"),
        (bpy.types.Bone, "select"),
        (bpy.types.Object, "mode"),
    )
    for key in keys:
        try:
            bpy.msgbus.subscribe_rna(
                key=key,
                owner=_pose_tool_msgbus,
                args=(),
                notify=_pose_tool_on_rna,
            )
        except Exception:
            continue


@persistent
def _pose_tool_depsgraph(_scene, _depsgraph):
    if _pose_tool_defer_depth:
        return
    try:
        context = bpy.context
        if context is None or getattr(context, "mode", None) != "POSE":
            return
        if _pose_tool_busy or _file_browser_open(context):
            return
        pose_bone = _active_pose_bone(context)
        if pose_bone is None:
            if _last_pose_tool_bone != ("", ""):
                _schedule_pose_tool()
            return
        key = (pose_bone.id_data.as_pointer(), pose_bone.name)
        if key == _last_pose_tool_bone:
            return
        _schedule_pose_tool()
    except Exception:
        pass


@persistent
def _pose_tool_load_post(_dummy):
    _subscribe_pose_tool_msgbus()
    _IK_VIS_CACHE.clear()
    _ensure_ik_drivers_on_loaded_rigs()


def _ensure_ik_drivers_on_loaded_rigs():
    for obj in bpy.data.objects:
        if obj.type != 'ARMATURE':
            continue
        if not (obj.data.get(ARMATURE_FLAG) or obj.data.get('sub_independent_ik')):
            continue
        if armature_has_ik(obj):
            if obj.data.get('sub_independent_ik') and obj.name in bpy.context.view_layer.objects:
                from .ik_channels import upgrade_pull_controls
                upgrade_pull_controls(bpy.context, obj)
            _ensure_ik_influence_drivers(obj)
            _sync_ik_fk_fcurve_mutes(obj)


def _heal_widgets_once():
    try:
        context = bpy.context
        if context is not None:
            _heal_widget_view_layer(context)
        _ensure_ik_drivers_on_loaded_rigs()
    except Exception:
        pass
    return None


def _update_stretch_chain(self, context):
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.data == self:
            obj.update_tag(refresh={'OBJECT'})


def _unregister_ik_fk_props():
    for cls in (bpy.types.Object, bpy.types.Armature, bpy.types.Bone):
        for name in ("sub_use_ik", "sub_use_ik_arms", "sub_use_ik_legs",
                     "sub_ik_stretch_arms", "sub_ik_stretch_legs",
                     "sub_ik_stretch_chain_arms", "sub_ik_stretch_chain_legs",
                     "sub_ik_arm_pull"):
            if hasattr(cls, name):
                try:
                    delattr(cls, name)
                except Exception:
                    pass


def register():
    _unregister_ik_fk_props()
    bpy.types.Bone.sub_ik_arm_pull = bpy.props.FloatProperty(
        name='ArmIK Pull',
        description='Pull this arm chain toward its ArmIK control without scaling',
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        options={'ANIMATABLE'},
    )
    for kind in ('arms', 'legs'):
        setattr(bpy.types.Armature, 'sub_ik_stretch_chain_' + kind, bpy.props.BoolProperty(
            name='Stretch Chain',
            description='Progressively pull bone positions toward the IK target without adding scale',
            default=False,
            update=_update_stretch_chain,
        ))
        setattr(bpy.types.Armature, 'sub_ik_stretch_' + kind, bpy.props.BoolProperty(
            name='IK Stretch ' + kind.title(),
            description='Let hands or feet follow the IK target beyond limb reach',
            default=False,
            update=_update_stretch_chain,
        ))
    bpy.types.Armature.sub_use_ik_arms = bpy.props.FloatProperty(
        name="Arms",
        description="0 is arm FK, 1 is arm IK (including extra arms). Keys ease between them",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        options={'ANIMATABLE'},
        update=_update_use_ik_arms,
    )
    bpy.types.Armature.sub_use_ik_legs = bpy.props.FloatProperty(
        name="Legs",
        description="0 is leg FK, 1 is leg IK. Keys ease between them",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        options={'ANIMATABLE'},
        update=_update_use_ik_legs,
    )
    bpy.types.Armature.sub_use_ik = bpy.props.FloatProperty(
        name="Both",
        description="0 is FK on arms and legs, 1 is IK. Keys ease between them",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        options={'ANIMATABLE'},
        update=_update_use_ik,
    )
    _subscribe_pose_tool_msgbus()
    if _pose_tool_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_pose_tool_load_post)
    if _pose_tool_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_pose_tool_depsgraph)
    if _sync_ik_fk_visibility not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_sync_ik_fk_visibility)
    if not bpy.app.timers.is_registered(_pose_tool_timer):
        bpy.app.timers.register(_pose_tool_timer, persistent=True, first_interval=0.05)
    if not bpy.app.timers.is_registered(_heal_widgets_once):
        bpy.app.timers.register(_heal_widgets_once, first_interval=0.2)


def unregister():
    global _last_pose_tool_bone
    _last_pose_tool_bone = None
    _unregister_ik_fk_props()
    bpy.msgbus.clear_by_owner(_pose_tool_msgbus)
    if _pose_tool_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_pose_tool_load_post)
    if _pose_tool_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_pose_tool_depsgraph)
    if _sync_ik_fk_visibility in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_sync_ik_fk_visibility)
    _IK_VIS_CACHE.clear()
    if bpy.app.timers.is_registered(_pose_tool_timer):
        bpy.app.timers.unregister(_pose_tool_timer)
    if bpy.app.timers.is_registered(_pose_tool_apply_soon):
        bpy.app.timers.unregister(_pose_tool_apply_soon)
    if bpy.app.timers.is_registered(_heal_widgets_once):
        bpy.app.timers.unregister(_heal_widgets_once)

