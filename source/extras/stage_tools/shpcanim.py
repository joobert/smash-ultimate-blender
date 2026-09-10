"""Import and export .shpcanim ambient SH grids as an editable Blender mesh."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import shpc_format


COLLECTION_NAME = "Ultimate SHPC"
MESH_NAME = "SHPC Grid"
ROOT_MARKER = "sub_shpc_root"
MESH_MARKER = "sub_shpc_mesh"
BYTES_MARKER = "sub_shpc_bytes_text"
DATA_MARKER = "sub_shpc_data_text"
GAIN_MARKER = "sub_shpc_display_gain"
FRAME_MARKER = "sub_shpc_last_key"


def find_shpc_root(context):
    active = context.view_layer.objects.active
    if active is not None:
        if active.get(ROOT_MARKER):
            return active
        if active.parent is not None and active.parent.get(ROOT_MARKER):
            return active.parent
    for obj in context.scene.objects:
        if obj.get(ROOT_MARKER):
            return obj
    return None


def find_shpc_mesh(root):
    if root is None:
        return None
    for child in root.children:
        if child.get(MESH_MARKER) and child.type == "MESH":
            return child
    if root.get(MESH_MARKER) and root.type == "MESH":
        return root
    return None


def _write_text(name, contents):
    text = bpy.data.texts.get(name)
    if text is None:
        text = bpy.data.texts.new(name)
    text.clear()
    text.write(contents)
    return text.name


def _read_text(name):
    if not name:
        return ""
    text = bpy.data.texts.get(name)
    if text is None:
        return ""
    return text.as_string()


def _original_bytes(root):
    encoded = _read_text(root.get(BYTES_MARKER, ""))
    if encoded:
        try:
            return base64.b64decode(encoded)
        except Exception:
            pass
    source = root.get("sub_shpc_source")
    if source and os.path.isfile(source):
        with open(source, "rb") as handle:
            return handle.read()
    return b""


def _load_shan(root):
    text = _read_text(root.get(DATA_MARKER, ""))
    if not text:
        raise ValueError("Selected SHPC object is missing grid data")
    return shpc_format.shan_from_json(text, _original_bytes(root))


def _store_shan(root, shan: shpc_format.Shan):
    prefix = f"sub_shpc_{root.name}".replace(".", "_")
    root[DATA_MARKER] = _write_text(root.get(DATA_MARKER) or f"{prefix}_data", shpc_format.shan_to_json(shan))
    if shan.original_bytes:
        root[BYTES_MARKER] = _write_text(
            root.get(BYTES_MARKER) or f"{prefix}_bin",
            base64.b64encode(shan.original_bytes).decode("ascii"),
        )


def _display_gain(shan: shpc_format.Shan) -> float:
    peak = 0.0
    for tpcb in shan.tpcbs:
        for cell in tpcb.cells:
            r, g, b = shpc_format.cell_l0_color(cell)
            peak = max(peak, abs(r), abs(g), abs(b))
    if peak <= 0.0001:
        return 1.0
    return 1.0 / peak


def _preview_color(cell, intensity, tint, gain):
    r, g, b = shpc_format.cell_l0_color(cell)
    return (
        max(0.0, min(1.0, r * intensity * tint[0] * gain)),
        max(0.0, min(1.0, g * intensity * tint[1] * gain)),
        max(0.0, min(1.0, b * intensity * tint[2] * gain)),
    )


def _quad_size(tpcb: shpc_format.Tpcb) -> float:
    spacing = [abs(v) for v in tpcb.grid_spacing_xyz if abs(v) > 0.0001]
    if not spacing:
        return 2.0
    return min(spacing) * 0.35


def _build_grid_geometry(tpcb: shpc_format.Tpcb):
    half = _quad_size(tpcb)
    verts = []
    faces = []
    for index in range(tpcb.grid_cell_count):
        center = shpc_format.smash_to_blender(shpc_format.cell_position_smash(tpcb, index))
        base = len(verts)
        verts.extend((
            (center[0] - half, center[1] - half, center[2]),
            (center[0] + half, center[1] - half, center[2]),
            (center[0] + half, center[1] + half, center[2]),
            (center[0] - half, center[1] + half, center[2]),
        ))
        faces.append((base, base + 1, base + 2, base + 3))
    return verts, faces


def _ensure_preview_material():
    name = "Ultimate_SHPC_Preview"
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        emission = nodes.new("ShaderNodeEmission")
        vertex_color = nodes.new("ShaderNodeVertexColor")
        vertex_color.layer_name = "Col"
        output.location = (300, 0)
        emission.location = (80, 0)
        vertex_color.location = (-140, 0)
        links.new(vertex_color.outputs["Color"], emission.inputs["Color"])
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def _set_vertex_colors(mesh, colors):
    attr = mesh.color_attributes.get("Col")
    if attr is None:
        attr = mesh.color_attributes.new(name="Col", type="FLOAT_COLOR", domain="POINT")
    for cell_index, color in enumerate(colors):
        for corner in range(4):
            vert_index = cell_index * 4 + corner
            if vert_index < len(attr.data):
                attr.data[vert_index].color = (color[0], color[1], color[2], 1.0)
    mesh.color_attributes.active_color = attr


def _read_vertex_colors(mesh, cell_count):
    attr = mesh.color_attributes.get("Col")
    if attr is None:
        return None
    colors = []
    for cell_index in range(cell_count):
        accum = [0.0, 0.0, 0.0]
        used = 0
        for corner in range(4):
            vert_index = cell_index * 4 + corner
            if vert_index < len(attr.data):
                col = attr.data[vert_index].color
                accum[0] += col[0]
                accum[1] += col[1]
                accum[2] += col[2]
                used += 1
        if used:
            colors.append((accum[0] / used, accum[1] / used, accum[2] / used))
        else:
            colors.append((0.0, 0.0, 0.0))
    return colors


def _settings(root):
    settings = getattr(root, "sub_shpc", None)
    intensity = 1.0
    tint = (1.0, 1.0, 1.0)
    if settings is not None:
        intensity = float(settings.intensity)
        tint = tuple(settings.tint)
    return intensity, tint


def refresh_shpc_preview_object(root, frame: float):
    shan = _load_shan(root)
    if not shan.tpcbs:
        return
    key_index, tpcb = shpc_format.tpcb_for_frame(shan, frame)
    mesh_obj = find_shpc_mesh(root)
    if mesh_obj is None:
        return
    intensity, tint = _settings(root)
    gain = float(root.get(GAIN_MARKER, _display_gain(shan)))
    colors = [_preview_color(cell, intensity, tint, gain) for cell in tpcb.cells]
    _set_vertex_colors(mesh_obj.data, colors)
    root[FRAME_MARKER] = key_index
    root.name = f"SHPC {shan.name} [{key_index + 1}/{len(shan.tpcbs)}]"


def refresh_shpc_preview(context):
    root = find_shpc_root(context)
    if root is None:
        return
    refresh_shpc_preview_object(root, context.scene.frame_current)


def _apply_vertex_colors_to_tpcb(root, shan, tpcb):
    mesh_obj = find_shpc_mesh(root)
    if mesh_obj is None:
        return
    settings = getattr(root, "sub_shpc", None)
    if settings is None or not settings.use_vertex_colors:
        return
    colors = _read_vertex_colors(mesh_obj.data, tpcb.grid_cell_count)
    if not colors:
        return
    intensity, tint = _settings(root)
    gain = float(root.get(GAIN_MARKER, 1.0))
    if gain <= 0.0001:
        gain = 1.0
    for cell, color in zip(tpcb.cells, colors):
        # Invert the preview mapping so painted colors become raw L0.
        safe_i = intensity if intensity != 0.0 else 1.0
        cell.r[3] = color[0] / (gain * safe_i * (tint[0] if tint[0] != 0.0 else 1.0))
        cell.g[3] = color[1] / (gain * safe_i * (tint[1] if tint[1] != 0.0 else 1.0))
        cell.b[3] = color[2] / (gain * safe_i * (tint[2] if tint[2] != 0.0 else 1.0))


def import_shpcanim(context, filepath: str):
    shan = shpc_format.read_shpcanim(filepath)
    if not shan.tpcbs:
        raise ValueError("This shpcanim has no TPCB lighting grids")

    stem = Path(filepath).stem
    collection = bpy.data.collections.new(f"{COLLECTION_NAME} ({stem})")
    context.scene.collection.children.link(collection)
    root = bpy.data.objects.new(f"SHPC {shan.name or stem}", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 4.0
    root[ROOT_MARKER] = True
    root["sub_shpc_source"] = filepath
    _store_shan(root, shan)
    gain = _display_gain(shan)
    root[GAIN_MARKER] = gain
    root[FRAME_MARKER] = 0
    collection.objects.link(root)

    tpcb = shan.tpcbs[0]
    verts, faces = _build_grid_geometry(tpcb)
    mesh = bpy.data.meshes.new(f"{MESH_NAME} {stem}")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh_obj = bpy.data.objects.new(f"{MESH_NAME} {stem}", mesh)
    mesh_obj.parent = root
    mesh_obj[MESH_MARKER] = True
    mesh_obj.show_in_front = True
    collection.objects.link(mesh_obj)
    mesh_obj.data.materials.append(_ensure_preview_material())

    if hasattr(root, "sub_shpc"):
        root.sub_shpc.intensity = 1.0
        root.sub_shpc.tint = (1.0, 1.0, 1.0)
        root.sub_shpc.use_vertex_colors = False
        root.sub_shpc.sync_scene_frame = True

    refresh_shpc_preview_object(root, context.scene.frame_current)
    context.view_layer.objects.active = root
    root.select_set(True)
    if shan.starting_frames:
        context.scene.frame_end = max(context.scene.frame_end, max(shan.starting_frames) + 1)
    return root, shan


from ...export_progress import export_progress


@export_progress
def export_shpcanim(context, filepath: str):
    root = find_shpc_root(context)
    if root is None:
        raise ValueError("No imported SHPC grid found. Import a .shpcanim first.")
    shan = _load_shan(root)
    if not shan.tpcbs:
        raise ValueError("SHPC object has no grid data")

    intensity, tint = _settings(root)
    key_index = int(root.get(FRAME_MARKER, 0))
    key_index = max(0, min(key_index, len(shan.tpcbs) - 1))
    _apply_vertex_colors_to_tpcb(root, shan, shan.tpcbs[key_index])

    for tpcb in shan.tpcbs:
        shpc_format.scale_tpcb_cells(tpcb, intensity, tint)

    shan.original_bytes = _original_bytes(root)
    shpc_format.write_shpcanim(filepath, shan)
    root["sub_shpc_source"] = filepath
    return len(shan.tpcbs), shan.tpcbs[0].grid_cell_count


class SUB_OP_import_shpcanim(Operator, ImportHelper):
    bl_idname = "sub.import_shpcanim"
    bl_label = "Import SHPC Anim"
    bl_description = "Import a .shpcanim ambient lighting grid for editing"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".shpcanim"
    filter_glob: StringProperty(default="*.shpcanim;*.shpc", options={"HIDDEN"})

    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        last = getattr(ssp, "last_stage_shpc_dir", "")
        if last:
            self.filepath = os.path.join(last, "chara.shpcanim")
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        ssp.last_stage_shpc_dir = os.path.dirname(self.filepath)
        try:
            root, shan = import_shpcanim(context, self.filepath)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        cells = shan.tpcbs[0].grid_cell_count if shan.tpcbs else 0
        self.report({"INFO"}, f"Imported SHPC '{root.name}' ({len(shan.tpcbs)} keyframes, {cells} cells)")
        return {"FINISHED"}


class SUB_OP_export_shpcanim(Operator, ExportHelper):
    bl_idname = "sub.export_shpcanim"
    bl_label = "Export SHPC Anim"
    bl_description = "Export the edited ambient SH grid back to .shpcanim"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".shpcanim"
    filter_glob: StringProperty(default="*.shpcanim", options={"HIDDEN"})

    def invoke(self, context, event):
        root = find_shpc_root(context)
        source = root.get("sub_shpc_source") if root else ""
        if source:
            self.filepath = source
        else:
            ssp = context.scene.sub_scene_properties
            last = getattr(ssp, "last_stage_shpc_dir", "")
            self.filepath = os.path.join(last, "chara.shpcanim") if last else "chara.shpcanim"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        ssp.last_stage_shpc_dir = os.path.dirname(self.filepath)
        try:
            keys, cells = export_shpcanim(context, self.filepath)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Exported SHPC ({keys} keyframes, {cells} cells)")
        return {"FINISHED"}


class SUB_OP_refresh_shpc_preview(Operator):
    bl_idname = "sub.refresh_shpc_preview"
    bl_label = "Refresh SH Preview"
    bl_description = "Rebuild the SHPC grid colors from intensity, tint, and the current frame"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        root = find_shpc_root(context)
        if root is None:
            self.report({"ERROR"}, "No imported SHPC grid found")
            return {"CANCELLED"}
        refresh_shpc_preview_object(root, context.scene.frame_current)
        return {"FINISHED"}


class SUB_OP_bake_shpc_multipliers(Operator):
    bl_idname = "sub.bake_shpc_multipliers"
    bl_label = "Bake Intensity / Tint"
    bl_description = "Bake intensity and tint into the stored SH coefficients and reset the sliders"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        root = find_shpc_root(context)
        if root is None:
            self.report({"ERROR"}, "No imported SHPC grid found")
            return {"CANCELLED"}
        shan = _load_shan(root)
        intensity, tint = _settings(root)
        key_index = int(root.get(FRAME_MARKER, 0))
        key_index = max(0, min(key_index, len(shan.tpcbs) - 1))
        _apply_vertex_colors_to_tpcb(root, shan, shan.tpcbs[key_index])
        for tpcb in shan.tpcbs:
            shpc_format.scale_tpcb_cells(tpcb, intensity, tint)
        _store_shan(root, shan)
        if hasattr(root, "sub_shpc"):
            root.sub_shpc.intensity = 1.0
            root.sub_shpc.tint = (1.0, 1.0, 1.0)
        root[GAIN_MARKER] = _display_gain(shan)
        refresh_shpc_preview_object(root, context.scene.frame_current)
        self.report({"INFO"}, "Baked intensity and tint into the SH grid")
        return {"FINISHED"}


@persistent
def shpc_frame_change(scene):
    for obj in scene.objects:
        if not obj.get(ROOT_MARKER):
            continue
        settings = getattr(obj, "sub_shpc", None)
        if settings is None or not settings.sync_scene_frame:
            continue
        try:
            refresh_shpc_preview_object(obj, scene.frame_current)
        except Exception:
            pass


classes = (
    SUB_OP_import_shpcanim,
    SUB_OP_export_shpcanim,
    SUB_OP_refresh_shpc_preview,
    SUB_OP_bake_shpc_multipliers,
)
