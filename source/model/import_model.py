import os
import os.path
from ..import_paths import walk_import_folders
import re
import bpy
import mathutils
import sqlite3
import time
import math
import traceback
import numpy as np

from ...dependencies import ssbh_data_py
from pathlib import Path
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy.types import Panel, Operator, EditBone
from bpy_extras import image_utils
from mathutils import Matrix
from .material.create_blender_materials_from_matl import create_blender_materials_from_matl
from ..blender_compat import assign_bone_to_collection, ensure_bone_collection

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..blender_property_extensions import SubSceneProperties
    from .skel.helper_bone_data import SubHelperBoneData, AimConstraint, OrientConstraint
    #from .material.sub_matl_data import SUB_PG_sub_matl_data
    from bpy.types import PoseBone, EditBone, CopyRotationConstraint, DampedTrackConstraint

MODEL_FILE_SUFFIXES = ('.numdlb', '.nusktb', '.numshb', '.numatb', '.nuhlpb')
REQUIRED_MODEL_SUFFIXES = ('.numdlb', '.nusktb', '.numshb', '.numatb')


def _is_model_file(filename: str) -> bool:
    return filename.endswith(MODEL_FILE_SUFFIXES)


def _is_importable_model_folder(folder: str) -> bool:
    """True when a folder contains the core SSBU model files needed for import."""
    if not folder or not os.path.isdir(folder):
        return False
    try:
        files = os.listdir(folder)
    except OSError:
        return False
    return all(any(file_name.endswith(suffix) for file_name in files) for suffix in REQUIRED_MODEL_SUFFIXES)


def find_model_folders(root_directory: str) -> list[str]:
    """Recursively find folders that contain importable SSBU model files."""
    root = os.path.normpath(root_directory)
    if not os.path.isdir(root):
        return []

    if _is_importable_model_folder(root):
        return [root]

    found = []
    for dirpath, dirnames, _filenames in walk_import_folders(root):
        if _is_importable_model_folder(dirpath):
            found.append(dirpath)
            dirnames.clear()
    found.sort()
    return found


def _model_display_name(model_path: str, search_root: str) -> str:
    model_path = os.path.normpath(model_path)
    search_root = os.path.normpath(search_root)

    if model_path == search_root or os.path.dirname(model_path) == search_root:
        return os.path.basename(model_path)

    try:
        return os.path.relpath(model_path, search_root).replace('\\', '/')
    except ValueError:
        return os.path.basename(model_path)


def _add_model_folder_to_list(ssp, model_folder_path: str, display_name: str) -> bool:
    try:
        files = [file_name for file_name in os.listdir(model_folder_path) if _is_model_file(file_name)]
    except OSError:
        return False
    if not files:
        return False

    model_item = ssp.model_import_models.add()
    model_item.name = display_name
    model_item.path = model_folder_path
    model_item.fallback_path = model_folder_path
    model_item.files.clear()
    model_item.alts.clear()
    alt = model_item.alts.add()
    alt.name = os.path.basename(model_folder_path)
    alt.path = model_folder_path
    for file_name in files:
        file_item = model_item.files.add()
        file_item.name = file_name
    return True


_C_SLOT = re.compile(r"^c\d+$", re.IGNORECASE)


def _folder_has_any_model_files(folder: str) -> bool:
    if not folder or not os.path.isdir(folder):
        return False
    try:
        return any(_is_model_file(name) for name in os.listdir(folder))
    except OSError:
        return False


def _list_body_alts(body_folder: str) -> list[tuple[str, str]]:
    alts = []
    try:
        names = os.listdir(body_folder)
    except OSError:
        return alts
    for name in names:
        path = os.path.join(body_folder, name)
        if os.path.isdir(path) and _C_SLOT.match(name) and _folder_has_any_model_files(path):
            alts.append((name.lower(), path))
    alts.sort(key=lambda item: item[0])
    return alts


def _first_complete_alt(alts: list[tuple[str, str]]) -> str:
    for _name, path in alts:
        if _is_importable_model_folder(path):
            return path
    return alts[0][1] if alts else ""


def _fighter_dirs(directory: str) -> list[str]:
    if not directory or not os.path.isdir(directory):
        return []
    body = os.path.join(directory, "model", "body")
    if os.path.isdir(body):
        return [directory]
    roots = [directory]
    nested = os.path.join(directory, "fighter")
    if os.path.isdir(nested):
        roots.append(nested)
    found = []
    seen = set()
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            if os.path.isdir(os.path.join(path, "model", "body")):
                key = os.path.normcase(os.path.normpath(path))
                if key not in seen:
                    seen.add(key)
                    found.append(path)
    found.sort()
    return found


def populate_mods_directory_models(ssp, directory: str) -> int:
    fighters = _fighter_dirs(directory)
    if fighters:
        ssp.model_import_models.clear()
        count = 0
        for fighter_dir in fighters:
            body = os.path.join(fighter_dir, "model", "body")
            alts = _list_body_alts(body)
            if not alts:
                continue
            fallback = _first_complete_alt(alts)
            item = ssp.model_import_models.add()
            item.name = os.path.basename(fighter_dir)
            item.path = alts[0][1]
            item.fallback_path = fallback
            item.files.clear()
            item.alts.clear()
            for alt_name, alt_path in alts:
                alt = item.alts.add()
                alt.name = alt_name
                alt.path = alt_path
            try:
                files = [name for name in os.listdir(item.path) if _is_model_file(name)]
            except OSError:
                files = []
            if not files and fallback:
                try:
                    files = [name for name in os.listdir(fallback) if _is_model_file(name)]
                except OSError:
                    files = []
            for file_name in files:
                file_item = item.files.add()
                file_item.name = file_name
            count += 1
        if count:
            first = ssp.model_import_models[0]
            files = [entry.name for entry in first.files]
            _assign_model_file_names(ssp, files)
        return count

    ssp.model_import_models.clear()
    count = 0
    for root, dirs, files in walk_import_folders(directory):
        for dir_name in dirs:
            body_folder_path = os.path.join(root, dir_name, "body")
            if os.path.exists(body_folder_path):
                for sub_dir_name in os.listdir(body_folder_path):
                    model_folder_path = os.path.join(body_folder_path, sub_dir_name)
                    if os.path.isdir(model_folder_path):
                        model_files = [file for file in os.listdir(model_folder_path) if _is_model_file(file)]
                        if model_files:
                            character_name = os.path.basename(
                                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(model_folder_path)))))
                            )
                            model_item = ssp.model_import_models.add()
                            model_item.name = character_name
                            model_item.path = model_folder_path
                            model_item.fallback_path = model_folder_path
                            model_item.alts.clear()
                            alt = model_item.alts.add()
                            alt.name = os.path.basename(model_folder_path)
                            alt.path = model_folder_path
                            _assign_model_file_names(ssp, model_files)
                            count += 1
                            break
                break
    return count


def _assign_model_file_names(ssp, model_files: list[str]) -> None:
    for file in model_files:
        if file.endswith('.numdlb'):
            ssp.model_import_numdlb_file_name = file
        elif file.endswith('.nusktb'):
            ssp.model_import_nusktb_file_name = file
        elif file.endswith('.numshb'):
            ssp.model_import_numshb_file_name = file
        elif file.endswith('.numatb'):
            ssp.model_import_numatb_file_name = file
        elif file.endswith('.nuhlpb'):
            ssp.model_import_nuhlpb_file_name = file


def _folder_has_model_files(folder: str) -> bool:
    if not folder or not os.path.isdir(folder):
        return False
    return _is_importable_model_folder(folder)


def populate_individual_model(ssp, directory: str) -> int:
    ssp.model_import_models.clear()
    model_folders = find_model_folders(directory)
    if not model_folders:
        return 0

    count = 0
    for model_folder in model_folders:
        display_name = _model_display_name(model_folder, directory)
        if _add_model_folder_to_list(ssp, model_folder, display_name):
            count += 1

    if count:
        first_files = [file_name for file_name in os.listdir(model_folders[0]) if _is_model_file(file_name)]
        _assign_model_file_names(ssp, first_files)
    return count


def refresh_model_import_list(ssp, directory: str) -> int:
    previous_path = ""
    if 0 <= ssp.model_import_models_index < len(ssp.model_import_models):
        previous_path = ssp.model_import_models[ssp.model_import_models_index].path

    if _folder_has_model_files(directory):
        count = populate_individual_model(ssp, directory)
    else:
        count = populate_mods_directory_models(ssp, directory)
        if count == 0:
            count = populate_individual_model(ssp, directory)

    if previous_path:
        for index, item in enumerate(ssp.model_import_models):
            if item.path == previous_path:
                ssp.model_import_models_index = index
                break
        else:
            ssp.model_import_models_index = min(ssp.model_import_models_index, max(len(ssp.model_import_models) - 1, 0))
    else:
        ssp.model_import_models_index = min(ssp.model_import_models_index, max(len(ssp.model_import_models) - 1, 0))

    return count


class SUB_PT_import_model(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Model Importer'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        self.layout.use_property_decorate = False
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        layout = self.layout
        layout.use_property_split = False

        # Try to load last directory when panel is first drawn
        if ssp.model_import_folder_path == '' and ssp.last_model_folder and os.path.exists(ssp.last_model_folder):
            ssp.model_import_folder_path = ssp.last_model_folder
            bpy.ops.sub.ssbh_model_folder_selector(directory=ssp.last_model_folder)
            
        if ssp.model_import_folder_path == '':
            row = layout.row(align=True)
            row.label(text='Please select a folder...')
            row = layout.row(align=True)
            row.operator(SUB_OP_select_model_import_folder.bl_idname, icon='ZOOM_ALL', text='Browse for mods directory')
            row = layout.row(align=True)
            row.operator(SUB_OP_select_individual_model.bl_idname, icon='ZOOM_ALL', text='Browse for individual model')
            return
        
        row = layout.row(align=True)
        row.label(text='Selected Folder: "' + ssp.model_import_folder_path + '"')
        row.operator(SUB_OP_refresh_model_import_list.bl_idname, text="", icon='FILE_REFRESH')
        row = layout.row(align=True)
        row.operator(SUB_OP_select_model_import_folder.bl_idname, icon='ZOOM_ALL', text='Browse for a different mods directory')
        row = layout.row(align=True)
        row.operator(SUB_OP_select_individual_model.bl_idname, icon='ZOOM_ALL', text='Browse for individual model')

        row = layout.row()
        layout.prop(ssp, "auto_import_default_eyelid")
        row.template_list("SUB_UL_model_import_list", "", ssp, "model_import_models", ssp, "model_import_models_index")

        row = layout.row()
        row.operator(SUB_OP_import_selected_model.bl_idname, text="Import Selected Model")

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_OP_select_model_import_folder(Operator):
    bl_idname = 'sub.ssbh_model_folder_selector'
    bl_label = 'Folder Selector'
    bl_description = 'Choose a mods directory or folder containing Smash model files'
    bl_options = {'UNDO'}

    filter_glob: StringProperty(
        default='*',
        options={'HIDDEN'}
    )
    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        ssp.model_import_folder_path = self.directory
        ssp.last_model_folder = self.directory
        count = populate_mods_directory_models(ssp, self.directory)
        if count == 0:
            count = populate_individual_model(ssp, self.directory)
        if count == 0:
            self.report({'WARNING'}, "No models found in the selected directory.")
        else:
            self.report({'INFO'}, f"Found {count} model(s).")
        return {'FINISHED'}


class SUB_OP_refresh_model_import_list(Operator):
    bl_idname = 'sub.refresh_model_import_list'
    bl_label = 'Refresh Model List'
    bl_description = "Rescan the selected folder for model changes"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        folder = ssp.model_import_folder_path or ssp.last_model_folder
        return bool(folder and os.path.isdir(folder))

    def execute(self, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        folder = ssp.model_import_folder_path or ssp.last_model_folder
        if not folder or not os.path.isdir(folder):
            self.report({'WARNING'}, "No valid folder to refresh.")
            return {'CANCELLED'}

        count = refresh_model_import_list(ssp, folder)
        if count == 0:
            self.report({'WARNING'}, "No models found in the selected folder.")
        else:
            self.report({'INFO'}, f"Refreshed {count} model(s).")
        return {'FINISHED'}

class SUB_OP_import_model(bpy.types.Operator):
    bl_description = 'Import a Smash model, including its meshes, skeleton, and materials'
    bl_idname = 'sub.model_importer'
    bl_label = 'Model Importer'
    bl_options = {'UNDO'}

    model_path: StringProperty()
    confirm_message: StringProperty(default="")

    @classmethod
    def description(cls, _context, properties):
        # Show a custom confirm message when provided, otherwise default help.
        msg = getattr(properties, 'confirm_message', "")
        return msg if msg else "Import a model from the selected folder."

    def invoke(self, context, event):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        # If NUHLPB is missing in this folder, ask to continue.
        nuhlpb = getattr(ssp, 'model_import_nuhlpb_file_name', '')
        folder = self.model_path if self.model_path else ssp.model_import_folder_path
        missing = (not nuhlpb) or (not os.path.exists(os.path.join(folder, nuhlpb)))
        if missing:
            self.confirm_message = "NUHLPB file is missing. Continue import without helper bone data?"
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        ssp.model_import_folder_path = self.model_path
        start = time.time()

        import_model(self, context)

        end = time.time()
        print(f'Imported model in {end - start} seconds')
        return {'FINISHED'}

class SUB_UL_model_import_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")

    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        # Return empty lists to use default filtering
        return [], []


def _color_slot_items(self, context):
    ssp = getattr(context.scene, "sub_scene_properties", None)
    if ssp is None or not ssp.model_import_models:
        return [("c00", "c00", "")]
    index = min(max(ssp.model_import_models_index, 0), len(ssp.model_import_models) - 1)
    item = ssp.model_import_models[index]
    alts = getattr(item, "alts", None)
    if alts:
        items = [(alt.name, alt.name.upper(), alt.path) for alt in alts if alt.name]
        if items:
            return items
    name = os.path.basename(item.path) or "c00"
    return [(name, name.upper(), item.path)]


def _resolve_model_path(folder: Path, fallback: Path, filename: str, suffix: str) -> Path:
    if filename:
        primary = folder / filename
        if primary.exists():
            return primary
        if fallback:
            alt = fallback / filename
            if alt.exists():
                return alt
    for source in (folder, fallback):
        if source is None or not source.exists():
            continue
        try:
            for name in os.listdir(source):
                if name.lower().endswith(suffix):
                    return source / name
        except OSError:
            continue
    return folder / (filename or f"model{suffix}")


class SUB_OP_import_selected_model(bpy.types.Operator):
    bl_description = 'Import the model selected in the model folder browser'
    bl_idname = 'sub.import_selected_model'
    bl_label = 'Import Selected Model'
    bl_options = {'UNDO'}

    confirm_message: StringProperty(default="")
    color: EnumProperty(
        name="Color",
        description="Costume slot to import",
        items=_color_slot_items,
    )

    @classmethod
    def description(cls, _context, properties):
        msg = getattr(properties, 'confirm_message', "")
        return msg if msg else "Import the currently selected model."

    def invoke(self, context, event):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        if not ssp.model_import_models:
            return {'CANCELLED'}
        selected_model = ssp.model_import_models[ssp.model_import_models_index]
        alts = list(getattr(selected_model, "alts", []))
        if len(alts) > 1:
            self.color = alts[0].name
            return context.window_manager.invoke_props_dialog(self)
        nuhlpb = getattr(ssp, 'model_import_nuhlpb_file_name', '')
        folder = selected_model.path
        missing = (not nuhlpb) or (not os.path.exists(os.path.join(folder, nuhlpb)))
        if missing:
            self.confirm_message = "NUHLPB file is missing. Continue import without helper bone data?"
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def draw(self, context):
        self.layout.prop(self, "color")

    def execute(self, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        selected_model = ssp.model_import_models[ssp.model_import_models_index]
        folder = selected_model.path
        alts = list(getattr(selected_model, "alts", []))
        if alts and self.color:
            for alt in alts:
                if alt.name == self.color:
                    folder = alt.path
                    break
        ssp.model_import_folder_path = folder
        fallback = getattr(selected_model, "fallback_path", "") or ""
        ssp["sub_model_import_fallback"] = fallback
        model_files = []
        try:
            model_files = [file_name for file_name in os.listdir(folder) if _is_model_file(file_name)]
        except OSError:
            model_files = []
        if not model_files and fallback and os.path.isdir(fallback):
            try:
                model_files = [file_name for file_name in os.listdir(fallback) if _is_model_file(file_name)]
            except OSError:
                model_files = []
        _assign_model_file_names(ssp, model_files)
        start = time.time()

        import_model(self, context)

        end = time.time()
        print(f'Imported model in {end - start} seconds')
        return {'FINISHED'}

class SUB_OP_select_individual_model(Operator):
    bl_idname = 'sub.ssbh_individual_model_selector'
    bl_label = 'Individual Model Selector'
    bl_description = 'Choose a folder containing an individual Smash model'
    bl_options = {'UNDO'}

    filter_glob: StringProperty(
        default='*',
        options={'HIDDEN'}
    )
    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        ssp.model_import_folder_path = self.directory
        ssp.last_model_folder = self.directory
        count = populate_individual_model(ssp, self.directory)
        if count == 0:
            self.report({'WARNING'}, "No model files found in the selected folder or its subfolders.")
        else:
            self.report({'INFO'}, f"Found {count} model(s).")
        return {'FINISHED'}

def import_model(operator: bpy.types.Operator, context: bpy.types.Context):
    from ..anim.import_anim import save_visible_animation_folders
    save_visible_animation_folders(context)
    ssp: SubSceneProperties = context.scene.sub_scene_properties
    dir = Path(ssp.model_import_folder_path)
    fallback_raw = ssp.get("sub_model_import_fallback", "") or ""
    fallback = Path(fallback_raw) if fallback_raw else dir
    numdlb_name = _resolve_model_path(dir, fallback, ssp.model_import_numdlb_file_name, ".numdlb")
    numshb_name = _resolve_model_path(dir, fallback, ssp.model_import_numshb_file_name, ".numshb")
    nusktb_name = _resolve_model_path(dir, fallback, ssp.model_import_nusktb_file_name, ".nusktb")
    numatb_name = _resolve_model_path(dir, fallback, ssp.model_import_numatb_file_name, ".numatb")
    nuhlpb_name = None
    if ssp.model_import_nuhlpb_file_name or fallback_raw:
        candidate = _resolve_model_path(
            dir, fallback, ssp.model_import_nuhlpb_file_name, ".nuhlpb"
        )
        if candidate.exists() and candidate.is_file():
            nuhlpb_name = candidate

    print(f'NUMDLB file: {numdlb_name}')
    print(f'NUMSHB file: {numshb_name}')
    print(f'NUSKTB file: {nusktb_name}')
    print(f'NUMATB file: {numatb_name}')
    print(f'NUHLPB file: {nuhlpb_name}')

    # Check if files exist
    if not numdlb_name.exists():
        operator.report({'ERROR'}, f'NUMDLB file not found: {numdlb_name}')
        return {'CANCELLED'}
    if not numshb_name.exists():
        operator.report({'ERROR'}, f'NUMSHB file not found: {numshb_name}')
        return {'CANCELLED'}
    if not nusktb_name.exists():
        operator.report({'ERROR'}, f'NUSKTB file not found: {nusktb_name}')
        return {'CANCELLED'}
    if not numatb_name.exists():
        operator.report({'ERROR'}, f'NUMATB file not found: {numatb_name}')
        return {'CANCELLED'}
    if nuhlpb_name is not None and not nuhlpb_name.exists():
        operator.report({'WARNING'}, f'NUHLPB file not found: {nuhlpb_name}. Continuing without helper bones.')
        nuhlpb_name = None

    start = time.time()
    ssbh_model = ssbh_data_py.modl_data.read_modl(str(numdlb_name)) if numdlb_name != '' else None

    # Numpy provides much faster performance than Python lists.
    # TODO: This API for ssbh_data_py will likely have changes and improvements in the future.
    ssbh_mesh = ssbh_data_py.mesh_data.read_mesh(str(numshb_name)) if numshb_name != '' else None
    ssbh_skel = ssbh_data_py.skel_data.read_skel(str(nusktb_name)) if nusktb_name != '' else None
    ssbh_matl = ssbh_data_py.matl_data.read_matl(str(numatb_name)) if numatb_name != '' else None
    end = time.time()
    print(f'Read files in {end - start} seconds')

    armature = None
    if ssbh_skel is not None:
        try:
            armature = create_armature(operator, ssbh_skel, context)
        except Exception as e:
            operator.report({'ERROR'}, f'Failed to import {nusktb_name}; Error="{e}" ; Traceback=\n{traceback.format_exc()}')

    material_label_to_material = {}
    if ssbh_matl is not None:
        try:
            material_label_to_material = create_blender_materials_from_matl(operator, ssbh_matl)
        except Exception as e:
            operator.report({'ERROR'}, f'Failed to import materials; Error="{e}" ; Traceback=\n{traceback.format_exc()}')

    if armature is not None:
        try:
            create_mesh(ssbh_model, ssbh_mesh, ssbh_skel, armature, context, material_label_to_material)
        except Exception as e:
            operator.report({'ERROR'}, f'Failed to import .NUMDLB, .NUMATB, or .NUMSHB; Error="{e}" ; Traceback=\n{traceback.format_exc()}')

    if nuhlpb_name is not None and armature is not None:
        try:
            read_nuhlpb_data(nuhlpb_name, armature)
        except Exception as e:
            operator.report({'ERROR'}, f'Failed to import NUHLPB; Error="{e}" ; Traceback=\n{traceback.format_exc()}')
        else:
            setup_helper_bone_constraints(armature)
        
    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

    # Store the model path for animation importing and Smash Viewport
    ssp.last_imported_model_path = str(dir)
    try:
        ssp.last_model_folder = str(dir)
    except Exception:
        pass
    if armature is not None:
        try:
            armature["sub_smash_model_folder"] = str(dir)
            if armature.data is not None:
                armature.data["sub_smash_model_folder"] = str(dir)
        except Exception:
            pass

    if armature is not None:
        try:
            from ..extras.face_picker import on_model_imported
            on_model_imported(armature, str(dir))
        except Exception:
            print(f'Face picker auto-load skipped:\n{traceback.format_exc()}')
    
    # Get related animation path
    model_path = str(dir)
    motion_path = model_path.replace("model", "motion")
    anim_path = Path(motion_path)
    
    # Clear previous animation files
    ssp.animation_import_files.clear()
    
    # First, try the direct motion path
    if anim_path.exists():
        # Store animation path
        ssp.animation_import_folder_path = str(anim_path)
        
        # Search for animation files (.nuanmb)
        nuanmb_files = [f for f in os.listdir(anim_path) if f.endswith('.nuanmb')]
        
        # Add found animations to the list
        for anim_file in nuanmb_files:
            anim_item = ssp.animation_import_files.add()
            # Strip the .nuanmb extension from the displayed name
            anim_item.name = os.path.splitext(anim_file)[0]
            anim_item.path = str(anim_path / anim_file)
        
        # If animations were found, report success  
        if nuanmb_files:
            operator.report({'INFO'}, f'Found {len(nuanmb_files)} animations in: {anim_path}')
        # If no animations were found in the direct motion path, look in subfolders    
        else:
            # Try to find the structure motion/body/[first subfolder]
            try:
                # Check if this is a fighter folder (contains "motion" subfolder)
                fighter_folder = Path(model_path).parent.parent.parent
                motion_folder = fighter_folder / "motion"
                
                if motion_folder.exists():
                    body_folder = motion_folder / "body"
                    
                    if body_folder.exists():
                        # Get the first subfolder in body
                        try:
                            subfolders = [f for f in os.listdir(body_folder) if os.path.isdir(body_folder / f)]
                            if subfolders:
                                deep_anim_path = body_folder / subfolders[0]
                                
                                if deep_anim_path.exists():
                                    # Update the stored animation path
                                    ssp.animation_import_folder_path = str(deep_anim_path)
                                    
                                    # Search for animation files (.nuanmb)
                                    deep_nuanmb_files = [f for f in os.listdir(deep_anim_path) if f.endswith('.nuanmb')]
                                    
                                    # Add found animations to the list
                                    for anim_file in deep_nuanmb_files:
                                        anim_item = ssp.animation_import_files.add()
                                        # Strip the .nuanmb extension from the displayed name
                                        anim_item.name = os.path.splitext(anim_file)[0]
                                        anim_item.path = str(deep_anim_path / anim_file)
                                    
                                    if deep_nuanmb_files:
                                        operator.report({'INFO'}, f'Found {len(deep_nuanmb_files)} animations in deep path: {deep_anim_path}')
                                    else:
                                        operator.report({'INFO'}, f'No animations found in deep path: {deep_anim_path}')
                        except Exception as e:
                            operator.report({'INFO'}, f'Failed to search in deep animation path: {str(e)}')
            except Exception as e:
                operator.report({'INFO'}, f'Failed to find deep animation structure: {str(e)}')
            
            if len(ssp.animation_import_files) == 0:
                operator.report({'INFO'}, f'No animations found in any location')
    else:
        # If no direct motion path exists, try the deep path immediately
        try:
            # Check if this is a fighter folder path
            fighter_folder = Path(model_path).parent.parent.parent
            motion_folder = fighter_folder / "motion"
            
            if motion_folder.exists():
                body_folder = motion_folder / "body"
                
                if body_folder.exists():
                    # Get the first subfolder in body
                    try:
                        subfolders = [f for f in os.listdir(body_folder) if os.path.isdir(body_folder / f)]
                        if subfolders:
                            deep_anim_path = body_folder / subfolders[0]
                            
                            if deep_anim_path.exists():
                                # Store animation path
                                ssp.animation_import_folder_path = str(deep_anim_path)
                                
                                # Search for animation files (.nuanmb)
                                deep_nuanmb_files = [f for f in os.listdir(deep_anim_path) if f.endswith('.nuanmb')]
                                
                                # Add found animations to the list
                                for anim_file in deep_nuanmb_files:
                                    anim_item = ssp.animation_import_files.add()
                                    # Strip the .nuanmb extension from the displayed name
                                    anim_item.name = os.path.splitext(anim_file)[0]
                                    anim_item.path = str(deep_anim_path / anim_file)
                                
                                if deep_nuanmb_files:
                                    operator.report({'INFO'}, f'Found {len(deep_nuanmb_files)} animations in deep path: {deep_anim_path}')
                                else:
                                    operator.report({'INFO'}, f'No animations found in deep path: {deep_anim_path}')
                    except Exception as e:
                        operator.report({'INFO'}, f'Failed to search in deep animation path: {str(e)}')
        except Exception as e:
            operator.report({'INFO'}, f'Failed to find deep animation structure: {str(e)}')
        
        if len(ssp.animation_import_files) == 0:
            operator.report({'INFO'}, f'Animation directory not found: {anim_path}')

    try:
        from ..anim.raw_anim import refresh_raw_animation_import_list
        refresh_raw_animation_import_list(ssp)
    except Exception:
        pass

    if armature is not None:
        try:
            from ..anim.import_anim import (
                bind_anim_folder_to_armature,
                related_motion_folder,
                sync_anim_importer_to_active,
            )
            exact_folder = related_motion_folder(str(dir))
            bind_anim_folder_to_armature(armature, exact_folder)
            sync_anim_importer_to_active(context, force=True, armature=armature)
        except Exception:
            pass

    if armature is not None and ssp.auto_import_default_eyelid:
        eyelid = find_default_eyelid(dir)
        if eyelid is not None:
            context.view_layer.objects.active = armature
            armature.select_set(True)
            try:
                result = bpy.ops.sub.import_anim(
                    filepath=str(eyelid), first_blender_frame=context.scene.frame_start)
                if result != {'FINISHED'}:
                    operator.report({'WARNING'}, f'Default eyelid import failed: {eyelid}')
            except Exception as error:
                operator.report({'WARNING'}, f'Default eyelid import failed: {error}')

    # File-backed idle poses are resolved from the active animation folder on use.
    from ..extras.idle_pose_library import initialize_predefined_poses
    initialize_predefined_poses(context)

    if armature is not None:
        try:
            from ..extras.collection_presets import auto_apply_model_preset
            auto_apply_model_preset(context, armature, str(dir), operator)
        except Exception as error:
            operator.report({'WARNING'}, f'Collection preset auto-apply failed: {error}')

    return {'FINISHED'}

def find_default_eyelid(model_folder):
    """Prefer this costume, then shared c00; never use another fighter's folder."""
    folder = Path(model_folder)
    for parent in (folder, *folder.parents):
        if parent.name.lower() == 'model':
            motion = parent.parent / 'motion'
            relative = folder.relative_to(parent)
            direct = motion / relative
            for candidate in (direct, direct.parent / 'c00', motion / 'body' / 'c00', motion / 'body', motion):
                path = candidate / 'a00defaulteyelid.nuanmb'
                if path.is_file():
                    return path
            break
    return None


def get_shader_db_file_path():
    # This file was generated with duplicates removed to optimize space.
    # https://github.com/ScanMountGoat/Smush-Material-Research#shader-database
    this_file_path = Path(__file__)
    return this_file_path.parent.parent.joinpath('shader_file').joinpath('Nufx.db').resolve()



def get_matrix4x4_blender(ssbh_matrix):
    return mathutils.Matrix(ssbh_matrix).transposed()


def find_bone(skel, name):
    for bone in skel.bones:
        if bone.name == name:
            return bone

    return None


def find_bone_index(skel, name):
    for i, bone in enumerate(skel.bones):
        if bone.name == name:
            return i

    return None


def get_name_from_index(index, bones):
    if index is None:
        return None
    return bones[index].name


def get_index_from_name(name, bones):
    for index, bone in enumerate(bones):
        if bone.name == name:
            return index


# In Ultimate, the bone's x-axis points from parent to child.
# In Blender, the bone's y-axis points from parent to child.
# https://en.wikipedia.org/wiki/Matrix_similarity
# Built once instead of rebuilt and re-inverted on every call; this runs
# hundreds of thousands of times during a model or animation import.
_ULTIMATE_TO_BLENDER_BASIS = Matrix([
    [0, -1, 0, 0],
    [1, 0, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1]
])
_ULTIMATE_TO_BLENDER_BASIS_INV = _ULTIMATE_TO_BLENDER_BASIS.inverted()


def get_blender_transform(m) -> Matrix:
    m = Matrix(m).transposed()
    # Perform the transformation m in Ultimate's basis and convert back to Blender.
    return _ULTIMATE_TO_BLENDER_BASIS @ m @ _ULTIMATE_TO_BLENDER_BASIS_INV

def are_vectors_close(a: mathutils.Vector, b: mathutils.Vector) -> bool:
    return all(math.isclose(a[i], b[i], abs_tol=0.00001) for i in [0,1,2])
        
def fix_bone_length(blender_bone: EditBone, edit_bones: bpy.types.ArmatureEditBones) -> None:
    if blender_bone.name.startswith("H_"):
        return

    if len(blender_bone.children) == 0:
        if blender_bone.parent:
            blender_bone.length = blender_bone.parent.length
        return
    
    if len(blender_bone.children) == 1:
        if are_vectors_close(blender_bone.head, blender_bone.children[0].head):
            return
        blender_bone.length = (blender_bone.head - blender_bone.children[0].head).length
        return
    
    for child in blender_bone.children:
        if child.name == blender_bone.name + '_eff':
            blender_bone.length = (blender_bone.head - child.head).length

    finger_base_bones = ['FingerL10','FingerL20', 'FingerL30','FingerL40',
                            'FingerR10','FingerR20', 'FingerR30','FingerR40',]
    if any(finger_base_bone == blender_bone.name for finger_base_bone in finger_base_bones):
        if finger_1_bone:= edit_bones.get(blender_bone.name[:-1]+'1'):
            blender_bone.length = (blender_bone.head - finger_1_bone.head).length
    
    if blender_bone.name == 'ArmL' or blender_bone.name == 'ArmR':
        if hand_bone:= edit_bones.get("Hand" + blender_bone.name[-1]):
            blender_bone.length = (blender_bone.head - hand_bone.head).length
    
    if blender_bone.name == 'ShoulderL' or blender_bone.name == 'ShoulderR':
        if arm_bone:= edit_bones.get("Arm" + blender_bone.name[-1]):
            blender_bone.length = (blender_bone.head - arm_bone.head).length

    if blender_bone.name == 'LegR' or blender_bone.name == 'LegL':
        if knee_bone:= edit_bones.get('Knee' + blender_bone.name[-1]):
            blender_bone.length = (blender_bone.head - knee_bone.head).length
    
    if blender_bone.name == 'KneeR' or blender_bone.name == 'KneeL':
        if foot_bone:= edit_bones.get('Foot' + blender_bone.name[-1]):
            blender_bone.length = (blender_bone.head - foot_bone.head).length
    
    if blender_bone.name == 'ClavicleC':
        if neck_bone:= edit_bones.get('Neck'):
            blender_bone.length = (blender_bone.head - neck_bone.head).length

def assign_bone_layers(arma_obj: bpy.types.Object) -> None:
    # Pose bones only exist in pose mode, so enter pose mode to properly set their colors.
    bpy.ops.object.mode_set(mode='POSE')
    standard_collection = ensure_bone_collection(arma_obj.data, "Standard Bones")
    helper_collection = ensure_bone_collection(arma_obj.data, "Helper Bones")
    exo_collection = ensure_bone_collection(arma_obj.data, '"Exo" Helper Bones')
    swing_collection = ensure_bone_collection(arma_obj.data, "Swing Bones")
    null_collection = ensure_bone_collection(arma_obj.data, "Null Swing Bones")
    system_collection = ensure_bone_collection(arma_obj.data, "System Bones")

    system_bone_names = ['Trans', 'Rot', 'Throw']
    system_bone_suffixes = ['_null', '_eff', '_offset']
    
    for bone in arma_obj.pose.bones:
        bone: PoseBone
        if bone.name.startswith('H_Exo_'):
            assign_bone_to_collection(exo_collection, bone)
            bone.color.palette = 'THEME09'
            bone.bone.color.palette = 'THEME09'
        elif bone.name.startswith('H_'):
            assign_bone_to_collection(helper_collection, bone)
            bone.color.palette = 'THEME06'
            bone.bone.color.palette = 'THEME06'
        elif bone.name.startswith('S_'):
            assign_bone_to_collection(swing_collection, bone)
            bone.color.palette = 'THEME04'
            bone.bone.color.palette = 'THEME04'
            if '_null' in bone.name:
                assign_bone_to_collection(null_collection, bone)
                bone.color.palette = 'THEME10'
                bone.bone.color.palette = 'THEME10'
        else:
            assign_bone_to_collection(standard_collection, bone)
            # Fixed the variable names in the any() expressions
            if any(name == bone.name for name in system_bone_names) or \
               any(suffix in bone.name for suffix in system_bone_suffixes):
                assign_bone_to_collection(system_collection, bone)
                bone.color.palette = 'THEME10'
                bone.bone.color.palette = 'THEME10'

    bpy.ops.object.mode_set(mode='OBJECT')

def create_armature(operator: Operator, ssbh_skel: ssbh_data_py.skel_data.SkelData, context: bpy.types.Context) -> bpy.types.Object: 
    '''
    So blender bone matrixes are not relative to their parent, unlike the ssbh skel.
    Also, blender has a different coordinate system for the bones.
    Also, ssbh matrixes need to be transposed first.
    Also, the root bone needs to be modified differently to fix the world orientation
    Also, the ssbh bones are not guaranteed to appear in 'hierarchical' order, 
                 which is where the parent always appears before the child.
    Also, iterating through the blender bones appears to preserve the order of insertion,
                 so its also not guaranteed hierarchical order.
    '''
    start = time.time()
    
    # Create a new armature and select it.
    base_skel_name = "smush_blender_import"
    arma_obj: bpy.types.Object = bpy.data.objects.new(base_skel_name, bpy.data.armatures.new(base_skel_name))
    arma_data: bpy.types.Armature = arma_obj.data
    arma_obj.rotation_mode = 'QUATERNION'
    arma_obj.show_in_front = True
    arma_data.display_type = 'STICK'
    context.view_layer.active_layer_collection.collection.objects.link(arma_obj)
    context.view_layer.objects.active = arma_obj
    
    # Create Blender Bones
    # Edit bones only exist in edit mode, so enter edit mode
    bpy.ops.object.mode_set(mode='EDIT', toggle=False)
    for ssbh_bone in ssbh_skel.bones:
        new_edit_bone = arma_data.edit_bones.new(name=ssbh_bone.name)
        new_edit_bone.head = [0,0,0]
        new_edit_bone.tail = [0,1,0] # Doesnt actually matter where its pointing, it just needs to point somewhere
        smash_world_transform = ssbh_data_py.skel_data.SkelData.calculate_world_transform(ssbh_skel, ssbh_bone)
        smash_world_transform_matrix = Matrix(smash_world_transform).transposed()
        y_up_to_z_up = Matrix.Rotation(math.radians(90), 4, 'X')
        x_major_to_y_major = Matrix.Rotation(math.radians(-90), 4, 'Z')
        new_edit_bone.matrix = y_up_to_z_up @ smash_world_transform_matrix @ x_major_to_y_major
        # For some reason, the .nusktb rarely contains scale values for bones
        # Even though this is accounted for with ssbh_data_py's calculate_world_transform function,
        # and the skel will be properly positioned,
        # animations will still import wierd as the scale will be "doubled up"
        scale_vec = smash_world_transform_matrix.to_scale()
        if not all(math.isclose(i, 1.0, abs_tol=.001) for i in scale_vec):
            operator.report({'WARNING'}, f'The bone {new_edit_bone.name} contained scale values! Imported animations may look strange, and the scale values will be lost on model export!')

    # Assign parents to bones
    bone_name_parent_name_dict: dict[str,str] = {bone.name:get_name_from_index(bone.parent_index, ssbh_skel.bones) for bone in ssbh_skel.bones}
    for edit_bone in arma_data.edit_bones:
        if parent_bone_name:= bone_name_parent_name_dict.get(edit_bone.name):
            if parent_bone:= arma_data.edit_bones.get(parent_bone_name):
                edit_bone.parent = parent_bone

    # Fix bone length
    for edit_bone in arma_data.edit_bones:   
        fix_bone_length(edit_bone, arma_data.edit_bones)
        # Fallback in case the bone was made to be too short
        if edit_bone.length < .001:
            operator.report({'INFO'}, f"The bone \"{edit_bone.name}\" has a length less than .001, so it was set to .001.") 
            edit_bone.length = .001


    # Assign bone colors and bone layers
    assign_bone_layers(arma_obj)

    bpy.ops.object.mode_set(mode='OBJECT')
    end = time.time()
    print(f'Created armature in {end - start} seconds')

    return arma_obj


def attach_armature_create_vertex_groups(mesh_obj, skel, armature, ssbh_mesh_object):
    if skel is not None:
        # Create vertex groups for each bone to support skinning.
        for bone in skel.bones:
            mesh_obj.vertex_groups.new(name=bone.name)

        # Apply the initial parent bone transform if present.
        parent_bone = find_bone(skel, ssbh_mesh_object.parent_bone_name)
        if parent_bone is not None:
            world_transform = skel.calculate_world_transform(parent_bone)
            mesh_obj.data.transform(get_matrix4x4_blender(world_transform))

            # Use regular skin weights for mesh objects parented to a bone.
            # TODO: Should this only apply if there are no influences?
            # TODO: Should this be handled by actual parenting in Blender?

            # Avoid creating duplicate vertex groups.
            if parent_bone.name in mesh_obj.vertex_groups:
                vertex_group = mesh_obj.vertex_groups[parent_bone.name]
            else:
                vertex_group = mesh_obj.vertex_groups.new(name=parent_bone.name)

            # VertexGroup.add() requires plain Python ints, but ssbh_data_py's
            # use_numpy=True mode returns vertex_indices as a numpy uint32 array.
            vertex_group.add([int(i) for i in ssbh_mesh_object.vertex_indices], 1.0, 'REPLACE')
        else:
            # Set the vertex skin weights for each bone.
            # VertexGroup.add() takes a list of indices, so vertices that share a
            # weight go in together. Smash weights are quantized, so a bone's
            # influences collapse to a handful of calls instead of one per vertex.
            for influence in ssbh_mesh_object.bone_influences:
                # Avoid creating duplicate vertex groups.
                # Influences may refer to effect bones not in the skel for some models.
                if influence.bone_name in mesh_obj.vertex_groups:
                    vertex_group = mesh_obj.vertex_groups[influence.bone_name]
                else:
                    vertex_group = mesh_obj.vertex_groups.new(name=influence.bone_name)

                weight_to_indices: dict[float, list[int]] = {}
                seen_indices: set[int] = set()
                duplicate_index = False
                for w in influence.vertex_weights:
                    vertex_index = int(w.vertex_index)
                    if vertex_index in seen_indices:
                        # A repeated index means later writes must overwrite earlier
                        # ones in order, so grouping is not safe here.
                        duplicate_index = True
                        break
                    seen_indices.add(vertex_index)
                    weight_to_indices.setdefault(w.vertex_weight, []).append(vertex_index)

                if duplicate_index:
                    for w in influence.vertex_weights:
                        vertex_group.add([int(w.vertex_index)], w.vertex_weight, 'REPLACE')
                else:
                    for weight, indices in weight_to_indices.items():
                        vertex_group.add(indices, weight, 'REPLACE')

        # Convert from Y up to Z up.
        mesh_obj.data.transform(Matrix.Rotation(math.radians(90), 4, 'X'))

    # Attach the mesh object to the armature object.
    if armature is not None:
        mesh_obj.parent = armature
        modifier = mesh_obj.modifiers.new(armature.data.name, type='ARMATURE')
        modifier.object = armature


def create_blender_mesh(ssbh_mesh_object, skel, name_index_mat_dict):
    blender_mesh = bpy.data.meshes.new(ssbh_mesh_object.name)

    # TODO: Handle attribute data arrays not having the appropriate number of rows and columns.
    # This won't be an issue for in game models.

    # Using foreach_set is much faster than bmesh or from_pydata.
    # https://devtalk.blender.org/t/alternative-in-2-80-to-create-meshes-from-python-using-the-tessfaces-api/7445/3
    positions = ssbh_mesh_object.positions[0].data[:,:3]
    blender_mesh.vertices.add(positions.shape[0])
    blender_mesh.vertices.foreach_set('co', positions.flatten())

    # Assume triangles, which is the only primitive used in Smash Ultimate.
    vertex_indices = np.array(ssbh_mesh_object.vertex_indices, dtype=np.int32)
    loop_start = np.arange(0, vertex_indices.shape[0], 3, dtype=np.int32)
    loop_total = np.full(loop_start.shape[0], 3, dtype=np.int32)

    blender_mesh.loops.add(vertex_indices.shape[0])
    blender_mesh.loops.foreach_set('vertex_index', vertex_indices)

    blender_mesh.polygons.add(loop_start.shape[0])
    blender_mesh.polygons.foreach_set('loop_start', loop_start)
    blender_mesh.polygons.foreach_set('loop_total', loop_total)

    for attribute_data in ssbh_mesh_object.texture_coordinates:
        uv_layer = blender_mesh.uv_layers.new(name=attribute_data.name)

        # Flip vertical.
        uvs = attribute_data.data[:,:2].copy()
        uvs[:,1] = 1.0 - uvs[:,1]

        # This is set per loop rather than per vertex.
        loop_uvs = uvs[vertex_indices].flatten()
        uv_layer.data.foreach_set('uv', loop_uvs)

    for attribute_data in ssbh_mesh_object.color_sets:
        # TODO: Just set this per vertex instead?
        # Byte color still uses floats but restricts their range to 0.0 to 1.0.
        color_attribute = blender_mesh.color_attributes.new(name=attribute_data.name, type='BYTE_COLOR', domain='CORNER')
        colors = attribute_data.data[:,:4]

        # This is set per loop rather than per vertex.
        loop_colors = colors[vertex_indices].flatten()
        color_attribute.data.foreach_set('color', loop_colors)

    # These calls are necessary since we're setting mesh data manually.
    blender_mesh.update()
    blender_mesh.validate()

    blender_mesh.normals_split_custom_set_from_vertices(ssbh_mesh_object.normals[0].data[:,:3])

    # Try and assign the material.
    # Mesh import should still succeed even if materials couldn't be created.
    # Users can still choose to not export the matl.
    # TODO: Report errors to the user?
    try:
        material = name_index_mat_dict[(ssbh_mesh_object.name, ssbh_mesh_object.subindex)]
        blender_mesh.materials.append(material)
    except Exception as e:
        print(f'Failed to assign material for {ssbh_mesh_object.name}{ssbh_mesh_object.subindex}: {e}')


    return blender_mesh


def create_mesh(ssbh_model: ssbh_data_py.modl_data.ModlData, ssbh_mesh, ssbh_skel, armature, context, material_label_to_material):
    '''
    So the goal here is to create a set of materials to share among the meshes for this model.
    But, other previously created models can have materials of the same name.
    Gonna make sure not to conflict.
    example, bpy.data.materials.new('A') might create 'A' or 'A.001', so store reference to the mat created rather than the name
    '''
    created_meshes = []
    '''
    unique_numdlb_material_labels = {e.material_label for e in ssbh_model.entries}
    
    texture_name_to_image_dict = {}
    texture_name_to_image_dict = import_material_images(ssbh_matl, context.scene.sub_scene_properties.model_import_folder_path)

    label_to_material_dict = {}
    for label in unique_numdlb_material_labels:
        blender_mat = bpy.data.materials.new(label)

        # Mesh import should still succeed even if materials can't be created.
        # TODO: Report some sort of error to the user?
        try:
            setup_blender_mat(blender_mat, label, ssbh_matl, texture_name_to_image_dict)
            label_to_material_dict[label] = blender_mat
        except Exception as e:
            # TODO: Report an exception instead.
            print(f'Failed to create material for {label}:  Error="{e}" ; Traceback=\n{traceback.format_exc()}')
    '''
    name_index_mat_dict = { 
        (e.mesh_object_name,e.mesh_object_subindex):material_label_to_material[e.material_label] 
        for e in ssbh_model.entries if e.material_label in material_label_to_material
    }

    start = time.time()

    for i, ssbh_mesh_object in enumerate(ssbh_mesh.objects):
        blender_mesh = create_blender_mesh(ssbh_mesh_object, ssbh_skel, name_index_mat_dict)
        mesh_obj = bpy.data.objects.new(blender_mesh.name, blender_mesh)

        attach_armature_create_vertex_groups(mesh_obj, ssbh_skel, armature, ssbh_mesh_object)
        mesh_obj["numshb order"] = i
        mesh_obj["numshb name"] = ssbh_mesh_object.name
        mesh_obj["numshb subindex"] = int(ssbh_mesh_object.subindex)
        context.collection.objects.link(mesh_obj)
        created_meshes.append(mesh_obj)
    
    end = time.time()
    print(f'Created meshes in {end - start} seconds')

    return created_meshes

def import_material_images(ssbh_matl, dir):
    texture_name_to_image_dict = {}
    texture_name_set = set()

    for ssbh_mat_entry in ssbh_matl.entries:
        for attribute in ssbh_mat_entry.textures:
            texture_name_set.add(attribute.data)

    for texture_name in texture_name_set:
        try:
            image = image_utils.load_image(texture_name + '.png', dir, place_holder=True, check_existing=False)  
            texture_name_to_image_dict[texture_name] = image
        except Exception as e:
            if "UnexpectedMipmapCount" in str(e):
                operator.report({'WARNING'}, f'Failed to convert texture {texture_name}. Please convert manually to PNG.')
            else:
                operator.report({'ERROR'}, f'Failed to convert texture {texture_name}: {str(e)}')

    return texture_name_to_image_dict


def enable_inputs(node_group_node, param_id):
    for input in node_group_node.inputs:
        if input.name.split(' ')[0] == param_id:
            input.hide = False


def get_vertex_attributes(node_group_node, shader_name):
    # Query the shader database for attribute information.
    # Using SQLite is much faster than iterating through the JSON dump.
    with sqlite3.connect(get_shader_db_file_path()) as con:
        # Construct a query to find all the vertex attributes for this shader.
        # Invalid shaders will return an empty list.
        sql = """
            SELECT v.AttributeName 
            FROM VertexAttribute v 
            INNER JOIN ShaderProgram s ON v.ShaderProgramID = s.ID 
            WHERE s.Name = ?
            """
        # The database has a single entry for each program, so don't include the render pass tag.
        return [row[0] for row in con.execute(sql, (shader_name[:len('SFX_PBS_0000000000000080')],)).fetchall()]

"""
def setup_blender_mat(blender_mat:bpy.types.Material, material_label, ssbh_matl: ssbh_data_py.matl_data.MatlData, texture_name_to_image_dict):
    # TODO: Handle none?
    entry = None
    for ssbh_mat_entry in ssbh_matl.entries:
        if ssbh_mat_entry.material_label == material_label:
            entry = ssbh_mat_entry
    
    # Change Mat Settings
    BlendFactor = ssbh_data_py.matl_data.BlendFactor
    CullMode = ssbh_data_py.matl_data.CullMode
    discard_shaders = get_discard_shaders()
    if entry.shader_label[:len('SFX_PBS_0000000000000080')] in discard_shaders:
        blender_mat.blend_method = 'CLIP'
    else:
        if len(entry.blend_states) > 0:
            if entry.blend_states[0].data.alpha_sample_to_coverage:
                blender_mat.blend_method = 'HASHED'
            elif entry.blend_states[0].data.destination_color.value == BlendFactor.OneMinusSourceAlpha.value:
                '''
                Alpha Blending would be the correct setting if EEVEE could do per-fragment alpha sorting.
                since it cant, alpha blending here would run into several mesh self-sorting issues so instead
                use another blending mode
                '''
                #blender_mat.blend_method = 'BLEND'
                blender_mat.blend_method = 'HASHED'
            else:
                blender_mat.blend_method = 'OPAQUE'

    if len(entry.rasterizer_states) > 0:
        if entry.rasterizer_states[0].data.cull_mode.value == CullMode.Back.value:
            blender_mat.use_backface_culling = True
    
    # Clone Master Shader
    master_shader_name = master_shader.get_master_shader_name()
    master_node_group = bpy.data.node_groups.get(master_shader_name)
    clone_group = master_node_group.copy()

    # Setup Clone
    clone_group.name = entry.shader_label

    # Add our new Nodes
    blender_mat.use_nodes = True
    nodes = blender_mat.node_tree.nodes
    links = blender_mat.node_tree.links

    # Cleanse Node Tree
    nodes.clear()
    
    material_output_node = nodes.new('ShaderNodeOutputMaterial')
    material_output_node.location = (900,0)
    node_group_node = nodes.new('ShaderNodeGroup')
    node_group_node.name = 'smash_ultimate_shader'
    node_group_node.width = 600
    node_group_node.location = (-300, 300)
    node_group_node.node_tree = clone_group
    for input in node_group_node.inputs:
        input.hide = True
    shader_label = node_group_node.inputs['Shader Label']
    shader_label.hide = False
    shader_name = entry.shader_label
    shader_label.default_value = entry.shader_label
    material_label = node_group_node.inputs['Material Name']
    material_label.hide = False
    material_label.default_value = entry.material_label
    
    # TODO: Refactor this to be cleaner?
    blend_state = entry.blend_states[0].data
    enable_inputs(node_group_node, entry.blend_states[0].param_id.name)

    blend_state_inputs = []
    for input in node_group_node.inputs:
        if input.name.split(' ')[0] == 'BlendState0':
            blend_state_inputs.append(input)
            
    for input in blend_state_inputs:
        field_name = input.name.split(' ')[1]
        if field_name == 'Field1':
            input.default_value = blend_state.source_color.name
        if field_name == 'Field3':
            input.default_value = blend_state.destination_color.name
        if field_name == 'Field7':
            input.default_value = blend_state.alpha_sample_to_coverage

    rasterizer_state = entry.rasterizer_states[0].data
    enable_inputs(node_group_node, entry.rasterizer_states[0].param_id.name)

    rasterizer_state_inputs = [input for input in node_group_node.inputs if input.name.split(' ')[0] == 'RasterizerState0']
    for input in rasterizer_state_inputs:
        field_name = input.name.split(' ')[1]
        if field_name == 'Field1':
            input.default_value = rasterizer_state.fill_mode.name
        if field_name == 'Field2':
            input.default_value = rasterizer_state.cull_mode.name
        if field_name == 'Field3':
            input.default_value = rasterizer_state.depth_bias
    
    for param in entry.booleans:
        input = node_group_node.inputs.get(param.param_id.name)
        input.hide = False
        input.default_value = param.data

    for param in entry.floats:
        input = node_group_node.inputs.get(param.param_id.name)
        input.hide = False
        input.default_value = param.data
    
    for param in entry.vectors:
        param_name = param.param_id.name

        if param_name in material_inputs.vec4_param_to_inputs:
            # Find and enable inputs.
            inputs = [node_group_node.inputs.get(name) for _, name, _ in material_inputs.vec4_param_to_inputs[param_name]]
            for input in inputs:
                input.hide = False

            # Assume inputs are RGBA, RGB/A, or X/Y/Z/W.
            x, y, z, w = param.data
            if len(inputs) == 1:
                inputs[0].default_value = (x,y,z,w)
            elif len(inputs) == 2:
                inputs[0].default_value = (x,y,z,1)
                inputs[1].default_value = w
            elif len(inputs) == 4:
                inputs[0].default_value = x
                inputs[1].default_value = y
                inputs[2].default_value = z
                inputs[3].default_value = w

            if param_name == 'CustomVector47':
                node_group_node.inputs['use_custom_vector_47'].default_value = 1.0

    links.new(material_output_node.inputs[0], node_group_node.outputs[0])

    # Add image texture nodes
    node_count = 0

    for texture_param in entry.textures:
        enable_inputs(node_group_node, texture_param.param_id.name)
        
        texture_node = nodes.new('ShaderNodeTexImage')
        texture_node.location = (-800, -500 * node_count + 1000)
        texture_file_name = texture_param.data
        texture_node.name = texture_param.param_id.name
        texture_node.label = texture_param.param_id.name
        texture_node.image = texture_name_to_image_dict[texture_file_name]
        matched_rgb_input = None
        matched_alpha_input = None
        for input in node_group_node.inputs:
            if texture_param.param_id.name == input.name.split(' ')[0]:
                if 'RGB' == input.name.split(' ')[1]:
                    matched_rgb_input = input
                else:
                    matched_alpha_input = input
        # For now, manually set the colorspace types....
        linear_textures = ['Texture6', 'Texture4']
        if texture_param.param_id.name in linear_textures:
            texture_node.image.colorspace_settings.name = 'Linear'
            texture_node.image.alpha_mode = 'CHANNEL_PACKED'
        
        uv_map_node = nodes.new('ShaderNodeUVMap')
        uv_map_node.name = 'uv_map_node'
        uv_map_node.location = (texture_node.location[0] - 1200, texture_node.location[1])
        uv_map_node.label = texture_param.param_id.name + ' UV Map'

        if texture_param.param_id.name == 'Texture9':
            uv_map_node.uv_map = 'bake1'
        elif texture_param.param_id.name == 'Texture1':
            uv_map_node.uv_map = 'uvSet'
        else:
            uv_map_node.uv_map = 'map1'

        # Create UV Transform Node
        # Also set the default_values here. I know it makes more sense to have the default_values
        # be in the init func of the node itself, but it just doesn't work there lol
        from ..shader_nodes import custom_uv_transform_node
        uv_transform_node = nodes.new(custom_uv_transform_node.SUB_CSN_ultimate_uv_transform.bl_idname)
        uv_transform_node.name = 'uv_transform_node'
        uv_transform_node.label = 'UV Transform' + texture_param.param_id.name.split('Texture')[1]
        uv_transform_node.location = (texture_node.location[0] - 900, texture_node.location[1])
        uv_transform_node.inputs[0].default_value = 1.0 # Scale X
        uv_transform_node.inputs[1].default_value = 1.0 # Scale Y

        # Create Sampler Node
        from ..shader_nodes import custom_sampler_node
        sampler_node = nodes.new(custom_sampler_node.SUB_CSN_ultimate_sampler.bl_idname)
        sampler_node.name = 'sampler_node'
        sampler_node.label = 'Sampler' + texture_param.param_id.name.split('Texture')[1]
        sampler_node.location = (texture_node.location[0] - 600, texture_node.location[1])
        sampler_node.width = 500

        # TODO: Handle the None case?
        sampler_entry = None
        for sampler_param in entry.samplers:
            if texture_param.param_id.name.split('Texture')[1] == sampler_param.param_id.name.split('Sampler')[1]:
                sampler_entry = sampler_param
                break

        enable_inputs(node_group_node, sampler_entry.param_id.name)
        sampler_data = sampler_entry.data
        sampler_node.wrap_s = sampler_data.wraps.name
        sampler_node.wrap_t = sampler_data.wrapt.name
        sampler_node.wrap_r = sampler_data.wrapr.name
        sampler_node.min_filter = sampler_data.min_filter.name
        sampler_node.mag_filter = sampler_data.mag_filter.name
        sampler_node.anisotropic_filtering = sampler_data.max_anisotropy is not None
        sampler_node.max_anisotropy = sampler_data.max_anisotropy.name if sampler_data.max_anisotropy else 'One'
        sampler_node.border_color = tuple(sampler_data.border_color)
        sampler_node.lod_bias = sampler_data.lod_bias       

        links.new(uv_transform_node.inputs[4], uv_map_node.outputs[0])
        links.new(sampler_node.inputs['UV Input'], uv_transform_node.outputs[0])
        links.new(texture_node.inputs[0], sampler_node.outputs[0])
        links.new(matched_rgb_input, texture_node.outputs['Color'])
        links.new(matched_alpha_input, texture_node.outputs['Alpha'])
        node_count = node_count + 1

    # Set up color sets.
    # Use the default values for non required attributes to be consistent between renderers.
    # Ignore the rendering accuracy of missing required attributes for now.
    required_attributes = get_vertex_attributes(node_group_node, shader_name)

    def create_and_enable_color_set(name, row):
        enable_inputs(node_group_node, name)

        color_set_node = nodes.new('ShaderNodeVertexColor')
        color_set_node.name = name
        color_set_node.label = name
        color_set_node.layer_name = name
        # Vertically stack color sets with even spacing.
        color_set_node.location = (-500, 150 - row * 150)

        links.new(node_group_node.inputs[f'{name} RGB'], color_set_node.outputs['Color'])
        links.new(node_group_node.inputs[f'{name} Alpha'], color_set_node.outputs['Alpha'])

    if 'colorSet1' in required_attributes:
        create_and_enable_color_set('colorSet1', 0)

    if 'colorSet5' in required_attributes:
        create_and_enable_color_set('colorSet5', 1)

    # Apply Solid view fixes to ensure proper display
    from ..material.create_blender_materials_from_matl import setup_material_for_solid_view, setup_eye_material_for_solid_view
    setup_material_for_solid_view(blender_mat)
    
    # Special handling for eye materials
    if 'Eye' in material_label:
        setup_eye_material_for_solid_view(blender_mat)

"""


def get_from_mesh_list_with_pruned_name(meshes:list, pruned_name:str, fallback=None) -> bpy.types.Object:
    for mesh in meshes:
        if mesh.name.startswith(pruned_name):
            return mesh
    return fallback

def read_nuhlpb_data(nuhlpb_path: Path, armature: bpy.types.Armature):
    if armature is None:
        raise ValueError("Armature is None, cannot read NUHLPB data.")
    
    ssbh_hlpb = ssbh_data_py.hlpb_data.read_hlpb(str(nuhlpb_path))
    shbd: SubHelperBoneData = armature.data.sub_helper_bone_data
    shbd.major_version = ssbh_hlpb.major_version
    shbd.minor_version = ssbh_hlpb.minor_version
    for a in ssbh_hlpb.aim_constraints:
        constraint: AimConstraint = shbd.aim_constraints.add()
        constraint.name = a.name
        constraint.aim_bone_name1 = a.aim_bone_name1
        constraint.aim_bone_name2 = a.aim_bone_name2
        constraint.aim_type1 = a.aim_type1
        constraint.aim_type2 = a.aim_type2
        constraint.target_bone_name1 = a.target_bone_name1
        constraint.target_bone_name2 = a.target_bone_name2
        constraint.aim = a.aim
        constraint.up = a.up
        # Smash is XYZW but blender is WXYZ
        constraint.quat1 = [a.quat1[3],  a.quat1[0], a.quat1[1], a.quat1[2]]
        # Smash is XYZW but blender is WXYZ
        constraint.quat2 = [a.quat2[3],  a.quat2[0], a.quat2[1], a.quat2[2]]
    for o in ssbh_hlpb.orient_constraints:
        constraint: OrientConstraint = shbd.orient_constraints.add()
        constraint.name = o.name
        constraint.parent_bone_name1 = o.parent_bone_name1
        constraint.parent_bone_name2 = o.parent_bone_name2
        constraint.source_bone_name = o.source_bone_name
        constraint.target_bone_name = o.target_bone_name
        constraint.unk_type = o.unk_type
        constraint.constraint_axes = o.constraint_axes
        # Smash is XYZW but blender is WXYZ
        constraint.quat1 = [o.quat1[3], o.quat1[0], o.quat1[1], o.quat1[2]]
        constraint.quat2 = [o.quat2[3], o.quat2[0], o.quat2[1], o.quat2[2]]
        constraint.range_min = o.range_min
        constraint.range_max = o.range_max

def create_aim_type_helper_bone_constraints(constraint_name: str, arma: bpy.types.Object,
                                            owner_bone_name: str, target_bone_name: str):
    owner_bone = arma.pose.bones.get(owner_bone_name, None)
    if owner_bone is not None:
        dtc: DampedTrackConstraint = owner_bone.constraints.new('DAMPED_TRACK')
        dtc.name = constraint_name
        dtc.track_axis = 'TRACK_Y'
        dtc.influence = 1.0
        dtc.target = arma
        dtc.subtarget = target_bone_name

def create_interpolation_type_helper_bone_constraints(constraint_name: str, arma: bpy.types.Object,
                                                      owner_bone_name: str, target_bone_name: str,
                                                      aoi_xyz_list: list[float]):
    owner_bone: PoseBone = arma.pose.bones.get(owner_bone_name, None)
    if owner_bone is not None:
        x,y,z = 'X', 'Y', 'Z'
        for index, axis in enumerate([x,y,z]):
            crc: CopyRotationConstraint = owner_bone.constraints.new('COPY_ROTATION')
            crc.name = f'{constraint_name}.{axis}'
            crc.target = arma
            crc.subtarget =  target_bone_name
            crc.target_space = 'POSE'
            crc.owner_space = 'POSE'
            crc.use_x = True if axis is x else False
            crc.use_y = True if axis is y else False
            crc.use_z = True if axis is z else False
            crc.influence = aoi_xyz_list[index]

def setup_helper_bone_constraints(arma: bpy.types.Object):
    bpy.ops.object.mode_set(mode='POSE', toggle=False)
    shbd: SubHelperBoneData = arma.data.sub_helper_bone_data
    for aim_entry in shbd.aim_constraints:
        aim_entry: AimConstraint
        create_aim_type_helper_bone_constraints(aim_entry.name, arma, aim_entry.target_bone_name1, aim_entry.aim_bone_name1)
    for interpolation_entry in shbd.orient_constraints:
        interpolation_entry: OrientConstraint
        constraint_axes: mathutils.Vector = interpolation_entry.constraint_axes
        create_interpolation_type_helper_bone_constraints(
            interpolation_entry.name,
            arma,
            interpolation_entry.target_bone_name,
            interpolation_entry.source_bone_name,
            [constraint_axes.y, constraint_axes.x, constraint_axes.z]
        )

def remove_helper_bone_constraints(arma: bpy.types.Object):
    bpy.ops.object.mode_set(mode='POSE', toggle=False)
    helper_bones: list[PoseBone] = [bone for bone in arma.pose.bones if bone.name.startswith('H_')]
    for bone in helper_bones:
        for constraint in bone.constraints:
            bone.constraints.remove(constraint)

def refresh_helper_bone_constraints(arma: bpy.types.Object):
    remove_helper_bone_constraints(arma)
    setup_helper_bone_constraints(arma)


def auto_refresh_loaded_model_folders():
    """Rescan saved model import folders on startup or file load."""
    try:
        for scene in bpy.data.scenes:
            ssp = getattr(scene, 'sub_scene_properties', None)
            if not ssp:
                continue
            folder = ssp.model_import_folder_path or getattr(ssp, 'last_model_folder', '')
            if folder and os.path.isdir(folder):
                refresh_model_import_list(ssp, folder)
    except Exception as exc:
        print(f"Model import auto-refresh failed: {exc}")
    return None


@bpy.app.handlers.persistent
def on_load_post_auto_refresh_model_import(_dummy):
    bpy.app.timers.register(auto_refresh_loaded_model_folders, first_interval=0.2)


def register_handlers():
    if on_load_post_auto_refresh_model_import not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_load_post_auto_refresh_model_import)
    bpy.app.timers.register(auto_refresh_loaded_model_folders, first_interval=0.5)


def unregister_handlers():
    if on_load_post_auto_refresh_model_import in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post_auto_refresh_model_import)
