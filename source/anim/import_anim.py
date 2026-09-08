import math
import bpy
import mathutils
import re
import collections
import time
import numpy as np
import cProfile
import pstats
import json
import os
from pathlib import Path

from ...dependencies import ssbh_data_py
from bpy_extras.io_utils import ImportHelper
from bpy.props import CollectionProperty, IntProperty, StringProperty, BoolProperty, FloatProperty, EnumProperty
from bpy.types import Operator, Panel, Menu
from mathutils import Matrix, Quaternion, Vector
from ..import_paths import walk_import_folders
from ..model.import_model import get_blender_transform
from ..blender_compat import assign_action, draw_progress, ensure_action_slot
from ..addon_preferences import format_animation_name_on_import
from .fcurve_compat import find_fcurve, new_fcurve, style_material_fcurve, style_visibility_fcurve
from .visibility_tracks import merge_imported_visibility_nodes, visibility_name_from_mesh
from .raw_anim import (
    RAW_ANIM_EXTENSION,
    import_raw_animation,
    is_fighter_motion_body_path,
    refresh_raw_animation_import_list,
    schedule_raw_animation_list_refresh,
    get_raw_anim_import_directory,
)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .anim_data import SUB_PG_sub_anim_data, SUB_PG_mat_track, SUB_PG_mat_track_property
    from bpy.types import ShaderNodeGroup, Material
    from ..model.material.sub_matl_data import SUB_PG_sub_matl_data
    from ..blender_property_extensions import SubSceneProperties

ANIM_FOLDER_KEY = "sub_anim_import_folder"
_last_anim_sync_ptr = 0


def remember_animation_folder(ssp, folder):
    if not folder:
        return
    folder = os.path.normpath(bpy.path.abspath(str(folder)))
    key = os.path.normcase(folder)
    if not any(os.path.normcase(item.path) == key for item in ssp.animation_import_folders):
        item = ssp.animation_import_folders.add()
        item.path = folder


def fill_animation_import_list(ssp, folder):
    remember_animation_folder(ssp, ssp.animation_import_folder_path)
    remember_animation_folder(ssp, folder)
    ssp.animation_import_files.clear()
    ssp.animation_import_files_index = 0
    if not folder or not os.path.isdir(folder):
        ssp.animation_import_folder_path = folder or ""
        return 0
    ssp.animation_import_folder_path = folder
    count = 0
    try:
        names = sorted(
            (name for name in os.listdir(folder) if name.lower().endswith(".nuanmb")),
            key=str.casefold,
        )
    except OSError:
        return 0
    for anim_file in names:
        anim_item = ssp.animation_import_files.add()
        anim_item.name = os.path.splitext(anim_file)[0]
        anim_item.path = str(Path(folder) / anim_file)
        anim_item.selected = count == 0
        count += 1
    ssp.animation_import_files_index = 0
    return count


def bind_anim_folder_to_armature(armature, folder):
    if armature is None or not folder:
        return
    try:
        armature[ANIM_FOLDER_KEY] = folder
        if armature.data is not None:
            armature.data[ANIM_FOLDER_KEY] = folder
    except Exception:
        pass


def anim_folder_for_armature(armature):
    if armature is None:
        return ""
    folder = armature.get(ANIM_FOLDER_KEY, "") or ""
    if folder:
        return folder
    data = getattr(armature, "data", None)
    if data is not None:
        folder = data.get(ANIM_FOLDER_KEY, "") or ""
        if folder:
            return folder
    smash = armature.get("sub_smash_model_folder", "") or ""
    if data is not None and not smash:
        smash = data.get("sub_smash_model_folder", "") or ""
    if smash:
        motion = smash.replace("model", "motion")
        if os.path.isdir(motion):
            nuanmb = [name for name in os.listdir(motion) if name.endswith(".nuanmb")]
            if nuanmb:
                return motion
            body = Path(motion) / "body" if os.path.basename(motion) != "body" else Path(motion)
            if not str(body).endswith("body"):
                fighter = Path(smash).parent.parent.parent
                body = fighter / "motion" / "body"
            if body.is_dir():
                subs = [name for name in os.listdir(body) if os.path.isdir(body / name)]
                if subs:
                    return str(body / subs[0])
    return ""


def sync_anim_importer_to_active(context=None):
    global _last_anim_sync_ptr
    context = context or bpy.context
    obj = getattr(context, "object", None)
    if obj is None or getattr(obj, "type", "") != "ARMATURE":
        return
    try:
        ptr = int(obj.as_pointer())
    except Exception:
        ptr = 0
    if ptr == _last_anim_sync_ptr:
        return
    folder = anim_folder_for_armature(obj)
    if not folder:
        _last_anim_sync_ptr = ptr
        return
    ssp = getattr(getattr(context, "scene", None), "sub_scene_properties", None)
    if ssp is None:
        return
    current = getattr(ssp, "animation_import_folder_path", "") or ""
    if os.path.normcase(os.path.normpath(current)) == os.path.normcase(os.path.normpath(folder)):
        _last_anim_sync_ptr = ptr
        return
    fill_animation_import_list(ssp, folder)
    try:
        from .raw_anim import refresh_raw_animation_import_list
        refresh_raw_animation_import_list(ssp)
    except Exception:
        pass
    _last_anim_sync_ptr = ptr


def import_animation_file(
    context: bpy.types.Context,
    operator: bpy.types.Operator,
    obj: bpy.types.Object,
    filepath: str,
    include_transform: bool,
    include_material: bool,
    include_visibility: bool,
    first_frame: int,
) -> bool:
    def refresh_smash_viewport():
        # Action assignment can update Blender at the current frame without a
        # frame-change event. Make the native Smash model consume that pose on
        # its very next draw instead of waiting for playback to start.
        try:
            from ..extras.smash_viewport import invalidate_animation_state
            invalidate_animation_state()
        except Exception:
            pass

    if filepath.lower().endswith(RAW_ANIM_EXTENSION):
        if obj.type != 'ARMATURE':
            operator.report({'ERROR'}, 'Raw animation import requires an armature.')
            return False
        old_mode = context.mode
        if old_mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE', toggle=False)
        success = import_raw_animation(context, obj, filepath, operator)
        if context.mode != old_mode:
            bpy.ops.object.mode_set(mode=old_mode, toggle=False)
        if success:
            refresh_smash_viewport()
        return success

    if obj.type == 'ARMATURE':
        old_mode = context.mode
        if old_mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE', toggle=False)
        import_model_anim(
            context,
            filepath,
            include_transform,
            include_material,
            include_visibility,
            first_frame,
            armature_object=obj,
        )
        if context.mode != old_mode:
            bpy.ops.object.mode_set(mode=old_mode, toggle=False)
    else:
        import_camera_anim(operator, context, filepath, first_frame)
    refresh_smash_viewport()
    return True


def import_animation_paths(context, operator, filepaths):
    """Import multiple animation paths in name order and leave the last one active."""
    supported = ('.nuanmb', RAW_ANIM_EXTENSION)
    paths = sorted(
        {
            Path(filepath).resolve()
            for filepath in filepaths
            if filepath and Path(filepath).suffix.lower() in supported
        },
        key=lambda path: path.name.casefold(),
    )
    if not paths:
        operator.report({'ERROR'}, 'No animation files selected.')
        return 0, 0

    ssp = context.scene.sub_scene_properties
    obj = context.object
    include_transform = ssp.anim_include_transform
    include_material = ssp.anim_include_material
    include_visibility = ssp.anim_include_visibility
    old_auto_key = context.scene.tool_settings.use_keyframe_insert_auto
    old_mode = context.mode
    imported = 0
    failed = 0
    context.scene.tool_settings.use_keyframe_insert_auto = False
    context.window_manager.progress_begin(0, len(paths))
    try:
        for index, path in enumerate(paths):
            context.window_manager.progress_update(index)
            if not path.is_file():
                operator.report({'WARNING'}, f'Animation file not found: {path}')
                failed += 1
                continue
            try:
                if import_animation_file(
                    context,
                    operator,
                    obj,
                    str(path),
                    include_transform,
                    include_material,
                    include_visibility,
                    1,
                ):
                    imported += 1
                    ssp.last_anim_import_dir = str(path.parent)
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                operator.report({'ERROR'}, f"Failed to import '{path.name}': {exc}")
    finally:
        context.window_manager.progress_end()
        context.scene.tool_settings.use_keyframe_insert_auto = old_auto_key
        if obj.type == 'ARMATURE' and context.mode != old_mode:
            try:
                bpy.ops.object.mode_set(mode=old_mode, toggle=False)
            except Exception:
                pass
    return imported, failed


class SUB_UL_animation_import_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            op = layout.operator(
                SUB_OP_toggle_animation_import_selection.bl_idname,
                text=item.name,
                icon='CHECKBOX_HLT' if item.selected else 'CHECKBOX_DEHLT',
                depress=item.selected,
            )
            op.index = index
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            op = layout.operator(
                SUB_OP_toggle_animation_import_selection.bl_idname,
                text=item.name,
                depress=item.selected,
            )
            op.index = index


class SUB_OP_toggle_animation_import_selection(Operator):
    bl_idname = 'sub.toggle_animation_import_selection'
    bl_label = 'Select Animation'
    bl_description = 'Click to select one; Ctrl-click toggles; Shift-click selects a range'
    bl_options = {'INTERNAL'}

    index: IntProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        items = ssp.animation_import_files
        if self.index < 0 or self.index >= len(items):
            return {'CANCELLED'}

        previous_index = max(0, min(ssp.animation_import_files_index, len(items) - 1))
        if event.shift:
            if not event.ctrl:
                for item in items:
                    item.selected = False
            first, last = sorted((previous_index, self.index))
            for index in range(first, last + 1):
                items[index].selected = True
        elif event.ctrl:
            items[self.index].selected = not items[self.index].selected
        else:
            for index, item in enumerate(items):
                item.selected = index == self.index

        ssp.animation_import_files_index = self.index
        return {'FINISHED'}

    def execute(self, _context):
        return {'FINISHED'}


class SUB_OP_select_all_animation_imports(Operator):
    bl_idname = 'sub.select_all_animation_imports'
    bl_label = 'Select All Animations'
    bl_description = 'Select every animation in the folder list'
    bl_options = {'INTERNAL'}

    select: BoolProperty(default=True, options={'HIDDEN'})

    def execute(self, context):
        for item in context.scene.sub_scene_properties.animation_import_files:
            item.selected = self.select
        return {'FINISHED'}


class SUB_UL_raw_animation_import_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text=item.name)

class SUB_OP_import_all_animations(bpy.types.Operator):
    bl_idname = 'sub.import_all_animations'
    bl_label = 'Import All Animations'
    bl_options = {'REGISTER', 'UNDO'}

    # Choice of range to import
    import_mode: EnumProperty(
        name="Import Range",
        description="Choose whether to import all animations or start from the selected one",
        items=(
            ('ALL', "All Animations", "Import every animation in the list"),
            ('FROM_SELECTED', "From Selected Onward", "Start importing at the selected animation and continue to the end"),
        ),
        default='ALL'
    )

    @classmethod
    def poll(cls, context):
        obj: bpy.types.Object = context.object
        if obj is None:
            return False
        elif obj.type != 'ARMATURE' and obj.type != 'CAMERA':
            return False
        
        ssp = context.scene.sub_scene_properties
        return len(ssp.animation_import_files) > 0
    
    # Progress tracking properties
    progress: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    progress_text: bpy.props.StringProperty(default="")
    is_importing: bpy.props.BoolProperty(default=False)
    current_animation_index: bpy.props.IntProperty(default=0)
    imported_count: bpy.props.IntProperty(default=0)
    
    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        anim_count = len(ssp.animation_import_files)
        self.progress = 0.0
        self.progress_text = f"Ready to import {anim_count} animations"
        self.is_importing = False
        self.current_animation_index = 0
        self.imported_count = 0
        return context.window_manager.invoke_props_dialog(self, width=450)
    
    def draw(self, context):
        layout = self.layout
        ssp = context.scene.sub_scene_properties
        anim_count = len(ssp.animation_import_files)
        
        if not self.is_importing:
            # Confirmation phase
            layout.label(text=f"Choose what to import ({anim_count} found):")
            layout.prop(self, "import_mode", expand=True)
            if self.import_mode == 'FROM_SELECTED' and 0 <= ssp.animation_import_files_index < anim_count:
                sel_name = ssp.animation_import_files[ssp.animation_import_files_index].name
                layout.label(text=f"Starting from: {sel_name}")
        else:
            # Progress phase
            layout.label(text=self.progress_text)
            draw_progress(layout, self.progress)
            layout.label(text=f"Imported: {self.imported_count}/{anim_count}")
            
    def modal(self, context, event):
        if event.type == 'TIMER':
            # Check if we're still importing to prevent multiple calls
            if self.is_importing:
                self.import_next_animation(context)
                return {'RUNNING_MODAL'}
            else:
                # Import is finished, clean up and exit
                return {'FINISHED'}
        elif event.type == 'ESC':
            # Cancel the import process
            self.cancel_import(context)
            return {'FINISHED'}
        return {'PASS_THROUGH'}
            
    def execute(self, context):
        if not self.is_importing:
            # Start the import process
            self.is_importing = True
            self.imported_count = 0
            
            # Setup for modal operation
            ssp = context.scene.sub_scene_properties
            
            # Use scene properties instead of operator properties
            self.include_transform = ssp.anim_include_transform
            self.include_material = ssp.anim_include_material  
            self.include_visibility = ssp.anim_include_visibility
            self.first_frame = 1
            # Determine starting index
            total_animations = len(ssp.animation_import_files)
            if self.import_mode == 'FROM_SELECTED' and total_animations > 0:
                start_index = max(0, min(ssp.animation_import_files_index, total_animations - 1))
            else:
                start_index = 0
            self.current_animation_index = start_index
            
            # Save current auto-keyframe setting and disable it
            self.use_keyframe_insert_auto = context.scene.tool_settings.use_keyframe_insert_auto
            context.scene.tool_settings.use_keyframe_insert_auto = False
            
            # Set to pose mode if needed
            self.old_mode = context.mode
            obj = context.object
            if obj.type == 'ARMATURE' and self.old_mode != 'POSE':
                bpy.ops.object.mode_set(mode='POSE', toggle=False)
            
            # Start timer for processing animations
            self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        else:
            return {'FINISHED'}
    
    def import_next_animation(self, context):
        ssp = context.scene.sub_scene_properties
        total_animations = len(ssp.animation_import_files)
        
        if self.current_animation_index >= total_animations:
            # Import complete - only finish if we're still importing
            if self.is_importing:
                self.finish_import(context)
            return
        
        anim_item = ssp.animation_import_files[self.current_animation_index]
        self.progress_text = f"Importing: {anim_item.name}"
        self.progress = self.current_animation_index / total_animations
        
        # Force UI update
        for area in context.screen.areas:
            area.tag_redraw()
        
        if not Path(anim_item.path).exists():
            self.report({"WARNING"}, f"Animation file not found: {anim_item.path}")
        else:
            try:
                obj = context.object
                import_animation_file(
                    context,
                    self,
                    obj,
                    anim_item.path,
                    self.include_transform,
                    self.include_material,
                    self.include_visibility,
                    self.first_frame,
                )
                
                self.imported_count += 1
                
            except Exception as e:
                self.report({"ERROR"}, f"Failed to import animation '{anim_item.name}': {str(e)}")
        
        self.current_animation_index += 1
    
    def cancel_import(self, context):
        # Clean up
        if hasattr(self, '_timer'):
            context.window_manager.event_timer_remove(self._timer)
            delattr(self, '_timer')
        
        # Restore original mode
        obj = context.object
        if obj.type == 'ARMATURE' and self.old_mode != 'POSE':
            bpy.ops.object.mode_set(mode=self.old_mode, toggle=False)
        
        # Restore auto-keyframe setting
        context.scene.tool_settings.use_keyframe_insert_auto = self.use_keyframe_insert_auto
        
        # Mark as finished to prevent multiple reports
        self.is_importing = False
        
        # Report cancellation
        ssp = context.scene.sub_scene_properties
        total_animations = len(ssp.animation_import_files)
        self.report({"WARNING"}, f"Bulk import cancelled. Imported {self.imported_count}/{total_animations} animations")
        
        # Force UI update
        for area in context.screen.areas:
            area.tag_redraw()
    
    def finish_import(self, context):
        # Clean up
        if hasattr(self, '_timer'):
            context.window_manager.event_timer_remove(self._timer)
            delattr(self, '_timer')
        
        # Restore original mode
        obj = context.object
        if obj.type == 'ARMATURE' and self.old_mode != 'POSE':
            bpy.ops.object.mode_set(mode=self.old_mode, toggle=False)
        
        # Restore auto-keyframe setting
        context.scene.tool_settings.use_keyframe_insert_auto = self.use_keyframe_insert_auto
        
        # Final progress update
        ssp = context.scene.sub_scene_properties
        total_animations = len(ssp.animation_import_files)
        self.progress = 1.0
        self.progress_text = f"Complete! Imported {self.imported_count}/{total_animations} animations"
        
        # Force final UI update
        for area in context.screen.areas:
            area.tag_redraw()
        
        # Mark as finished to prevent multiple reports
        self.is_importing = False
        
        self.report({"INFO"}, f"Successfully imported {self.imported_count}/{total_animations} animations")
        return {'FINISHED'}

class SUB_OP_import_selected_anim(bpy.types.Operator):
    bl_idname = 'sub.import_selected_anim'
    bl_label = 'Import Selected Animations'
    bl_description = 'Import every animation selected in the folder list'
    bl_options = {'UNDO'}

    include_transform_track: BoolProperty(
        name='Include Transform',
        description='Include Transform Track',
        default=True,
    )
    include_material_track: BoolProperty(
        name='Include Material',
        description='Include Material Track',
        default=True,
    )
    include_visibility_track: BoolProperty(
        name='Include Visibility',
        description='Include Visibility Track',
        default=True,
    )
    first_blender_frame: IntProperty(
        name='Start Frame',
        description='What frame to start importing the track on',
        default=1,
    )
    use_debug_timer: BoolProperty(
        name='Debug timing stats',
        description='Print advance import timing info to the console',
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj: bpy.types.Object = context.object
        if obj is None:
            return False
        elif obj.type != 'ARMATURE' and obj.type != 'CAMERA':
            return False
        
        ssp = context.scene.sub_scene_properties
        return any(item.selected for item in ssp.animation_import_files)
    
    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        paths = [item.path for item in ssp.animation_import_files if item.selected]
        imported, failed = import_animation_paths(context, self, paths)
        if imported == 0:
            return {'CANCELLED'}
        message = f'Imported {imported} selected animation(s)'
        if failed:
            message += f'; {failed} failed'
        self.report({'WARNING'} if failed else {'INFO'}, message)
        return {'FINISHED'}


class SUB_OP_browse_raw_animation_folder(Operator):
    bl_idname = 'sub.browse_raw_animation_folder'
    bl_label = 'Browse Raw Animation Folder'
    bl_description = 'Choose a folder containing raw animation files'
    bl_options = {'UNDO'}

    directory: StringProperty(subtype="DIR_PATH")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE' and obj.select_get()

    def invoke(self, context, _event):
        ssp = context.scene.sub_scene_properties
        if ssp.raw_animation_import_folder_path:
            self.directory = ssp.raw_animation_import_folder_path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        folder_path = refresh_raw_animation_import_list(ssp, self.directory)
        if folder_path and os.path.isdir(folder_path):
            count = len(ssp.raw_animation_import_files)
            self.report({'INFO'}, f'Found {count} raw animation(s) in: {folder_path}')
        elif folder_path:
            self.report({'INFO'}, f'Raw animation folder not found yet: {folder_path}')
        else:
            self.report({'INFO'}, 'No raw animation folder selected.')
        return {'FINISHED'}


class SUB_OP_import_raw_anim_file(Operator, ImportHelper):
    bl_idname = 'sub.import_raw_anim_file'
    bl_label = 'Import Raw Animation File'
    bl_description = 'Import a single .rawanim onto the selected armature'
    bl_options = {'UNDO'}

    filter_glob: StringProperty(default='*.rawanim', options={'HIDDEN'})
    filename_ext = '.rawanim'

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE' and obj.select_get()

    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        folder = get_raw_anim_import_directory(ssp)
        if folder and os.path.isdir(folder):
            self.filepath = os.path.join(folder, '')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = context.active_object
        filepath = self.filepath
        if not filepath or not os.path.isfile(filepath):
            self.report({'ERROR'}, 'No raw animation file selected.')
            return {'CANCELLED'}
        if not filepath.lower().endswith(RAW_ANIM_EXTENSION):
            self.report({'ERROR'}, 'Selected file is not a .rawanim.')
            return {'CANCELLED'}
        use_keyframe_insert_auto = context.scene.tool_settings.use_keyframe_insert_auto
        context.scene.tool_settings.use_keyframe_insert_auto = False
        try:
            ok = import_animation_file(
                context, self, obj, filepath, True, False, False, 1
            )
        finally:
            context.scene.tool_settings.use_keyframe_insert_auto = use_keyframe_insert_auto
        if not ok:
            return {'CANCELLED'}
        folder = os.path.dirname(filepath)
        if folder:
            refresh_raw_animation_import_list(context.scene.sub_scene_properties, folder)
        self.report({'INFO'}, f"Imported raw animation: {os.path.basename(filepath)}")
        return {'FINISHED'}


class SUB_OP_refresh_raw_animation_list(Operator):
    bl_idname = 'sub.refresh_raw_animation_list'
    bl_label = 'Refresh Raw Animation List'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE' and obj.select_get()

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        folder_path = refresh_raw_animation_import_list(ssp)
        count = len(ssp.raw_animation_import_files)
        if folder_path:
            self.report({'INFO'}, f'Found {count} raw animation(s) in: {folder_path}')
        else:
            self.report({'INFO'}, 'No raw animation folder detected.')
        return {'FINISHED'}


class SUB_OP_import_selected_raw_anim(Operator):
    bl_idname = 'sub.import_selected_raw_anim'
    bl_label = 'Import Selected Raw Animation'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'ARMATURE':
            return False
        ssp = context.scene.sub_scene_properties
        return (
            len(ssp.raw_animation_import_files) > 0
            and ssp.raw_animation_import_files_index < len(ssp.raw_animation_import_files)
        )

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        selected_anim = ssp.raw_animation_import_files[ssp.raw_animation_import_files_index]
        if not Path(selected_anim.path).exists():
            self.report({'ERROR'}, f"Raw animation file not found: {selected_anim.path}")
            return {'CANCELLED'}

        obj = context.active_object
        use_keyframe_insert_auto = context.scene.tool_settings.use_keyframe_insert_auto
        context.scene.tool_settings.use_keyframe_insert_auto = False
        import_animation_file(context, self, obj, selected_anim.path, True, False, False, 1)
        context.scene.tool_settings.use_keyframe_insert_auto = use_keyframe_insert_auto
        self.report({'INFO'}, f"Imported raw animation: {selected_anim.name}")
        return {'FINISHED'}


class SUB_OP_import_all_raw_anims(Operator):
    bl_idname = 'sub.import_all_raw_anims'
    bl_label = 'Import All Raw Animations'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'ARMATURE':
            return False
        ssp = context.scene.sub_scene_properties
        return len(ssp.raw_animation_import_files) > 0

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        obj = context.active_object
        use_keyframe_insert_auto = context.scene.tool_settings.use_keyframe_insert_auto
        context.scene.tool_settings.use_keyframe_insert_auto = False

        imported_count = 0
        for anim_item in ssp.raw_animation_import_files:
            if not Path(anim_item.path).exists():
                self.report({'WARNING'}, f"Raw animation file not found: {anim_item.path}")
                continue
            try:
                import_animation_file(context, self, obj, anim_item.path, True, False, False, 1)
                imported_count += 1
            except Exception as exc:
                self.report({'ERROR'}, f"Failed to import raw animation '{anim_item.name}': {exc}")

        context.scene.tool_settings.use_keyframe_insert_auto = use_keyframe_insert_auto
        self.report({'INFO'}, f"Imported {imported_count}/{len(ssp.raw_animation_import_files)} raw animations")
        return {'FINISHED'}


class SUB_PT_import_anim(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Animation Importer'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        if context.mode == "POSE" or context.mode == "OBJECT":
            return True
        return False
    
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        obj: bpy.types.Object = context.active_object
        ssp = context.scene.sub_scene_properties
        
        # Show browse button
        row = layout.row()
        if obj is None:
            row.label(text="Click on an Armature or Camera.")
        elif obj.select_get() is False:
            row.label(text="Click on an Armature or Camera.")
        elif obj.type == 'ARMATURE' or obj.type == 'CAMERA':
            row.operator(SUB_OP_import_anim.bl_idname, icon='IMPORT', text='Browse .NUANMB Files')
        else:
            row.label(text=f'The selected {obj.type.lower()} is not an armature or a camera.')
            
        # Show animations from imported model
        if obj and obj.select_get() and (obj.type == 'ARMATURE' or obj.type == 'CAMERA'):
            # Add button to browse for an animation folder
            row = layout.row()
            row.operator(SUB_OP_select_animation_folder.bl_idname, icon='ZOOM_ALL', text='Add Animation Folder')
            
            if ssp.animation_import_folder_path or len(ssp.animation_import_folders) > 0:
                # Collapsible Related Animations section
                box = layout.box()
                header_row = box.row()
                header_row.prop(ssp, "related_animations_expanded", 
                               icon="TRIA_DOWN" if ssp.related_animations_expanded else "TRIA_RIGHT",
                               icon_only=True, emboss=False)
                header_row.label(text="Related Animations:")
                
                # Only show content if expanded
                if ssp.related_animations_expanded:
                    # Collapsible Import Options section
                    header_row = box.row()
                    header_row.prop(ssp, "import_options_expanded", 
                                   icon="TRIA_DOWN" if ssp.import_options_expanded else "TRIA_RIGHT",
                                   icon_only=True, emboss=False)
                    header_row.label(text="Import Options:")
                    
                    # Only show import options if expanded
                    if ssp.import_options_expanded:
                        row = box.row()
                        col = row.column()
                        col.prop(ssp, "anim_include_transform", text="Include Transform")
                        col.prop(ssp, "anim_include_material", text="Include Material")  
                        col.prop(ssp, "anim_include_visibility", text="Include Visibility")
                    
                    if ssp.animation_import_folder_path:
                        row = box.row()
                        row.menu(SUB_MT_animation_folders.bl_idname, text=f"Folder: {ssp.animation_import_folder_path}")
                    
                    row = box.row()
                    row.template_list(
                        "SUB_UL_animation_import_list",
                        "",
                        ssp,
                        "animation_import_files",
                        ssp,
                        "animation_import_files_index",
                        rows=5,
                    )

                    help_row = box.row()
                    help_row.scale_y = 0.8
                    help_row.label(text="Click: one  Ctrl-click: toggle  Shift-click: range", icon='INFO')

                    row = box.row(align=True)
                    op = row.operator(SUB_OP_select_all_animation_imports.bl_idname, text="Select All")
                    op.select = True
                    op = row.operator(SUB_OP_select_all_animation_imports.bl_idname, text="Deselect All")
                    op.select = False
                    
                    row = box.row()
                    row.scale_y = 1.2
                    selected_count = sum(1 for item in ssp.animation_import_files if item.selected)
                    row.operator(
                        SUB_OP_import_selected_anim.bl_idname,
                        icon='IMPORT',
                        text=f"Import Selected Animations ({selected_count})",
                    )
                    
                    # Add batch import button
                    row = box.row()
                    row.operator(SUB_OP_import_all_animations.bl_idname, text="Import All Animations")


class SUB_PT_raw_animations(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Raw Animations'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.mode in {"POSE", "OBJECT"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        ssp = context.scene.sub_scene_properties
        obj = context.active_object

        help_box = layout.box()
        help_box.label(text="What are Raw Animations?", icon='INFO')
        col = help_box.column(align=True)
        col.scale_y = 0.9
        col.label(text="Sparse .rawanim files for animator round-trips.")
        col.label(text="They keep pose/IK keys without baking every frame,")
        col.label(text="unlike game .nuanmb exports.")

        if obj is None or not obj.select_get() or obj.type != 'ARMATURE':
            layout.label(text="Select an armature to import or export raw anims.")
            return

        if (
            not ssp.raw_animation_import_folder_path
            and is_fighter_motion_body_path(ssp.animation_import_folder_path)
        ):
            schedule_raw_animation_list_refresh(context)

        import_box = layout.box()
        import_box.label(text="Import", icon='IMPORT')

        row = import_box.row()
        row.operator(
            SUB_OP_browse_raw_animation_folder.bl_idname,
            icon='ZOOM_ALL',
            text='Browse Raw Animation Folder',
        )
        row.operator(SUB_OP_refresh_raw_animation_list.bl_idname, icon='FILE_REFRESH', text='')

        row = import_box.row()
        row.operator(
            SUB_OP_import_raw_anim_file.bl_idname,
            icon='IMPORT',
            text='Import Raw Animation File',
        )

        display_raw_folder = get_raw_anim_import_directory(ssp)
        if display_raw_folder:
            row = import_box.row()
            row.label(text=f"Folder: {display_raw_folder}")

        if len(ssp.raw_animation_import_files) > 0:
            row = import_box.row()
            row.template_list(
                "SUB_UL_raw_animation_import_list",
                "",
                ssp,
                "raw_animation_import_files",
                ssp,
                "raw_animation_import_files_index",
                rows=3,
            )
            row = import_box.row()
            row.operator(
                SUB_OP_import_selected_raw_anim.bl_idname,
                text="Import Selected Raw Animation",
            )
            row = import_box.row()
            row.operator(
                SUB_OP_import_all_raw_anims.bl_idname,
                text="Import All Raw Animations",
            )
        elif display_raw_folder:
            row = import_box.row()
            row.label(text="No .rawanim files found in this folder.", icon='INFO')

        export_box = layout.box()
        export_box.label(text="Export", icon='EXPORT')
        export_box.prop(ssp, "anim_include_raw_animation", text="Include Raw with .NUANMB Export")
        row = export_box.row()
        row.scale_y = 1.2
        row.operator('sub.raw_anim_export', icon='EXPORT', text='Export Raw Animation')


class SUB_OP_import_anim(Operator):
    bl_idname = 'sub.import_anim'
    bl_label = 'Import Animations'
    bl_description = 'Import one or more selected .nuanmb files'
    bl_options = {'UNDO'}

    filter_glob: StringProperty(
        default='*.nuanmb',
        options={'HIDDEN'}
    )
    include_transform_track: BoolProperty(
        name='Include Transform',
        description='Include Transform Track',
        default=True,
    )
    include_material_track: BoolProperty(
        name='Include Material',
        description='Include Material Track',
        default=True,
    )
    include_visibility_track: BoolProperty(
        name='Include Visibility',
        description='Include Visibility Track',
        default=True,
    )
    first_blender_frame: IntProperty(
        name='Start Frame',
        description='What frame to start importing the track on',
        default=1,
    )
    use_debug_timer: BoolProperty(
        name='Debug timing stats',
        description='Print advance import timing info to the console',
        default=False,
    )

    filepath: StringProperty(subtype="FILE_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)

    @classmethod
    def poll(cls, context):
        obj: bpy.types.Object = context.object
        if obj is None:
            return False
        elif obj.type != 'ARMATURE' and obj.type != 'CAMERA':
            return False
        return True
    
    def invoke(self, context, event):
        self.first_blender_frame = context.scene.frame_start
        last_directory = context.scene.sub_scene_properties.last_anim_import_dir
        if last_directory and os.path.isdir(last_directory):
            self.filepath = os.path.join(last_directory, '')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.files:
            folder = Path(self.directory or self.filepath).parent if not self.directory else Path(self.directory)
            paths = [folder / entry.name for entry in self.files]
        elif self.filepath and not Path(self.filepath).is_dir():
            paths = [Path(self.filepath)]
        else:
            paths = []
        invalid_paths = [path for path in paths if path.suffix.lower() != '.nuanmb']
        paths = [path for path in paths if path.suffix.lower() == '.nuanmb']
        if invalid_paths:
            self.report(
                {'WARNING'},
                f"Skipped {len(invalid_paths)} non-.nuanmb file(s); use Raw Animations for .rawanim files",
            )
        if not paths:
            self.report({"ERROR"}, "No animation files selected!")
            return {'CANCELLED'}
        imported, failed = import_animation_paths(context, self, paths)
        if imported == 0:
            return {'CANCELLED'}
        message = f'Imported {imported} animation(s)'
        if failed:
            message += f'; {failed} failed'
        self.report({'WARNING'} if failed else {'INFO'}, message)
        return {'FINISHED'}
  
def poll_cameras(self, obj):
    return obj.type == 'CAMERA'

# Smash is Y-up / X-major, Blender is Z-up / Y-major. These never change, so
# build them once instead of once per root-bone keyframe.
_Y_UP_TO_Z_UP = Matrix.Rotation(math.radians(90), 4, 'X')
_X_MAJOR_TO_Y_MAJOR = Matrix.Rotation(math.radians(-90), 4, 'Z')


def hierarchy_order(bone, reordered):
        if bone not in reordered:
            reordered.append(bone)
        for child in bone.children:
            hierarchy_order(child, reordered)

def get_hierarchy_order(bone_list: list[bpy.types.PoseBone]) -> list[bpy.types.PoseBone]:
    root_bones: list[bpy.types.PoseBone] = []
    for bone in bone_list:
        if bone.parent is None:
            root_bones.append(bone)
    return root_bones + [c for root_bone in root_bones for c in root_bone.children_recursive if c in bone_list]

class BoneTranslationFCurves():
    # Keyframe values are stashed as one flat [frame, value, frame, value, ...]
    # list per channel, which is exactly what FCurve.foreach_set('co') wants.
    # Building per-keyframe pair lists and flattening them at the end allocated
    # millions of tiny lists on a full-length animation.
    def __init__(self, action, bone_name, values_length):
        self.data_path = f'pose.bones["{bone_name}"].location'
        self.x: bpy.types.FCurve = create_fcurve(action, 'OBJECT', self.data_path, 0, f'{bone_name}')
        self.y: bpy.types.FCurve = create_fcurve(action, 'OBJECT', self.data_path, 1, f'{bone_name}')
        self.z: bpy.types.FCurve = create_fcurve(action, 'OBJECT', self.data_path, 2, f'{bone_name}')
        self.values_length = values_length
        self.x_stashed_values = [0.0] * (values_length * 2)
        self.y_stashed_values = [0.0] * (values_length * 2)
        self.z_stashed_values = [0.0] * (values_length * 2)
    def get_translation_matrix(self, index: int):
        if index >= len(self.x.keyframe_points):
            index = 0
        offset = index * 2 + 1
        return Matrix.Translation([self.x_stashed_values[offset],
                                   self.y_stashed_values[offset],
                                   self.z_stashed_values[offset]])
    def stash_keyframe_set_from_vector(self, index, frame, translation_vector: Vector):
        x, y, z = translation_vector
        offset = index * 2
        xs = self.x_stashed_values
        ys = self.y_stashed_values
        zs = self.z_stashed_values
        xs[offset] = frame
        xs[offset + 1] = x
        ys[offset] = frame
        ys[offset + 1] = y
        zs[offset] = frame
        zs[offset + 1] = z
    def set_keyframe_values_from_stash(self):
        count = self.values_length
        self.x.keyframe_points.add(count=count)
        self.y.keyframe_points.add(count=count)
        self.z.keyframe_points.add(count=count)
        self.x.keyframe_points.foreach_set('co', self.x_stashed_values)
        self.y.keyframe_points.foreach_set('co', self.y_stashed_values)
        self.z.keyframe_points.foreach_set('co', self.z_stashed_values)

class BoneRotationFCurves():
    def __init__(self, action, base_data_path, bone_name, values_length):
        self.w: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.rotation_quaternion', 0, f'{bone_name}')
        self.x: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.rotation_quaternion', 1, f'{bone_name}')
        self.y: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.rotation_quaternion', 2, f'{bone_name}')
        self.z: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.rotation_quaternion', 3, f'{bone_name}')
        self.values_length = values_length
        self.w_stashed_values = [0.0] * (values_length * 2)
        self.x_stashed_values = [0.0] * (values_length * 2)
        self.y_stashed_values = [0.0] * (values_length * 2)
        self.z_stashed_values = [0.0] * (values_length * 2)
    def get_rotation_matrix(self, index: int):
        if index >= len(self.w.keyframe_points):
            index = 0
        offset = index * 2 + 1
        q = Quaternion([self.w_stashed_values[offset],
                        self.x_stashed_values[offset],
                        self.y_stashed_values[offset],
                        self.z_stashed_values[offset]])
        return Matrix.Rotation(q.angle, 4, q.axis)
    def stash_keyframe_values_from_quaternion(self, index, frame, quaternion: Quaternion):
        w, x, y, z = quaternion
        offset = index * 2
        ws = self.w_stashed_values
        xs = self.x_stashed_values
        ys = self.y_stashed_values
        zs = self.z_stashed_values
        ws[offset] = frame
        ws[offset + 1] = w
        xs[offset] = frame
        xs[offset + 1] = x
        ys[offset] = frame
        ys[offset + 1] = y
        zs[offset] = frame
        zs[offset + 1] = z
    def set_keyframe_values_from_stash(self):
        count = self.values_length
        self.w.keyframe_points.add(count=count)
        self.x.keyframe_points.add(count=count)
        self.y.keyframe_points.add(count=count)
        self.z.keyframe_points.add(count=count)
        self.w.keyframe_points.foreach_set('co', self.w_stashed_values)
        self.x.keyframe_points.foreach_set('co', self.x_stashed_values)
        self.y.keyframe_points.foreach_set('co', self.y_stashed_values)
        self.z.keyframe_points.foreach_set('co', self.z_stashed_values)

class BoneScaleFCurves():
    def __init__(self, action, base_data_path, bone_name, values_length):
        self.x: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.scale', 0, f'{bone_name}')
        self.y: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.scale', 1, f'{bone_name}')
        self.z: bpy.types.FCurve = create_fcurve(action, 'OBJECT', f'{base_data_path}.scale', 2, f'{bone_name}')
        self.values_length = values_length
        self.x_stashed_values = [0.0] * (values_length * 2)
        self.y_stashed_values = [0.0] * (values_length * 2)
        self.z_stashed_values = [0.0] * (values_length * 2)
    def get_scale_matrix(self, index: int):
        if index >= len(self.x.keyframe_points):
            index = 0
        offset = index * 2 + 1
        return Matrix.Diagonal([self.x_stashed_values[offset],
                                self.y_stashed_values[offset],
                                self.z_stashed_values[offset],
                                1.0])
    def stash_keyframe_set_from_vector(self, index, frame, scale_vector: Vector):
        x, y, z = scale_vector
        offset = index * 2
        xs = self.x_stashed_values
        ys = self.y_stashed_values
        zs = self.z_stashed_values
        xs[offset] = frame
        xs[offset + 1] = x
        ys[offset] = frame
        ys[offset + 1] = y
        zs[offset] = frame
        zs[offset + 1] = z
    def set_keyframe_values_from_stash(self):
        count = self.values_length
        self.x.keyframe_points.add(count=count)
        self.y.keyframe_points.add(count=count)
        self.z.keyframe_points.add(count=count)
        self.x.keyframe_points.foreach_set('co', self.x_stashed_values)
        self.y.keyframe_points.foreach_set('co', self.y_stashed_values)
        self.z.keyframe_points.foreach_set('co', self.z_stashed_values)

class BoneFCurves():
    def __init__(self, bone_name, action, values_length):
        self.bone_name: str = bone_name
        self.base_data_path: str = f'pose.bones["{bone_name}"]'
        self.translation = BoneTranslationFCurves(action, bone_name, values_length)
        self.rotation = BoneRotationFCurves(action, self.base_data_path, bone_name, values_length)
        self.scale = BoneScaleFCurves(action, self.base_data_path, bone_name, values_length)
    def get_matrix_basis(self, index):
        tm = self.translation.get_translation_matrix(index)
        rm = self.rotation.get_rotation_matrix(index)
        sm = self.scale.get_scale_matrix(index)
        return Matrix(tm @ rm @ sm)
    def stash_keyframe_set_from_matrix(self, index, frame, matrix: Matrix):
        t, r, s = matrix.decompose()
        self.translation.stash_keyframe_set_from_vector(index, frame, t)
        self.rotation.stash_keyframe_values_from_quaternion(index, frame, r)
        self.scale.stash_keyframe_set_from_vector(index, frame, s)
    def stash_keyframe_set_from_components(self, index, frame, t, r, s):
        self.translation.stash_keyframe_set_from_vector(index, frame, t)
        self.rotation.stash_keyframe_values_from_quaternion(index, frame, r)
        self.scale.stash_keyframe_set_from_vector(index, frame, s)
    def set_keyframe_values_from_stash(self):
        self.translation.set_keyframe_values_from_stash()
        self.rotation.set_keyframe_values_from_stash()
        self.scale.set_keyframe_values_from_stash()


def reset_bones_to_rest_pose(armature):
    """Reset all bones in the armature to their rest pose."""
    if armature.type != 'ARMATURE':
        return
    
    # Store current mode
    old_mode = bpy.context.mode
    
    # Switch to pose mode if needed
    if old_mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
    
    # Reset all bones to rest pose
    for bone in armature.pose.bones:
        bone.matrix_basis = Matrix.Identity(4)
    
    # Restore original mode
    if old_mode != 'POSE':
        bpy.ops.object.mode_set(mode=old_mode, toggle=False)


def remove_visibility_drivers(context):
    remove_visibility_drivers_for_armature(context.object)


def remove_visibility_drivers_for_armature(armature_object):
    if armature_object is None:
        return
    mesh_children = [child for child in armature_object.children if child.type == 'MESH']
    for mesh in mesh_children:
        if not mesh.animation_data:
            continue
        drivers = mesh.animation_data.drivers
        for driver in list(drivers):
            if driver.data_path in {'hide_viewport', 'hide_render'}:
                drivers.remove(driver)


# How close basis_constant @ M has to stay to what Blender itself produces before
# the shortcut is trusted. Blender keeps pose matrices in single precision, so a
# small residual is expected on deep bone chains; anything larger means the rig
# does not satisfy the assumption.
_BASIS_SHORTCUT_TOLERANCE = 1e-3


def _basis_constants(animated) -> dict | None:
    """Per-bone constant mapping an anim transform to a pose bone's matrix_basis.

    Blender composes a pose as ``pose = parent_pose @ offs_bone @ basis`` where
    ``offs_bone`` comes from the rest pose. The importer wants
    ``pose = parent_pose @ M``, so ``basis = offs_bone**-1 @ M`` and the parent's
    animated pose cancels out entirely. Probing each bone once with ``M`` set to
    the identity recovers ``offs_bone**-1``, after which every keyframe is a single
    matrix multiply instead of a write and two reads across the Blender API.

    That identity does not hold for bones that disable rotation inheritance or use
    a non-default inherit_scale, so each bone is checked against Blender's own
    answer and None is returned if any bone disagrees, leaving the caller on the
    original path.
    """
    constants = {}
    for bone, _values, _count, _parent_values, _flags, _fcurves, parent in animated:
        if parent is None:
            bone.matrix = Matrix.Identity(4)
        else:
            bone.matrix = parent.matrix
        constants[bone] = bone.matrix_basis.copy()

    # Verify with a transform that exercises rotation, translation and scale
    # rather than the identity the constants were read from.
    probe = Matrix.Translation((0.25, -0.5, 0.75)) @ Matrix.Rotation(0.7, 4, 'Y') @ Matrix.Diagonal((1.3, 0.8, 1.1, 1.0))
    for bone, _values, _count, _parent_values, _flags, _fcurves, parent in animated:
        if parent is None:
            bone.matrix = probe
        else:
            bone.matrix = parent.matrix @ probe
        expected = bone.matrix_basis
        predicted = constants[bone] @ probe
        for row in range(4):
            for column in range(4):
                if abs(expected[row][column] - predicted[row][column]) > _BASIS_SHORTCUT_TOLERANCE:
                    return None

    return constants


def import_model_anim(context: bpy.types.Context, filepath: str,
                      include_transform_track, include_material_track,
                      include_visibility_track, first_blender_frame,
                      armature_object: bpy.types.Object | None = None):
    # Load the anim data first with ssbh_data_py since blender setup relies on data from it
    ssbh_anim_data = ssbh_data_py.anim_data.read_anim(filepath)
    # Blender Action setup
    arma: bpy.types.Object = armature_object or context.object
    if arma is None or arma.type != 'ARMATURE':
        raise ValueError("import_model_anim requires an armature object")
    if context.view_layer.objects.active != arma:
        for scene_obj in context.view_layer.objects:
            scene_obj.select_set(scene_obj == arma)
        context.view_layer.objects.active = arma
    if arma.animation_data is None: # For the bones
        arma.animation_data_create()
    if arma.data.animation_data is None: # For vis and mat tracks
        arma.data.animation_data_create()

    action_name = format_animation_name_on_import(Path(filepath).stem, '.nuanmb', context)
    bone_action = bpy.data.actions.new(action_name)
    sap_action = bpy.data.actions.new(f"{arma.name} {bone_action.name} SAP Data")
    try:
        bone_action["sub_anim_source_path"] = str(Path(filepath).resolve())
        sap_action["sub_anim_source_path"] = str(Path(filepath).resolve())
    except Exception:
        bone_action["sub_anim_source_path"] = filepath
        sap_action["sub_anim_source_path"] = filepath
    ensure_action_slot(bone_action, arma)
    ensure_action_slot(sap_action, arma.data)
    assign_action(arma.animation_data, bone_action)

    # Blender frame range setup
    scene = context.scene
    # Ensure we're using integers for frame calculation
    frame_count = int(ssbh_anim_data.final_frame_index + 1)
    scene.frame_start = first_blender_frame
    scene.frame_end = scene.frame_start + frame_count - 1
    # Convenience dict for group gathering
    name_to_group_dict = {group.group_type.name : group for group in ssbh_anim_data.groups}
    # Transform group import stuff
    transform_group = name_to_group_dict.get('Transform') if include_transform_track else None
    if transform_group:
        bones: list[bpy.types.PoseBone] = arma.pose.bones
        bone_to_node = {bones[n.name]:n for n in transform_group.nodes if n.name in bones}
        reordered: list[bpy.types.PoseBone] = get_hierarchy_order(list(bones)) # Do this to gaurantee we never process a child before its parent
        bone_to_fcurves = {b:BoneFCurves(b.name, bone_action, len(n.tracks[0].values)) for b,n in bone_to_node.items()} # only create fcurves for animated bones

        # Hoist everything that doesn't change per frame out of the inner loop.
        # `node.tracks[0].values` crosses the ssbh_data_py boundary on every access,
        # and this loop runs once per animated bone per frame.
        animated: list[tuple] = []
        for bone in reordered:
            node = bone_to_node.get(bone)
            # Some bones may not be animated, but their children may be.
            if node is None:
                continue
            track = node.tracks[0]
            values = track.values
            flags = track.transform_flags
            parent_values = None
            if track.compensate_scale and bone.parent is not None:
                parent_node = bone_to_node.get(bone.parent)
                if parent_node is not None:
                    parent_values = parent_node.tracks[0].values
            animated.append((
                bone,
                values,
                len(values),
                parent_values,
                flags,
                bone_to_fcurves[bone],
                bone.parent,
            ))

        # Reset all bones to rest pose before importing this animation
        reset_bones_to_rest_pose(arma)

        basis_constants = _basis_constants(animated)

        if basis_constants is None:
            # Rest-pose shortcut rejected for this rig; drive the pose through
            # Blender one bone-frame at a time as before.
            for index, frame in enumerate(range(scene.frame_start, scene.frame_end + 1)): # +1 because range() excludes the final value
                for bone, values, value_count, parent_values, flags, bone_fcurves, parent in animated:
                    # Bones either have a value on the first frame or every frame.
                    if index >= value_count:
                        continue

                    raw_matrix = get_raw_matrix(values[index], parent_values, index)

                    if parent is None:
                        # The root bone
                        bone.matrix = _Y_UP_TO_Z_UP @ raw_matrix @ _X_MAJOR_TO_Y_MAJOR

                        bone_fcurves.stash_keyframe_set_from_matrix(index, frame, bone.matrix_basis)
                    else:
                        # The anim transform is relative to the parent bone's animated world transform.
                        bone.matrix = parent.matrix @ get_blender_transform(raw_matrix).transposed()

                        # Matrix basis is the transform set for the pose bone by the user.
                        # The fcurves work on these user configurable values.
                        # Always goes through apply_transform_flags: the decompose /
                        # recompose it does normalizes the matrix, so skipping it when
                        # no override flag is set would change the imported values.
                        matrix_basis = apply_transform_flags(bone.matrix_basis, flags)

                        bone_fcurves.stash_keyframe_set_from_matrix(index, frame, matrix_basis)
        else:
            # matrix_basis is basis_constant @ (the anim transform), so no bone
            # needs its parent's pose and nothing has to round trip through Blender.
            for index, frame in enumerate(range(scene.frame_start, scene.frame_end + 1)): # +1 because range() excludes the final value
                for bone, values, value_count, parent_values, flags, bone_fcurves, parent in animated:
                    # Bones either have a value on the first frame or every frame.
                    if index >= value_count:
                        continue

                    raw_matrix = get_raw_matrix(values[index], parent_values, index)
                    basis_constant = basis_constants[bone]

                    if parent is None:
                        # The root bone
                        matrix_basis = basis_constant @ _Y_UP_TO_Z_UP @ raw_matrix @ _X_MAJOR_TO_Y_MAJOR
                    else:
                        # The anim transform is relative to the parent bone's animated world transform.
                        matrix_basis = basis_constant @ get_blender_transform(raw_matrix).transposed()

                        # Matrix basis is the transform set for the pose bone by the user.
                        # The fcurves work on these user configurable values.
                        # Always goes through apply_transform_flags: the decompose /
                        # recompose it does normalizes the matrix, so skipping it when
                        # no override flag is set would change the imported values.
                        bone_fcurves.stash_keyframe_set_from_components(
                            index, frame, *apply_transform_flags_components(matrix_basis, flags)
                        )
                        continue

                    bone_fcurves.stash_keyframe_set_from_matrix(index, frame, matrix_basis)

            reset_bones_to_rest_pose(arma)

        for bone, bone_fcurves in bone_to_fcurves.items():
            bone_fcurves.set_keyframe_values_from_stash()

    visibility_group = name_to_group_dict.get('Visibility') if include_visibility_track else None
    material_group = name_to_group_dict.get('Material') if include_material_track else None
    visibility_tracks = (
        merge_imported_visibility_nodes(arma, visibility_group.nodes)
        if visibility_group
        else []
    )

    if visibility_group:
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        entries_by_case = {entry.name.casefold(): entry for entry in sap.vis_track_entries}
        for track_name, _values in visibility_tracks:
            sub_vis_track_entry = entries_by_case.get(track_name.casefold())
            if sub_vis_track_entry is None:
                sub_vis_track_entry = sap.vis_track_entries.add()
                sub_vis_track_entry.name = track_name
                entries_by_case[track_name.casefold()] = sub_vis_track_entry

    # Material group import stuff
    if material_group:
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        # Initial Setup
        for node in material_group.nodes:
            mat_track: SUB_PG_mat_track = sap.mat_tracks.get(node.name)
            if mat_track is None:
                mat_track = sap.mat_tracks.add()
                mat_track.name = node.name
            for track in node.tracks:
                prop: SUB_PG_mat_track_property = mat_track.properties.get(track.name)
                if prop is None:
                    prop = mat_track.properties.add()
                    prop.name = track.name
                prop.name = track.name
                if 'CustomBoolean' in track.name:
                    prop.sub_type = 'BOOL'
                elif 'CustomFloat' in track.name:
                    prop.sub_type = 'FLOAT'
                elif 'CustomVector' in track.name:
                    prop.sub_type = 'VECTOR'
                elif 'PatternIndex' in track.name:
                    prop.sub_type = 'PATTERN'
                elif 'Texture' in track.name:
                    prop.sub_type = 'TEXTURE'
                elif track.name == 'DiffuseUVTransform':
                    prop.sub_type = 'DIFFUSE_UV'
                else:
                    raise TypeError(f'Unsupported track name {track.name}')

    # Bind the SAP action before writing visibility/material keys so Blender 5
    # routes keyframe_insert/create_fcurve to the correct action slot.
    if visibility_group or material_group:
        assign_action(arma.data.animation_data, sap_action)

    if visibility_group:
        sap = arma.data.sub_anim_properties
        entry_indices_by_case = {
            entry.name.casefold(): index
            for index, entry in enumerate(sap.vis_track_entries)
        }
        for track_name, values in visibility_tracks:
            entry_index = entry_indices_by_case.get(track_name.casefold(), -1)
            if entry_index < 0:
                continue
            data_path = f'sub_anim_properties.vis_track_entries[{entry_index}].value'
            vis_entry = sap.vis_track_entries[entry_index]
            last_value = None
            for index, value in enumerate(values):
                bool_value = bool(value)
                if bool_value == last_value:
                    continue
                vis_entry.value = bool_value
                arma.data.keyframe_insert(
                    data_path=data_path,
                    frame=scene.frame_start + index,
                    group='Visibility',
                )
                last_value = bool_value
            fcurve = find_fcurve(sap_action, data_path, id_type='ARMATURE')
            if fcurve is not None:
                for keyframe in fcurve.keyframe_points:
                    keyframe.interpolation = 'CONSTANT'
                style_visibility_fcurve(fcurve)
                fcurve.update()

    if material_group:
        sap = arma.data.sub_anim_properties
        for node in material_group.nodes:
            mat_track: SUB_PG_mat_track = sap.mat_tracks.get(node.name)
            mat_track_index = sap.mat_tracks.find(mat_track.name)
            for track in node.tracks:
                prop = mat_track.properties.get(track.name)
                prop_index = mat_track.properties.find(prop.name)
                if prop.sub_type == 'VECTOR':
                    data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].custom_vector'
                    for index in (0,1,2,3):
                        vector_index_values = [vector[index] for vector in track.values]
                        fcurve = create_fcurve(sap_action, 'ARMATURE', data_path, index=index, action_group=f'Material ({mat_track.name})')
                        fcurve.keyframe_points.add(count=len(vector_index_values))
                        frame_and_value_flattened = []
                        for index, value in enumerate(vector_index_values):
                            frame_and_value_flattened.extend([scene.frame_start + index, value])
                        fcurve.keyframe_points.foreach_set('co', frame_and_value_flattened)
                        fcurve.update()
                        style_material_fcurve(fcurve)
                elif prop.sub_type == 'FLOAT':
                    data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].custom_float'
                    fcurve = create_fcurve(sap_action, 'ARMATURE', data_path, action_group=f'Material ({mat_track.name})')
                    fcurve.keyframe_points.add(count=len(track.values))
                    frame_and_value_flattened = []
                    for index, value in enumerate(track.values):
                        frame_and_value_flattened.extend([scene.frame_start + index, value])
                    fcurve.keyframe_points.foreach_set('co', frame_and_value_flattened)
                    fcurve.update()
                    style_material_fcurve(fcurve)
                elif prop.sub_type == 'BOOL':
                    data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].custom_bool'
                    fcurve = create_fcurve(sap_action, 'ARMATURE', data_path, action_group=f'Material ({mat_track.name})')
                    fcurve.keyframe_points.add(count=len(track.values))
                    frame_and_value_flattened = []
                    for index, value in enumerate(track.values):
                        frame_and_value_flattened.extend([scene.frame_start + index, value])
                    fcurve.keyframe_points.foreach_set('co', frame_and_value_flattened)
                    fcurve.update()
                    style_material_fcurve(fcurve)
                elif prop.sub_type == 'PATTERN':
                    data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].pattern_index'
                    fcurve = create_fcurve(sap_action, 'ARMATURE', data_path, action_group=f'Material ({mat_track.name})')
                    fcurve.keyframe_points.add(count=len(track.values))
                    frame_and_value_flattened = []
                    for index, value in enumerate(track.values):
                        frame_and_value_flattened.extend([scene.frame_start + index, value])
                    fcurve.keyframe_points.foreach_set('co', frame_and_value_flattened)
                    fcurve.update()
                    style_material_fcurve(fcurve)
                elif prop.sub_type == 'TEXTURE':
                    data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].texture_transform'
                    for index in (0,1,2,3,4):
                        if index == 0:
                            vector_index_values = [uv_transform.scale_u for uv_transform in track.values]
                        elif index == 1:
                            vector_index_values = [uv_transform.scale_v for uv_transform in track.values]
                        elif index == 2:
                            vector_index_values = [uv_transform.rotation for uv_transform in track.values]
                        elif index == 3:
                            vector_index_values = [uv_transform.translate_u for uv_transform in track.values]
                        elif index == 4:
                            vector_index_values = [uv_transform.translate_v for uv_transform in track.values]
                        fcurve = create_fcurve(sap_action, 'ARMATURE', data_path, index=index, action_group=f'Material ({mat_track.name})')
                        fcurve.keyframe_points.add(count=len(vector_index_values))
                        frame_and_value_flattened = []
                        for index, value in enumerate(vector_index_values):
                            frame_and_value_flattened.extend([scene.frame_start + index, value])
                        fcurve.keyframe_points.foreach_set('co', frame_and_value_flattened)
                        fcurve.update()
                        style_material_fcurve(fcurve)
                elif prop.sub_type == 'DIFFUSE_UV':
                    # TODO: implement support for diffuse UV transforms
                    pass

    if visibility_group:
        setup_visibility_drivers(arma)
    if material_group:
        setup_material_drivers(arma)

    # Assign actions (and slots on Blender 4.4+ / 5.x).
    assign_action(arma.animation_data, bone_action)
    assign_action(arma.data.animation_data, sap_action)

    from .anim_data import mark_sap_sync_known
    mark_sap_sync_known(arma)
    try:
        from ..extras.eye_rig import ensure_eye_live_preview
        ensure_eye_live_preview(scene)
    except Exception:
        pass
    # Solid view: faces must keep armature deform after vis tracks load
    try:
        from ..extras.smash_viewport import heal_solid_view_deform_after_anim
        heal_solid_view_deform_after_anim()
    except Exception:
        pass


def get_raw_matrix(value, parent_values, index: int) -> Matrix:
    """Build the Smash-space matrix for one transform track value.

    `parent_values` is the parent bone's track values when scale compensation
    applies and None otherwise, so the common no-compensation path skips both
    the lookup and an identity matrix multiply.
    """
    translation = value.translation
    rotation = value.rotation
    scale = value.scale

    tm = Matrix.Translation(translation)
    qr = Quaternion([rotation[3], rotation[0], rotation[1], rotation[2]])
    rm = Matrix.Rotation(qr.angle, 4, qr.axis)
    # Blender doesn't have this built in for some reason.
    scale_matrix = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))

    if parent_values is None:
        return tm @ rm @ scale_matrix

    scale_compensation = get_scale_compensation(parent_values, index)
    return tm @ scale_compensation @ rm @ scale_matrix


def get_scale_compensation(parent_values, frame):
    # Scale compensation "compensates" the effect of the immediate parent's scale.
    try:
        # The parent may not have the same frame count.
        # Handle the case where the parent has only one frame.
        if frame >= len(parent_values):
            parent_scale = parent_values[0].scale
        else:
            parent_scale = parent_values[frame].scale
        return Matrix.Diagonal((1.0 / parent_scale[0], 1.0 / parent_scale[1], 1.0 / parent_scale[2], 1.0))
    except IndexError:
        # TODO: Handle the case when the parent has no animation track?
        return Matrix.Identity(4)


def apply_transform_flags(matrix_basis: Matrix, transform_flags: ssbh_data_py.anim_data.TransformFlags):
    mbtv, mbrq, mbsv = apply_transform_flags_components(matrix_basis, transform_flags)

    mbtm = Matrix.Translation(mbtv)
    mbrm = Matrix.Rotation(mbrq.angle, 4, mbrq.axis)
    mbsm = Matrix.Diagonal((mbsv[0], mbsv[1], mbsv[2], 1.0))

    return mbtm @ mbrm @ mbsm


def apply_transform_flags_components(matrix_basis: Matrix, transform_flags: ssbh_data_py.anim_data.TransformFlags):
    """The translation / rotation / scale the f-curves want, without the round trip.

    The caller only ever needs these three components, so recomposing them into a
    matrix here just to decompose it again on the way into the keyframe stash is
    wasted work on every bone of every frame.
    """
    # Some tracks override parts of the anim transform.
    # This allows bones like swing bones to be animated in other ways.
    mbtv, mbrq, mbsv = matrix_basis.decompose()

    if transform_flags.override_translation:
        mbtv = Vector((0.0, 0.0, 0.0))
    if transform_flags.override_rotation:
        mbrq = Quaternion([1,0,0,0])
    if transform_flags.override_scale:
        mbsv = Vector((1.0, 1.0, 1.0))

    return mbtv, mbrq, mbsv


def keyframe_insert_camera_locrotscale(camera, frame):
    for parameter in ['location', 'rotation_quaternion', 'scale']:
        camera.keyframe_insert(
            data_path=f'{parameter}',
            frame=frame,
            group='Transform',
            #options={'INSERTKEY_NEEDED'}, started causing errors errors in 4.1
        ) 

def uvtransform_to_list(uvtransform) -> list[float]:
    scale_u = uvtransform.scale_u
    scale_v = uvtransform.scale_v
    rotation = uvtransform.rotation
    translate_u = uvtransform.translate_u
    translate_v = uvtransform.translate_v
    return [scale_u, scale_v, rotation, translate_u, translate_v]

def setup_material_drivers(arma: bpy.types.Object):
    from ..model.export_model import trim_name
    sub_anim_data: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
    mesh_children = [child for child in arma.children if child.type == 'MESH']
    materials: set[Material] = {material_slot.material for mesh in mesh_children for material_slot in mesh.material_slots}
    trimmed_material_name_to_material: dict[str, Material] = {trim_name(material.name) : material for material in materials}
    
    for track_index, mat_track in enumerate(sub_anim_data.mat_tracks):
        for property_index, mat_track_property in enumerate(mat_track.properties):
            if mat_track_property.sub_type == 'VECTOR':
                for axis_index, axis in enumerate(['X', 'Y', 'Z', 'W']):
                    material = trimmed_material_name_to_material.get(mat_track.name)
                    if material is None:
                        continue
                    value_node: bpy.types.ShaderNodeValue = material.node_tree.nodes.get(f"{mat_track_property.name}_{axis}")
                    if value_node is None:
                        continue
                    # Remove Existing Driver
                    value_node.outputs[0].driver_remove('default_value')
                    # Setup Driver
                    driver_fcurve: bpy.types.FCurve = value_node.outputs[0].driver_add('default_value')
                    var = driver_fcurve.driver.variables.new()
                    var.name = "var"
                    target = var.targets[0]
                    target.id_type = 'ARMATURE'
                    target.id = arma.data
                    target.data_path = f'sub_anim_properties.mat_tracks[{track_index}].properties[{property_index}].custom_vector[{axis_index}]'
                    driver_fcurve.driver.expression = f'{var.name}'

def do_material_stuff(context, material_group, index, frame):
    arma = context.scene.sub_scene_properties.anim_import_arma
    sap = arma.data.sub_anim_properties
    for node in material_group.nodes:
        mat_track = sap.mat_tracks.get(node.name)
        mat_track_index = sap.mat_tracks.find(mat_track.name)
        for track in node.tracks:
            try:
                track.values[index]
            except IndexError:
                continue
            value = track.values[index]
            prop = mat_track.properties.get(track.name)
            prop_index = mat_track.properties.find(prop.name)
            if prop.sub_type == 'VECTOR':
                prop.custom_vector = value
                arma.data.keyframe_insert(data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].custom_vector', frame=frame, group=f'Material ({mat_track.name})', options={'INSERTKEY_NEEDED'})
            elif prop.sub_type == 'FLOAT':
                prop.custom_float = value
                arma.data.keyframe_insert(data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].custom_float', frame=frame,  group=f'Material ({mat_track.name})', options={'INSERTKEY_NEEDED'})
            elif prop.sub_type == 'BOOL':
                prop.custom_bool = value
                arma.data.keyframe_insert(data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].custom_bool', frame=frame,  group=f'Material ({mat_track.name})', options={'INSERTKEY_NEEDED'})
            elif prop.sub_type == 'PATTERN':
                prop.pattern_index = value
                arma.data.keyframe_insert(data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].pattern_index', frame=frame,  group=f'Material ({mat_track.name})', options={'INSERTKEY_NEEDED'})
            elif prop.sub_type == 'TEXTURE':
                prop.texture_transform = [value.scale_u, value.scale_v, value.rotation, value.translate_u, value.translate_v]
                arma.data.keyframe_insert(data_path=f'sub_anim_properties.mat_tracks[{mat_track_index}].properties[{prop_index}].texture_transform', frame=frame,  group=f'Material ({mat_track.name})', options={'INSERTKEY_NEEDED'})

def setup_sap_material_properties(context, material_group):
    arma = context.scene.sub_scene_properties.anim_import_arma
    sap = arma.data.sub_anim_properties
    # Setup
    for node in material_group.nodes:
        mat_track = sap.mat_tracks.get(node.name, None)
        if mat_track is None:
            mat_track = sap.mat_tracks.add()
            mat_track.name = node.name
        for track in node.tracks:
            prop = mat_track.properties.get(track.name, None)
            if prop is None:
                prop = mat_track.properties.add()
                prop.name = track.name
                if 'CustomBoolean' in track.name:
                    prop.sub_type = 'BOOL'
                elif 'CustomFloat' in track.name:
                    prop.sub_type = 'FLOAT'
                elif 'CustomVector' in track.name:
                    prop.sub_type = 'VECTOR'
                elif 'PatternIndex' in track.name:
                    prop.sub_type = 'PATTERN'
                elif 'Texture' in track.name:
                    prop.sub_type = 'TEXTURE'
                else:
                    raise TypeError(f'Unsupported track name {track.name}')         
            

def setup_visibility_drivers(arma:bpy.types.Object):
    # Setup Vis Drivers
    vis_track_entries = arma.data.sub_anim_properties.vis_track_entries
    entry_indices_by_case = {
        entry.name.casefold(): index
        for index, entry in enumerate(vis_track_entries)
    }
    mesh_children = [child for child in arma.children if child.type == 'MESH']
    for mesh in mesh_children:
        true_mesh_name = visibility_name_from_mesh(mesh.name)
        entries_index = entry_indices_by_case.get(true_mesh_name.casefold())
        if entries_index is not None:
            for property in ['hide_viewport', 'hide_render']:
                # driver_add() returns the existing driver, so without this every
                # animation import piled another duplicate variable onto the same
                # driver. Match how setup_material_drivers rebuilds its drivers.
                mesh.driver_remove(property)
                driver_handle = mesh.driver_add(property)
                var = driver_handle.driver.variables.new()
                var.name = "var"
                target = var.targets[0]
                target.id_type = 'ARMATURE'
                target.id = arma.data
                target.data_path = f'sub_anim_properties.vis_track_entries[{entries_index}].value'
                driver_handle.driver.expression = f'1 - {var.name}'

def do_visibility_stuff(context, visibility_group, index, frame):
    for node in visibility_group.nodes:
        try:
            node.tracks[0].values[index]
        except IndexError: # Not every vis track entry will have values on every frame. Many only have the first frame.
            continue
        value = node.tracks[0].values[index]

        arma = context.scene.sub_scene_properties.anim_import_arma
        entries = arma.data.sub_anim_properties.vis_track_entries
        sub_vis_track_entry = entries.get(node.name, None)
        if sub_vis_track_entry is None:
            sub_vis_track_entry = entries.add()
            sub_vis_track_entry.name = node.name
        sub_vis_track_entry.value = value
        entry_index = entries.find(sub_vis_track_entry.name)
        arma.data.keyframe_insert(data_path=f'sub_anim_properties.vis_track_entries[{entry_index}].value', frame=frame, group='Visibility', options={'INSERTKEY_NEEDED'})

'''
Typical SSBH Camera Layout.
Group: 'Transform'
    Node: 'gya_camera'
        Track: 'Transform'
Group: 'Camera'
    Node: 'gya_cameraShape'
        Track: 'FarClip'
        Track: 'FieldOfView'
        Track: 'NearClip'
'''
# TODO: Stages use additional anim layouts.
def import_camera_anim(operator, context:bpy.types.Context, filepath, first_blender_frame):
    camera: bpy.types.Object = context.object
    ssbh_anim_data = ssbh_data_py.anim_data.read_anim(filepath)
    name_group_dict = {group.group_type.name : group for group in ssbh_anim_data.groups}
    transform_group = name_group_dict.get('Transform')
    camera_group = name_group_dict.get('Camera')

    # Ensure we're using integers for frame calculation
    frame_count = int(ssbh_anim_data.final_frame_index + 1)
    scene = context.scene
    scene.frame_start = first_blender_frame
    scene.frame_end = scene.frame_start + frame_count - 1
    scene.frame_set(scene.frame_start)

    #try:
    #    bpy.ops.object.mode_set(mode='OBJECT', toggle=False) # whatever object is currently selected, exit whatever mode its in
    #except RuntimeError: # There may not have been any active or selected object
    #    pass
    context.view_layer.objects.active = camera

    from pathlib import Path
    imported_name = format_animation_name_on_import(Path(filepath).stem, '.nuanmb', context)
    action_name = camera.name + ' ' + imported_name
    if camera.animation_data is None:
        camera.animation_data_create()
    action = bpy.data.actions.new(action_name)
    ensure_action_slot(action, camera)
    assign_action(camera.animation_data, action)
    camera.matrix_local.identity()
    camera.rotation_mode = 'QUATERNION'

    for index, frame in enumerate(range(scene.frame_start, scene.frame_end+1)):
        scene.frame_set(frame)
        if camera_group is not None:
            update_camera_properties(operator, camera, camera_group, index, frame)
        if transform_group is not None:
            update_camera_transforms(camera, transform_group, index, frame)

def update_camera_properties(operator: bpy.types.Operator, camera:bpy.types.Object, camera_group, index, frame):
    node: ssbh_data_py.anim_data.NodeData = None
    # Imported anim should always have at least one node under the camera group
    if len(camera_group.nodes) == 0:
        message = f'The camera anim has no Nodes in the Camera group! Skipping setting camera properties'
        operator.report({'WARNING'}, message)
        return
    # The standard behavior
    if len(camera_group.nodes) == 1:
        node = camera_group.nodes[0]
    # If the camera group has multiple nodes instead of just 'gya_cameraShape', just use the 'gya_cameraShape' one
    if len(camera_group.nodes) > 1:
        message = f'The camera anim has multiple Camera Property Nodes! Will use the one called "gya_camera_Shape", but will not be able to export the other Node!'
        operator.report({'WARNING'}, message)
        for n in camera_group.nodes:
            if n.name == 'gya_cameraShape':
                node = n
        if node is None:
            node = camera_group.nodes[0]
    for track in node.tracks:
        if track.name == 'FieldOfView':
            if index < len(track.values):
                #scp.field_of_view = track.values[index]
                #cam_keyframe_insert(camera, 'field_of_view', frame)
                camera.data.angle_y = track.values[index]
                camera.data.keyframe_insert(data_path = 'lens', frame=frame)
        elif track.name == 'FarClip':
            if index < len(track.values):
                #scp.far_clip = track.values[index]
                #cam_keyframe_insert(camera, 'far_clip', frame)
                camera.data.clip_end= track.values[index]
                camera.data.keyframe_insert(data_path = 'clip_end', frame=frame)
        elif track.name == 'NearClip':
            if index < len(track.values):
                #scp.near_clip = track.values[index]
                #cam_keyframe_insert(camera, 'near_clip', frame)
                camera.data.clip_start = track.values[index]
                camera.data.keyframe_insert(data_path = 'clip_start', frame=frame)
        else:
            operator.report({'WARNING'}, f'Unsupported track {track.name} in camera group, skipping!')

def update_camera_transforms(camera: bpy.types.Object, transform_group, index, frame):
    value = transform_group.nodes[0].tracks[0].values[index]
    translation =  Matrix.Translation(value.translation)
    quaternion = Quaternion([value.rotation[3], value.rotation[0], value.rotation[1], value.rotation[2]])
    rotation = Matrix.Rotation(quaternion.angle, 4, quaternion.axis)
    # Blender doesn't have this built in for some reason.
    scale = Matrix.Diagonal((value.scale[0], value.scale[1], value.scale[2], 1.0))
    axis_correction = Matrix.Rotation(math.radians(90), 4, 'X')   
    camera.matrix_local = axis_correction @ translation @ rotation @ scale
    keyframe_insert_camera_locrotscale(camera, frame)

class SUB_MT_animation_folders(Menu):
    bl_idname = 'SUB_MT_animation_folders'
    bl_label = 'Animation Folders'

    def draw(self, context):
        ssp = context.scene.sub_scene_properties
        paths = [item.path for item in ssp.animation_import_folders]
        if ssp.animation_import_folder_path and ssp.animation_import_folder_path not in paths:
            paths.append(ssp.animation_import_folder_path)
        for path in paths:
            op = self.layout.operator(
                SUB_OP_switch_animation_folder.bl_idname, text=path,
                icon='CHECKMARK' if path == ssp.animation_import_folder_path else 'FILE_FOLDER')
            op.folder = path


class SUB_OP_switch_animation_folder(Operator):
    bl_idname = 'sub.switch_animation_folder'
    bl_label = 'Switch Animation Folder'
    bl_options = {'UNDO'}

    folder: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        fill_animation_import_list(ssp, self.folder)
        obj = context.object
        if obj is not None and obj.type == 'ARMATURE':
            bind_anim_folder_to_armature(obj, self.folder)
        return {'FINISHED'}


class SUB_OP_select_animation_folder(Operator):
    bl_idname = 'sub.ssbh_animation_folder_selector'
    bl_label = 'Add Animation Folder'
    bl_description = 'Add a folder, including linked folders, containing .nuanmb animations'
    bl_options = {'UNDO'}

    filter_glob: StringProperty(default='*', options={'HIDDEN'})
    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, _event):
        self.directory = context.scene.sub_scene_properties.animation_import_folder_path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        folder = os.path.normpath(bpy.path.abspath(self.directory))
        if not os.path.isdir(folder):
            self.report({'ERROR'}, f'Animation directory not found: {folder}')
            return {'CANCELLED'}
        folders = []
        for root, dirs, files in walk_import_folders(folder):
            if any(name.lower().endswith('.nuanmb') for name in files):
                folders.append(root)
                if root == folder:
                    break
        for path in folders or [folder]:
            remember_animation_folder(ssp, path)
        count = fill_animation_import_list(ssp, folders[0] if folders else folder)
        refresh_raw_animation_import_list(ssp)
        obj = context.object
        if obj is not None and obj.type == 'ARMATURE':
            bind_anim_folder_to_armature(obj, ssp.animation_import_folder_path)
        self.report({'INFO'}, f'Added {len(folders) or 1} folder(s); {count} animations in selected folder')
        return {'FINISHED'}


def create_fcurve(action, id_type: str, data_path: str, index: int = 0, action_group: str = '') -> bpy.types.FCurve:
    return new_fcurve(action, data_path, index=index, action_group=action_group, id_type=id_type)
