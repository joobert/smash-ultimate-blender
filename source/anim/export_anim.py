import bpy
import mathutils
import math
import re
#import numpy as np
import time
import cProfile
import pstats
import os
import stat
import tempfile

from mathutils import Euler, Matrix, Quaternion
from bpy.types import Operator, Panel, Context, UIList
from bpy.props import IntProperty, StringProperty, BoolProperty, CollectionProperty, PointerProperty, BoolVectorProperty

from pathlib import Path

from ...dependencies import ssbh_data_py
from .import_anim import get_hierarchy_order
from .fcurve_compat import get_all_action_fcurves, get_fcurves, get_fcurves_for_assigned_slot
from .raw_anim import (
    ensure_fighter_raw_anim_directory,
    ensure_rawanim_filename,
    ensure_rawanim_filepath,
    export_raw_animation,
    get_anim_folder_path,
    get_suggested_raw_anim_directory,
    normalize_anim_stem,
    resolve_raw_anim_export_path,
    strip_rawanim_suffix,
)
from .visibility_tracks import merge_exported_visibility_values

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .anim_data import SUB_PG_vis_track_entry, SUB_PG_sub_anim_data, SUB_PG_mat_track, SUB_PG_mat_track_property
    CustomVector = list[int]
    CustomFloat = float
    CustomBool = bool
    PatternIndex = int
    TextureTransform = ssbh_data_py.anim_data.UvTransform
    pose_bone: bpy.types.PoseBone # Workaround for typechecking, remove if obsolete
    fcurve: bpy.types.FCurve # Workaround for typechecking, remove if obsolete
    from ..blender_property_extensions import SubSceneProperties

# Bone override list items used to filter which bones receive transform flags
class SUB_PG_bone_override_item(bpy.types.PropertyGroup):
    name: StringProperty(name="Bone Name", description='Bone included in the transform override filter')

class SUB_UL_bone_override_list(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text=item.name)

def ensure_override_bone_list_populated(context: bpy.types.Context):
    ssp = context.scene.sub_scene_properties
    obj = context.active_object
    if not obj or obj.type != 'ARMATURE':
        return
    # Populate if empty or if switching to a different armature than last time
    if len(ssp.anim_override_bone_list) == 0 or getattr(ssp, 'anim_override_armature_name', '') != obj.name:
        # Perform changes outside of draw; this function is called from invoke
        ssp.anim_override_bone_list.clear()
        for bone in obj.data.bones:
            item = ssp.anim_override_bone_list.add()
            item.name = bone.name
        ssp.anim_override_armature_name = obj.name

class SUB_OP_add_selected_bones_to_override(Operator):
    bl_idname = 'sub.add_selected_bones_to_override'
    bl_label = 'Add Selected Bones'
    bl_description = 'Add currently selected pose bones to the override bone list'

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        arma = context.active_object if context.active_object and context.active_object.type == 'ARMATURE' else None
        if arma is None:
            self.report({'WARNING'}, 'Select an Armature first.')
            return {'CANCELLED'}
        selected_names = set()
        # Prefer explicit context collections for reliability
        if context.mode == 'POSE' and getattr(context, 'selected_pose_bones', None):
            selected_names = {b.name for b in context.selected_pose_bones}
        elif context.mode == 'EDIT_ARMATURE' and getattr(context, 'selected_bones', None):
            selected_names = {b.name for b in context.selected_bones}
        else:
            # Fallback: scan pose bones for selected flag
            selected_names = {b.name for b in arma.pose.bones if getattr(b.bone, 'select', False)}
        if not selected_names:
            self.report({'WARNING'}, 'No bones selected. Enter Pose Mode and select bones to add.')
            return {'CANCELLED'}
        existing = {i.name for i in ssp.anim_override_bone_list}
        for name in sorted(selected_names):
            if name not in existing:
                item = ssp.anim_override_bone_list.add()
                item.name = name
        return {'FINISHED'}

class SUB_OP_remove_active_bone_from_override(Operator):
    bl_idname = 'sub.remove_active_bone_from_override'
    bl_label = 'Remove Selected Entry'
    bl_description = 'Remove the active entry from the override bone list'

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        idx = ssp.anim_override_bone_list_index
        if 0 <= idx < len(ssp.anim_override_bone_list):
            ssp.anim_override_bone_list.remove(idx)
            ssp.anim_override_bone_list_index = min(idx, len(ssp.anim_override_bone_list) - 1)
        return {'FINISHED'}

class SUB_OP_clear_bone_override_list(Operator):
    bl_idname = 'sub.clear_bone_override_list'
    bl_label = 'Clear List'
    bl_description = 'Clear the override bone list'

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        ssp.anim_override_bone_list.clear()
        return {'FINISHED'}

class SUB_OP_populate_override_from_armature(Operator):
    bl_idname = 'sub.populate_override_from_armature'
    bl_label = 'Populate From Armature'
    bl_description = 'Fill the override list with all bones from the active armature'
    clear_existing: BoolProperty(name='Clear Existing', default=True,
        description='Replace the current override list instead of adding to it')

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        arma = context.active_object if context.active_object and context.active_object.type == 'ARMATURE' else None
        if arma is None:
            self.report({'WARNING'}, 'Select an Armature first.')
            return {'CANCELLED'}
        if self.clear_existing:
            ssp.anim_override_bone_list.clear()
        existing = {i.name for i in ssp.anim_override_bone_list}
        for bone in arma.data.bones:
            if bone.name not in existing:
                item = ssp.anim_override_bone_list.add()
                item.name = bone.name
        ssp.anim_override_armature_name = arma.name
        return {'FINISHED'}

class SUB_OP_apply_override_preset_thrown(Operator):
    bl_idname = 'sub.apply_override_preset_thrown'
    bl_label = 'Apply Preset: Thrown'
    bl_description = 'Preset: set Include List to bones containing trans/hip/throw/rot'

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        arma = context.active_object if context.active_object and context.active_object.type == 'ARMATURE' else None
        if arma is None:
            # Try to restore the previous armature used for the list
            prev_name = getattr(ssp, 'anim_override_armature_name', '')
            arma = bpy.data.objects.get(prev_name) if prev_name else None
        if arma is None:
            self.report({'WARNING'}, 'Select an Armature first.')
            return {'CANCELLED'}

        # Use include list behavior for this preset
        ssp.anim_override_use_exclude_list = False

        # Keywords to match for the thrown preset
        keywords = ("trans", "hip", "throw", "rot")

        # Rebuild the list with all bones EXCEPT those that match the keywords
        ssp.anim_override_bone_list.clear()
        kept = []
        removed = []
        for bone in arma.data.bones:
            name_lower = bone.name.lower()
            if any(k in name_lower for k in keywords):
                removed.append(bone.name)
            else:
                kept.append(bone.name)
        for name in sorted(kept):
            item = ssp.anim_override_bone_list.add()
            item.name = name

        ssp.anim_override_armature_name = arma.name
        # Ensure translation override is on for next export
        ssp.anim_preset_force_override_translation = True
        self.report({'INFO'}, f"Applied 'Thrown' preset: removed {len(removed)}; kept {len(kept)} in Include List")
        return {'FINISHED'}

# Action item for the batch export list
class SUB_PG_anim_action_item(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", description='Animation filename used for this batch export entry')
    action: PointerProperty(type=bpy.types.Action, description='Blender action to sample for this animation')
    export: BoolProperty(name="Export", default=True, description='Include this animation in the next batch export')

# UI List for displaying available actions
class SUB_UL_action_export_list(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        from .selection import visible_list_indices
        visible = ','.join(map(str, visible_list_indices(self, data.action_export_list)))
        layout.operator_context = 'INVOKE_DEFAULT'
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.prop(item, "export", text="")
            op = layout.operator(
                SUB_OP_toggle_action_export_selection.bl_idname,
                text=item.name,
                icon='ACTION',
                depress=item.export,
            )
            op.index = index
            op.visible_indices = visible
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            op = layout.operator(
                SUB_OP_toggle_action_export_selection.bl_idname,
                text=item.name,
                depress=item.export,
            )
            op.index = index
            op.visible_indices = visible


class SUB_OP_toggle_action_export_selection(Operator):
    bl_idname = 'sub.toggle_action_export_selection'
    bl_label = 'Select Animation'
    bl_description = 'Click to select one; Ctrl-click toggles; Shift-click selects a range'
    bl_options = {'INTERNAL'}

    index: IntProperty(options={'HIDDEN'})
    visible_indices: StringProperty(options={'HIDDEN'})
    toggle: BoolProperty(default=False, options={'HIDDEN'})

    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        items = ssp.action_export_list
        if self.index < 0 or self.index >= len(items):
            return {'CANCELLED'}

        from .selection import select_range
        ssp.action_export_selection_anchor = select_range(
            items, 'export', self.index, ssp.action_export_selection_anchor,
            visible_indices=[int(i) for i in self.visible_indices.split(',') if i] or None,
            shift=event.shift, toggle=event.ctrl or event.oskey or (self.toggle and not event.shift),
        )
        ssp.action_export_list_index = self.index
        return {'FINISHED'}

    def execute(self, context):
        from .selection import select_range
        ssp = context.scene.sub_scene_properties
        if not 0 <= self.index < len(ssp.action_export_list):
            return {'CANCELLED'}
        ssp.action_export_selection_anchor = select_range(
            ssp.action_export_list, 'export', self.index, ssp.action_export_selection_anchor, toggle=self.toggle)
        ssp.action_export_list_index = self.index
        return {'FINISHED'}

class SUB_PT_export_anim(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Animation Exporter'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        if context.mode == "POSE" or context.mode == "OBJECT":
            return True
        return False
    
    def draw(self, context: bpy.types.Context):
        self.layout.use_property_decorate = False
        layout = self.layout
        layout.use_property_split = False
        
        obj: bpy.types.Object = context.active_object
        row = layout.row()
        if obj is None:
            row.label(text="Click on an Armature or Camera.")
        elif obj.select_get() is False:
            row.label(text="Click on an Armature or Camera.")
        elif obj.type == 'ARMATURE' or obj.type == 'CAMERA':
            if obj.animation_data is None:
                row.label(text=f'The selected {obj.type.lower()} has no animation data!', icon='ERROR')
            elif obj.animation_data.action is None:
                row.label(text=f'The selected {obj.type.lower()} has no action!', icon='ERROR')
            else:
                ssp = context.scene.sub_scene_properties
                row.operator(SUB_OP_anim_export.bl_idname, icon='EXPORT', text='Export Current Animation')
                
                # Add collapsible batch export section
                box = layout.box()
                header_row = box.row()
                header_row.prop(ssp, "batch_export_actions_expanded", 
                               icon="TRIA_DOWN" if ssp.batch_export_actions_expanded else "TRIA_RIGHT",
                               icon_only=True, emboss=False)
                header_row.label(text="Batch Export Actions")
                
                # Only show content if expanded
                if ssp.batch_export_actions_expanded:
                    # Refresh actions button
                    row = box.row()
                    row.operator(SUB_OP_refresh_actions.bl_idname, icon='FILE_REFRESH', text="Refresh Action List")
                    
                    # Action list
                    row = box.row()
                    row.template_list("SUB_UL_action_export_list", "", ssp, "action_export_list", 
                                     ssp, "action_export_list_index", rows=5)

                    box.label(text="Drag checkboxes to include or exclude")
                    help_row = box.row()
                    help_row.scale_y = 0.8
                    help_row.label(text="Name: select  Ctrl: toggle  Shift: range", icon='INFO')
                    
                    # Select/deselect all actions
                    row = box.row(align=True)
                    row.operator(SUB_OP_select_all_actions.bl_idname, text="Select All")
                    row.operator(SUB_OP_deselect_all_actions.bl_idname, text="Deselect All")
                    
                    # Batch export button
                    row = box.row()
                    row.scale_y = 1.0
                    selected_count = sum(1 for item in ssp.action_export_list if item.export)
                    row.operator(
                        SUB_OP_batch_export_anim.bl_idname,
                        icon='EXPORT',
                        text=f'Export Selected Animations ({selected_count})',
                    )
        else:
            row.label(text=f'The selected {obj.type.lower()} is not an armature or a camera.')

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_OP_refresh_actions(Operator):
    bl_idname = 'sub.refresh_actions'
    bl_label = 'Refresh Actions'
    bl_description = 'Refresh the list of available actions'
    
    @classmethod
    def poll(cls, context):
        return context.active_object and (context.active_object.type == 'ARMATURE' or context.active_object.type == 'CAMERA')
    
    def execute(self, context):
        # Clear current list
        ssp = context.scene.sub_scene_properties
        ssp.action_export_list.clear()
        
        # Add available actions to the list, filtering out those with "SAP" or "_old" in their names
        for action in bpy.data.actions:
            # Skip actions with "SAP" or "_old" in their names
            if "SAP" in action.name or "_old" in action.name:
                continue
                
            item = ssp.action_export_list.add()
            item.name = action.name
            item.action = action
            item.export = True
            
        return {'FINISHED'}

class SUB_OP_select_all_actions(Operator):
    bl_idname = 'sub.select_all_actions'
    bl_label = 'Select All Actions'
    bl_description = 'Select all actions for export'
    
    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        for action in ssp.action_export_list:
            action.export = True
        return {'FINISHED'}

class SUB_OP_deselect_all_actions(Operator):
    bl_idname = 'sub.deselect_all_actions'
    bl_label = 'Deselect All Actions'
    bl_description = 'Deselect all actions for export'
    
    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        for action in ssp.action_export_list:
            action.export = False
        return {'FINISHED'}

class AnimationExportJob:
    """Cooperatively evaluate Blender data without calling bpy from worker threads."""
    _timer = None
    _job = None
    _running = False

    def execute(self, context):
        if AnimationExportJob._running:
            self.report({'WARNING'}, 'An animation export is already running')
            return {'CANCELLED'}
        obj = context.active_object
        self._object = obj
        self._scene = context.scene
        self._frame = (context.scene.frame_current, context.scene.frame_subframe)
        self._actions = []
        for owner in (obj, obj.data):
            anim = owner.animation_data
            self._actions.append((owner, anim is not None,
                                  anim.action if anim else None,
                                  getattr(anim, 'action_slot', None) if anim else None))
        self._auto_key = context.scene.tool_settings.use_keyframe_insert_auto
        context.scene.tool_settings.use_keyframe_insert_auto = False
        self._job = self.export_steps(context)
        from ..export_progress import ExportProgress
        self._progress = ExportProgress(context)
        self._progress.__enter__()
        AnimationExportJob._running = True
        if bpy.app.background:
            try:
                while True:
                    next(self._job)
            except StopIteration as done:
                return done.value or {'FINISHED'}
            finally:
                self._finish(context)
        try:
            self._timer = context.window_manager.event_timer_add(0.02, window=context.window)
            context.window_manager.modal_handler_add(self)
            context.workspace.status_text_set('Exporting animation... Esc to cancel')
        except Exception:
            self._finish(context)
            raise
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self._finish(context)
            self.report({'INFO'}, 'Export cancelled; completed files are retained')
            return {'CANCELLED'}
        if event.type != 'TIMER' or event.timer != self._timer:
            return {'RUNNING_MODAL'}
        deadline = time.perf_counter() + 0.012
        try:
            while time.perf_counter() < deadline:
                next(self._job)
        except StopIteration as done:
            self._finish(context)
            return done.value or {'FINISHED'}
        except Exception as exc:
            self._finish(context)
            self.report({'ERROR'}, f'Animation export failed: {exc}')
            return {'CANCELLED'}
        for area in context.screen.areas:
            area.tag_redraw()
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        # File-browser cancellation happens before an export job exists.
        if self._job is not None:
            self._finish(context)

    def _finish(self, context):
        try:
            if self._job is not None:
                self._job.close()
                self._job = None
            for owner, had_animation, action, slot in self._actions:
                if had_animation:
                    owner.animation_data.action = action
                    if slot is not None:
                        owner.animation_data.action_slot = slot
                elif owner.animation_data:
                    owner.animation_data_clear()
            self._scene.frame_set(self._frame[0], subframe=self._frame[1])
        finally:
            self._scene.tool_settings.use_keyframe_insert_auto = self._auto_key
            if self._timer is not None:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            if context.workspace:
                context.workspace.status_text_set(None)
            if getattr(self, "_progress", None) is not None:
                self._progress.__exit__(None, None, None)
                self._progress = None
            AnimationExportJob._running = False


class SUB_OP_batch_export_anim(AnimationExportJob, Operator):
    bl_idname = 'sub.batch_export_anim'
    bl_label = 'Batch Export Animations'
    bl_description = 'Export the selected actions as .nuanmb animation files'
    
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
    # Transform flags
    transform_compensate_scale: BoolProperty(
        name='Compensate Scale',
        description='Enable compensate scale on Transform tracks',
        default=False,
    )
    transform_override_translation: BoolProperty(
        name='Override Translation',
        description='Force overriding translation on Transform tracks',
        default=False,
    )
    transform_override_rotation: BoolProperty(
        name='Override Rotation',
        description='Force overriding rotation on Transform tracks',
        default=False,
    )
    transform_override_scale: BoolProperty(
        name='Override Scale',
        description='Force overriding scale on Transform tracks',
        default=False,
    )
    transform_override_compensate_scale: BoolProperty(
        name='Override Compensate Scale',
        description='Force overriding compensate scale on Transform tracks',
        default=False,
    )
    first_blender_frame: IntProperty(
        name='Start Frame',
        description='First Exported Frame',
        default=1,
    )
    use_debug_timer: BoolProperty(
        name='Debug timing stats',
        description='Print advance import timing info to the console',
        default=False,
    )
    use_auto_range: BoolProperty(
        name='Auto-Detect Range',
        description='Automatically detect the frame range for each animation based on its keyframes',
        default=True,
    )
    
    directory: StringProperty(subtype="DIR_PATH", description='Destination folder for exported animations and the starting point for motion-list detection')
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj and 
                (obj.type == 'ARMATURE' or obj.type == 'CAMERA') and 
                obj.animation_data and
                any(item.export for item in context.scene.sub_scene_properties.action_export_list))
    
    def invoke(self, context, event):
        ssp = context.scene.sub_scene_properties
        
        # Set initial values
        self.first_blender_frame = context.scene.frame_start
        
        # Set initial directory, prefer the importer-discovered animation folder
        if getattr(ssp, 'animation_import_folder_path', ''):
            self.directory = ssp.animation_import_folder_path
        elif ssp.last_anim_import_dir:
            self.directory = ssp.last_anim_import_dir
        elif ssp.last_anim_export_dir:
            self.directory = ssp.last_anim_export_dir
        
        # Ensure bone list populated for the active armature
        try:
            ensure_override_bone_list_populated(context)
        except Exception:
            pass
        # Apply preset-driven overrides
        if getattr(ssp, 'anim_preset_force_override_translation', False):
            self.transform_override_translation = True

        # Open file browser
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    def draw(self, context):
        layout = self.layout
        from .motion_list_ui import draw_settings
        draw_settings(layout, context)
        layout.prop(self, "include_transform_track")
        layout.prop(self, "include_material_track")
        layout.prop(self, "include_visibility_track")
        # Bone filter controls
        ssp = context.scene.sub_scene_properties
        # Reflect preset-driven flags immediately in the UI
        if getattr(ssp, 'anim_preset_force_override_translation', False) and not self.transform_override_translation:
            self.transform_override_translation = True
        box = layout.box()
        row = box.row()
        row.label(text="Transform Override Bone Filter")
        if hasattr(ssp, "anim_override_use_exclude_list"):
            use_exclude = getattr(ssp, "anim_override_use_exclude_list", True)
            row.prop(ssp, "anim_override_use_exclude_list", text=("Exclude List" if use_exclude else "Include List"))
        else:
            row.label(text="(reload addon to enable toggle)")
        row = box.row()
        if hasattr(ssp, "anim_override_bone_list") and hasattr(ssp, "anim_override_bone_list_index"):
            row.template_list("SUB_UL_bone_override_list", "", ssp, "anim_override_bone_list", ssp, "anim_override_bone_list_index", rows=6)
            col = row.column(align=True)
            col.operator(SUB_OP_add_selected_bones_to_override.bl_idname, text="Add Select")
            col.operator(SUB_OP_populate_override_from_armature.bl_idname, text="Populate")
            col.operator(SUB_OP_remove_active_bone_from_override.bl_idname, text="Remove")
            col.operator(SUB_OP_clear_bone_override_list.bl_idname, text="Clear")
            # Presets
            row = box.row(align=True)
            row.label(text="Presets")
            row.operator(SUB_OP_apply_override_preset_thrown.bl_idname, text="Thrown")
        else:
            row.label(text="(reload addon to enable list)")
        # Transform Flags
        col = layout.column(align=True)
        col.enabled = self.include_transform_track
        col.label(text="Transform Flags")
        col.prop(self, "transform_compensate_scale")
        col.prop(self, "transform_override_translation")
        col.prop(self, "transform_override_rotation")
        col.prop(self, "transform_override_scale")
        col.prop(self, "transform_override_compensate_scale")
        layout.prop(self, "first_blender_frame")
        layout.prop(self, "use_auto_range", text="Auto-Detect Frame Range")
        layout.prop(self, "use_debug_timer")
    
    def find_last_keyframe(self, action):
        last_frame = 1
        fcurves = get_fcurves(action)
        if fcurves:
            for fcurve in fcurves:
                for keyframe in fcurve.keyframe_points:
                    if keyframe.co[0] > last_frame:
                        last_frame = int(keyframe.co[0])
        return max(last_frame, 1)  # Ensure we always have at least 1 frame
    
    def export_steps(self, context):
        from ..doctor import preflight
        if not preflight(context, self, 'ANIM'):
            return {'CANCELLED'}

        ssp = context.scene.sub_scene_properties
        obj = context.active_object

        # Store current action
        current_action = None
        if obj.animation_data:
            current_action = obj.animation_data.action
        
        # Store current frame
        current_frame = context.scene.frame_current
        
        # Save directory for future use
        ssp.last_anim_export_dir = self.directory
        
        # Count selected actions for progress reporting
        selected_actions = [item for item in ssp.action_export_list if item.export]
        total_count = len(selected_actions)
        
        if total_count == 0:
            self.report({'WARNING'}, "No actions selected for export")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"Starting batch export of {total_count} animations...")
        start_time = time.perf_counter()
        
        export_count = 0
        for i, item in enumerate(selected_actions):
            if not item.export:
                continue
                
            action_name = item.name
            # Set the action on the object
            obj.animation_data.action = item.action

            # --- SAP Data Sync: Set the SAP action to match this animation ---
            expected_sap_action_name = f"{obj.name} {action_name} SAP Data"
            expected_sap_action = bpy.data.actions.get(expected_sap_action_name)
            if expected_sap_action or obj.data.animation_data:
                # Clear a previous clip's SAP action when this clip has none.
                # Ensure animation_data exists on the armature data
                if obj.data.animation_data is None:
                    obj.data.animation_data_create()
                obj.data.animation_data.action = expected_sap_action
            # --------------------------------------------------------------
            
            # Create export path with sanitized filename
            safe_name = ensure_nuanmb_filename(sanitize_filename(action_name))
            filepath = os.path.join(self.directory, safe_name)
            
            # Determine last keyframe for this action if auto-range is enabled
            if self.use_auto_range:
                last_blender_frame = self.find_last_keyframe(item.action)
                self.report({'INFO'}, f"Auto-detected frame range for {action_name}: {self.first_blender_frame}-{last_blender_frame}")
            else:
                # Use scene frame range if auto-range is disabled
                last_blender_frame = context.scene.frame_end
            
            try:
                if obj.type == 'ARMATURE':
                    yield from export_model_anim_fast_steps(
                        context, self, obj, filepath,
                        self.include_transform_track, self.include_material_track,
                        self.include_visibility_track, self.first_blender_frame,
                        last_blender_frame,
                        self.transform_compensate_scale,
                        self.transform_override_translation,
                        self.transform_override_rotation,
                        self.transform_override_scale,
                        self.transform_override_compensate_scale,
                        [i.name for i in ssp.anim_override_bone_list],
                        ssp.anim_override_use_exclude_list)
                else:
                    # Camera export
                    yield from export_camera_anim_steps(context, self, obj, filepath,
                        self.first_blender_frame, last_blender_frame,
                        self.transform_compensate_scale,
                        self.transform_override_translation,
                        self.transform_override_rotation,
                        self.transform_override_scale,
                        self.transform_override_compensate_scale)
                
                export_count += 1
                # Report progress
                progress = (i + 1) / total_count * 100
                self.report({'INFO'}, f"Exported {i+1}/{total_count} ({progress:.1f}%): {action_name}")
                
            except Exception as e:
                self.report({'ERROR'}, f"Failed to export {safe_name}: {str(e)}")
        
        # Clear transient preset flags
        ssp.anim_preset_force_override_translation = False

        # Restore original action and frame
        if current_action:
            obj.animation_data.action = current_action
        context.scene.frame_set(current_frame)
        
        end_time = time.perf_counter()
        self.report({'INFO'}, f"Successfully exported {export_count}/{total_count} animations in {end_time - start_time:.2f} seconds")
        
        return {'FINISHED'} if export_count == total_count else {'CANCELLED'}

# Add this function to sanitize filenames - place it before the SUB_OP_anim_export class
def sanitize_filename(filename):
    """
    Replace invalid Windows filename characters with underscores.
    Invalid characters include slash, backslash, colon, asterisk, question
    mark, quote, angle brackets, and pipe.
    """
    # Characters not allowed in Windows filenames
    invalid_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    
    # Replace each invalid character with an underscore
    for char in invalid_chars:
        filename = filename.replace(char, '_')
        
    return filename


def strip_nuanmb_suffix(name):
    """Remove .nuanmb, .rawanim, and Blender duplicate suffixes from a name."""
    return normalize_anim_stem(name)


def ensure_nuanmb_filename(filename):
    """Return a filename with exactly one .nuanmb suffix."""
    return strip_nuanmb_suffix(filename) + '.nuanmb'


def ensure_nuanmb_filepath(filepath):
    directory, filename = os.path.split(filepath)
    filename = ensure_nuanmb_filename(filename)
    if directory:
        return os.path.join(directory, filename)
    return filename


from ..export_progress import export_progress


@export_progress
def export_raw_animation_for_object(
    context: Context,
    operator: Operator,
    obj: bpy.types.Object,
    filepath: str,
    frame_start: int,
    frame_end: int,
) -> bool:
    if obj.type != 'ARMATURE':
        operator.report({'WARNING'}, 'Raw animation export is only supported for armatures.')
        return False
    if obj.animation_data is None or obj.animation_data.action is None:
        operator.report({'ERROR'}, 'No action to export as raw animation.')
        return False

    raw_filepath = ensure_rawanim_filepath(filepath)
    action = obj.animation_data.action
    assigned = get_fcurves_for_assigned_slot(obj)
    if not export_raw_animation(
        obj,
        action,
        raw_filepath,
        frame_start,
        frame_end,
        operator,
    ):
        return False

    curve_hint = len(assigned) if assigned else "all-slots"
    operator.report(
        {'INFO'},
        f"Successfully exported raw animation to {os.path.basename(raw_filepath)} "
        f"(source curves: {curve_hint})",
    )
    return True


class SUB_OP_raw_anim_export(Operator):
    bl_idname = 'sub.raw_anim_export'
    bl_label = 'Export Raw Animation'
    bl_description = 'Export pose bone keyframes without baking every frame (.rawanim)'

    filter_glob: StringProperty(
        default='*.rawanim',
        options={'HIDDEN'}
    )
    first_blender_frame: IntProperty(
        name='Start Frame',
        description='First exported frame',
        default=1,
    )
    last_blender_frame: IntProperty(
        name='End Frame',
        description='Last exported frame',
        default=1,
    )
    filepath: StringProperty(subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == 'ARMATURE'
            and obj.animation_data is not None
            and obj.animation_data.action is not None
        )

    def invoke(self, context: Context, _event):
        action_name = strip_rawanim_suffix(context.active_object.animation_data.action.name)
        safe_name = sanitize_filename(ensure_rawanim_filename(action_name))
        self.first_blender_frame = context.scene.frame_start
        self.last_blender_frame = context.scene.frame_end

        ssp = context.scene.sub_scene_properties
        raw_dir = get_suggested_raw_anim_directory(ssp)
        if raw_dir:
            ensure_fighter_raw_anim_directory(raw_dir)
            self.filepath = os.path.join(raw_dir, safe_name)
        else:
            base_dir = get_anim_folder_path(ssp)
            if base_dir:
                self.filepath = os.path.join(base_dir, safe_name)
            else:
                self.filepath = safe_name

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "first_blender_frame")
        layout.prop(self, "last_blender_frame")

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        raw_filepath = ensure_rawanim_filepath(self.filepath)
        ssp.last_anim_export_dir = os.path.dirname(raw_filepath)

        obj = context.active_object
        if not export_raw_animation_for_object(
            context,
            self,
            obj,
            raw_filepath,
            self.first_blender_frame,
            self.last_blender_frame,
        ):
            return {'CANCELLED'}
        return {'FINISHED'}


class SUB_OP_anim_export(AnimationExportJob, Operator):
    bl_idname = 'sub.anim_export'
    bl_label = 'Export Anim'
    bl_description = 'Export the active action as a .nuanmb animation file'

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
    # Transform flags
    transform_compensate_scale: BoolProperty(
        name='Compensate Scale',
        description='Enable compensate scale on Transform tracks',
        default=False,
    )
    transform_override_translation: BoolProperty(
        name='Override Translation',
        description='Force overriding translation on Transform tracks',
        default=False,
    )
    transform_override_rotation: BoolProperty(
        name='Override Rotation',
        description='Force overriding rotation on Transform tracks',
        default=False,
    )
    transform_override_scale: BoolProperty(
        name='Override Scale',
        description='Force overriding scale on Transform tracks',
        default=False,
    )
    transform_override_compensate_scale: BoolProperty(
        name='Override Compensate Scale',
        description='Force overriding compensate scale on Transform tracks',
        default=False,
    )
    first_blender_frame: IntProperty(
        name='Start Frame',
        description='First Exported Frame',
        default=1,
    )
    last_blender_frame: IntProperty(
        name='End Frame',
        description='Last Exported Frame',
        default=1,
    )
    use_debug_timer: BoolProperty(
        name='Debug timing stats',
        description='Print advance import timing info to the console',
        default=False,
    )

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        obj: bpy.types.Object = context.active_object
        if obj is None:
            return False
        if obj.type != 'ARMATURE' and obj.type != 'CAMERA':
            return False
        if obj.animation_data is None:
            return False
        if obj.animation_data.action is None:
            return False
        return True

    def invoke(self, context: Context, _event):
        # Imported actions are already named *.nuanmb; do not add the suffix twice.
        action_name = ensure_nuanmb_filename(context.active_object.animation_data.action.name)
        safe_name = sanitize_filename(action_name)
        
        # Set filepath
        self.filepath = safe_name
        
        # Set frame ranges
        self.first_blender_frame = context.scene.frame_start
        self.last_blender_frame = context.scene.frame_end

        # Set initial directory from importer when available
        ssp = context.scene.sub_scene_properties
        base_dir = None
        if getattr(ssp, 'animation_import_folder_path', ''):
            base_dir = ssp.animation_import_folder_path
        elif ssp.last_anim_import_dir:
            base_dir = ssp.last_anim_import_dir
        elif ssp.last_anim_export_dir:
            base_dir = ssp.last_anim_export_dir
        if base_dir:
            self.filepath = os.path.join(base_dir, safe_name)
        
        # Ensure bone list populated for the active armature
        try:
            ensure_override_bone_list_populated(context)
        except Exception:
            pass
        # Apply preset-driven overrides
        if getattr(ssp, 'anim_preset_force_override_translation', False):
            self.transform_override_translation = True

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        from .motion_list_ui import draw_settings
        draw_settings(layout, context)
        layout.prop(self, "include_transform_track")
        layout.prop(self, "include_material_track")
        layout.prop(self, "include_visibility_track")
        # Bone filter controls
        ssp = context.scene.sub_scene_properties
        # Reflect preset-driven flags immediately in the UI
        if getattr(ssp, 'anim_preset_force_override_translation', False) and not self.transform_override_translation:
            self.transform_override_translation = True
        box = layout.box()
        row = box.row()
        row.label(text="Transform Override Bone Filter")
        if hasattr(ssp, "anim_override_use_exclude_list"):
            use_exclude = getattr(ssp, "anim_override_use_exclude_list", True)
            row.prop(ssp, "anim_override_use_exclude_list", text=("Exclude List" if use_exclude else "Include List"))
        else:
            row.label(text="(reload addon to enable toggle)")
        row = box.row()
        if hasattr(ssp, "anim_override_bone_list") and hasattr(ssp, "anim_override_bone_list_index"):
            row.template_list("SUB_UL_bone_override_list", "", ssp, "anim_override_bone_list", ssp, "anim_override_bone_list_index", rows=6)
            col = row.column(align=True)
            col.operator(SUB_OP_add_selected_bones_to_override.bl_idname, text="Add Select")
            col.operator(SUB_OP_populate_override_from_armature.bl_idname, text="Populate")
            col.operator(SUB_OP_remove_active_bone_from_override.bl_idname, text="Remove")
            col.operator(SUB_OP_clear_bone_override_list.bl_idname, text="Clear")
            # Presets
            row = box.row(align=True)
            row.label(text="Presets")
            row.operator(SUB_OP_apply_override_preset_thrown.bl_idname, text="Thrown")
        else:
            row.label(text="(reload addon to enable list)")
        # Transform Flags
        col = layout.column(align=True)
        col.enabled = self.include_transform_track
        col.label(text="Transform Flags")
        col.prop(self, "transform_compensate_scale")
        col.prop(self, "transform_override_translation")
        col.prop(self, "transform_override_rotation")
        col.prop(self, "transform_override_scale")
        col.prop(self, "transform_override_compensate_scale")
        layout.prop(self, "first_blender_frame")
        layout.prop(self, "last_blender_frame")

    def export_steps(self, context):
        from ..doctor import preflight
        if not preflight(context, self, 'ANIM'):
            return {'CANCELLED'}

        # Save directory for future use
        ssp = context.scene.sub_scene_properties
        ssp.last_anim_export_dir = os.path.dirname(self.filepath)
        # Clear transient preset flags
        ssp.anim_preset_force_override_translation = False
        
        # Ensure filepath has exactly one .nuanmb extension
        filepath = ensure_nuanmb_filepath(self.filepath)

        # Get the filename part without path
        filename = os.path.basename(filepath)
        
        # Sanitize the filename (in case it was modified in the file browser)
        safe_filename = sanitize_filename(filename)
        if safe_filename != filename:
            # If the filename changed, update the path
            filepath = os.path.join(os.path.dirname(filepath), safe_filename)
            
        obj: bpy.types.Object = context.active_object
        
        if obj.type == 'ARMATURE':
            yield from export_model_anim_fast_steps(
            context, self, obj, filepath,
                self.include_transform_track, self.include_material_track,
                self.include_visibility_track, self.first_blender_frame,
                self.last_blender_frame,
                self.transform_compensate_scale,
                self.transform_override_translation,
                self.transform_override_rotation,
                self.transform_override_scale,
                self.transform_override_compensate_scale,
                [i.name for i in ssp.anim_override_bone_list],
                ssp.anim_override_use_exclude_list)
        else:
        # Camera export
            yield from export_camera_anim_steps(context, self, obj, filepath,
                self.first_blender_frame, self.last_blender_frame,
                self.transform_compensate_scale,
                self.transform_override_translation,
                self.transform_override_rotation,
                self.transform_override_scale,
                self.transform_override_compensate_scale)  

        self.report({'INFO'}, f"Successfully exported animation to {os.path.basename(filepath)}")

        if ssp.anim_include_raw_animation and obj.type == 'ARMATURE':
            raw_filepath = resolve_raw_anim_export_path(filepath, ssp)
            if not export_raw_animation_for_object(
                context,
                self,
                obj,
                raw_filepath,
                self.first_blender_frame,
                self.last_blender_frame,
            ):
                return {'CANCELLED'}

        return {'FINISHED'}
           
class Location():
    def __init__(self, x, y, z):
        self.x: float = x
        self.y: float = y
        self.z: float = z
    def __repr__(self) -> str:
        return f'[{self.x=}, {self.y=}, {self.z=}]'

class Rotation():
    def __init__(self, w, x, y, z):
        self.w: float = w
        self.x: float = x
        self.y: float = y
        self.z: float = z
    def __repr__(self) -> str:
        return f'[{self.w=}, {self.x=}, {self.y=}, {self.z=}]'

class Scale():
    def __init__(self, x, y, z):
        self.x: float = x
        self.y: float = y
        self.z: float = z
    def __repr__(self) -> str:
        return f'[{self.x=}, {self.y=}, {self.z=}]'

class EulerXYZ():
    def __init__(self, x, y, z):
        self.x: float = x
        self.y: float = y
        self.z: float = z
    def __repr__(self) -> str:
        return f'[{self.x=}, {self.y=}, {self.z=}]'


def _ssbh_quaternion(xyzw) -> Quaternion:
    return Quaternion((xyzw[3], xyzw[0], xyzw[1], xyzw[2]))


def _rotation_basis_matrix(bone, index, quat_values, euler_values, bones_with_euler, bones_with_quat):
    mode = getattr(bone, 'rotation_mode', 'QUATERNION') or 'QUATERNION'
    use_euler = (
        bone.name in bones_with_euler
        and mode not in {'QUATERNION', 'AXIS_ANGLE'}
    )
    if not use_euler and bone.name in bones_with_euler and bone.name not in bones_with_quat:
        use_euler = True
    if use_euler:
        euler = euler_values[bone.name][index]
        rot = Euler((euler.x, euler.y, euler.z), mode)
        return rot.to_matrix().to_4x4()
    rot_basis_vec = quat_values[bone.name][index]
    rot_basis_quat = Quaternion((rot_basis_vec.w, rot_basis_vec.x, rot_basis_vec.y, rot_basis_vec.z))
    if rot_basis_quat.magnitude > 1e-8:
        rot_basis_quat.normalize()
    else:
        rot_basis_quat = Quaternion((1.0, 0.0, 0.0, 0.0))
    return rot_basis_quat.to_matrix().to_4x4()

_Y_UP_TO_Z_UP = Matrix.Rotation(math.radians(90.0), 4, 'X')
_X_MAJOR_TO_Y_MAJOR = Matrix.Rotation(math.radians(-90.0), 4, 'Z')


def get_smash_transform(m) -> Matrix:
    # This is the inverse of the get_blender_transform permutation matrix.
    # https://en.wikipedia.org/wiki/Matrix_similarity
    p = Matrix([
        [0, 1, 0, 0],
        [-1, 0, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1]
    ])
    # Perform the transformation m in Blender's basis and convert back to Ultimate.
    try:
        return p @ m @ p.inverted()
    except ValueError:
        # If inversion fails, the matrix is singular - this shouldn't happen for p but handle it gracefully
        raise ValueError("Cannot compute Smash transform: input matrix is singular")


def get_smash_root_matrix(blender_pose_matrix) -> Matrix:
    """Inverse of anim import: bone.matrix = RotX(90) @ smash @ RotZ(-90)."""
    return _Y_UP_TO_Z_UP.inverted() @ blender_pose_matrix @ _X_MAJOR_TO_Y_MAJOR.inverted()

def transform_group_fix_floating_point_inaccuracies(trans_group: ssbh_data_py.anim_data.GroupData):
    from math import isclose
    for node in trans_group.nodes:
        track = node.tracks[0]
        if len(track.values) <= 1:
            continue
        first_transform = track.values[0]
        for index, val in enumerate(first_transform.scale):
            if isclose(val, 1, abs_tol=.00001):
                first_transform.scale[index] = 1
        for current_transform_index, current_transform in enumerate(track.values[1:], start=1):
            # To avoid quaternion math issues, have to check if every value is close and replace the entire quaternion,
            #  not just the 'x' or 'y' or 'z' or 'w'
            all_rot_vals_close = True
            for i in (0,1,2,3):
                if not isclose(current_transform.rotation[i], first_transform.rotation[i], abs_tol=.00001):
                    all_rot_vals_close = False
            if all_rot_vals_close is True:
                track.values[current_transform_index].rotation = first_transform.rotation
            
            for i in (0,1,2):
                if isclose(current_transform.scale[i], first_transform.scale[i], abs_tol=.00001):
                    track.values[current_transform_index].scale[i] = first_transform.scale[i]
            
            for i in (0,1,2):
                if isclose(current_transform.translation[i], first_transform.translation[i], abs_tol=.00001):
                    track.values[current_transform_index].translation[i] = first_transform.translation[i]

def save_ssbh_anim_data(ssbh_anim_data, filepath, operator=None):
    """Validate optional motion edits before export, then commit after saving."""
    from .motion_list_ui import prepare, commit
    settings = getattr(bpy.context.scene, 'sub_motion_list', None)
    prepared = prepare(settings, filepath, ssbh_anim_data.final_frame_index) if settings else None
    saved_path = _save_ssbh_anim_data(ssbh_anim_data, filepath, operator)
    if os.path.abspath(saved_path) == os.path.abspath(filepath):
        commit(prepared, operator)
    elif prepared is not None and operator is not None:
        operator.report({'WARNING'}, 'Motion list unchanged because animation was saved under an alternate filename')
    return saved_path


def _save_ssbh_anim_data(ssbh_anim_data, filepath, operator=None):
    """Write a .nuanmb, recovering from Windows file locks when possible."""
    filepath = os.path.abspath(os.fspath(filepath))
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)

    def _is_access_denied(exc):
        text = str(exc).lower()
        if "access is denied" in text or "os error 5" in text:
            return True
        return int(getattr(exc, "winerror", 0) or getattr(exc, "errno", 0) or 0) == 5

    try:
        if os.path.isfile(filepath):
            try:
                os.chmod(filepath, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
        ssbh_anim_data.save(filepath)
        return filepath
    except Exception as exc:
        if not _is_access_denied(exc):
            raise

    fd, tmp_path = tempfile.mkstemp(prefix="smash_anim_", suffix=".nuanmb", dir=directory or None)
    os.close(fd)
    try:
        ssbh_anim_data.save(tmp_path)
        try:
            os.replace(tmp_path, filepath)
            return filepath
        except Exception:
            pass
        base, ext = os.path.splitext(filepath)
        alt = filepath
        for index in range(1, 50):
            alt = f"{base}_{index}{ext}"
            if not os.path.exists(alt):
                break
        try:
            os.replace(tmp_path, alt)
            if operator is not None:
                operator.report(
                    {'WARNING'},
                    f"Could not overwrite {filepath} because Windows locked it. Saved as {os.path.basename(alt)} instead. Close SSBH Editor or the game and try again if you need the original name.",
                )
            return alt
        except Exception:
            pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    message = (
        f"Access denied writing {filepath}. Close any program using that file "
        "(SSBH Editor, Smash, Explorer preview) and export again."
    )
    if operator is not None:
        operator.report({'ERROR'}, message)
    raise PermissionError(message)


def uv_transform_equality(a: ssbh_data_py.anim_data.UvTransform, b: ssbh_data_py.anim_data.UvTransform) -> bool:
    if a.rotation != b.rotation:
        return False
    if a.scale_u != b.scale_u:
        return False
    if a.scale_v != b.scale_v:
        return False
    if a.translate_u != b.translate_u:
        return False
    if a.translate_v != b.translate_v:
        return False
    return True

def does_armature_data_have_fcurves(arma: bpy.types.Object) -> bool:
    if arma.data.animation_data is None:
        return False
    if arma.data.animation_data.action is None:
        return False
    
    return len(get_all_action_fcurves(arma.data.animation_data.action)) > 0

def export_model_anim_fast(*args, **kwargs):
    for _ in export_model_anim_fast_steps(*args, **kwargs):
        pass


def export_model_anim_fast_steps(context, operator: bpy.types.Operator, arma: bpy.types.Object, filepath, include_transform_track, include_material_track, include_visibility_track, first_blender_frame, last_blender_frame, transform_compensate_scale: bool = False, transform_override_translation: bool = False, transform_override_rotation: bool = False, transform_override_scale: bool = False, transform_override_compensate_scale: bool = False, override_bone_names: list[str] | None = None, use_exclude_list: bool = True):
    if last_blender_frame < first_blender_frame:
        raise ValueError('End frame must be greater than or equal to start frame')
    # SSBH Anim Setup
    ssbh_anim_data =  ssbh_data_py.anim_data.AnimData()
    final_frame_index = last_blender_frame - first_blender_frame
    ssbh_anim_data.final_frame_index = final_frame_index

    # Gather Groups
    if include_transform_track:
        # First gather the blender animation data, then create the ssbh data
        # Create value dicts ahead of time
        bone_name_to_location_values: dict[str, list[Location]] = {}
        bone_name_to_rotation_values: dict[str, list[Rotation]] = {}
        bone_name_to_euler_values: dict[str, list[EulerXYZ]] = {}
        bone_name_to_scale_values: dict[str, list[Scale]] = {}
        bones_with_quat: set[str] = set()
        bones_with_euler: set[str] = set()
        bone_to_rel_matrix_local = {}
        reordered_pose_bones = [
            bone for bone in get_hierarchy_order(list(arma.pose.bones))
            if not bone.name.startswith('BL_')
        ]

        # Fill value dicts with default values. Not every bone will be animated, so for these the default values of a matrix basis will be needed
        for pose_bone in reordered_pose_bones:
            yield
            bone_name_to_location_values[pose_bone.name] = [Location(0.0, 0.0, 0.0) for _ in range(first_blender_frame, last_blender_frame + 1)]
            bone_name_to_rotation_values[pose_bone.name] = [Rotation(1.0, 0.0, 0.0, 0.0) for _ in range(first_blender_frame, last_blender_frame + 1)]
            bone_name_to_euler_values[pose_bone.name] = [EulerXYZ(0.0, 0.0, 0.0) for _ in range(first_blender_frame, last_blender_frame + 1)]
            bone_name_to_scale_values[pose_bone.name] = [Scale(1.0, 1.0, 1.0) for _ in range(first_blender_frame, last_blender_frame + 1)]
            if pose_bone.parent: # non-root bones
                bone_to_rel_matrix_local[pose_bone] = pose_bone.parent.bone.matrix_local.inverted() @ pose_bone.bone.matrix_local
            else: # root bones
                bone_to_rel_matrix_local[pose_bone] = pose_bone.bone.matrix_local

        # Go through the pose bones' fcurves and store all the values at each frame.
        animated_pose_bones: set[bpy.types.PoseBone] = set()
        
        def _ensure_export_bone(bone_name):
            if bone_name in bone_name_to_location_values:
                return
            nframes = last_blender_frame - first_blender_frame + 1
            bone_name_to_location_values[bone_name] = [Location(0.0, 0.0, 0.0) for _ in range(nframes)]
            bone_name_to_rotation_values[bone_name] = [Rotation(1.0, 0.0, 0.0, 0.0) for _ in range(nframes)]
            bone_name_to_euler_values[bone_name] = [EulerXYZ(0.0, 0.0, 0.0) for _ in range(nframes)]
            bone_name_to_scale_values[bone_name] = [Scale(1.0, 1.0, 1.0) for _ in range(nframes)]
            operator.report({'INFO'}, f"Added missing bone '{bone_name}' to animation export data")

        object_level_transform_reported = False
        for fcurve in get_all_action_fcurves(arma.animation_data.action):
            yield
            regex = r'pose\.bones\[\"(.*)\"\]\.(.*)'
            matches = re.match(regex, fcurve.data_path)
            if matches is None: # A fcurve in the action that isn't a bone transform, such as the user keyframing the Armature Object itself.
                object_level_transfrom_data_path_regex = r'^location$|^scale$|^rotation_quaternion$|^rotation_euler$'
                if re.match(object_level_transfrom_data_path_regex, fcurve.data_path):
                    if object_level_transform_reported == False:
                        operator.report(type={'WARNING'}, message=f"The Armature's \"Object Mode\" location/rotation/scale was keyframed, this will not be exported! Make sure to enter Pose Mode, and keyframe a bone's location/rotation/scale instead!")
                        object_level_transform_reported = True
                    continue
                operator.report(type={'WARNING'}, message=f"The fcurve with data path {fcurve.data_path} will not be exported, since it didn't match the pattern of a bone fcurve.")
                continue
            if len(matches.groups()) != 2: # TODO: Is this possible?
                operator.report(type={'WARNING'}, message=f"The fcurve with data path {fcurve.data_path} will not be exported, its format only partially matched the expected pattern of a bone fcurve.")
                continue
            bone_name = matches.groups()[0]
            if bone_name.startswith('BL_'):
                continue
            transform_subtype = matches.groups()[1]
            if transform_subtype == 'location':
                for index, frame in enumerate(range(first_blender_frame, last_blender_frame+1)):
                    context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
                    yield
                    _ensure_export_bone(bone_name)
                    if fcurve.array_index == 0:
                        bone_name_to_location_values[bone_name][index].x = fcurve.evaluate(frame)
                    elif fcurve.array_index == 1:
                        bone_name_to_location_values[bone_name][index].y = fcurve.evaluate(frame)
                    elif fcurve.array_index == 2:
                        bone_name_to_location_values[bone_name][index].z = fcurve.evaluate(frame)
            elif transform_subtype == 'rotation_quaternion':
                bones_with_quat.add(bone_name)
                for index, frame in enumerate(range(first_blender_frame, last_blender_frame+1)):
                    context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
                    yield
                    _ensure_export_bone(bone_name)
                    if fcurve.array_index == 0:
                        bone_name_to_rotation_values[bone_name][index].w = fcurve.evaluate(frame)
                    elif fcurve.array_index == 1:
                        bone_name_to_rotation_values[bone_name][index].x = fcurve.evaluate(frame)
                    elif fcurve.array_index == 2:
                        bone_name_to_rotation_values[bone_name][index].y = fcurve.evaluate(frame)
                    elif fcurve.array_index == 3:
                        bone_name_to_rotation_values[bone_name][index].z = fcurve.evaluate(frame)
            elif transform_subtype == 'rotation_euler':
                bones_with_euler.add(bone_name)
                for index, frame in enumerate(range(first_blender_frame, last_blender_frame+1)):
                    context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
                    yield
                    _ensure_export_bone(bone_name)
                    if fcurve.array_index == 0:
                        bone_name_to_euler_values[bone_name][index].x = fcurve.evaluate(frame)
                    elif fcurve.array_index == 1:
                        bone_name_to_euler_values[bone_name][index].y = fcurve.evaluate(frame)
                    elif fcurve.array_index == 2:
                        bone_name_to_euler_values[bone_name][index].z = fcurve.evaluate(frame)
            elif transform_subtype == 'scale':
                for index, frame in enumerate(range(first_blender_frame, last_blender_frame+1)):
                    context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
                    yield
                    _ensure_export_bone(bone_name)
                    if fcurve.array_index == 0:
                        bone_name_to_scale_values[bone_name][index].x = fcurve.evaluate(frame)
                    elif fcurve.array_index == 1:
                        bone_name_to_scale_values[bone_name][index].y = fcurve.evaluate(frame)
                    elif fcurve.array_index == 2:
                        bone_name_to_scale_values[bone_name][index].z = fcurve.evaluate(frame)
            animated_pose_bone = arma.pose.bones.get(bone_name)
            if animated_pose_bone is not None:
                animated_pose_bones.add(animated_pose_bone)

        # Detect Negative Scale, Fix Zero Scale
        zero_scale_reported = False
        for bone_name, scale_values_list in bone_name_to_scale_values.items():
            yield
            for index, frame in enumerate(range(first_blender_frame, last_blender_frame+1)):
                context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
                yield
                scale = scale_values_list[index]
                negative_axis: set[str] = set()
                if scale.x < 0.0:
                    negative_axis.add('X')
                if scale.y < 0.0:
                    negative_axis.add('Y')
                if scale.z < 0.0:
                    negative_axis.add('Z')
                if negative_axis:
                    operator.report(type={'ERROR'}, message=f"Negative Scale Detected! Negative scale is not supported, and so the export was cancelled! The first instance was on bone {bone_name} on blender frame {frame} in the {negative_axis} axis.")
                    raise ValueError('Animation contains unsupported negative scale')
                zero_axis: set[str] = set()
                # Use a larger clamping value to avoid numerical instability in matrix inversion
                clamp_value = 0.001
                # Use a tighter tolerance for detection to catch actual zeros
                zero_tolerance = 0.00001
                if scale.x <= zero_tolerance:
                    zero_axis.add('X')
                    scale.x = clamp_value
                if scale.y <= zero_tolerance:
                    zero_axis.add('Y')
                    scale.y = clamp_value
                if scale.z <= zero_tolerance:
                    zero_axis.add('Z')
                    scale.z = clamp_value
                if zero_axis:
                    if not zero_scale_reported:
                        operator.report(type={'INFO'}, message=f"Clamped scale values of `0` to `{clamp_value}` for export. The first instance was on bone {bone_name} on blender frame {frame} in the {zero_axis} axis.")
                        zero_scale_reported = True
                        
        # Create SSBH Transform Group
        trans_group = ssbh_data_py.anim_data.GroupData(ssbh_data_py.anim_data.GroupType.Transform)
        ssbh_anim_data.groups.append(trans_group)

        # Create ssbh nodes for the animated bones, no values just yet tho. Also, its normal for smash anims to skip some un-animated bones.
        # Prepare bone name filter
        override_name_set = set(override_bone_names) if override_bone_names else set()

        for bone in animated_pose_bones:
            yield
            node = ssbh_data_py.anim_data.NodeData(bone.name)
            track = ssbh_data_py.anim_data.TrackData('Transform')
            # Determine if overrides should apply to this bone
            apply_overrides = True
            if override_name_set:
                if use_exclude_list:
                    apply_overrides = bone.name not in override_name_set
                else:
                    apply_overrides = bone.name in override_name_set
            if apply_overrides:
                track.compensate_scale = transform_compensate_scale
                track.transform_flags = ssbh_data_py.anim_data.TransformFlags(
                    override_translation=transform_override_translation,
                    override_rotation=transform_override_rotation,
                    override_scale=transform_override_scale,
                    override_compensate_scale=transform_override_compensate_scale
                )
            node.tracks.append(track)
            trans_group.nodes.append(node)

        # Convenience dict for later node access
        node_name_to_node = {node.name:node for node in trans_group.nodes}

        # Blender stores the 'matrix basis' values in the fcurves
        # Smash stores a 'relative matrix', such that bone.parent.final_matrix @ bone.relative_matrix = bone.final_matrix
        # Need to calculate the final_matrix of each bone at each frame, even the un-animated ones, so that the child bones can be properly calculated.
        bone_to_world_matrix = {}
        for bone in reordered_pose_bones:
            yield
            for index, _ in enumerate(range(first_blender_frame, last_blender_frame+1)):
                yield
                # Get the matrix basis from the stored values of this frame.
                trans_basis_vec = bone_name_to_location_values[bone.name][index]
                trans_basis_mat = Matrix.Translation([trans_basis_vec.x, trans_basis_vec.y, trans_basis_vec.z])
                rot_basis_mat = _rotation_basis_matrix(
                    bone,
                    index,
                    bone_name_to_rotation_values,
                    bone_name_to_euler_values,
                    bones_with_euler,
                    bones_with_quat,
                )
                scale_basis_vec = bone_name_to_scale_values[bone.name][index]
                scale_basis_mat = Matrix.Diagonal((scale_basis_vec.x, scale_basis_vec.y, scale_basis_vec.z, 1.0))
                matrix_basis = Matrix(trans_basis_mat @ rot_basis_mat @ scale_basis_mat)

                # Smash stores the full local transform. Blender fcurves store matrix_basis
                # (the delta from rest). Root bones must go back through the same Y-up
                # conversion import uses, including rest, or Trans identity re-imports as
                # ~63° on Y.
                parent_world = bone_to_world_matrix.get(bone.parent) if bone.parent else None
                is_smash_root = parent_world is None
                if is_smash_root:
                    blender_pose = bone.bone.matrix_local @ matrix_basis
                else:
                    blender_pose = parent_world @ bone_to_rel_matrix_local[bone] @ matrix_basis
                bone_to_world_matrix[bone] = blender_pose

                # Now if theres a matching node, we can update the values for that node.
                node = node_name_to_node.get(bone.name)
                if node is not None:
                    try:
                        if is_smash_root:
                            smash_rel_matrix = get_smash_root_matrix(blender_pose)
                        else:
                            smash_rel_matrix = get_smash_transform(
                                parent_world.inverted() @ blender_pose
                            )
                        t,q,s = smash_rel_matrix.decompose()
                        transform = ssbh_data_py.anim_data.Transform(
                            [s.x, s.y, s.z],
                            [q.x, q.y, q.z, q.w],
                            [t.x, t.y, t.z]
                        )
                        node.tracks[0].values.append(transform)
                        # Check for quaternion interpolation issues
                        if index > 0:
                            prev = node.tracks[0].values[index-1].rotation
                            curr = node.tracks[0].values[index].rotation
                            if _ssbh_quaternion(prev).dot(_ssbh_quaternion(curr)) < 0:
                                node.tracks[0].values[index].rotation = [-c for c in curr]
                    except ValueError as e:
                        # Matrix is not invertible - this can happen with zero/very small scales
                        frame_number = frame if 'frame' in locals() else first_blender_frame + index
                        parent_info = f" (parent: {bone.parent.name})" if bone.parent else ""
                        operator.report(type={'ERROR'}, message=f"Failed to export {bone.name}{parent_info}: Matrix is not invertible at frame {frame_number}. This usually happens when a bone or its parent has zero scale on all axes. Please fix the animation data.")
                        raise ValueError('Animation contains a non-invertible bone matrix') from e
        # Pre-Saving Optimizations
        transform_group_fix_floating_point_inaccuracies(trans_group)
        # Vanilla anims sort the nodes alphabetically. 
        # Without this, certain anims will behave incorrectly, such as the Trans bone motion not working in-game.
        trans_group.nodes.sort(key=lambda node: node.name)

    if include_visibility_track and does_armature_data_have_fcurves(arma):
        # Convenience variable for the sub_anim_properties
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        
        # First gather the values
        vis_track_index_to_name: dict[int, str] = {}
        vis_track_index_to_values: dict[int, list[bool]] = {}
        fcurve: bpy.types.FCurve
        for fcurve in get_all_action_fcurves(arma.data.animation_data.action):
            yield
            regex = r'.*\[(\d*)\]\.value'
            matches = re.match(regex, fcurve.data_path)
            if matches is None: # Not a visibility fcurve, its probably a material track fcurve
                continue
            if not fcurve.data_path.startswith('sub_anim_properties.vis_track_entries'):
                continue
            vis_track_index = int(matches.groups()[0])
            if vis_track_index >= len(sap.vis_track_entries): # this can happen if the user removes entries manually but not the fcurves
                operator.report(type={'WARNING'}, message=f'The fcurve with data path {fcurve.data_path} will be skipped, its index was out of bounds.')
                continue
            vis_track_index_to_name[vis_track_index] = sap.vis_track_entries[vis_track_index].name
            vis_track_index_to_values[vis_track_index] = [bool(fcurve.evaluate(frame)) for frame in range(first_blender_frame, last_blender_frame+1)]

        named_values = [
            (vis_track_index_to_name[index], vis_track_index_to_values[index])
            for index in sorted(vis_track_index_to_values)
        ]
        named_values = merge_exported_visibility_values(arma, named_values)

        # Create Vis Group
        vis_group = ssbh_data_py.anim_data.GroupData(ssbh_data_py.anim_data.GroupType.Visibility)
        ssbh_anim_data.groups.append(vis_group)

        # Create nodes
        for vis_track_name, values in named_values:
            yield
            node = ssbh_data_py.anim_data.NodeData(vis_track_name)
            track = ssbh_data_py.anim_data.TrackData('Visibility')
            track.values = values.copy()
            node.tracks.append(track)
            vis_group.nodes.append(node)

    if include_material_track and does_armature_data_have_fcurves(arma):
        # Convenience variable for the sub_anim_properties
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties

        # Gather the Values
        # Not every CustomVector, CustomBool, etc will be animated, so only the animated ones should be exported.
        # In addition, fcurves may only exist for a few indices of a CustomVector or TextureTransform, since the user may not have animated them all
        # Example: mat_name_prop_name_to_values['EyeL']['CustomVector31'] -> [[1.0,1.0,1.0,1.0], ...]
        mat_name_prop_name_to_values: dict[str, dict[str, list[CustomVector|CustomFloat|CustomBool|PatternIndex|TextureTransform]]] = {}
        for fcurve in get_all_action_fcurves(arma.data.animation_data.action):
            yield
            regex = r"sub_anim_properties\.mat_tracks\[(\d+)\]\.properties\[(\d+)\](\.\w+)"
            matches = re.match(regex, fcurve.data_path)
            if matches is None: # The vis and mat track fcurves are in the same action, so its normal to not match every fcurve
                continue
            if len(matches.groups()) != 3: # TODO: Is this possible?
                operator.report(type={'WARNING'}, message=f"The fcurve with data path {fcurve.data_path} will not be exported, its format only partially matched the expected pattern of a mat track.")
                continue
            # The material index may be out of bounds, this can happen due to improper removal of the MatTrack from the sub_anim_properties.
            # This should however not happen when removed properly through the implemented operators
            material_index = int(matches.groups()[0])
            if material_index >= len(sap.mat_tracks):
                operator.report(type={'WARNING'}, message=f'The fcurve with data path {fcurve.data_path} will be skipped, its material index was out of bounds.')
                continue
            # Now that the material index is validated, can grab the coresponding MatTrack
            mat_track: SUB_PG_mat_track = sap.mat_tracks[material_index]
            material_name = mat_track.name
            # This dict won't exist yet for the first fcurve belonging to a material, so we add it now.
            if mat_name_prop_name_to_values.get(material_name) is None: 
                mat_name_prop_name_to_values[material_name] = {}
            # The property index may be out of bounds, this can happen due to improper removal of the MatTrackProperty from the MatTrack.
            # This should however not happen when removed properly through the implemented operators
            property_index = int(matches.groups()[1])
            if property_index >= len(mat_track.properties):
                operator.report(type={'WARNING'}, message=f'The fcurve with data path {fcurve.data_path} will be skipped, its property index was out of bounds.')
                continue
            # Now that the property index is validated, can grab the coresponding MatTrackProperty
            mat_track_property: SUB_PG_mat_track_property = mat_track.properties[property_index]
            property_name = mat_track_property.name
            # This dict won't exist yet for the first fcurve belonging to a material's property, so we add it now.
            # If it didn't exist, then the default values also didn't exist yet so nows a good time to add them.
            # The default values need to be filled out because an fcurve for each array_index may not exist.
            # This only applies to the CustomVector and TextureTransforms, all others only have one fcurve for the property.  
            if mat_name_prop_name_to_values.get(material_name).get(property_name) is None:
                if mat_track_property.sub_type == 'VECTOR':
                    cv = mat_track_property.custom_vector
                    # Use numpy as this one line takes way to long
                    #mat_name_prop_name_to_values[material_name][property_name] = [[cv[0], cv[1], cv[2], cv[3]] for _ in range(0, final_frame_index+1)]
                    #mat_name_prop_name_to_values[material_name][property_name] = np.full((final_frame_index+1, 4), [cv[0], cv[1], cv[2], cv[3]]).tolist()
                    # Nevermind it seems like the numpy array needs to be converted back into a list before being saved
                    mat_name_prop_name_to_values[material_name][property_name] = [[cv[0], cv[1], cv[2], cv[3]] for _ in range(0, final_frame_index+1)]
                elif mat_track_property.sub_type == 'TEXTURE':
                    tt = mat_track_property.texture_transform
                    mat_name_prop_name_to_values[material_name][property_name] = [ssbh_data_py.anim_data.UvTransform(tt[0], tt[1], tt[2], tt[3], tt[4]) for _ in range(0, final_frame_index+1)]
                else: # Bools, Floats, PatternIndex have only one fcurve, so any default value filled here would get replaced anyways
                    mat_name_prop_name_to_values[material_name][property_name] = []
            # Finally can add the values at each frame
            for index, frame in enumerate(range(first_blender_frame, last_blender_frame+1)):
                context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
                yield
                if mat_track_property.sub_type == 'VECTOR':
                    mat_name_prop_name_to_values[material_name][property_name][index][fcurve.array_index] = fcurve.evaluate(frame)
                elif mat_track_property.sub_type == 'BOOL':
                    mat_name_prop_name_to_values[material_name][property_name].append(bool(fcurve.evaluate(frame)))
                elif mat_track_property.sub_type == 'TEXTURE':
                    if fcurve.array_index == 0:
                        mat_name_prop_name_to_values[material_name][property_name][index].scale_u = fcurve.evaluate(frame)
                    elif fcurve.array_index == 1:
                        mat_name_prop_name_to_values[material_name][property_name][index].scale_v = fcurve.evaluate(frame)
                    elif fcurve.array_index == 2:
                        mat_name_prop_name_to_values[material_name][property_name][index].rotation = fcurve.evaluate(frame)
                    elif fcurve.array_index == 3:
                        mat_name_prop_name_to_values[material_name][property_name][index].translate_u = fcurve.evaluate(frame)
                    elif fcurve.array_index == 4:
                        mat_name_prop_name_to_values[material_name][property_name][index].translate_v = fcurve.evaluate(frame)
                else:
                    mat_name_prop_name_to_values[material_name][property_name].append(fcurve.evaluate(frame))
                
        # Now we can finally process the data
        # Create the material group
        mat_group = ssbh_data_py.anim_data.GroupData(ssbh_data_py.anim_data.GroupType.Material)
        ssbh_anim_data.groups.append(mat_group)
        # Create the nodes and tracks
        for mat_name in mat_name_prop_name_to_values:
            yield
            node = ssbh_data_py.anim_data.NodeData(mat_name)
            mat_group.nodes.append(node)
            for prop_name in mat_name_prop_name_to_values[mat_name]:
                yield
                track = ssbh_data_py.anim_data.TrackData(prop_name)
                node.tracks.append(track)
                track.values.extend(mat_name_prop_name_to_values[mat_name][prop_name])
        # Sort the nodes and tracks by their user-defined position
        mat_group.nodes.sort(key= lambda x: sap.mat_tracks.find(x.name))
        for node in mat_group.nodes:
            yield
            node.tracks.sort(key= lambda x: sap.mat_tracks[node.name].properties.find(x.name))

    # Pre-Saving Optimizations
    for group in ssbh_anim_data.groups:
        yield
        for node in group.nodes:
            yield
            for track in node.tracks:
                yield
                if type(track.values[0]) == ssbh_data_py.anim_data.UvTransform:
                    if all(uv_transform_equality(value, track.values[0]) for value in track.values):
                        track.values = [track.values[0]]
                elif all(value == track.values[0] for value in track.values):
                    track.values = [track.values[0]]
    
    # Done!
    save_ssbh_anim_data(ssbh_anim_data, filepath, operator) 
                
def export_camera_anim(*args, **kwargs):
    for _ in export_camera_anim_steps(*args, **kwargs):
        pass


def export_camera_anim_steps(context, operator, camera: bpy.types.Object, filepath, first_blender_frame, last_blender_frame, transform_compensate_scale: bool = False, transform_override_translation: bool = False, transform_override_rotation: bool = False, transform_override_scale: bool = False, transform_override_compensate_scale: bool = False):
    if last_blender_frame < first_blender_frame:
        raise ValueError('End frame must be greater than or equal to start frame')
    ssbh_anim_data = ssbh_data_py.anim_data.AnimData()
    ssbh_anim_data.final_frame_index = last_blender_frame - first_blender_frame
    
    transform_group = ssbh_data_py.anim_data.GroupData(ssbh_data_py.anim_data.GroupType.Transform)
    transform_group.nodes.append(ssbh_data_py.anim_data.NodeData('gya_camera'))
    transform_group.nodes[0].tracks.append(ssbh_data_py.anim_data.TrackData('Transform'))

    camera_group = ssbh_data_py.anim_data.GroupData(ssbh_data_py.anim_data.GroupType.Camera)
    camera_group.nodes.append(ssbh_data_py.anim_data.NodeData('gya_cameraShape'))
    camera_group.nodes[0].tracks.append(ssbh_data_py.anim_data.TrackData('FarClip'))
    camera_group.nodes[0].tracks.append(ssbh_data_py.anim_data.TrackData('FieldOfView'))
    camera_group.nodes[0].tracks.append(ssbh_data_py.anim_data.TrackData('NearClip'))

    track_name_to_track = {track.name : track for track in camera_group.nodes[0].tracks}
    trans_track = transform_group.nodes[0].tracks[0]
    # Apply flags to the camera transform track as well
    trans_track.compensate_scale = transform_compensate_scale
    trans_track.transform_flags = ssbh_data_py.anim_data.TransformFlags(
        override_translation=transform_override_translation,
        override_rotation=transform_override_rotation,
        override_scale=transform_override_scale,
        override_compensate_scale=transform_override_compensate_scale
    )
    for index, frame in enumerate(range(first_blender_frame, last_blender_frame + 1)):
        context.window_manager.progress_update(95 * index / max(1, last_blender_frame - first_blender_frame + 1))
        yield
        context.scene.frame_set(frame)
        track_name_to_track['FieldOfView'].values.append(camera.data.angle_y)
        track_name_to_track['FarClip'].values.append(camera.data.clip_end)
        track_name_to_track['NearClip'].values.append(camera.data.clip_start)
        fixed_matrix = camera.matrix_local.copy()
        axis_correction = Matrix.Rotation(math.radians(90), 4, 'X') 
        original_matrix = axis_correction.inverted() @ fixed_matrix

        mt, mq, ms = original_matrix.decompose()
        new_ssbh_transform = ssbh_data_py.anim_data.Transform(
            [ms[0], ms[1], ms[2]], 
            [mq.x, mq.y, mq.z, mq.w],
            [mt[0], mt[1], mt[2]]
        )
        trans_track.values.append(new_ssbh_transform)
        # Check for quaternion interpolation issues
        if index > 0:
            pq = mathutils.Quaternion(trans_track.values[index-1].rotation)
            cq = mathutils.Quaternion(trans_track.values[index].rotation)
            if pq.dot(cq) < 0:
                trans_track.values[index].rotation = [-c for c in trans_track.values[index].rotation]

    ssbh_anim_data.groups.append(transform_group)
    ssbh_anim_data.groups.append(camera_group)

    save_ssbh_anim_data(ssbh_anim_data, filepath, operator)
