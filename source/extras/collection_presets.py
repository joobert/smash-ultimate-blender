"""Safe, versioned collection and armature-layout presets."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from datetime import datetime, timezone

import bpy
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList


FORMAT_ID = "smash-ultimate-blender.collection-preset"
FORMAT_VERSION = 2
_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_. -]+")


def _prefs(context=None):
    from ..addon_preferences import get_addon_preferences

    return get_addon_preferences(context)


def _library_dir(context, create=True):
    props = context.scene.sub_collection_presets
    if props.library_scope == 'BLEND' and bpy.data.filepath:
        path = os.path.join(os.path.dirname(bpy.data.filepath), "collection_presets")
    elif props.library_scope == 'CUSTOM':
        prefs = _prefs(context)
        configured = bpy.path.abspath(prefs.collection_preset_directory) if prefs else ""
        path = configured or bpy.utils.user_resource(
            'CONFIG', path="smash-ultimate-blender/collection_presets", create=create
        )
    else:
        path = bpy.utils.user_resource(
            'CONFIG', path="smash-ultimate-blender/collection_presets", create=create
        )
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _clean_name(name):
    clean = _SAFE_FILENAME.sub("_", name.strip()).strip(". ")
    return clean or "collection_preset"


def _preset_path(context, name):
    return os.path.join(_library_dir(context), _clean_name(name) + ".json")


def _load_path(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Preset root must be a JSON object")
    if data.get("format") == FORMAT_ID:
        version = int(data.get("version", 0))
        if version > FORMAT_VERSION:
            raise ValueError(f"Preset version {version} is newer than supported version {FORMAT_VERSION}")
        return data
    if "collection_tree" in data or "bone_collections" in data:
        fingerprint = data.get("armature_fingerprint", {})
        related_names = set(fingerprint.get("meshes", []))
        if data.get("armature_name"):
            related_names.add(data["armature_name"])
        placements = data.get("object_primary_collection", {})
        materials = data.get("material_assignments", {})
        if related_names:
            placements = {name: value for name, value in placements.items() if name in related_names}
            materials = {name: value for name, value in materials.items() if name in related_names}
            collection_tree = _filter_collection_nodes(data.get("collection_tree", []), related_names)
        else:
            collection_tree = data.get("collection_tree", [])
        return {
            "format": FORMAT_ID,
            "version": 1,
            "name": data.get("name") or os.path.splitext(os.path.basename(path))[0],
            "armature_name": data.get("armature_name", ""),
            "armature_fingerprint": data.get("armature_fingerprint", {}),
            "sections": {
                "scene_collections": collection_tree,
                "object_collections": {
                    name: [collection] if collection else []
                    for name, collection in placements.items()
                },
                "materials": materials,
                "armature_display": data.get("armature_display", {}),
                "bone_collections": data.get("bone_collections", []),
                "bone_display": data.get("bone_data", {}),
            },
            "legacy_source": True,
        }
    raise ValueError("Not a recognized collection preset")


def _filter_collection_nodes(nodes, object_names):
    """Keep only legacy collection branches needed by scoped preset objects."""
    filtered = []
    for node in nodes:
        children = _filter_collection_nodes(node.get("children", []), object_names)
        objects = [name for name in node.get("objects", []) if name in object_names]
        if not children and not objects:
            continue
        copy = dict(node)
        copy["children"] = children
        copy["objects"] = objects
        filtered.append(copy)
    return filtered


def _write_preset(path, preset):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(preset, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp_path, path)


def _active_item(context):
    props = context.scene.sub_collection_presets
    if not props.presets:
        return None
    index = min(max(props.active_index, 0), len(props.presets) - 1)
    return props.presets[index]


def refresh_presets(context, select_path=""):
    props = context.scene.sub_collection_presets
    previous = select_path or (_active_item(context).path if _active_item(context) else "")
    props.presets.clear()
    directory = _library_dir(context)
    for filename in sorted(os.listdir(directory), key=str.casefold):
        if not filename.lower().endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        item = props.presets.add()
        item.name = os.path.splitext(filename)[0]
        item.path = path
        try:
            preset = _load_path(path)
            item.name = preset.get("name") or item.name
            item.model_folder = preset.get("model_folder", "")
            item.version = int(preset.get("version", 1))
            item.is_valid = True
            item.is_legacy = bool(preset.get("legacy_source") or item.version < FORMAT_VERSION)
        except Exception as error:
            item.is_valid = False
            item.error = str(error)
    props.active_index = 0
    for index, item in enumerate(props.presets):
        if os.path.normcase(item.path) == os.path.normcase(previous):
            props.active_index = index
            break


def _normalize_name(name):
    return _BLENDER_SUFFIX.sub("", name).casefold().strip()


def match_names(preset_names, current_names, use_fuzzy=False, threshold=0.85):
    """Greedily map preset names to unique current names with conservative tiers."""
    remaining = list(dict.fromkeys(current_names))
    result = {"exact": [], "normalized": [], "fuzzy": [], "missing": [], "new": []}
    for preset_name in dict.fromkeys(preset_names):
        if preset_name in remaining:
            result["exact"].append((preset_name, preset_name, 1.0))
            remaining.remove(preset_name)
            continue
        normalized = _normalize_name(preset_name)
        normal_hits = [name for name in remaining if _normalize_name(name) == normalized]
        if normal_hits:
            current_name = sorted(normal_hits, key=lambda value: (len(value), value.casefold()))[0]
            result["normalized"].append((preset_name, current_name, 0.99))
            remaining.remove(current_name)
            continue
        if use_fuzzy and remaining:
            ranked = sorted(
                (
                    (difflib.SequenceMatcher(None, normalized, _normalize_name(name)).ratio(), name)
                    for name in remaining
                ),
                reverse=True,
            )
            ratio, current_name = ranked[0]
            if ratio >= threshold:
                result["fuzzy"].append((preset_name, current_name, ratio))
                remaining.remove(current_name)
                continue
        result["missing"].append(preset_name)
    result["new"] = remaining
    result["map"] = {
        preset: current
        for key in ("exact", "normalized", "fuzzy")
        for preset, current, _ratio in result[key]
    }
    return result


def _related_objects(armature, include_descendants=True, include_shapes=False):
    related = {armature}
    for obj in bpy.data.objects:
        if obj == armature:
            continue
        if obj.type == 'MESH' and any(
            modifier.type == 'ARMATURE' and modifier.object == armature
            for modifier in obj.modifiers
        ):
            related.add(obj)
        if obj.parent == armature:
            related.add(obj)
    if include_descendants:
        related.update(armature.children_recursive)
    if include_shapes and armature.pose:
        related.update(
            pose_bone.custom_shape
            for pose_bone in armature.pose.bones
            if pose_bone.custom_shape is not None
        )
    return sorted(related, key=lambda obj: obj.name.casefold())


def _serialize_layer_collection(layer_collection, scoped_names):
    children = []
    for child in layer_collection.children:
        serialized = _serialize_layer_collection(child, scoped_names)
        if serialized:
            children.append(serialized)
    direct_objects = sorted(
        (obj.name for obj in layer_collection.collection.objects if obj.name in scoped_names),
        key=str.casefold,
    )
    if not direct_objects and not children:
        return None
    collection = layer_collection.collection
    return {
        "name": collection.name,
        "color_tag": collection.color_tag,
        "hide_render": collection.hide_render,
        "hide_viewport_col": collection.hide_viewport,
        "exclude": layer_collection.exclude,
        "hide_viewport_lc": layer_collection.hide_viewport,
        "holdout": layer_collection.holdout,
        "indirect_only": layer_collection.indirect_only,
        "objects": direct_objects,
        "children": children,
    }


def _serialize_color(color):
    result = {"palette": color.palette}
    if color.palette == 'CUSTOM':
        result["custom"] = {
            "normal": list(color.custom.normal),
            "select": list(color.custom.select),
            "active": list(color.custom.active),
        }
    return result


def _serialize_bone(pose_bone):
    bone = pose_bone.bone
    data = {}
    if hasattr(bone, "color"):
        data["color"] = _serialize_color(bone.color)
    data["custom_shape"] = pose_bone.custom_shape.name if pose_bone.custom_shape else None
    data["custom_shape_scale_xyz"] = list(pose_bone.custom_shape_scale_xyz)
    data["use_custom_shape_bone_size"] = pose_bone.use_custom_shape_bone_size
    for attribute in ("custom_shape_translation", "custom_shape_rotation_euler"):
        value = getattr(pose_bone, attribute, None)
        if value is not None:
            data[attribute] = list(value)
    if hasattr(pose_bone, "custom_shape_wire_width"):
        data["custom_shape_wire_width"] = pose_bone.custom_shape_wire_width
    data["custom_shape_transform"] = (
        pose_bone.custom_shape_transform.name if pose_bone.custom_shape_transform else None
    )
    if hasattr(pose_bone, "color"):
        data["pose_color"] = _serialize_color(pose_bone.color)
    return data


def _all_bone_collections(armature_data):
    collections = getattr(armature_data, "collections_all", None)
    if collections is not None:
        return collections
    # Blender 4 exposes children on root collections even where collections_all
    # is unavailable.
    result = []
    stack = list(armature_data.collections)
    while stack:
        collection = stack.pop(0)
        result.append(collection)
        stack[0:0] = list(getattr(collection, "children", ()))
    return result


def _bone_collection_get(armature_data, name):
    collections = _all_bone_collections(armature_data)
    getter = getattr(collections, "get", None)
    if getter:
        return getter(name)
    return next((collection for collection in collections if collection.name == name), None)


def _serialize_bone_collections(armature):
    return [
        {
            "name": collection.name,
            "is_visible": collection.is_visible,
            "parent": collection.parent.name if collection.parent else None,
            "bones": sorted((bone.name for bone in collection.bones), key=str.casefold),
        }
        for collection in _all_bone_collections(armature.data)
    ]


def build_preset(name, armature, context):
    props = context.scene.sub_collection_presets
    objects = _related_objects(
        armature, props.include_descendants, props.include_custom_shapes
    )
    object_names = {obj.name for obj in objects}
    collection_tree = []
    for child in context.view_layer.layer_collection.children:
        serialized = _serialize_layer_collection(child, object_names)
        if serialized:
            collection_tree.append(serialized)
    sections = {}
    if props.use_scene_collections:
        sections["scene_collections"] = collection_tree
    if props.use_object_placement:
        sections["object_collections"] = {
            obj.name: sorted((collection.name for collection in obj.users_collection), key=str.casefold)
            for obj in objects
        }
        sections["object_types"] = {obj.name: obj.type for obj in objects}
    if props.use_materials:
        sections["materials"] = {
            obj.name: [slot.material.name if slot.material else None for slot in obj.material_slots]
            for obj in objects if obj.type == 'MESH'
        }
    if props.use_armature_display:
        arm = armature.data
        sections["armature_display"] = {
            "display_type": arm.display_type,
            "show_names": arm.show_names,
            "show_axes": arm.show_axes,
            "show_bone_colors": getattr(arm, "show_bone_colors", True),
            "show_in_front": armature.show_in_front,
        }
    if props.use_bone_collections:
        sections["bone_collections"] = _serialize_bone_collections(armature)
    if props.use_bone_display:
        sections["bone_display"] = {
            pose_bone.name: _serialize_bone(pose_bone) for pose_bone in armature.pose.bones
        }
    return {
        "format": FORMAT_ID,
        "version": FORMAT_VERSION,
        "name": name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "armature_name": armature.name,
        "armature_fingerprint": {
            "bones": sorted((bone.name for bone in armature.data.bones), key=str.casefold),
            "objects": sorted(object_names, key=str.casefold),
        },
        "capture": {
            "include_descendants": props.include_descendants,
            "include_custom_shapes": props.include_custom_shapes,
        },
        "sections": sections,
    }


def _find_layer_collection(root, collection):
    stack = [root]
    while stack:
        layer = stack.pop()
        if layer.collection == collection:
            return layer
        stack.extend(layer.children)
    return None


def _scene_collections(root):
    result = {root}
    stack = list(root.children)
    while stack:
        collection = stack.pop()
        if collection in result:
            continue
        result.add(collection)
        stack.extend(collection.children)
    return result


def _ensure_collection_tree(parent, node, view_layer, report):
    name = node.get("name", "Collection")
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        report["collections_created"].append(name)
    linked_to_parent = collection.name in parent.children
    if not linked_to_parent:
        try:
            parent.children.link(collection)
            linked_to_parent = True
        except RuntimeError as error:
            report["warnings"].append(
                f"Could not link collection {collection.name!r} under {parent.name!r}: {error}"
            )
    if linked_to_parent:
        # Restore one hierarchy inside this scene. Links belonging exclusively
        # to other scenes are deliberately left alone.
        scene_root = view_layer.layer_collection.collection
        for possible_parent in _scene_collections(scene_root):
            if possible_parent != parent and collection.name in possible_parent.children:
                possible_parent.children.unlink(collection)
    for attribute, key, default in (
        ("color_tag", "color_tag", 'NONE'),
        ("hide_render", "hide_render", False),
        ("hide_viewport", "hide_viewport_col", False),
    ):
        try:
            setattr(collection, attribute, node.get(key, default))
        except (AttributeError, TypeError, ValueError):
            pass
    layer = _find_layer_collection(view_layer.layer_collection, collection)
    if layer:
        for attribute, key in (
            ("exclude", "exclude"),
            ("hide_viewport", "hide_viewport_lc"),
            ("holdout", "holdout"),
            ("indirect_only", "indirect_only"),
        ):
            if key in node:
                try:
                    setattr(layer, attribute, bool(node[key]))
                except (AttributeError, TypeError, ValueError):
                    pass
    for child in node.get("children", []):
        _ensure_collection_tree(collection, child, view_layer, report)


def _apply_color(color, data):
    try:
        color.palette = data.get("palette", 'DEFAULT')
        custom = data.get("custom")
        if color.palette == 'CUSTOM' and custom:
            color.custom.normal = custom["normal"]
            color.custom.select = custom["select"]
            color.custom.active = custom["active"]
    except (AttributeError, KeyError, TypeError, ValueError):
        pass


def _match_for(preset_names, current_names, props):
    return match_names(
        preset_names,
        current_names,
        use_fuzzy=props.use_fuzzy_matching,
        threshold=props.fuzzy_threshold,
    )


def preview_preset(preset, armature, context):
    props = context.scene.sub_collection_presets
    sections = preset.get("sections", {})
    objects = _related_objects(
        armature, props.include_descendants, props.include_custom_shapes
    )
    preset_objects = set(sections.get("object_collections", {}))
    preset_objects.update(sections.get("materials", {}))
    object_match = _match_for(
        sorted(preset_objects, key=str.casefold), [obj.name for obj in objects], props
    )
    preset_bones = set(sections.get("bone_display", {}))
    preset_bones.update(
        bone_name
        for collection in sections.get("bone_collections", [])
        for bone_name in collection.get("bones", [])
    )
    bone_match = _match_for(
        sorted(preset_bones, key=str.casefold),
        [bone.name for bone in armature.data.bones],
        props,
    )
    preset_collections = [item.get("name", "") for item in sections.get("bone_collections", [])]
    bone_collection_match = _match_for(
        preset_collections,
        [collection.name for collection in _all_bone_collections(armature.data)],
        props,
    )
    return {
        "objects": object_match,
        "bones": bone_match,
        "bone_collections": bone_collection_match,
        "sections": sorted(sections),
    }


def _apply_bone_display(armature, data, match, report):
    for preset_name, settings in data.items():
        current_name = match["map"].get(preset_name)
        if not current_name:
            continue
        bone = armature.data.bones.get(current_name)
        pose_bone = armature.pose.bones.get(current_name)
        if bone and "color" in settings and hasattr(bone, "color"):
            _apply_color(bone.color, settings["color"])
        if not pose_bone:
            continue
        shape_name = settings.get("custom_shape")
        pose_bone.custom_shape = bpy.data.objects.get(shape_name) if shape_name else None
        if shape_name and pose_bone.custom_shape is None:
            report["warnings"].append(
                f"Bone {current_name}: custom shape {shape_name!r} was not found"
            )
        for attribute in (
            "custom_shape_scale_xyz",
            "use_custom_shape_bone_size",
            "custom_shape_translation",
            "custom_shape_rotation_euler",
            "custom_shape_wire_width",
        ):
            if attribute in settings and hasattr(pose_bone, attribute):
                try:
                    setattr(pose_bone, attribute, settings[attribute])
                except (TypeError, ValueError):
                    pass
        transform_name = settings.get("custom_shape_transform")
        if transform_name:
            pose_bone.custom_shape_transform = armature.pose.bones.get(
                match["map"].get(transform_name, transform_name)
            )
        else:
            pose_bone.custom_shape_transform = None
        if "pose_color" in settings and hasattr(pose_bone, "color"):
            _apply_color(pose_bone.color, settings["pose_color"])


def _apply_bone_collections(armature, data, bone_match, collection_match, report):
    arm = armature.data
    collection_map = dict(collection_match["map"])
    for item in data:
        preset_name = item.get("name", "Collection")
        current_name = collection_map.get(preset_name)
        collection = _bone_collection_get(arm, current_name) if current_name else None
        if collection is None:
            collection = arm.collections.new(preset_name)
            collection_map[preset_name] = collection.name
            report["bone_collections_created"].append(collection.name)
        collection.is_visible = item.get("is_visible", True)
    # Parenting is available in Blender 4.0+, but keep flat layouts usable if an
    # older point release exposes it as read-only.
    for item in data:
        parent_name = item.get("parent")
        if not parent_name:
            continue
        collection = _bone_collection_get(arm, collection_map.get(item.get("name")))
        parent = _bone_collection_get(arm, collection_map.get(parent_name, parent_name))
        if collection and parent and collection != parent:
            try:
                collection.parent = parent
            except (AttributeError, TypeError, RuntimeError):
                report["warnings"].append(
                    f"Could not parent bone collection {collection.name!r} to {parent.name!r}"
                )
    desired = {}
    for item in data:
        collection = _bone_collection_get(arm, collection_map.get(item.get("name")))
        if not collection:
            continue
        for preset_bone in item.get("bones", []):
            current_bone = bone_match["map"].get(preset_bone)
            if current_bone:
                desired.setdefault(current_bone, []).append(collection)
    for bone_name, target_collections in desired.items():
        bone = arm.bones.get(bone_name)
        if not bone:
            continue
        for collection in _all_bone_collections(arm):
            if bone.name in collection.bones and collection not in target_collections:
                collection.unassign(bone)
        for collection in target_collections:
            collection.assign(bone)


def apply_preset(preset, armature, context):
    props = context.scene.sub_collection_presets
    sections = preset.get("sections", {})
    preview = preview_preset(preset, armature, context)
    report = {
        **preview,
        "collections_created": [],
        "bone_collections_created": [],
        "objects_moved": [],
        "materials_applied": 0,
        "warnings": [],
    }
    object_map = preview["objects"]["map"]
    bone_match = preview["bones"]
    bone_collection_match = preview["bone_collections"]

    if props.use_scene_collections:
        for root in sections.get("scene_collections", []):
            _ensure_collection_tree(context.scene.collection, root, context.view_layer, report)

    if props.use_object_placement:
        current_scene_collections = _scene_collections(context.scene.collection)
        for preset_name, collection_names in sections.get("object_collections", {}).items():
            obj = bpy.data.objects.get(object_map.get(preset_name, ""))
            targets = [bpy.data.collections.get(name) for name in collection_names]
            targets = [collection for collection in targets if collection is not None]
            if not obj or not targets:
                continue
            for collection in list(obj.users_collection):
                if collection in current_scene_collections:
                    collection.objects.unlink(obj)
            for collection in targets:
                if obj.name not in collection.objects:
                    collection.objects.link(obj)
            report["objects_moved"].append(obj.name)
        if props.unmatched_behavior == 'MOVE':
            unmatched = bpy.data.collections.get(props.unmatched_collection)
            if unmatched is None:
                unmatched = bpy.data.collections.new(props.unmatched_collection)
                context.scene.collection.children.link(unmatched)
                unmatched.color_tag = 'COLOR_01'
            for current_name in preview["objects"]["new"]:
                obj = bpy.data.objects.get(current_name)
                if not obj:
                    continue
                for collection in list(obj.users_collection):
                    if collection in current_scene_collections:
                        collection.objects.unlink(obj)
                unmatched.objects.link(obj)
                report["objects_moved"].append(obj.name)

    if props.use_materials:
        for preset_name, materials in sections.get("materials", {}).items():
            obj = bpy.data.objects.get(object_map.get(preset_name, ""))
            if not obj or obj.type != 'MESH':
                continue
            for index, material_name in enumerate(materials):
                if index >= len(obj.material_slots):
                    report["warnings"].append(
                        f"{obj.name}: preset has more material slots than the current mesh"
                    )
                    break
                material = bpy.data.materials.get(material_name) if material_name else None
                if material_name and material is None:
                    report["warnings"].append(
                        f"{obj.name}: material {material_name!r} was not found"
                    )
                    continue
                obj.material_slots[index].material = material
                report["materials_applied"] += 1

    if props.use_armature_display and sections.get("armature_display"):
        data = sections["armature_display"]
        arm = armature.data
        for attribute in ("display_type", "show_names", "show_axes", "show_bone_colors"):
            if attribute in data and hasattr(arm, attribute):
                setattr(arm, attribute, data[attribute])
        if "show_in_front" in data:
            armature.show_in_front = data["show_in_front"]

    if props.use_bone_collections and sections.get("bone_collections"):
        _apply_bone_collections(
            armature, sections["bone_collections"], bone_match, bone_collection_match, report
        )

    if props.use_bone_display and sections.get("bone_display"):
        _apply_bone_display(armature, sections["bone_display"], bone_match, report)
    return report


def _format_match(lines, label, match):
    lines.extend(
        (
            f"{label}: {len(match['exact'])} exact, {len(match['normalized'])} normalized, "
            f"{len(match['fuzzy'])} fuzzy, {len(match['missing'])} missing, {len(match['new'])} unmatched"
        ,)
    )
    for preset, current, ratio in match["normalized"] + match["fuzzy"]:
        lines.append(f"  {preset} -> {current} ({ratio:.0%})")
    for name in match["missing"]:
        lines.append(f"  Missing: {name}")


def format_report(preset, report, applied=False):
    lines = [f"Collection preset: {preset.get('name', 'Unnamed')}"]
    lines.append("Applied successfully" if applied else "Preview only; no data changed")
    lines.append("Sections: " + ", ".join(report.get("sections", [])))
    lines.append("")
    _format_match(lines, "Objects", report["objects"])
    _format_match(lines, "Bones", report["bones"])
    _format_match(lines, "Bone collections", report["bone_collections"])
    if applied:
        lines.extend((
            "",
            f"Scene collections created: {len(report['collections_created'])}",
            f"Bone collections created: {len(report['bone_collections_created'])}",
            f"Objects moved: {len(set(report['objects_moved']))}",
            f"Material slots applied: {report['materials_applied']}",
        ))
    if report.get("warnings"):
        lines.append("")
        lines.append("Warnings:")
        lines.extend("  " + warning for warning in report["warnings"])
    return "\n".join(lines)


def _preset_and_armature(context, operator):
    armature = context.active_object
    if not armature or armature.type != 'ARMATURE':
        operator.report({'ERROR'}, "Select an armature first")
        return None, None
    item = _active_item(context)
    if not item or not item.is_valid:
        operator.report({'ERROR'}, "Select a valid preset first")
        return None, None
    try:
        return _load_path(item.path), armature
    except Exception as error:
        operator.report({'ERROR'}, f"Could not load preset: {error}")
        return None, None


class SUB_PG_collection_preset_item(PropertyGroup):
    model_folder: StringProperty(default="")
    path: StringProperty(subtype='FILE_PATH')
    version: IntProperty(default=0)
    is_valid: BoolProperty(default=False)
    is_legacy: BoolProperty(default=False)
    error: StringProperty(default="")


def _refresh_library(self, context):
    if context and context.scene and hasattr(context.scene, "sub_collection_presets"):
        refresh_presets(context)


class SUB_PG_collection_preset_settings(PropertyGroup):
    presets: CollectionProperty(type=SUB_PG_collection_preset_item)
    active_index: IntProperty(default=0)
    library_scope: EnumProperty(
        name="Library",
        items=(
            ('BLEND', "Blend File", "Store next to the saved blend file; unsaved files use Global"),
            ('GLOBAL', "Global", "Store in the Blender user configuration"),
            ('CUSTOM', "Custom", "Use the directory configured in add-on preferences"),
        ),
        default='BLEND',
        update=_refresh_library,
    )
    last_report: StringProperty(default="")
    include_descendants: BoolProperty(
        name="Include Descendants", default=True,
        description="Include descendants of the selected armature in object placement matching",
    )
    include_custom_shapes: BoolProperty(
        name="Include Custom Shapes", default=False,
        description="Include pose-bone custom shape objects in object placement matching",
    )
    use_scene_collections: BoolProperty(name="Collection Hierarchy", default=True)
    use_object_placement: BoolProperty(name="Object Placement", default=True)
    use_materials: BoolProperty(name="Materials", default=True)
    use_bone_collections: BoolProperty(name="Bone Collections", default=True)
    use_bone_display: BoolProperty(name="Bone Colors and Shapes", default=True)
    use_armature_display: BoolProperty(name="Armature Display", default=True)
    use_fuzzy_matching: BoolProperty(
        name="Allow Fuzzy Matching", default=False,
        description="Permit similarity matching after exact, case-insensitive, and .001-normalized matching",
    )
    fuzzy_threshold: FloatProperty(
        name="Fuzzy Threshold", default=0.85, min=0.5, max=1.0, subtype='FACTOR'
    )
    unmatched_behavior: EnumProperty(
        name="Unmatched Objects",
        items=(
            ('KEEP', "Keep in Place", "Do not move current objects absent from the preset"),
            ('MOVE', "Move to Unmatched", "Move current unmatched related objects to a collection"),
        ),
        default='KEEP',
    )
    unmatched_collection: StringProperty(name="Collection", default="Unmatched")
    show_options: BoolProperty(name="Options", default=False)


class SUB_UL_collection_presets(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_prop, _index):
        if not item.is_valid:
            layout.label(text=item.name, icon='ERROR')
            return
        layout.label(text=item.name, icon='PRESET')

    def filter_items(self, _context, data, propname):
        items = getattr(data, propname)
        flags = [self.bitflag_filter_item] * len(items)
        if self.filter_name:
            needle = self.filter_name.casefold()
            flags = [self.bitflag_filter_item if needle in item.name.casefold() else 0 for item in items]
        return flags, []


class SUB_OP_collection_presets_refresh(Operator):
    bl_idname = "sub.collection_presets_refresh"
    bl_label = "Refresh Collection Presets"
    bl_description = "Rescan the active collection preset library"

    def execute(self, context):
        refresh_presets(context)
        return {'FINISHED'}


def _model_folder_parts(path):
    return [part for part in str(path).replace("\\", "/").split("/") if part]


def auto_apply_model_preset(context, armature, model_folder, operator):
    """Apply the closest uniquely linked preset from the active library."""
    if not hasattr(context.scene, "sub_collection_presets"):
        return
    directory = _library_dir(context, create=False)
    if not directory or not os.path.isdir(directory):
        return
    parts = [part.casefold() for part in reversed(_model_folder_parts(model_folder))]
    matches = []
    for filename in sorted(os.listdir(directory), key=str.casefold):
        if not filename.lower().endswith(".json"):
            continue
        try:
            preset = _load_path(os.path.join(directory, filename))
            linked = preset.get("model_folder", "").strip().casefold()
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if linked and linked in parts:
            matches.append((parts.index(linked), preset))
    if not matches:
        return
    nearest = min(rank for rank, _preset in matches)
    presets = [preset for rank, preset in matches if rank == nearest]
    if len(presets) != 1:
        operator.report({'WARNING'}, "Collection preset auto-apply skipped: multiple presets link to this model folder")
        return
    preset = presets[0]
    report = apply_preset(preset, armature, context)
    context.scene.sub_collection_presets.last_report = format_report(preset, report, applied=True)
    operator.report({'INFO'}, f"Auto-applied collection preset {preset.get('name', 'Unnamed')!r}")


class SUB_OP_collection_preset_link_model(Operator):
    bl_idname = "sub.collection_preset_link_model"
    bl_label = "Link to Model Folder"
    bl_description = "Auto-apply this preset when an imported model path contains this folder name; blank removes the link"

    model_folder: StringProperty(name="Model Folder Name", default="")

    @classmethod
    def poll(cls, context):
        item = _active_item(context)
        return item is not None and item.is_valid

    def invoke(self, context, _event):
        item = _active_item(context)
        self.model_folder = item.model_folder
        if not self.model_folder:
            obj = context.active_object
            path = obj.get("sub_smash_model_folder", "") if obj else ""
            parts = _model_folder_parts(path)
            # Prefer the fighter/model name over a generic costume folder.
            model_index = next((i for i, part in enumerate(parts) if part.casefold() == 'model'), -1)
            self.model_folder = parts[model_index - 1] if model_index > 0 else (parts[-1] if parts else "")
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        self.layout.prop(self, "model_folder")
        self.layout.label(text="Matches a folder name anywhere in the model path.")
        self.layout.label(text="Leave blank to disable automatic application.")

    def execute(self, context):
        item = _active_item(context)
        if item is None or not item.is_valid:
            return {'CANCELLED'}
        folder = self.model_folder.strip()
        if folder in {'.', '..'} or '/' in folder or "\\" in folder:
            self.report({'ERROR'}, "Enter one folder name, not a full path")
            return {'CANCELLED'}
        try:
            preset = _load_path(item.path)
            preset["model_folder"] = folder
            _write_preset(item.path, preset)
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, f"Could not save model folder link: {error}")
            return {'CANCELLED'}
        refresh_presets(context, item.path)
        return {'FINISHED'}


class SUB_OP_collection_preset_save(Operator):
    bl_idname = "sub.collection_preset_save"
    bl_label = "Save Collection Preset"
    bl_description = "Save the selected armature layout as a new collection preset"
    bl_options = {'REGISTER'}

    preset_name: StringProperty(name="Name", default="collection_preset")
    overwrite: BoolProperty(name="Overwrite Existing", default=False)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, _context):
        self.layout.prop(self, "preset_name")
        self.layout.prop(self, "overwrite")

    def execute(self, context):
        name = self.preset_name.strip()
        if not name:
            self.report({'ERROR'}, "Preset name cannot be empty")
            return {'CANCELLED'}
        path = _preset_path(context, name)
        if os.path.exists(path) and not self.overwrite:
            self.report({'ERROR'}, "A preset with this name exists; enable Overwrite Existing")
            return {'CANCELLED'}
        preset = build_preset(name, context.active_object, context)
        if os.path.exists(path):
            preset["model_folder"] = _load_path(path).get("model_folder", "")
        _write_preset(path, preset)
        refresh_presets(context, path)
        self.report({'INFO'}, f"Saved collection preset {name!r}")
        return {'FINISHED'}


class SUB_OP_collection_preset_update(Operator):
    bl_idname = "sub.collection_preset_update"
    bl_label = "Update Collection Preset"
    bl_description = "Replace the selected preset using the current armature and section settings"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None and context.active_object.type == 'ARMATURE'
            and _active_item(context) is not None
        )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Update Collection Preset?", icon='WARNING'
        )

    def execute(self, context):
        item = _active_item(context)
        if not item:
            return {'CANCELLED'}
        previous = _load_path(item.path)
        preset = build_preset(item.name, context.active_object, context)
        preset["model_folder"] = previous.get("model_folder", "")
        _write_preset(item.path, preset)
        refresh_presets(context, item.path)
        self.report({'INFO'}, f"Updated collection preset {item.name!r}")
        return {'FINISHED'}


class SUB_OP_collection_preset_apply(Operator):
    bl_idname = "sub.collection_preset_apply"
    bl_label = "Apply Collection Preset"
    bl_description = "Preview matches and apply enabled preset sections to the selected armature"
    bl_options = {'REGISTER', 'UNDO'}

    accept_fuzzy: BoolProperty(
        name="Accept Fuzzy Matches", default=False,
        description="Confirm the fuzzy mappings displayed above",
    )
    _preview = None
    _preset = None

    def invoke(self, context, _event):
        preset, armature = _preset_and_armature(context, self)
        if preset is None:
            return {'CANCELLED'}
        self._preset = preset
        self._preview = preview_preset(preset, armature, context)
        return context.window_manager.invoke_props_dialog(self, width=680)

    def draw(self, context):
        layout = self.layout
        preview = self._preview
        if not preview:
            layout.label(text="Preview unavailable", icon='ERROR')
            return
        for label, key in (("Objects", "objects"), ("Bones", "bones"), ("Bone Collections", "bone_collections")):
            match = preview[key]
            row = layout.row()
            row.label(text=label)
            row.label(
                text=(
                    f"{len(match['exact'])} exact, {len(match['normalized'])} normalized, "
                    f"{len(match['fuzzy'])} fuzzy, {len(match['missing'])} missing"
                )
            )
            for preset_name, current_name, ratio in match["fuzzy"][:8]:
                layout.label(
                    text=f"{preset_name}  ->  {current_name} ({ratio:.0%})",
                    icon='QUESTION',
                )
        fuzzy_count = sum(len(preview[key]["fuzzy"]) for key in ("objects", "bones", "bone_collections"))
        if fuzzy_count:
            box = layout.box()
            box.alert = not self.accept_fuzzy
            box.prop(self, "accept_fuzzy")
        layout.separator()
        layout.label(text="Enabled sections are taken from the panel options.", icon='INFO')

    def execute(self, context):
        preset, armature = _preset_and_armature(context, self)
        if preset is None:
            return {'CANCELLED'}
        preview = preview_preset(preset, armature, context)
        fuzzy_count = sum(len(preview[key]["fuzzy"]) for key in ("objects", "bones", "bone_collections"))
        if fuzzy_count and not self.accept_fuzzy:
            self.report({'ERROR'}, "Review and accept the fuzzy matches before applying")
            return {'CANCELLED'}
        report = apply_preset(preset, armature, context)
        context.scene.sub_collection_presets.last_report = format_report(preset, report, applied=True)
        self.report(
            {'INFO'},
            f"Applied {preset.get('name', 'preset')!r}: {len(set(report['objects_moved']))} objects moved, "
            f"{len(report['warnings'])} warning(s)",
        )
        return {'FINISHED'}


class SUB_OP_collection_preset_preview(Operator):
    bl_idname = "sub.collection_preset_preview"
    bl_label = "Preview Collection Preset"
    bl_description = "Generate a non-destructive match report for the selected preset"

    def execute(self, context):
        preset, armature = _preset_and_armature(context, self)
        if preset is None:
            return {'CANCELLED'}
        report = preview_preset(preset, armature, context)
        context.scene.sub_collection_presets.last_report = format_report(preset, report)
        bpy.ops.sub.collection_preset_report('INVOKE_DEFAULT')
        return {'FINISHED'}


class SUB_OP_collection_preset_report(Operator):
    bl_idname = "sub.collection_preset_report"
    bl_label = "Collection Preset Report"
    bl_description = "Show the latest collection preset preview or application report"

    def invoke(self, context, _event):
        if not context.scene.sub_collection_presets.last_report:
            self.report({'INFO'}, "No collection preset report is available")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=720)

    def draw(self, context):
        column = self.layout.column()
        for line in context.scene.sub_collection_presets.last_report.splitlines()[:80]:
            column.label(text=line or " ")

    def execute(self, _context):
        return {'FINISHED'}


class SUB_OP_collection_preset_auto_select(Operator):
    bl_idname = "sub.collection_preset_auto_select"
    bl_label = "Auto-Select Best Preset"
    bl_description = "Select the preset with the greatest normalized bone-name overlap"

    def execute(self, context):
        armature = context.active_object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "Select an armature first")
            return {'CANCELLED'}
        current = {_normalize_name(bone.name) for bone in armature.data.bones}
        best = None
        for index, item in enumerate(context.scene.sub_collection_presets.presets):
            if not item.is_valid:
                continue
            try:
                preset = _load_path(item.path)
            except Exception:
                continue
            saved = {
                _normalize_name(name)
                for name in preset.get("armature_fingerprint", {}).get("bones", [])
            }
            union = saved | current
            score = len(saved & current) / len(union) if union else 0.0
            if best is None or score > best[0]:
                best = (score, index, item.name)
        if best is None:
            self.report({'WARNING'}, "No valid presets were found")
            return {'CANCELLED'}
        context.scene.sub_collection_presets.active_index = best[1]
        self.report({'INFO'}, f"Best preset: {best[2]!r} ({best[0]:.0%} bone overlap)")
        return {'FINISHED'}


class SUB_OP_collection_preset_delete(Operator):
    bl_idname = "sub.collection_preset_delete"
    bl_label = "Delete Collection Preset"
    bl_description = "Permanently delete the selected preset JSON file"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Delete Collection Preset?", icon='WARNING'
        )

    def execute(self, context):
        item = _active_item(context)
        if not item or not os.path.isfile(item.path):
            return {'CANCELLED'}
        name = item.name
        os.remove(item.path)
        refresh_presets(context)
        self.report({'INFO'}, f"Deleted collection preset {name!r}")
        return {'FINISHED'}


class SUB_OP_collection_preset_duplicate(Operator):
    bl_idname = "sub.collection_preset_duplicate"
    bl_label = "Duplicate Collection Preset"
    bl_description = "Copy the selected preset under a new name"

    new_name: StringProperty(name="New Name", default="collection_preset_copy")

    def invoke(self, context, _event):
        item = _active_item(context)
        if item:
            self.new_name = item.name + " Copy"
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, _context):
        self.layout.prop(self, "new_name")

    def execute(self, context):
        item = _active_item(context)
        if not item:
            return {'CANCELLED'}
        name = self.new_name.strip()
        if not name:
            self.report({'ERROR'}, "Preset name cannot be empty")
            return {'CANCELLED'}
        destination = _preset_path(context, name)
        if os.path.exists(destination):
            self.report({'ERROR'}, "A preset with this name already exists")
            return {'CANCELLED'}
        preset = _load_path(item.path)
        preset["name"] = name
        preset.pop("model_folder", None)
        preset["version"] = FORMAT_VERSION
        preset["format"] = FORMAT_ID
        preset.pop("legacy_source", None)
        _write_preset(destination, preset)
        refresh_presets(context, destination)
        return {'FINISHED'}


class SUB_OP_collection_preset_rename(Operator):
    bl_idname = "sub.collection_preset_rename"
    bl_label = "Rename Collection Preset"
    bl_description = "Rename the selected preset and its JSON file"

    new_name: StringProperty(name="New Name")

    def invoke(self, context, _event):
        item = _active_item(context)
        if not item:
            return {'CANCELLED'}
        self.new_name = item.name
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, _context):
        self.layout.prop(self, "new_name")

    def execute(self, context):
        item = _active_item(context)
        name = self.new_name.strip()
        if not item or not name:
            return {'CANCELLED'}
        destination = _preset_path(context, name)
        if os.path.normcase(destination) != os.path.normcase(item.path) and os.path.exists(destination):
            self.report({'ERROR'}, "A preset with this name already exists")
            return {'CANCELLED'}
        preset = _load_path(item.path)
        preset["name"] = name
        _write_preset(destination, preset)
        if os.path.normcase(destination) != os.path.normcase(item.path):
            os.remove(item.path)
        refresh_presets(context, destination)
        return {'FINISHED'}


class SUB_OP_collection_preset_import(Operator, ImportHelper):
    bl_idname = "sub.collection_preset_import"
    bl_label = "Import Collection Preset(s)"
    bl_description = "Import one or more compatible collection preset JSON files"

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)

    @classmethod
    def description(cls, _context, _properties):
        return cls.bl_description

    def execute(self, context):
        paths = [os.path.join(os.path.dirname(self.filepath), item.name) for item in self.files]
        if not paths:
            paths = [self.filepath]
        imported = 0
        last_path = ""
        for source in paths:
            try:
                preset = _load_path(source)
                fallback_name = os.path.splitext(os.path.basename(source))[0]
                destination = _preset_path(context, preset.get("name") or fallback_name)
                if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(destination)):
                    last_path = destination
                    imported += 1
                    continue
                if os.path.exists(destination):
                    base, extension = os.path.splitext(destination)
                    number = 1
                    while os.path.exists(f"{base}.{number:03d}{extension}"):
                        number += 1
                    destination = f"{base}.{number:03d}{extension}"
                # Preserve the original schema so importing itself is lossless.
                shutil.copy2(source, destination)
                imported += 1
                last_path = destination
            except Exception as error:
                self.report({'WARNING'}, f"Skipped {os.path.basename(source)}: {error}")
        refresh_presets(context, last_path)
        self.report({'INFO'}, f"Imported {imported} collection preset(s)")
        return {'FINISHED'} if imported else {'CANCELLED'}


class SUB_OP_collection_preset_export(Operator, ExportHelper):
    bl_idname = "sub.collection_preset_export"
    bl_label = "Export Collection Preset"
    bl_description = "Export the selected collection preset as JSON"

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    @classmethod
    def description(cls, _context, _properties):
        return cls.bl_description

    def invoke(self, context, event):
        item = _active_item(context)
        if not item:
            return {'CANCELLED'}
        self.filepath = _clean_name(item.name) + ".json"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        item = _active_item(context)
        if not item or not os.path.isfile(item.path):
            self.report({'ERROR'}, "Preset file not found")
            return {'CANCELLED'}
        shutil.copy2(item.path, self.filepath)
        self.report({'INFO'}, f"Exported {item.name!r}")
        return {'FINISHED'}


class SUB_PT_collection_presets(Panel):
    bl_idname = "SUB_PT_collection_presets"
    bl_label = "Armature Collection Presets"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.sub_collection_presets
        row = layout.row(align=True)
        row.prop(props, "library_scope", text="")
        row.operator("sub.collection_presets_refresh", text="", icon='FILE_REFRESH')
        if props.library_scope == 'BLEND' and not bpy.data.filepath:
            layout.label(text="Unsaved file: using the Global library", icon='INFO')
        elif props.library_scope == 'CUSTOM' and not (_prefs(context) and _prefs(context).collection_preset_directory):
            layout.label(text="No custom directory set; using Global", icon='INFO')

        row = layout.row()
        row.template_list(
            "SUB_UL_collection_presets", "", props, "presets", props, "active_index", rows=5
        )
        controls = row.column(align=True)
        controls.operator("sub.collection_preset_auto_select", text="", icon='VIEWZOOM')
        controls.operator("sub.collection_preset_import", text="", icon='IMPORT')
        controls.operator("sub.collection_preset_export", text="", icon='EXPORT')
        controls.separator()
        controls.operator("sub.collection_preset_duplicate", text="", icon='DUPLICATE')
        controls.operator("sub.collection_preset_rename", text="", icon='GREASEPENCIL')
        controls.operator("sub.collection_preset_delete", text="", icon='TRASH')

        if context.active_object is None or context.active_object.type != 'ARMATURE':
            layout.label(text="Select an armature to save, preview, or apply.", icon='INFO')
        actions = layout.row(align=True)
        actions.operator("sub.collection_preset_save", text="Save New", icon='ADD')
        actions.operator("sub.collection_preset_update", text="Update", icon='FILE_TICK')
        actions = layout.row(align=True)
        actions.scale_y = 1.3
        actions.operator("sub.collection_preset_preview", text="Preview", icon='HIDE_OFF')
        actions.operator("sub.collection_preset_apply", text="Apply", icon='CHECKMARK')
        item = _active_item(context)
        layout.operator("sub.collection_preset_link_model", text="Link to Model Folder", icon='LINKED')
        if item and item.model_folder:
            layout.label(text=f"Auto-apply: {item.model_folder}", icon='FILE_FOLDER')
        if props.last_report:
            layout.operator("sub.collection_preset_report", text="View Last Report", icon='TEXT')

        header = layout.row()
        header.prop(
            props,
            "show_options",
            text="Preset Sections and Matching",
            icon='TRIA_DOWN' if props.show_options else 'TRIA_RIGHT',
            emboss=False,
        )
        if not props.show_options:
            return
        box = layout.box()
        box.label(text="Included When Saving and Applying")
        grid = box.grid_flow(columns=2, align=True)
        grid.prop(props, "use_scene_collections")
        grid.prop(props, "use_object_placement")
        grid.prop(props, "use_materials")
        grid.prop(props, "use_bone_collections")
        grid.prop(props, "use_bone_display")
        grid.prop(props, "use_armature_display")
        box.separator()
        box.prop(props, "include_descendants")
        box.prop(props, "include_custom_shapes")
        box.separator()
        box.prop(props, "use_fuzzy_matching")
        if props.use_fuzzy_matching:
            box.prop(props, "fuzzy_threshold")
        box.prop(props, "unmatched_behavior")
        if props.unmatched_behavior == 'MOVE':
            box.prop(props, "unmatched_collection")


CLASSES = (
    SUB_PG_collection_preset_item,
    SUB_PG_collection_preset_settings,
    SUB_UL_collection_presets,
    SUB_OP_collection_presets_refresh,
    SUB_OP_collection_preset_link_model,
    SUB_OP_collection_preset_save,
    SUB_OP_collection_preset_update,
    SUB_OP_collection_preset_apply,
    SUB_OP_collection_preset_preview,
    SUB_OP_collection_preset_report,
    SUB_OP_collection_preset_auto_select,
    SUB_OP_collection_preset_delete,
    SUB_OP_collection_preset_duplicate,
    SUB_OP_collection_preset_rename,
    SUB_OP_collection_preset_import,
    SUB_OP_collection_preset_export,
    SUB_PT_collection_presets,
)


def _refresh_when_ready():
    scene = getattr(bpy.context, "scene", None)
    if scene is None or not hasattr(scene, "sub_collection_presets"):
        return 0.2
    try:
        refresh_presets(bpy.context)
    except Exception:
        pass
    return None


@persistent
def _collection_presets_load_post(_unused):
    if not bpy.app.timers.is_registered(_refresh_when_ready):
        bpy.app.timers.register(_refresh_when_ready, first_interval=0.05)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sub_collection_presets = bpy.props.PointerProperty(
        type=SUB_PG_collection_preset_settings
    )
    if _collection_presets_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_collection_presets_load_post)
    if not bpy.app.timers.is_registered(_refresh_when_ready):
        bpy.app.timers.register(_refresh_when_ready, first_interval=0.05)


def unregister():
    if _collection_presets_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_collection_presets_load_post)
    if bpy.app.timers.is_registered(_refresh_when_ready):
        bpy.app.timers.unregister(_refresh_when_ready)
    if hasattr(bpy.types.Scene, "sub_collection_presets"):
        del bpy.types.Scene.sub_collection_presets
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
