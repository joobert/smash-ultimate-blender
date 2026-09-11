import re
import sys
import inspect

import bpy

from bpy.types import Panel, Operator, UIList, Menu, PropertyGroup, Armature
from bpy.props import (
    IntProperty,
    StringProperty, 
    EnumProperty, 
    BoolProperty, 
    FloatProperty, 
    CollectionProperty, 
    PointerProperty,
    FloatVectorProperty,)

from .fcurve_compat import (
    apply_dopesheet_key_colors,
    get_id_action_fcurves,
    restore_dopesheet_key_colors,
    style_eye_control_action,
    style_visibility_action,
)

mat_sub_types = (
    ('VECTOR', 'Custom Vector', 'Custom Vector'),
    ('FLOAT', 'Custom Float', 'Custom Float'),
    ('BOOL', 'Custom Bool', 'Custom Bool'),
    ('PATTERN', 'Pattern Index', 'Pattern Index'),
    ('TEXTURE', 'Texture Transform', 'Texture Transform'),
    ('DIFFUSE_UV', 'Diffuse UV Transform', 'Diffuse UV Transform')
)

# Store the last known action for each armature to detect changes
_last_known_actions = {}

# Global owner object for msgbus subscriptions
_msgbus_owner = object()

# Runtime switch for depsgraph/frame/timer SAP sync (UI: Ultimate Animation Data).
_sap_auto_sync_enabled = True


def is_sap_auto_sync_enabled() -> bool:
    return bool(_sap_auto_sync_enabled)


def _install_sap_auto_sync_handlers():
    if sync_sap_action_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(sync_sap_action_handler)
    # Do not use depsgraph_update_post here. It fires continuously and is enough
    # to keep EEVEE/Cycles Rendered sampling from ever finishing. Frame change,
    # msgbus, and a slow timer are enough to catch action switches.
    if sync_sap_action_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sync_sap_action_depsgraph_handler)
    if not bpy.app.timers.is_registered(sync_sap_timer):
        bpy.app.timers.register(sync_sap_timer, first_interval=0.5, persistent=True)
    subscribe_to_action_changes()


def _uninstall_sap_auto_sync_handlers():
    if sync_sap_action_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(sync_sap_action_handler)
    if sync_sap_action_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sync_sap_action_depsgraph_handler)
    if bpy.app.timers.is_registered(sync_sap_timer):
        bpy.app.timers.unregister(sync_sap_timer)
    unsubscribe_from_action_changes()


def set_sap_auto_sync_enabled(enabled: bool) -> None:
    """Enable or disable SAP auto-sync handlers/timers."""
    global _sap_auto_sync_enabled
    enabled = bool(enabled)
    if enabled == _sap_auto_sync_enabled:
        if enabled:
            # Ensure handlers exist after addon reload without double-subscribe.
            if sync_sap_action_handler not in bpy.app.handlers.frame_change_post:
                _install_sap_auto_sync_handlers()
        else:
            _uninstall_sap_auto_sync_handlers()
        return
    _sap_auto_sync_enabled = enabled
    if enabled:
        _install_sap_auto_sync_handlers()
    else:
        _uninstall_sap_auto_sync_handlers()


# Handler to sync SAP data action with bone animation action
@bpy.app.handlers.persistent
def sync_sap_action_handler(scene):
    """
    Handler that automatically switches the SAP data action when the main action changes.
    This ensures that visibility and material animation data stays in sync with bone animation.
    """
    if not _sap_auto_sync_enabled:
        return
    global _last_known_actions
    
    for obj in bpy.data.objects:
        if obj.type != 'ARMATURE':
            continue
        
        # Skip if no animation data
        if not obj.animation_data or not obj.animation_data.action:
            # Clear the stored action if there's no current action
            if obj.name in _last_known_actions:
                print(f"Clearing stored action for {obj.name}")
                del _last_known_actions[obj.name]
            continue
            
        current_action = obj.animation_data.action

        # Skip if no armature data animation data
        if not obj.data.animation_data:
            continue
        current_sap_action = obj.data.animation_data.action
        
        # Check if the action has changed since last time
        last_action = _last_known_actions.get(obj.name)
        if last_action == current_action:
            continue  # No change, skip
            
        # Update the stored action
        _last_known_actions[obj.name] = current_action
        
        # Look for corresponding SAP action
        expected_sap_action_name = f"{obj.name} {current_action.name} SAP Data"
        expected_sap_action = bpy.data.actions.get(expected_sap_action_name)
        
        # If we found a matching SAP action and it's different from current, switch to it
        if expected_sap_action and expected_sap_action != current_sap_action:
            from ..blender_compat import assign_action
            assign_action(obj.data.animation_data, expected_sap_action)
            current_sap_action = expected_sap_action
            try:
                from ..extras.eye_rig import ensure_eye_live_preview, match_eye_look_from_material
                match_eye_look_from_material(obj, overwrite=False)
                ensure_eye_live_preview(scene)
            except Exception:
                pass

        if current_sap_action is not None:
            style_visibility_action(current_sap_action)


def mark_sap_sync_known(armature_object: bpy.types.Object):
    """Record the current bone action so the SAP sync handler does not fight imports."""
    global _last_known_actions
    if armature_object.animation_data and armature_object.animation_data.action:
        _last_known_actions[armature_object.name] = armature_object.animation_data.action

# Additional handler for depsgraph updates (more frequent)
@bpy.app.handlers.persistent  
def sync_sap_action_depsgraph_handler(scene, depsgraph):
    """
    Alternative handler that runs on depsgraph updates.
    This catches more events including action changes.
    """
    try:
        from .import_anim import sync_anim_importer_to_active
        sync_anim_importer_to_active(bpy.context)
    except Exception:
        pass
    if not _sap_auto_sync_enabled:
        return
    sync_sap_action_handler(scene)
    # Do NOT restyle visibility/eye F-Curves here. Writing RNA on every
    # depsgraph update (theme colors, bone palette, keyframe types) creates a
    # feedback loop that restarts EEVEE/Cycles viewport sampling forever.

# Timer function for periodic checking
def sync_sap_timer():
    """
    Timer function that runs periodically to check for action changes.
    This is a fallback method to ensure SAP actions stay synced.
    """
    if not _sap_auto_sync_enabled:
        return None
    try:
        # Check if we're in a valid context for modifying data
        if bpy.context.mode in {'OBJECT', 'POSE'}:
            scene = bpy.context.scene
            sync_sap_action_handler(scene)
    except Exception as e:
        # Silently handle context errors
        pass
    
    # Return the interval for the next call
    return 0.5

# Message bus callback for action changes
def action_change_msgbus_callback(*args):
    """
    Callback function for msgbus that triggers when animation_data.action changes.
    This provides more direct detection of action changes in the UI.
    """
    if not _sap_auto_sync_enabled:
        return
    try:
        if bpy.context.mode in {'OBJECT', 'POSE'}:
            scene = bpy.context.scene
            sync_sap_action_handler(scene)
    except Exception as e:
        # Silently handle context errors
        pass
    try:
        from ..extras.smash_viewport import invalidate_animation_state
        invalidate_animation_state()
    except Exception:
        pass

# Subscribe to action changes via msgbus
def subscribe_to_action_changes():
    """
    Subscribe to animation_data.action changes using Blender's message bus system.
    This provides more direct detection of action switching in the UI.
    """
    try:
        bpy.msgbus.clear_by_owner(_msgbus_owner)
        # Subscribe to changes in animation_data.action for all objects
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.AnimData, "action"),
            owner=_msgbus_owner,
            args=(),
            notify=action_change_msgbus_callback,
        )
    except Exception as e:
        # Silently handle msgbus errors
        pass

def unsubscribe_from_action_changes():
    """
    Unsubscribe from action changes when cleaning up.
    """
    try:
        bpy.msgbus.clear_by_owner(_msgbus_owner)
    except:
        pass

# Modal operator for continuous monitoring
class SUB_OP_sap_sync_monitor(Operator):
    bl_idname = 'sub.sap_sync_monitor'
    bl_label = 'SAP Sync Monitor'
    bl_description = 'Start/stop continuous SAP action monitoring'
    
    action: EnumProperty(
        items=[
            ('TOGGLE', 'Toggle', 'Toggle monitoring on/off'),
            ('START', 'Start', 'Start monitoring'),
            ('STOP', 'Stop', 'Stop monitoring'),
        ],
        default='TOGGLE'
    )
    
    _timer = None
    _is_running = False
    
    @classmethod
    def poll(cls, context):
        return True
    
    def modal(self, context, event):
        if event.type == 'TIMER':
            # Check for action changes
            try:
                if context.mode in {'OBJECT', 'POSE'}:
                    sync_sap_action_handler(context.scene)
            except Exception as e:
                if "not allowed" not in str(e):
                    print(f"SAP sync monitor error: {e}")
        
        # Continue running
        return {'PASS_THROUGH'}
    
    def execute(self, context):
        should_start = False
        
        if self.action == 'START' or (self.action == 'TOGGLE' and not SUB_OP_sap_sync_monitor._is_running):
            should_start = True
        elif self.action == 'STOP' or (self.action == 'TOGGLE' and SUB_OP_sap_sync_monitor._is_running):
            should_start = False
        
        if should_start and not SUB_OP_sap_sync_monitor._is_running:
            # Start monitoring
            wm = context.window_manager
            SUB_OP_sap_sync_monitor._timer = wm.event_timer_add(0.1, window=context.window)
            wm.modal_handler_add(self)
            SUB_OP_sap_sync_monitor._is_running = True
            self.report({'INFO'}, "SAP sync monitoring started")
            return {'RUNNING_MODAL'}
        elif not should_start and SUB_OP_sap_sync_monitor._is_running:
            # Stop monitoring
            if SUB_OP_sap_sync_monitor._timer:
                wm = context.window_manager
                wm.event_timer_remove(SUB_OP_sap_sync_monitor._timer)
                SUB_OP_sap_sync_monitor._timer = None
            SUB_OP_sap_sync_monitor._is_running = False
            self.report({'INFO'}, "SAP sync monitoring stopped")
            return {'FINISHED'}
        
        return {'FINISHED'}



class SUB_OP_sync_sap_action(Operator):
    bl_idname = 'sub.sync_sap_action'
    bl_label = 'Sync SAP Action'
    bl_description = 'Manually sync the SAP data action with the current bone animation action'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.object and 
                context.object.type == 'ARMATURE' and 
                context.object.animation_data and 
                context.object.animation_data.action)

    def execute(self, context):
        obj = context.object
        
        if not obj.data.animation_data:
            self.report({'WARNING'}, "No SAP animation data found")
            return {'CANCELLED'}
            
        current_action = obj.animation_data.action
        expected_sap_action_name = f"{obj.name} {current_action.name} SAP Data"
        expected_sap_action = bpy.data.actions.get(expected_sap_action_name)
        
        if expected_sap_action:
            from ..blender_compat import assign_action
            assign_action(obj.data.animation_data, expected_sap_action)
            self.report({'INFO'}, f"Synced SAP action to: {expected_sap_action_name}")
        else:
            self.report({'WARNING'}, f"No matching SAP action found: {expected_sap_action_name}")

        # Best effort: a missing motion list should not fail the SAP sync.
        from . import motion_list_ui
        try:
            self.report({'INFO'}, motion_list_ui.load_into_action(context))
        except Exception as error:
            self.report({'WARNING'}, f"Motion list not synced: {error}")

        return {'FINISHED'}

class SUB_PT_sub_smush_anim_data_main(Panel):
    bl_label = "Ultimate Animation Data"
    bl_idname = __qualname__
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        ssp = context.scene.sub_scene_properties
        
        # Auto-sync toggles share one split row; manual sync spans both below them.
        motion = context.scene.sub_motion_list
        box = layout.box()
        row = box.row(align=True)
        row.prop(
            ssp,
            "sap_auto_sync_enabled",
            text="SAP Auto-Sync",
            toggle=True,
            icon='CHECKMARK' if ssp.sap_auto_sync_enabled else 'PAUSE',
        )
        row.prop(
            motion,
            "auto_sync",
            text="Motion List Auto-Sync",
            toggle=True,
            icon='CHECKMARK' if motion.auto_sync else 'PAUSE',
        )
        box.row().operator(SUB_OP_sync_sap_action.bl_idname, icon='FILE_REFRESH', text="Manual Sync")
        if not (ssp.sap_auto_sync_enabled and motion.auto_sync):
            col = box.column(align=True)
            col.scale_y = 0.85
            col.label(text="Off: use Manual Sync after switching actions.", icon='INFO')
        layout.operator("sub.face_picker_popup", text="Easy Facial Animation", icon="IMAGE_DATA")


class SUB_PT_sub_smush_anim_data_vis_tracks(Panel):
    bl_label = "Ultimate Visibility Track Entries"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}
    bl_parent_id = SUB_PT_sub_smush_anim_data_main.bl_idname

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        obj = context.object
        arma = obj.data
        row = layout.row()
        row.template_list(
            "SUB_UL_vis_track_entries",
            "",
            arma.sub_anim_properties,
            "vis_track_entries",
            arma.sub_anim_properties,
            "active_vis_track_index",
            rows=5,
            maxrows=10,
            )
        col = row.column(align=True)
        col.operator(SUB_OP_vis_entry_add.bl_idname, icon='ADD', text="")
        col.operator(SUB_OP_vis_entry_remove.bl_idname, icon='REMOVE', text="")
        col.separator()
        col.menu("SUB_MT_vis_entry_context_menu", icon='DOWNARROW_HLT', text="")
        col.separator()
        col.operator(SUB_OP_vis_entry_shift.bl_idname, icon='TRIA_UP', text='').shift_direction = 'UP'
        col.operator(SUB_OP_vis_entry_shift.bl_idname, icon='TRIA_DOWN', text='').shift_direction = 'DOWN'
        row = layout.row(align=True)
        op = row.operator(SUB_OP_purge_unused_vis_tracks.bl_idname, text="Purge Current", icon='BRUSH_DATA')
        op.scope = 'CURRENT'
        op = row.operator(SUB_OP_purge_unused_vis_tracks.bl_idname, text="Purge All Anims", icon='TRASH')
        op.scope = 'ALL'


class SUB_PT_sub_smush_anim_data_mat_tracks(Panel):
    bl_label = "Ultimate Material Tracks"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}
    bl_parent_id = SUB_PT_sub_smush_anim_data_main.bl_idname

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        obj = context.object
        arma = obj.data
        col = layout.column()
        row = col.row()
        split = row.split(factor=.4)
        c = split.column()
        c.label(text='Material Names')
        c.template_list(
            "SUB_UL_mat_tracks",
            "",
            arma.sub_anim_properties,
            "mat_tracks",
            arma.sub_anim_properties,
            "active_mat_track_index",
            rows=5,
            maxrows=5,
            )
        split = split.split(factor=.66)
        c = split.column()
        c.label(text='Property Names')
        amti = arma.sub_anim_properties.active_mat_track_index
        if len(arma.sub_anim_properties.mat_tracks) > 0:
            c.template_list(
                "SUB_UL_mat_properties",
                "",
                arma.sub_anim_properties.mat_tracks[amti],
                "properties",
                arma.sub_anim_properties.mat_tracks[amti],
                "active_property_index",
                rows=5,
                maxrows=5,
            )
        else:
            c.enabled = False
        split = split.split()
        c = split.column()
        c.enabled = False
        c.label(text='Property Values')
        if len(arma.sub_anim_properties.mat_tracks) > 0:
            if len(arma.sub_anim_properties.mat_tracks[amti].properties) > 0:
                '''
                After removing the last entry from the list, the 'active' index can remain its previous value
                which is now out of bounds
                '''
                amtpi = arma.sub_anim_properties.mat_tracks[amti].active_property_index
                if amtpi < len(arma.sub_anim_properties.mat_tracks[amti].properties):
                    ap = arma.sub_anim_properties.mat_tracks[amti].properties[amtpi]
                    if ap.sub_type == 'VECTOR':
                        c.prop(ap, "custom_vector", text="")
                        c.prop(ap, "custom_vector", text="", index=0)
                        c.prop(ap, "custom_vector", text="", index=1)
                        c.prop(ap, "custom_vector", text="", index=2)
                        c.prop(ap, "custom_vector", text="", index=3)
                    elif ap.sub_type == 'FLOAT':
                        c.prop(ap, "custom_float", text="", emboss=False)
                    elif ap.sub_type == 'BOOL':
                        icon = 'CHECKBOX_HLT' if ap.custom_bool == True else 'CHECKBOX_DEHLT'
                        c.prop(ap, "custom_bool", text="", icon=icon, emboss=False)
                    elif ap.sub_type == 'PATTERN':
                        c.prop(ap, "pattern_index", text="", emboss=False)
                    elif ap.sub_type == 'TEXTURE':
                        c.prop(ap, "texture_transform", text="", emboss=False)
                    c.enabled = True
        # Bottom Row, composed of 3 Sub Rows algined with the above columns
        row = layout.row()
        # Sub Row 1
        split = row.split(factor=.4)
        sr = split.row(align=True)
        sr.operator(SUB_OP_mat_track_add.bl_idname, text='+')
        sr.operator(SUB_OP_mat_track_remove.bl_idname, text='-')
        # Sub Row 2
        split = split.split(factor=.66)
        sr = split.row(align=True)
        sr.operator(SUB_OP_mat_property_add.bl_idname, text='+')
        sr.operator(SUB_OP_mat_property_remove.bl_idname, text='-')
        sr.operator(SUB_OP_mat_property_shift.bl_idname, icon='TRIA_UP', text='').shift_direction = 'UP'
        sr.operator(SUB_OP_mat_property_shift.bl_idname, icon='TRIA_DOWN', text='').shift_direction = 'DOWN'
        # Sub 3
        split = split.split()
        sr = split.row(align=True)
        sr.menu('SUB_MT_mat_entry_context_menu', text='Drivers...')      


class SUB_OP_mat_track_add(Operator):
    bl_description = 'Add an animated material track to the active armature'
    bl_idname = 'sub.mat_track_add'
    bl_label  = 'Add Mat Track'

    def execute(self, context):
        mat_tracks = context.object.data.sub_anim_properties.mat_tracks
        mat_track = mat_tracks.add()
        mat_track.name = 'NewMaterialTrack'
        sap = context.object.data.sub_anim_properties
        sap.active_mat_track_index = sap.mat_tracks.find(mat_track.name)
        return {'FINISHED'}

class SUB_OP_mat_track_remove(Operator):
    bl_description = 'Remove the selected material animation track and its properties'
    bl_idname = 'sub.mat_track_remove'
    bl_label = 'Remove Mat Track'

    @classmethod
    def poll(cls, context):
        sap = context.object.data.sub_anim_properties
        return len(sap.mat_tracks) > 0

    def execute(self, context):
        sap = context.object.data.sub_anim_properties
        amt = sap.mat_tracks[sap.active_mat_track_index]
        # Find matching Fcurve and Remove
        fcurves = get_id_action_fcurves(context.object.data)
        if fcurves is None:
            sap.mat_tracks.remove(sap.active_mat_track_index)
            i = sap.active_mat_track_index
            sap.active_mat_track_index = min(max(0,i-1),len(sap.mat_tracks))
            return {'FINISHED'}
        # Remove fcurves of all properties of this material track
        for fc in fcurves:
            amti = sap.active_mat_track_index
            if fc.data_path.startswith(f"sub_anim_properties.mat_tracks[{amti}]"):
                fcurves.remove(fc)
        # The remaining materials with an index greater than this one must have all thier fcurves adjusted
        fcurves = get_id_action_fcurves(context.object.data)
        if fcurves is None:
            sap.mat_tracks.remove(sap.active_mat_track_index)
            i = sap.active_mat_track_index
            sap.active_mat_track_index = min(max(0,i-1),len(sap.mat_tracks))
            return {'FINISHED'}
        for fc in fcurves:
            regex = r"sub_anim_properties\.mat_tracks\[(\d+)\](\.properties\[\d+\]\.\w+)"
            matches = re.match(regex, fc.data_path)
            if matches is None:
                continue
            if len(matches.groups()) < 2:
                continue
            cmti = int(matches.groups()[0])
            suffix = matches.groups()[1]
            amti = sap.active_mat_track_index
            if cmti < amti:
                continue
            new_data_path = f"sub_anim_properties.mat_tracks[{cmti-1}]{suffix}"
            fc.data_path = new_data_path
        # Now actually remove the material track
        sap.mat_tracks.remove(sap.active_mat_track_index)
        i = sap.active_mat_track_index
        sap.active_mat_track_index = min(max(0,i-1),len(sap.mat_tracks))
        # Refresh Material Drivers
        remove_anim_material_drivers(context.object)
        from .import_anim import setup_material_drivers
        setup_material_drivers(context.object)
        return {'FINISHED'}

class SUB_OP_mat_property_add(Operator):
    bl_description = 'Add an animated shader property to the selected material track'
    bl_idname = 'sub.mat_prop_add'
    bl_label = 'Add Material Property'
    bl_property = "sub_type"

    sub_type: bpy.props.EnumProperty(
        name='Mat Track Entry Subtype',
        description='',
        items=mat_sub_types, 
        default='VECTOR',)

    @classmethod
    def poll(cls, context):
        sap = context.object.data.sub_anim_properties
        return len(sap.mat_tracks) > 0

    def execute(self, context):
        sap = context.object.data.sub_anim_properties
        props = sap.mat_tracks[sap.active_mat_track_index].properties
        prop = props.add()
        prop.sub_type = self.sub_type
        if prop.sub_type == 'VECTOR':
            prop.name = f'CustomVectorX'
        elif prop.sub_type == 'FLOAT':
            prop.name = f'CustomFloatX'
        elif prop.sub_type == 'BOOL':
            prop.name = f'CustomBooleanX'
        else:
            prop.name = f'New{prop.sub_type}Property'
        sap.mat_tracks[sap.active_mat_track_index].active_property_index = props.find(prop.name)
        return {'FINISHED'}
    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

def refresh_material_drivers(context):
    from .import_anim import setup_material_drivers
    remove_anim_material_drivers(context.object)
    setup_material_drivers(context.object)

class SUB_OP_mat_property_remove(Operator):
    bl_description = 'Remove the selected animated shader property from its material track'
    bl_idname = 'sub.mat_prop_remove'
    bl_label = 'Remove Material Property'

    @classmethod
    def poll(cls, context):
        sap = context.object.data.sub_anim_properties
        if len(sap.mat_tracks) > 0:
            active_track = sap.mat_tracks[sap.active_mat_track_index]
            if len(active_track.properties) > 0:
                return True
        return False

    def execute(self, context):
        sap = context.object.data.sub_anim_properties
        amt = sap.mat_tracks[sap.active_mat_track_index]  
        fcurves = get_id_action_fcurves(context.object.data)
        if fcurves is None:
            amt.properties.remove(amt.active_property_index)
            i = amt.active_property_index
            amt.active_property_index = min(max(0,i-1), len(amt.properties)-1)
            return {'FINISHED'}
        # Remove matching fcurve
        for fc in fcurves:
            amti = sap.active_mat_track_index
            api = sap.mat_tracks[amti].active_property_index
            if fc.data_path.startswith(f"sub_anim_properties.mat_tracks[{amti}].properties[{api}]"):
                fcurves.remove(fc)
        # The material's remaining properties' fcurves with indexes greater to this one must be decremented
        fcurves = get_id_action_fcurves(context.object.data)
        if fcurves is None:
            amt.properties.remove(amt.active_property_index)
            i = amt.active_property_index
            amt.active_property_index = min(max(0,i-1), len(amt.properties)-1)
            return {'FINISHED'}

        for fc in fcurves:    
            regex = r"sub_anim_properties\.mat_tracks\[(\d+)\]\.properties\[(\d+)\](\.\w+)"
            matches = re.match(regex, fc.data_path)
            if matches is None:
                continue
            if len(matches.groups()) < 3:
                continue
            cmti = int(matches.groups()[0])
            cpi = int(matches.groups()[1])
            suffix = matches.groups()[2]
            amti = sap.active_mat_track_index
            api = sap.mat_tracks[amti].active_property_index
            if cmti != amti or cpi <= api:
                continue
            new_data_path = f"sub_anim_properties.mat_tracks[{cmti}].properties[{cpi-1}]{suffix}"
            fc.data_path = new_data_path 
        # Now actually remove the property
        amt.properties.remove(amt.active_property_index)
        i = amt.active_property_index
        amt.active_property_index = min(max(0,i-1), len(amt.properties)-1)
        # Refresh Material Drivers
        refresh_material_drivers(context)
        return {'FINISHED'}

def change_mat_property_fcurve_target_index(fcurve, new_property_index):
    regex = r"sub_anim_properties\.mat_tracks\[(\d+)\]\.properties\[(\d+)\](\.\w+)"
    matches = re.match(regex, fcurve.data_path)
    if matches is None:
        return
    if len(matches.groups()) < 3:
        return
    mat_track_index = int(matches.groups()[0])
    _property_index = int(matches.groups()[1])
    suffix = matches.groups()[2]
    new_data_path = f"sub_anim_properties.mat_tracks[{mat_track_index}].properties[{new_property_index}]{suffix}"
    fcurve.data_path = new_data_path

def swap_mat_property_fcurve_target_indices(fcurves, sap, index_a, index_b):
    amti = sap.active_mat_track_index

    a_data_path = f"sub_anim_properties.mat_tracks[{amti}].properties[{index_a}]"
    a_fcurves = [fc for fc in fcurves if fc.data_path.startswith(a_data_path)]
    
    b_data_path = f"sub_anim_properties.mat_tracks[{amti}].properties[{index_b}]"
    b_fcurves = [fc for fc in fcurves if fc.data_path.startswith(b_data_path)]
    
    for fc in a_fcurves:
        change_mat_property_fcurve_target_index(fc, index_b)
    for fc in b_fcurves:
        change_mat_property_fcurve_target_index(fc, index_a)
          
class SUB_OP_mat_property_shift(Operator):
    bl_description = 'Move the selected material property up or down in the list'
    bl_idname = 'sub.mat_property_shift'
    bl_label = 'Shift Mat Propery'

    shift_direction: EnumProperty(
        name='Shift Direction',
        description='The direction to shift',
        items=[('UP', 'Up', 'Shift it up'),
                ('DOWN', 'Down', 'Shift it down')])
    
    @classmethod
    def poll(cls, context):
        sap = context.object.data.sub_anim_properties
        if len(sap.mat_tracks) >= 1:
            active_track = sap.mat_tracks[sap.active_mat_track_index]
            if len(active_track.properties) >= 2:
                return True
        return False
    
    def execute(self, context):
        sap = context.object.data.sub_anim_properties
        active_mat = sap.mat_tracks[sap.active_mat_track_index]
        active_property_index = active_mat.active_property_index
            
        if (self.shift_direction == 'UP' and active_property_index == 0) or \
           (self.shift_direction == 'DOWN' and active_property_index == len(active_mat.properties)-1):
                return {'CANCELLED'}
        
        other_index = active_property_index-1 if self.shift_direction == 'UP' else active_property_index+1
            
        fcurves = get_id_action_fcurves(context.object.data)
        if fcurves is not None:
            swap_mat_property_fcurve_target_indices(fcurves, sap, active_property_index, other_index)

        active_mat.properties.move(active_property_index, other_index)
        active_mat.active_property_index = other_index
        # Refresh Material Drivers
        refresh_material_drivers(context)
        return {'FINISHED'}
    
class SUB_OP_vis_entry_add(Operator):
    bl_description = 'Add a mesh visibility track to the active armature'
    bl_idname = 'sub.vis_entry_add'
    bl_label = 'Add Vis Track Entry'

    def execute(self, context):
        entries = context.object.data.sub_anim_properties.vis_track_entries
        entry = entries.add()
        entry.name = 'NewVisTrackEntry'
        entry.value = True
        sap = context.object.data.sub_anim_properties
        sap.active_vis_track_index = entries.find(entry.name)
        return {'FINISHED'} 

def refresh_visibility_drivers(context):
    from .import_anim import setup_visibility_drivers
    remove_visibility_drivers(context)
    setup_visibility_drivers(context.object)

class SUB_OP_vis_entry_remove(Operator):
    bl_description = 'Remove the selected mesh visibility track'
    bl_idname = 'sub.vis_entry_remove'
    bl_label = 'Remove Vis Track Entry'

    @classmethod
    def poll(cls, context):
        sap = context.object.data.sub_anim_properties
        return len(sap.vis_track_entries) > 0

    def execute(self, context):
        sap = context.object.data.sub_anim_properties
        active_vis_track_index = sap.active_vis_track_index

        from .visibility_tracks import remap_visibility_entry_order

        new_order = [
            index for index in range(len(sap.vis_track_entries))
            if index != active_vis_track_index
        ]
        remap_visibility_entry_order(context.object, new_order)
        sap.active_vis_track_index = min(
            max(0, active_vis_track_index - 1),
            max(len(sap.vis_track_entries) - 1, 0),
        )

        refresh_visibility_drivers(context)       
        return {'FINISHED'} 
    
class SUB_OP_vis_entry_shift(Operator):
    bl_description = 'Move the selected visibility track up or down in the list'
    bl_idname = 'sub.vis_entry_shift'
    bl_label = 'Shift Vis Entry'

    shift_direction: EnumProperty(
        name='Shift Direction',
        description='The direction to shift',
        items=[('UP', 'Up', 'Shift it up'),
                ('DOWN', 'Down', 'Shift it down')])
    
    @classmethod
    def poll(cls, context):
        sap = context.object.data.sub_anim_properties
        return len(sap.vis_track_entries) > 1
    
    def execute(self, context):
        sap = context.object.data.sub_anim_properties
        vis_entries = sap.vis_track_entries
        active_vis_entry_index = sap.active_vis_track_index
            
        if (self.shift_direction == 'UP' and active_vis_entry_index == 0) or \
           (self.shift_direction == 'DOWN' and active_vis_entry_index == len(vis_entries)-1):
                return {'CANCELLED'}
        
        other_index = active_vis_entry_index-1 if self.shift_direction == 'UP' else active_vis_entry_index+1
            
        from .visibility_tracks import remap_visibility_entry_order

        new_order = list(range(len(vis_entries)))
        new_order[active_vis_entry_index], new_order[other_index] = (
            new_order[other_index],
            new_order[active_vis_entry_index],
        )
        remap_visibility_entry_order(context.object, new_order)
        sap.active_vis_track_index = other_index
        refresh_visibility_drivers(context)
        return {'FINISHED'}

class SUB_OP_vis_drivers_refresh(Operator):
    bl_description = 'Rebuild mesh visibility drivers from the armature visibility tracks'
    bl_idname = 'sub.vis_drivers_refresh'
    bl_label = 'Refresh Visibility Drivers'

    def execute(self, context):
        refresh_visibility_drivers(context)
        return {'FINISHED'} 

class SUB_OP_vis_drivers_remove(Operator):
    bl_description = 'Remove the drivers that connect mesh visibility to animation tracks'
    bl_idname = 'sub.vis_drivers_remove'
    bl_label = 'Remove Visibility Drivers'

    def execute(self, context):
        remove_visibility_drivers(context)
        return {'FINISHED'}


class SUB_OP_purge_unused_vis_tracks(Operator):
    bl_idname = 'sub.purge_unused_vis_tracks'
    bl_label = 'Purge Unused Visibility Tracks'
    bl_description = (
        'Remove visibility tracks that do not match this model, compact their '
        'indices safely, and merge names that differ only by capitalization'
    )
    bl_options = {'REGISTER', 'UNDO'}

    scope: EnumProperty(
        name='Scope',
        items=(
            ('CURRENT', 'Current Animation', 'Purge invalid tracks from the current animation'),
            ('ALL', 'All Animations', 'Purge invalid tracks from every animation for this armature'),
        ),
        default='CURRENT',
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return (
            context.object is not None
            and context.object.type == 'ARMATURE'
            and len(context.object.data.sub_anim_properties.vis_track_entries) > 0
        )

    def invoke(self, context, _event):
        if self.scope == 'ALL':
            return context.window_manager.invoke_confirm(
                self,
                _event,
                title='Purge Visibility Tracks from All Animations?',
                message='This removes tracks that do not correspond to a mesh on this model.',
                confirm_text='Purge All Animations',
                icon='WARNING',
            )
        return self.execute(context)

    def execute(self, context):
        from .visibility_tracks import purge_unused_visibility_tracks

        result = purge_unused_visibility_tracks(context.object, self.scope)
        if result['actions'] == 0:
            self.report({'WARNING'}, 'No matching SAP visibility action was found.')
            return {'CANCELLED'}

        refresh_visibility_drivers(context)
        try:
            from ..extras.smash_viewport import invalidate_animation_state

            invalidate_animation_state()
        except Exception:
            pass
        scope_label = 'current animation' if self.scope == 'CURRENT' else f"{result['actions']} animations"
        self.report(
            {'INFO'},
            f"Purged {result['entries']} entries and {result['curves']} F-curves from {scope_label}; "
            f"merged {result['duplicates']} case-only duplicate(s)",
        )
        return {'FINISHED'}

class SUB_OP_auto_fill_vis_entries(Operator):
    bl_description = 'Create visibility tracks from meshes belonging to the active armature'
    bl_idname = 'sub.auto_fill_vis_entries'
    bl_label = 'Auto Fill Vis Entries'

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def execute(self, context):
        arma: bpy.types.Object = context.object
        from .visibility_tracks import model_visibility_names

        vis_names = set(model_visibility_names(arma).values())
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        existing_names = {entry.name.casefold() for entry in sap.vis_track_entries}
        for vis_name in vis_names:
            if vis_name.casefold() not in existing_names:
                new_entry: SUB_PG_vis_track_entry = sap.vis_track_entries.add()
                new_entry.name = vis_name
                new_entry.value = True
                existing_names.add(vis_name.casefold())
        return {'FINISHED'}

class SUB_OP_set_all_vis_entries_false(Operator):
    bl_description = 'Hide every mesh controlled by a visibility track'
    bl_idname = 'sub.set_all_vis_entries_false'
    bl_label = 'Set All Vis Entries False'

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def execute(self, context):
        for vis_entry in context.object.data.sub_anim_properties.vis_track_entries:
            vis_entry.value = False
        return {'FINISHED'}

class SUB_OP_set_all_vis_entries_true(Operator):
    bl_description = 'Show every mesh controlled by a visibility track'
    bl_idname = 'sub.set_all_vis_entries_true'
    bl_label = 'Set All Vis Entries True'

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def execute(self, context):
        for vis_entry in context.object.data.sub_anim_properties.vis_track_entries:
            vis_entry.value = True
        return {'FINISHED'}

class SUB_OP_insert_all_vis_entry_keyframes(Operator):
    bl_description = 'Keyframe every visibility track at the current frame'
    bl_idname = 'sub.insert_all_vis_entry_keyframes'
    bl_label = 'Insert All Vis Entry Keyframes'

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def execute(self, context):
        arma: bpy.types.Object = context.object
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        for index, vis_entry in enumerate(sap.vis_track_entries):
            arma.data.keyframe_insert(data_path=f'sub_anim_properties.vis_track_entries[{index}].value', group='Visibility')
        if arma.data.animation_data is not None:
            style_visibility_action(arma.data.animation_data.action, create_spacer=True)
        return {'FINISHED'}

class SUB_OP_organize_vis_entries_alphabetically(Operator):
    bl_description = 'Sort visibility tracks alphabetically by their names'
    bl_idname = 'sub.organize_vis_entries_alphabetically'
    bl_label = 'Organize Vis Entries Alphabetically'

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def execute(self, context):
        arma: bpy.types.Object = context.object
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        
        from .visibility_tracks import remap_visibility_entry_order

        new_order = sorted(
            range(len(sap.vis_track_entries)),
            key=lambda index: sap.vis_track_entries[index].name.casefold(),
        )
        remap_visibility_entry_order(arma, new_order)
        
        # Reset active index
        sap.active_vis_track_index = 0
        
        refresh_visibility_drivers(context)
        return {'FINISHED'}

class SUB_OP_organize_vis_entries_by_move(Operator):
    bl_description = 'Group visibility tracks by the move names in their labels'
    bl_idname = 'sub.organize_vis_entries_by_move'
    bl_label = 'Organize Vis Entries by Move'

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def execute(self, context):
        arma: bpy.types.Object = context.object
        sap: SUB_PG_sub_anim_data = arma.data.sub_anim_properties
        
        from .visibility_tracks import remap_visibility_entry_order

        def move_sort_key(index):
            name = sap.vis_track_entries[index].name
            parts = name.rsplit('_', 1)
            move_type = parts[1] if len(parts) > 1 else 'no_move_type'
            return (move_type.casefold(), name.casefold())

        new_order = sorted(range(len(sap.vis_track_entries)), key=move_sort_key)
        remap_visibility_entry_order(arma, new_order)
        
        # Reset active index
        sap.active_vis_track_index = 0
        
        refresh_visibility_drivers(context)
        return {'FINISHED'}

def remove_visibility_drivers(context):
    from .import_anim import remove_visibility_drivers_for_armature
    remove_visibility_drivers_for_armature(context.object)

def remove_anim_material_drivers(arma:bpy.types.Object):
    from ..model.material.sub_matl_data import SUB_PG_sub_matl_data
    from ..model.material.create_blender_materials_from_matl import setup_sub_matl_data_node_drivers
    mesh_children = [child for child in arma.children if child.type == 'MESH']
    materials = {material_slot.material for mesh in mesh_children for material_slot in mesh.material_slots}
    for material in materials:
        for node in material.node_tree.nodes:
            for output in node.outputs:
                if hasattr(output, 'default_value'):
                    output.driver_remove('default_value')
        
        sub_matl_data: SUB_PG_sub_matl_data = material.sub_matl_data
        if sub_matl_data is not None:
            setup_sub_matl_data_node_drivers(sub_matl_data)    

class SUB_OP_mat_drivers_refresh(Operator):
    bl_description = 'Rebuild material drivers so shader values follow animated material tracks'
    bl_idname = 'sub.mat_drivers_refresh'
    bl_label = 'Refresh Material Drivers'   

    def execute(self, context):
        refresh_material_drivers(context)
        return {'FINISHED'}  

class SUB_OP_mat_drivers_remove(Operator):
    bl_description = 'Remove the drivers connecting shaders to material animation tracks'
    bl_idname = 'sub.mat_drivers_remove'
    bl_label = 'Remove Material Drivers'

    def execute(self, context):
        remove_anim_material_drivers(context.object)
        return {'FINISHED'}  

class SUB_MT_vis_entry_context_menu(Menu):
    bl_label = "Vis Entry Specials"

    def draw(self, context):
        layout = self.layout
        layout.operator('sub.vis_drivers_refresh', icon='FILE_REFRESH', text='Refresh Visibility Drivers')
        layout.operator('sub.vis_drivers_remove', icon='X', text='Remove Visibility Drivers')
        layout.separator()
        layout.operator('sub.auto_fill_vis_entries', icon='SHADERFX', text='Autofill Visibility Entries')
        layout.operator('sub.insert_all_vis_entry_keyframes', icon='KEY_HLT', text='Insert Keyframes for All Entries')
        layout.separator()
        op = layout.operator(SUB_OP_purge_unused_vis_tracks.bl_idname, icon='BRUSH_DATA', text='Purge Unused (Current Animation)')
        op.scope = 'CURRENT'
        op = layout.operator(SUB_OP_purge_unused_vis_tracks.bl_idname, icon='TRASH', text='Purge Unused (All Animations)')
        op.scope = 'ALL'
        layout.separator()
        layout.operator('sub.organize_vis_entries_alphabetically', icon='SORTALPHA', text='Organize Alphabetically')
        layout.operator('sub.organize_vis_entries_by_move', icon='SORTSIZE', text='Organize by Move')
        layout.separator()
        layout.operator('sub.set_all_vis_entries_false', icon='HIDE_ON', text='Set All Entries Off')
        layout.operator('sub.set_all_vis_entries_true', icon='HIDE_OFF', text='Set All Entries On')
        
class SUB_MT_mat_entry_context_menu(Menu):
    bl_label = "Mat Entry Specials"

    def draw(self, context):
        layout = self.layout
        layout.operator(SUB_OP_mat_drivers_refresh.bl_idname, icon='FILE_REFRESH', text='Refresh Material Drivers')
        layout.operator(SUB_OP_mat_drivers_remove.bl_idname, icon='X', text='Remove Material Drivers')

class SUB_UL_vis_track_entries(UIList):
    def draw_item(self, _context, layout, _data, item, icon, active_data, _active_propname, index):
        # assert(isinstance(item, bpy.types.ShapeKey))
        obj = active_data
        # key = data
        entry = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            split = layout.split(factor=0.66, align=False)
            split.prop(entry, "name", text="", emboss=False, icon='HIDE_OFF')
            row = split.row(align=True)
            row.emboss = 'NONE_OR_STATUS'
            row.label(text="")
            icon = 'CHECKBOX_HLT' if entry.value == True else 'CHECKBOX_DEHLT'
            row.prop(entry, "value", text="", icon=icon, emboss=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)

class SUB_UL_mat_tracks(UIList):
    def draw_item(self, _context, layout, _data, item, icon, active_data, _active_propname, index):
        obj = active_data
        entry = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row()
            row.prop(entry, "name", text="", emboss=False, icon='MATERIAL')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)

class SUB_UL_mat_properties(UIList):
    def draw_item(self, _context, layout, _data, item, icon, active_data, _active_propname, index):
        obj = active_data
        entry = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row()
            row.prop(entry, "name", text="", emboss=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)

class SUB_UL_mat_property_values(UIList):
    def draw_item(self, _context, layout, _data, item, icon, active_data, _active_propname, index):
        obj = active_data
        entry = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row()
            if entry.sub_type == 'VECTOR':
                row.prop(entry, "custom_vector", text="", emboss=False)
            elif entry.sub_type == 'FLOAT':
                row.prop(entry, "custom_float", text="", emboss=False)
            elif entry.sub_type == 'BOOL':
                row.prop(entry, "custom_bool", text="", emboss=False)
            elif entry.sub_type == 'PATTERN':
                row.prop(entry, "pattern_index", text="", emboss=False)
            elif entry.sub_type == 'TEXTURE':
                row.prop(entry, "texture_transform", text="", emboss=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)        




def vis_track_name_update(self, context):
    sap = context.object.data.sub_anim_properties
    dupe = None
    for vt in sap.vis_track_entries:
        if vt.as_pointer() == self.as_pointer():
            continue
        if vt.name == self.name:
            dupe = vt
            break  
    if dupe is None:
        return
    regex = r"(\w+\.)(\d+)"
    matches = re.match(regex, self.name)
    if matches is None:
        self.name = self.name + '.001'
    else:
        base_name = matches.groups()[0]
        number = int(matches.groups()[1])
        self.name = f'{base_name}{number+1:003d}' 



def mat_track_prop_name_update(self, context):
    sap = context.object.data.sub_anim_properties
    found = False
    current_mat_track_index = None
    for mat_track_index, mat_track in enumerate(sap.mat_tracks):
        for property in mat_track.properties:
            if property.as_pointer() == self.as_pointer():
                current_mat_track_index = mat_track_index
                found = True
                break
        if found:
            break
    current_mat_track = sap.mat_tracks[current_mat_track_index]
    # There should be at most only one duplicate
    dupe = None
    for p in current_mat_track.properties:
        if p.as_pointer() == self.as_pointer():
            continue
        if p.name == self.name:
            dupe = p
            break
    # No duplicate found, name can remain as is
    if dupe is None:
        return
    # Regex match the name, see if it already has like '.001'
    # if it doesnt then add the '.001', otherwise increment the number
    regex = r"(\w+\.)(\d+)"
    matches = re.match(regex, self.name)
    if matches is None:
        self.name = self.name + '.001'
    else:
        base_name = matches.groups()[0]
        number = int(matches.groups()[1])
        self.name = f'{base_name}{number+1:003d}'

def mat_track_name_update(self, context):
    sap = context.object.data.sub_anim_properties
    dupe = None
    for mt in sap.mat_tracks:
        if mt.as_pointer() == self.as_pointer():
            continue
        if mt.name == self.name:
            dupe = mt
            break  
    if dupe is None:
        return
    regex = r"(\w+\.)(\d+)"
    matches = re.match(regex, self.name)
    if matches is None:
        self.name = self.name + '.001'
    else:
        base_name = matches.groups()[0]
        number = int(matches.groups()[1])
        self.name = f'{base_name}{number+1:003d}' 

def dummy_update(self, context):
    '''
    This is needed to force blender to update the driver values when updating via a modal.
    '''
    if not bpy.app.timers.is_registered(_style_visibility_soon):
        bpy.app.timers.register(_style_visibility_soon, first_interval=0.05)


def _style_visibility_soon():
    try:
        apply_dopesheet_key_colors()
        for obj in bpy.data.objects:
            if obj.type != 'ARMATURE':
                continue
            data_ad = getattr(obj.data, 'animation_data', None)
            sap_action = getattr(data_ad, 'action', None) if data_ad else None
            if sap_action is not None:
                style_visibility_action(sap_action)
            obj_ad = getattr(obj, 'animation_data', None)
            obj_action = getattr(obj_ad, 'action', None) if obj_ad else None
            if obj_action is not None:
                style_eye_control_action(obj_action)
            pose = getattr(obj, 'pose', None)
            eye_bone = pose.bones.get('BL_EyeLook') if pose is not None else None
            if eye_bone is not None:
                try:
                    # Only write when needed — assigning the same palette every
                    # call still dirties the depsgraph and restarts viewport samples.
                    if getattr(eye_bone.color, "palette", None) != "THEME03":
                        eye_bone.color.palette = "THEME03"
                    bone = getattr(eye_bone, "bone", None)
                    if bone is not None and getattr(bone.color, "palette", None) != "THEME03":
                        bone.color.palette = "THEME03"
                except Exception:
                    pass
    except Exception:
        pass
    return None


def vis_value_update(self, context):
    dummy_update(self, context)

class SUB_PG_vis_track_entry(PropertyGroup):
    name: StringProperty(
        name="Vis Name",
        default="Unknown",
        update=vis_track_name_update,)
    value: BoolProperty(name="Visible", default=False, update=vis_value_update)

class SUB_PG_mat_track_property(PropertyGroup):
    name: StringProperty(
        name="Property Name",
        default="Unknown",
        update=mat_track_prop_name_update,)
    sub_type: EnumProperty(
        name='Mat Track Entry Subtype',
        description='CustomVector or CustomFloat or CustomBool',
        items=mat_sub_types, 
        default='VECTOR',)
    custom_vector: FloatVectorProperty(name='Custom Vector', size=4, update=dummy_update, subtype='COLOR_GAMMA', soft_min=0.0, soft_max=1.0)
    custom_bool: BoolProperty(name='Custom Bool')
    custom_float: FloatProperty(name='Custom Float')
    pattern_index: IntProperty(name='Pattern Index', subtype='UNSIGNED')
    texture_transform: FloatVectorProperty(name='Texture Transform', size=5)

class SUB_PG_mat_track(PropertyGroup):
    name: StringProperty(
        name="Material Name",
        default="Unknown",
        update=mat_track_name_update,)
    properties: CollectionProperty(type=SUB_PG_mat_track_property)
    active_property_index: IntProperty(name='Active Mat Property Index', default=0, options={'HIDDEN'})

class SUB_PG_sub_anim_data(PropertyGroup):
    vis_track_entries: CollectionProperty(type=SUB_PG_vis_track_entry)
    active_vis_track_index: IntProperty(name='Active Vis Track Index', default=0, options={'HIDDEN'})
    mat_tracks: CollectionProperty(type=SUB_PG_mat_track)
    active_mat_track_index: IntProperty(name='Active Mat Track Index', default=0, options={'HIDDEN'})

# Auto-start system functions
def init_sap_auto_sync():
    """Initialize the SAP auto-sync system - called from main addon register"""
    print("SAP Auto-Sync system activated")
    print("✅ SAP handlers and timer registered for automatic synchronization")
    
    # Subscribe to action changes for more direct detection
    subscribe_to_action_changes()

def cleanup_sap_auto_sync():
    """Cleanup the SAP auto-sync system - called from main addon unregister"""
    # Stop the SAP monitor if running
    if SUB_OP_sap_sync_monitor._is_running and SUB_OP_sap_sync_monitor._timer:
        try:
            wm = bpy.context.window_manager
            if wm:
                wm.event_timer_remove(SUB_OP_sap_sync_monitor._timer)
            SUB_OP_sap_sync_monitor._timer = None
            SUB_OP_sap_sync_monitor._is_running = False
            print("SAP Monitor stopped")
        except:
            pass
    
    # Unsubscribe from action changes
    unsubscribe_from_action_changes()

def register():
    from .import_anim import register_folder_sync
    register_folder_sync()
    """Register only handlers and timers - classes are registered separately"""
    enabled = True
    try:
        scene = bpy.context.scene
        ssp = getattr(scene, "sub_scene_properties", None)
        if ssp is not None and hasattr(ssp, "sap_auto_sync_enabled"):
            enabled = bool(ssp.sap_auto_sync_enabled)
    except Exception:
        enabled = True
    set_sap_auto_sync_enabled(enabled)
    apply_dopesheet_key_colors()
    try:
        from .fcurve_compat import get_all_action_fcurves, is_ik_fk_fcurve, is_visibility_fcurve
        for obj in bpy.data.objects:
            if obj.type != 'ARMATURE':
                continue
            anim = getattr(obj.data, 'animation_data', None)
            action = getattr(anim, 'action', None) if anim else None
            if action is None:
                continue
            for fcurve in get_all_action_fcurves(action, id_type='ARMATURE'):
                if (is_visibility_fcurve(fcurve) or is_ik_fk_fcurve(fcurve)) and fcurve.hide:
                    fcurve.hide = False
    except Exception:
        pass
    if enabled and not bpy.app.timers.is_registered(_style_visibility_soon):
        bpy.app.timers.register(_style_visibility_soon, first_interval=0.2)
    

    """
    for name, obj in inspect.getmembers(
        sys.modules[__name__], 
        lambda member: inspect.isclass(member) and member.__module__ == __name__ and issubclass(member, bpy.types.bpy_struct)):
        print(f"{name}, {obj}")
        bpy.utils.register_class()
    """

def unregister():
    from .import_anim import unregister_folder_sync
    unregister_folder_sync()
    """Unregister only handlers and timers - classes are unregistered separately"""
    set_sap_auto_sync_enabled(False)
    
    # Unregister the timer
    if bpy.app.timers.is_registered(_style_visibility_soon):
        bpy.app.timers.unregister(_style_visibility_soon)
    
    # Clear the stored actions
    global _last_known_actions
    _last_known_actions.clear()
    
    restore_dopesheet_key_colors()
    
if __name__ == '__main__':
    register()
