"""Import and export stage lighting .nuanmb files as editable Blender lights."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Quaternion, Vector

from ....dependencies import ssbh_data_py
from ...anim.fcurve_compat import find_fcurve, get_fcurves, new_fcurve
from ...blender_compat import assign_action


COLLECTION_NAME = "Ultimate Stage Lights"
FILL_LIGHT_NAME = "Ultimate Ambient Fill"
AMBIENT_WORLD_NAME = "Ultimate Stage Ambient"
AXIS_CORRECTION = Matrix.Rotation(math.radians(90.0), 4, "X")
SUN_ALIGN = Matrix.Rotation(math.radians(180.0), 4, "Y")
SUN_ALIGN_QUAT = Quaternion((0.0, 0.0, 1.0, 0.0))

SCENE_NODE_NAME = "sceneAttributesForShaderFX"

# SSBH Editor (ssbh_wgpu animation/lighting.rs + model.wgsl):
# color = CustomVector0 * CustomFloat0
# direction = RotationMatrix * +Z
# each mesh uses GetLight() — LightChr OR one LightStg[light_set], never all of them
# stages add Texture9 RGB * 8 as unlit ambient and Texture9 A as baked shadows
# SH constant term from the training vertex shader is ~1.11
SMASH_SH_AMBIENT = (1.11054, 1.11036, 1.11018)


def is_light_node_name(name: str) -> bool:
    if name.endswith("_bake"):
        return False
    return name == "LightChr" or name.startswith("LightStg")


def is_stage_light_object(obj) -> bool:
    return bool(obj.get("sub_stage_light_node"))


def is_stage_fill_light(obj) -> bool:
    return bool(obj.get("sub_stage_light_fill"))


def _sync_light_shader(light, energy=None):
    """Blender 5 lights always have nodes. Viewport intensity lives on Emission strength."""
    tree = getattr(light, "node_tree", None)
    if tree is None:
        return
    strength = float(light.energy if energy is None else energy)
    color = (float(light.color[0]), float(light.color[1]), float(light.color[2]), 1.0)
    for node in tree.nodes:
        if node.type == "EMISSION":
            node.inputs[0].default_value = color
            node.inputs[1].default_value = strength


def _set_light_contribution(obj, contribute: bool, shadows: bool = False):
    if obj is None or obj.type != "LIGHT":
        return
    obj.hide_viewport = False
    obj.hide_render = False
    obj.visible_camera = False
    obj.visible_diffuse = contribute
    obj.visible_glossy = contribute
    obj.visible_transmission = contribute
    obj.visible_volume_scatter = contribute
    obj.visible_shadow = bool(contribute and shadows)
    light = obj.data
    if hasattr(light, "use_shadow"):
        light.use_shadow = bool(contribute and shadows)
    _sync_light_shader(light, energy=light.energy if contribute else 0.0)


def _preview_illuminator(mode: str):
    if mode == "CHR":
        return "LightChr"
    if mode == "STG0":
        return "LightStg0"
    return None


def apply_stage_light_preview(context):
    """Match SSBH Editor: at most one nuanmb light illuminates, plus optional SH-like fill."""
    ssp = getattr(context.scene, "sub_scene_properties", None)
    mode = getattr(ssp, "stage_light_preview", "CHR") if ssp is not None else "CHR"
    illuminator = _preview_illuminator(mode)
    use_fill = mode != "NONE"
    use_all = mode == "ALL"

    for obj in find_stage_light_objects(context):
        if obj.type != "LIGHT":
            continue
        name = obj.get("sub_stage_light_node") or obj.name
        contribute = use_all or (illuminator is not None and name == illuminator)
        shadows = contribute and name == "LightStg0" and use_all
        _set_light_contribution(obj, contribute, shadows=shadows)

    fill = _find_fill_light(context)
    if fill is not None:
        _set_light_contribution(fill, use_fill, shadows=False)

    apply_ambient = getattr(ssp, "stage_light_apply_ambient", True) if ssp is not None else True
    if apply_ambient and use_fill:
        _ensure_stage_ambient_world(context.scene)
    _set_fill_light(context, use_fill and apply_ambient)


def _find_fill_light(context):
    collection = find_stage_light_collection(context)
    search = collection.objects if collection is not None else context.scene.objects
    for obj in search:
        if is_stage_fill_light(obj):
            return obj
    return bpy.data.objects.get(FILL_LIGHT_NAME)


def _set_fill_light(context, enabled: bool):
    collection = find_stage_light_collection(context)
    fill = _find_fill_light(context)
    if not enabled:
        if fill is not None:
            _set_light_contribution(fill, False)
        return
    if fill is None:
        light = bpy.data.lights.new(FILL_LIGHT_NAME, "SUN")
        light.energy = 6.0
        light.color = (1.0, 1.0, 1.0)
        if hasattr(light, "angle"):
            light.angle = math.radians(25.0)
        fill = bpy.data.objects.new(FILL_LIGHT_NAME, light)
        fill.rotation_mode = "QUATERNION"
        fill.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
        fill["sub_stage_light_fill"] = True
        if collection is not None:
            collection.objects.link(fill)
        else:
            context.scene.collection.objects.link(fill)
    fill.hide_viewport = False
    fill.hide_render = False
    fill.hide_select = True
    fill.data.energy = 6.0
    fill.data.color = (1.0, 1.0, 1.0)
    _set_light_contribution(fill, True, shadows=False)


def _ensure_stage_ambient_world(scene):
    """Approximate the SSBH training SH constant term so EEVEE Specular is not a black void."""
    world = bpy.data.worlds.get(AMBIENT_WORLD_NAME)
    if world is None:
        world = bpy.data.worlds.new(AMBIENT_WORLD_NAME)
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    background = None
    output = None
    for node in nodes:
        if node.type == "BACKGROUND":
            background = node
        elif node.type == "OUTPUT_WORLD":
            output = node
    if background is None:
        background = nodes.new("ShaderNodeBackground")
    if output is None:
        output = nodes.new("ShaderNodeOutputWorld")
    background.inputs[0].default_value = (0.45, 0.46, 0.48, 1.0)
    background.inputs[1].default_value = 1.0
    if not output.inputs[0].is_linked:
        links.new(background.outputs[0], output.inputs[0])
    if scene.world is None or scene.world.name in {"World", AMBIENT_WORLD_NAME}:
        scene.world = world


def find_stage_light_collection(context):
    active = context.view_layer.objects.active
    if active is not None:
        for collection in active.users_collection:
            if collection.get("sub_stage_light_collection"):
                return collection
    for collection in bpy.data.collections:
        if collection.get("sub_stage_light_collection"):
            return collection
    return bpy.data.collections.get(COLLECTION_NAME)


def find_stage_light_objects(context):
    collection = find_stage_light_collection(context)
    if collection is not None:
        return [obj for obj in collection.objects if is_stage_light_object(obj)]
    return [obj for obj in context.scene.objects if is_stage_light_object(obj)]


def _track_value(track, index):
    if not track.values:
        return None
    if index < len(track.values):
        return track.values[index]
    return track.values[-1]


def _serialize_value(value):
    if hasattr(value, "translation"):
        return {
            "t": [float(v) for v in value.translation],
            "r": [float(v) for v in value.rotation],
            "s": [float(v) for v in value.scale],
        }
    if isinstance(value, (bool,)) or type(value).__name__ == "bool_":
        return bool(value)
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        return [float(v) for v in value]
    return float(value)


def _serialize_track(track):
    values = track.values
    payload = {"name": track.name, "kind": "empty", "values": [], "value_count": len(values)}
    if hasattr(track, "compensate_scale"):
        payload["compensate_scale"] = bool(track.compensate_scale)
    flags = getattr(track, "transform_flags", None)
    if flags is not None:
        payload["transform_flags"] = {
            "override_translation": bool(getattr(flags, "override_translation", False)),
            "override_rotation": bool(getattr(flags, "override_rotation", False)),
            "override_scale": bool(getattr(flags, "override_scale", False)),
            "override_compensate_scale": bool(getattr(flags, "override_compensate_scale", False)),
        }
    if not values:
        return payload
    first = values[0]
    if hasattr(first, "translation"):
        payload["kind"] = "transform"
    elif isinstance(first, (bool,)) or type(first).__name__ == "bool_":
        payload["kind"] = "bool"
    elif hasattr(first, "__len__") and not isinstance(first, (str, bytes)):
        payload["kind"] = "vector"
    else:
        payload["kind"] = "float"
    # Keep one sample for structure. Long tracks live on F-Curves after import.
    payload["values"] = [_serialize_value(first)]
    return payload


def serialize_anim(anim) -> str:
    groups = []
    for group in anim.groups:
        nodes = []
        for node in group.nodes:
            nodes.append({
                "name": node.name,
                "tracks": [_serialize_track(track) for track in node.tracks],
            })
        groups.append({
            "group_type": group.group_type.name,
            "nodes": nodes,
        })
    return json.dumps({
        "final_frame_index": float(getattr(anim, "final_frame_index", 0.0)),
        "groups": groups,
    }, separators=(",", ":"))


def _store_cache(collection, cache_text: str):
    name = f"sub_stage_light_cache_{collection.name}"
    block = bpy.data.texts.get(name)
    if block is None:
        block = bpy.data.texts.new(name)
    block.clear()
    block.write(cache_text)
    collection["sub_stage_light_cache_text"] = name


def _load_cache(collection):
    if collection is None:
        return None
    name = collection.get("sub_stage_light_cache_text")
    if name:
        block = bpy.data.texts.get(name)
        if block is not None:
            return block.as_string()
    return collection.get("sub_stage_light_cache")


def smash_transform_to_matrix(translation, rotation_xyzw, scale, apply_scale=False, is_sun=True) -> Matrix:
    translation_mat = Matrix.Translation(Vector(translation))
    quat = Quaternion((
        float(rotation_xyzw[3]),
        float(rotation_xyzw[0]),
        float(rotation_xyzw[1]),
        float(rotation_xyzw[2]),
    ))
    rotation_mat = quat.to_matrix().to_4x4()
    if apply_scale:
        scale_mat = Matrix.Diagonal((float(scale[0]), float(scale[1]), float(scale[2]), 1.0))
    else:
        # Smash scale is the lighting region, not a Blender gizmo size.
        scale_mat = Matrix.Identity(4)
    align = SUN_ALIGN if is_sun else Matrix.Identity(4)
    return AXIS_CORRECTION @ translation_mat @ rotation_mat @ align @ scale_mat


def blender_matrix_to_smash(matrix: Matrix, is_sun=True):
    original = AXIS_CORRECTION.inverted() @ matrix
    translation, quat, scale = original.decompose()
    smash_quat = quat @ SUN_ALIGN_QUAT if is_sun else quat
    return translation, smash_quat, scale


def _set_custom_prop(obj, name, value):
    obj[name] = value
    try:
        ui = obj.id_properties_ui(name)
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            ui.update(min=-100.0, max=100.0)
        elif not isinstance(value, bool):
            ui.update(min=-1000.0, max=1000.0)
    except (TypeError, AttributeError, OverflowError):
        pass


def _store_smash_scale(obj, scale):
    _set_custom_prop(obj, "sub_smash_scale", [float(scale[0]), float(scale[1]), float(scale[2])])


def _smash_scale_of(obj):
    stored = obj.get("sub_smash_scale")
    if stored:
        return Vector((float(stored[0]), float(stored[1]), float(stored[2])))
    return Vector(obj.scale)


def _create_light_object(name, collection):
    light = bpy.data.lights.new(name, "SUN")
    if hasattr(light, "angle"):
        light.angle = math.radians(2.0)
    if hasattr(light, "use_nodes"):
        light.use_nodes = False
    if hasattr(light, "use_shadow"):
        light.use_shadow = False
    obj = bpy.data.objects.new(name, light)
    obj.rotation_mode = "QUATERNION"
    collection.objects.link(obj)
    obj["sub_stage_light_node"] = name
    obj["sub_stage_light_kind"] = "Light"
    _set_light_contribution(obj, False, shadows=False)
    return obj


def _create_empty(name, collection, kind):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = 2.0
    obj.rotation_mode = "QUATERNION"
    collection.objects.link(obj)
    obj["sub_stage_light_node"] = name
    obj["sub_stage_light_kind"] = kind
    return obj


def _ensure_action(id_data, name, id_type="OBJECT"):
    if id_data.animation_data is None:
        id_data.animation_data_create()
    action = bpy.data.actions.new(name)
    assign_action(id_data.animation_data, action)
    return action


def _write_fcurve(action, data_path, start_frame, values, index=0, group="", id_type="OBJECT"):
    fcurve = new_fcurve(action, data_path, index=index, action_group=group, id_type=id_type)
    count = len(values)
    fcurve.keyframe_points.add(count=count)
    coords = []
    for offset, value in enumerate(values):
        coords.extend((float(start_frame + offset), float(value)))
    fcurve.keyframe_points.foreach_set("co", coords)
    fcurve.update()
    return fcurve


def _parent_keep_world(obj, parent):
    world = obj.matrix_world.copy()
    obj.parent = parent
    obj.matrix_parent_inverse = parent.matrix_world.inverted()
    obj.matrix_basis = world


def _apply_transform_value(obj, value):
    _store_smash_scale(obj, value.scale)
    obj.matrix_world = smash_transform_to_matrix(
        value.translation, value.rotation, value.scale, is_sun=obj.type == "LIGHT"
    )


def _apply_custom_value(obj, name, value):
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes, bool)):
        stored = [float(v) for v in value]
    elif isinstance(value, (bool,)) or type(value).__name__ == "bool_":
        stored = bool(value)
    else:
        stored = float(value)
    _set_custom_prop(obj, name, stored)
    return stored


def _key_transform(action, obj, start_frame, values, id_type="OBJECT"):
    locations = []
    quaternions = []
    _store_smash_scale(obj, values[0].scale)
    is_sun = obj.type == "LIGHT"
    for value in values:
        matrix = smash_transform_to_matrix(value.translation, value.rotation, value.scale, is_sun=is_sun)
        translation, quat, _scale = matrix.decompose()
        locations.append(translation)
        quaternions.append(quat)
    obj.matrix_world = smash_transform_to_matrix(
        values[0].translation, values[0].rotation, values[0].scale, is_sun=is_sun
    )
    for index, axis_values in enumerate(zip(*locations)):
        _write_fcurve(action, "location", start_frame, axis_values, index=index, group="Transform", id_type=id_type)
    for index, axis_values in enumerate(zip(*[(q.w, q.x, q.y, q.z) for q in quaternions])):
        _write_fcurve(action, "rotation_quaternion", start_frame, axis_values, index=index, group="Transform", id_type=id_type)


def _key_vector_prop(action, obj, name, start_frame, values, id_type="OBJECT"):
    _apply_custom_value(obj, name, values[0])
    for index in range(len(values[0])):
        _write_fcurve(
            action,
            f'["{name}"]',
            start_frame,
            [float(value[index]) for value in values],
            index=index,
            group=name,
            id_type=id_type,
        )


def _import_node_tracks(obj, node, start_frame, stem):
    object_action = None
    data_action = None
    for track in node.tracks:
        values = list(track.values)
        if not values:
            continue
        animated = len(values) > 1
        if track.name == "Transform":
            if animated:
                if object_action is None:
                    object_action = _ensure_action(obj, f"{obj.name} {stem}", "OBJECT")
                _key_transform(object_action, obj, start_frame, values)
            else:
                _apply_transform_value(obj, values[0])
        elif track.name == "CustomFloat0" and obj.type == "LIGHT":
            obj.data.energy = float(values[0])
            _sync_light_shader(obj.data)
            if animated:
                if data_action is None:
                    data_action = _ensure_action(obj.data, f"{obj.name} {stem} Light", "LIGHT")
                _write_fcurve(data_action, "energy", start_frame, values, group="Light", id_type="LIGHT")
        elif track.name == "CustomVector0" and obj.type == "LIGHT":
            color = [float(v) for v in values[0]]
            obj.data.color = color[:3]
            _set_custom_prop(obj, "sub_custom_vector0_w", color[3] if len(color) > 3 else 1.0)
            _sync_light_shader(obj.data)
            if animated:
                if data_action is None:
                    data_action = _ensure_action(obj.data, f"{obj.name} {stem} Light", "LIGHT")
                if object_action is None:
                    object_action = _ensure_action(obj, f"{obj.name} {stem}", "OBJECT")
                for index in range(3):
                    _write_fcurve(
                        data_action,
                        "color",
                        start_frame,
                        [float(value[index]) for value in values],
                        index=index,
                        group="Light",
                        id_type="LIGHT",
                    )
                _write_fcurve(
                    object_action,
                    '["sub_custom_vector0_w"]',
                    start_frame,
                    [float(value[3]) if len(value) > 3 else 1.0 for value in values],
                    group="Light",
                )
        else:
            _apply_custom_value(obj, track.name, values[0])
            if animated:
                if object_action is None:
                    object_action = _ensure_action(obj, f"{obj.name} {stem}", "OBJECT")
                first = values[0]
                if hasattr(first, "__len__") and not isinstance(first, (str, bytes, bool)):
                    _key_vector_prop(object_action, obj, track.name, start_frame, values)
                else:
                    _write_fcurve(
                        object_action,
                        f'["{track.name}"]',
                        start_frame,
                        [float(value) for value in values],
                        group=track.name,
                    )


def import_stage_light(context, filepath: str, start_frame: int = 1):
    anim = ssbh_data_py.anim_data.read_anim(filepath)
    transform_group = None
    for group in anim.groups:
        if group.group_type.name == "Transform":
            transform_group = group
            break
    if transform_group is None:
        raise ValueError("This nuanmb has no Transform group")

    light_nodes = [node for node in transform_group.nodes if is_light_node_name(node.name)]
    other_nodes = [node for node in transform_group.nodes if not is_light_node_name(node.name)]
    if not light_nodes and not any(node.name == SCENE_NODE_NAME for node in other_nodes):
        raise ValueError("No LightChr / LightStg / sceneAttributesForShaderFX nodes found. Use a stage lighting nuanmb.")

    stem = Path(filepath).stem
    collection = bpy.data.collections.new(f"{COLLECTION_NAME} ({stem})")
    context.scene.collection.children.link(collection)
    collection["sub_stage_light_collection"] = True
    collection["sub_stage_light_source"] = filepath
    _store_cache(collection, serialize_anim(anim))

    objects = {}
    for node in light_nodes:
        objects[node.name] = _create_light_object(node.name, collection)
    for node in other_nodes:
        kind = "SceneAttributes" if node.name == SCENE_NODE_NAME else "Extra"
        objects[node.name] = _create_empty(node.name, collection, kind)

    frame_count = int(getattr(anim, "final_frame_index", 0)) + 1
    scene = context.scene
    scene.frame_start = start_frame
    scene.frame_end = start_frame + max(frame_count, 1) - 1

    for node in transform_group.nodes:
        obj = objects.get(node.name)
        if obj is None:
            continue
        _import_node_tracks(obj, node, start_frame, stem)

    root = objects.get("light_set")
    if root is not None:
        context.view_layer.update()
        for name, obj in objects.items():
            if obj == root:
                continue
            _parent_keep_world(obj, root)
        context.view_layer.update()

    active = objects.get("LightChr") or objects.get("LightStg0") or next(iter(objects.values()), None)
    if active is not None:
        context.view_layer.objects.active = active
    apply_stage_light_preview(context)
    _link_imported_lights_to_smash_viewport(context, filepath)
    return collection, len(objects), frame_count


def _group_type(name: str):
    return getattr(ssbh_data_py.anim_data.GroupType, name)


def _make_transform(translation, quat, scale):
    return ssbh_data_py.anim_data.Transform(
        [float(scale.x), float(scale.y), float(scale.z)],
        [float(quat.x), float(quat.y), float(quat.z), float(quat.w)],
        [float(translation.x), float(translation.y), float(translation.z)],
    )


def _fix_quat_continuity(values):
    for index in range(1, len(values)):
        previous = values[index - 1].rotation
        current = values[index].rotation
        pq = Quaternion((previous[3], previous[0], previous[1], previous[2]))
        cq = Quaternion((current[3], current[0], current[1], current[2]))
        if pq.dot(cq) < 0.0:
            values[index].rotation = [-c for c in current]


def _collapse_constant(values):
    if len(values) <= 1:
        return values
    first = values[0]
    if hasattr(first, "translation"):
        def same(a, b):
            return (
                list(a.translation) == list(b.translation)
                and list(a.rotation) == list(b.rotation)
                and list(a.scale) == list(b.scale)
            )
        if all(same(first, value) for value in values[1:]):
            return [first]
        return values
    if all(value == first for value in values[1:]):
        return [first]
    return values


def _read_custom_prop(obj, name, fallback):
    if name not in obj:
        return fallback
    value = obj[name]
    if isinstance(fallback, (list, tuple)):
        return [float(v) for v in value]
    if isinstance(fallback, bool):
        return bool(value)
    return float(value)


def _action_fcurve_map(id_data, id_type="OBJECT"):
    animation = getattr(id_data, "animation_data", None)
    if animation is None or animation.action is None:
        return {}
    mapping = {}
    for fcurve in get_fcurves(animation.action, id_type):
        mapping[(fcurve.data_path, fcurve.array_index)] = fcurve
    return mapping


def _eval_channels(fcurves, data_path, size, frame, fallback):
    result = list(fallback)
    found = False
    for index in range(size):
        fcurve = fcurves.get((data_path, index))
        if fcurve is not None:
            result[index] = float(fcurve.evaluate(frame))
            found = True
    return result, found


def _sample_track(obj, name, track, index, frame, object_fcurves, data_fcurves):
    kind = track.get("kind")
    cached = track.get("values") or []

    if name == "Transform" or kind == "transform":
        loc, has_loc = _eval_channels(object_fcurves, "location", 3, frame, obj.location)
        quat, has_quat = _eval_channels(
            object_fcurves,
            "rotation_quaternion",
            4,
            frame,
            (obj.rotation_quaternion.w, obj.rotation_quaternion.x, obj.rotation_quaternion.y, obj.rotation_quaternion.z),
        )
        scale, has_scale = _eval_channels(object_fcurves, "scale", 3, frame, obj.scale)
        if has_loc or has_quat or has_scale:
            local = (
                Matrix.Translation(Vector(loc))
                @ Quaternion((quat[0], quat[1], quat[2], quat[3])).to_matrix().to_4x4()
            )
            parent = obj.parent
            if parent is not None:
                matrix = parent.matrix_world @ obj.matrix_parent_inverse @ local
            else:
                matrix = local
            translation, smash_quat, _ignored = blender_matrix_to_smash(matrix, is_sun=obj.type == "LIGHT")
            return _make_transform(translation, smash_quat, _smash_scale_of(obj))
        if index == 0 or not cached:
            translation, smash_quat, _ignored = blender_matrix_to_smash(
                obj.matrix_world.copy(), is_sun=obj.type == "LIGHT"
            )
            return _make_transform(translation, smash_quat, _smash_scale_of(obj))
        item = cached[index] if index < len(cached) else cached[-1]
        return ssbh_data_py.anim_data.Transform(item["s"], item["r"], item["t"])

    if name == "CustomFloat0" and obj.type == "LIGHT":
        fcurve = data_fcurves.get(("energy", 0))
        if fcurve is not None:
            return float(fcurve.evaluate(frame))
        return float(obj.data.energy)

    if name == "CustomVector0" and obj.type == "LIGHT":
        color, has_color = _eval_channels(data_fcurves, "color", 3, frame, obj.data.color)
        w_curve = object_fcurves.get(('["sub_custom_vector0_w"]', 0))
        w = float(w_curve.evaluate(frame)) if w_curve is not None else float(obj.get("sub_custom_vector0_w", 1.0))
        if has_color or w_curve is not None or index == 0 or not cached:
            return [color[0], color[1], color[2], w]
        item = cached[index] if index < len(cached) else cached[-1]
        return list(item)

    data_path = f'["{name}"]'
    if kind == "vector":
        fallback = _read_custom_prop(obj, name, cached[0] if cached else [0.0, 0.0, 0.0, 0.0])
        value, found = _eval_channels(object_fcurves, data_path, len(fallback), frame, fallback)
        if found or index == 0 or not cached:
            return value
        return list(cached[index] if index < len(cached) else cached[-1])
    if kind == "bool":
        fcurve = object_fcurves.get((data_path, 0))
        if fcurve is not None:
            return bool(fcurve.evaluate(frame))
        if index == 0 or not cached:
            return _read_custom_prop(obj, name, cached[0] if cached else False)
        return bool(cached[index] if index < len(cached) else cached[-1])
    if kind == "float":
        fcurve = object_fcurves.get((data_path, 0))
        if fcurve is not None:
            return float(fcurve.evaluate(frame))
        if index == 0 or not cached:
            return _read_custom_prop(obj, name, cached[0] if cached else 0.0)
        return float(cached[index] if index < len(cached) else cached[-1])

    if cached:
        return cached[index] if index < len(cached) else cached[-1]
    return None


def _sample_all_nodes(objects, cache, frame_count, start_frame):
    sampled = {}
    node_tracks = {}
    curve_cache = {}
    for group_cache in cache.get("groups", []):
        for node_cache in group_cache.get("nodes", []):
            name = node_cache["name"]
            tracks = {track["name"]: track for track in node_cache.get("tracks", [])}
            node_tracks[name] = tracks
            sampled[name] = {track_name: [] for track_name in tracks}
            obj = objects.get(name)
            if obj is not None:
                data_type = "LIGHT" if obj.type == "LIGHT" else "OBJECT"
                curve_cache[name] = (
                    _action_fcurve_map(obj, "OBJECT"),
                    _action_fcurve_map(obj.data, data_type) if obj.type == "LIGHT" else {},
                )
            else:
                curve_cache[name] = ({}, {})

    for index in range(frame_count):
        frame = start_frame + index
        for node_name, tracks in node_tracks.items():
            obj = objects.get(node_name)
            object_fcurves, data_fcurves = curve_cache[node_name]
            for track_name, track in tracks.items():
                if obj is not None:
                    sampled[node_name][track_name].append(
                        _sample_track(obj, track_name, track, index, frame, object_fcurves, data_fcurves)
                    )
                else:
                    cached = track.get("values") or []
                    if track.get("kind") == "transform":
                        item = cached[index] if index < len(cached) else (cached[-1] if cached else None)
                        if item is not None:
                            sampled[node_name][track_name].append(
                                ssbh_data_py.anim_data.Transform(item["s"], item["r"], item["t"])
                            )
                    elif cached:
                        sampled[node_name][track_name].append(
                            cached[index] if index < len(cached) else cached[-1]
                        )
    return sampled


def _apply_track_flags(track_data, track_cache):
    if "compensate_scale" in track_cache:
        track_data.compensate_scale = track_cache["compensate_scale"]
    flags = track_cache.get("transform_flags")
    if flags:
        track_data.transform_flags = ssbh_data_py.anim_data.TransformFlags(
            override_translation=flags.get("override_translation", False),
            override_rotation=flags.get("override_rotation", False),
            override_scale=flags.get("override_scale", False),
            override_compensate_scale=flags.get("override_compensate_scale", False),
        )


from ...export_progress import export_progress


@export_progress
def export_stage_light(context, filepath: str, preview_frame=None):
    objects = {obj.get("sub_stage_light_node"): obj for obj in find_stage_light_objects(context)}
    if not objects:
        raise ValueError("No imported stage lights found. Import a light.nuanmb first.")

    collection = find_stage_light_collection(context)
    cache_text = _load_cache(collection)
    if not cache_text:
        cache = {
            "final_frame_index": float(context.scene.frame_end - context.scene.frame_start),
            "groups": [{
                "group_type": "Transform",
                "nodes": [{"name": name, "tracks": [
                    {"name": "Transform", "kind": "transform", "values": []},
                    {"name": "CustomFloat0", "kind": "float", "values": []},
                    {"name": "CustomVector0", "kind": "vector", "values": []},
                ]} for name in objects],
            }],
        }
    else:
        cache = json.loads(cache_text)

    if preview_frame is not None:
        start_frame = int(preview_frame)
        frame_count = 1
    else:
        start_frame = context.scene.frame_start
        end_frame = context.scene.frame_end
        frame_count = max(1, end_frame - start_frame + 1)

    sampled = _sample_all_nodes(objects, cache, frame_count, start_frame)

    anim = ssbh_data_py.anim_data.AnimData()
    anim.final_frame_index = 0.0 if preview_frame is not None else float(frame_count - 1)

    for group_cache in cache.get("groups", []):
        group = ssbh_data_py.anim_data.GroupData(_group_type(group_cache["group_type"]))
        for node_cache in group_cache.get("nodes", []):
            node = ssbh_data_py.anim_data.NodeData(node_cache["name"])
            node_samples = sampled.get(node_cache["name"], {})
            for track_cache in node_cache.get("tracks", []):
                track = ssbh_data_py.anim_data.TrackData(track_cache["name"])
                values = [value for value in node_samples.get(track_cache["name"], []) if value is not None]
                if not values:
                    kind = track_cache.get("kind")
                    cached_values = track_cache.get("values", [])
                    if kind == "transform":
                        values = [
                            ssbh_data_py.anim_data.Transform(item["s"], item["r"], item["t"])
                            for item in cached_values
                        ]
                    else:
                        values = list(cached_values)
                if track_cache.get("kind") == "transform":
                    _fix_quat_continuity(values)
                track.values.extend(_collapse_constant(values))
                _apply_track_flags(track, track_cache)
                node.tracks.append(track)
            group.nodes.append(node)
        anim.groups.append(group)

    anim.save(filepath)
    if collection is not None and preview_frame is None:
        collection["sub_stage_light_source"] = filepath
    return frame_count, len(objects)


_PREVIEW_LIGHT_NAME = "smash_stage_lights_preview.nuanmb"
_live_sync_hold = False
_live_sync_pending = False
_live_sync_busy = False
_last_light_fingerprint = None
_last_seen_frame = None


def hold_live_smash_sync():
    """Training Lights / Load Stage Lights should not be overwritten until the user edits a Stage Tools light."""
    global _live_sync_hold
    _live_sync_hold = True


def resume_live_smash_sync():
    global _live_sync_hold
    _live_sync_hold = False


def _using_stage_tools_preview(ssp):
    path = (getattr(ssp, "smash_vp_light_path", "") or "").strip()
    return os.path.basename(path).startswith("smash_stage_lights")


def _preview_light_path():
    root = getattr(bpy.app, "tempdir", "") or ""
    if not root:
        import tempfile
        root = tempfile.gettempdir()
    return os.path.join(root, _PREVIEW_LIGHT_NAME)


def _light_fingerprint(context):
    parts = []
    for obj in find_stage_light_objects(context):
        mw = obj.matrix_world
        loc = mw.to_translation()
        rot = mw.to_quaternion()
        parts.append((
            obj.get("sub_stage_light_node") or obj.name,
            round(loc.x, 4),
            round(loc.y, 4),
            round(loc.z, 4),
            round(rot.w, 4),
            round(rot.x, 4),
            round(rot.y, 4),
            round(rot.z, 4),
        ))
        if obj.type == "LIGHT":
            color = obj.data.color
            parts.append((
                round(float(obj.data.energy), 4),
                round(float(color[0]), 4),
                round(float(color[1]), 4),
                round(float(color[2]), 4),
            ))
        for key in obj.keys():
            if str(key).startswith("Custom"):
                parts.append((str(key), str(obj[key])))
    return tuple(parts)


def _link_imported_lights_to_smash_viewport(context, filepath):
    global _live_sync_hold, _last_light_fingerprint, _last_seen_frame
    ssp = getattr(context.scene, "sub_scene_properties", None)
    if ssp is None or not bool(getattr(ssp, "stage_light_drive_smash_viewport", True)):
        return
    _live_sync_hold = False
    _last_light_fingerprint = _light_fingerprint(context)
    _last_seen_frame = int(context.scene.frame_current)
    try:
        from ..smash_viewport import apply_lighting_file
        apply_lighting_file(filepath, context.scene, force=True)
    except Exception:
        ssp.smash_vp_light_path = filepath


def push_stage_lights_to_smash_viewport(context=None):
    """Write the current Stage Tools lights to a temp nuanmb and load them in Smash Viewport."""
    global _live_sync_busy, _last_light_fingerprint
    context = context or bpy.context
    ssp = getattr(context.scene, "sub_scene_properties", None)
    if ssp is None or not bool(getattr(ssp, "stage_light_drive_smash_viewport", True)):
        return False
    if not find_stage_light_objects(context):
        return False
    path = _preview_light_path()
    _live_sync_busy = True
    try:
        export_stage_light(context, path, preview_frame=context.scene.frame_current)
        from ..smash_viewport import apply_lighting_file
        apply_lighting_file(path, context.scene, force=True)
        _last_light_fingerprint = _light_fingerprint(context)
    except Exception:
        return False
    finally:
        _live_sync_busy = False
    return True


def _flush_live_smash_sync():
    global _live_sync_pending
    _live_sync_pending = False
    push_stage_lights_to_smash_viewport()
    return None


def _schedule_live_smash_sync():
    global _live_sync_pending
    if _live_sync_pending:
        return
    _live_sync_pending = True
    try:
        bpy.app.timers.register(_flush_live_smash_sync, first_interval=0.2)
    except Exception:
        _live_sync_pending = False


@persistent
def _stage_light_depsgraph_update(scene, _depsgraph):
    global _live_sync_hold, _last_light_fingerprint, _last_seen_frame
    if _live_sync_busy:
        return
    ssp = getattr(scene, "sub_scene_properties", None)
    if ssp is None or not bool(getattr(ssp, "stage_light_drive_smash_viewport", True)):
        return
    if getattr(scene.render, "engine", "") != "SMASH_VIEWPORT":
        return
    context = bpy.context
    if not find_stage_light_objects(context):
        return
    fingerprint = _light_fingerprint(context)
    frame = int(scene.frame_current)
    if fingerprint == _last_light_fingerprint:
        _last_seen_frame = frame
        return

    frame_changed = _last_seen_frame is not None and frame != _last_seen_frame
    _last_seen_frame = frame

    # Training Lights / Load Stage Lights: keep that lighting until the user edits a sun.
    if _live_sync_hold:
        if frame_changed:
            _last_light_fingerprint = fingerprint
            return
        _live_sync_hold = False

    # Imported original nuanmb already advances with the timeline in Smash Viewport.
    if frame_changed and not _using_stage_tools_preview(ssp):
        _last_light_fingerprint = fingerprint
        return

    _last_light_fingerprint = fingerprint
    _schedule_live_smash_sync()


class SUB_OP_import_stage_light(Operator, ImportHelper):
    bl_idname = "sub.import_stage_light"
    bl_label = "Import Light Nuanmb"
    bl_description = "Import a stage lighting .nuanmb as editable Blender lights"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".nuanmb"
    filter_glob: StringProperty(default="*.nuanmb", options={"HIDDEN"})

    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        last = getattr(ssp, "last_stage_light_dir", "")
        if last:
            self.filepath = os.path.join(last, "light.nuanmb")
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        ssp.last_stage_light_dir = os.path.dirname(self.filepath)
        try:
            collection, count, frames = import_stage_light(context, self.filepath, context.scene.frame_start)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {count} lighting nodes ({frames} frames) into '{collection.name}'")
        return {"FINISHED"}


class SUB_OP_export_stage_light(Operator, ExportHelper):
    bl_idname = "sub.export_stage_light"
    bl_label = "Export Light Nuanmb"
    bl_description = "Export the edited stage lights back to a lighting .nuanmb"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".nuanmb"
    filter_glob: StringProperty(default="*.nuanmb", options={"HIDDEN"})

    def invoke(self, context, event):
        collection = find_stage_light_collection(context)
        source = collection.get("sub_stage_light_source") if collection else ""
        if source:
            self.filepath = source
        else:
            ssp = context.scene.sub_scene_properties
            last = getattr(ssp, "last_stage_light_dir", "")
            self.filepath = os.path.join(last, "light.nuanmb") if last else "light.nuanmb"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        ssp.last_stage_light_dir = os.path.dirname(self.filepath)
        try:
            frames, count = export_stage_light(context, self.filepath)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Exported {count} lighting nodes ({frames} frames)")
        return {"FINISHED"}


classes = (
    SUB_OP_import_stage_light,
    SUB_OP_export_stage_light,
)
