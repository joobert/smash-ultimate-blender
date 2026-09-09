"""
Retargeting module - Integration of expy_kit into Smash Ultimate Blender Tools
This module provides 1:1 integration of expy_kit retargeting tools.
Panels are always visible and will guide the user to enter POSE mode when necessary.
All functionality is consolidated in the 'Retargeting' section of the 'Ultimate' tab.
"""
import bpy
import os
from bpy.types import Panel
from bpy.app.handlers import persistent

# Import expy_kit modules using relative imports
# expy_kit is at the plugin root level, so we go up two levels from source/retargeting/
from ...expy_kit import operators, properties, preferences, ui, preset_handler
from ..blender_compat import assign_action, set_pose_bone_select
from . import guided


# Auto-detection for Smash armatures
_last_detected_armature = None
_last_active_armature = None


def ensure_armature_preset_tracked(armature_obj):
    """Backfill active_preset for armatures that already have settings loaded."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return

    settings = armature_obj.data.expykit_retarget
    if settings.active_preset or not settings.has_settings():
        return

    if "smush_blender_import" in armature_obj.name.lower():
        settings.active_preset = 'Smash.py'


def get_preset_display_label(armature_obj):
    """Return the preset dropdown label for a specific armature."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return "Retarget Presets"

    ensure_armature_preset_tracked(armature_obj)
    settings = armature_obj.data.expykit_retarget

    if settings.active_preset:
        from os.path import splitext, basename
        return bpy.path.display_name(splitext(basename(settings.active_preset))[0], title_case=False)
    if settings.has_settings():
        return "-- Current Settings --"
    return "Retarget Presets"


def sync_preset_menu_label(context):
    """Keep the preset menu class label aligned with the active armature."""
    label = get_preset_display_label(context.object)
    ULTIMATE_MT_retarget_presets.bl_label = label


def set_armature_active_preset(armature_obj, preset_filepath):
    """Store which preset file is active for an armature."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return
    from os.path import basename
    armature_obj.data.expykit_retarget.active_preset = basename(preset_filepath)

def load_custom_bones_from_preset(preset_path):
    """Parse a preset file and extract custom bone definitions"""
    import os
    import re
    
    custom_bones = {}
    
    if not os.path.exists(preset_path):
        return custom_bones
    
    try:
        with open(preset_path, 'r') as f:
            content = f.read()
            
        # Look for lines like: skeleton.custom.identifier = 'BoneName'
        pattern = r"skeleton\.custom\.(\w+)\s*=\s*['\"]([^'\"]*)['\"]"
        matches = re.findall(pattern, content)
        
        for identifier, bone_name in matches:
            if identifier != 'name' and bone_name:  # Skip the legacy 'name' property
                custom_bones[identifier] = bone_name
    except Exception as e:
        print(f"Error parsing preset for custom bones: {e}")
    
    return custom_bones


def ensure_custom_bones_exist(skeleton, custom_bones_dict):
    """Ensure custom bone properties exist before setting them"""
    for identifier, bone_name in custom_bones_dict.items():
        if not hasattr(skeleton.custom, identifier):
            # Create the property using add_bone
            skeleton.custom.add_bone(identifier, bone_name)
        else:
            # Property exists, just set the value
            setattr(skeleton.custom, identifier, bone_name)


def load_preset_with_custom_bones(preset_name, armature_obj=None):
    """Load a preset and properly handle custom bones"""
    if armature_obj is None:
        armature_obj = bpy.context.object
    
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False
    
    try:
        # Get the preset path
        import os
        preset_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'expy_kit', 'rig_mapping', 'presets')
        preset_path = os.path.join(preset_dir, preset_name)
        
        # Parse custom bones from preset
        custom_bones = load_custom_bones_from_preset(preset_path)
        
        # Get skeleton
        skeleton = armature_obj.data.expykit_retarget
        
        # Pre-create custom bone properties
        if custom_bones:
            ensure_custom_bones_exist(skeleton, custom_bones)
        
        # Now load the preset normally
        preset_handler.set_preset_skel(preset_name, validate=True)
        set_armature_active_preset(armature_obj, preset_name)
        
        # Force UI refresh
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        
        return True
    except Exception as e:
        print(f"Error loading preset with custom bones: {e}")
        return False


def check_and_load_smash_preset(armature_obj):
    """Check if an armature is a smash rig and load preset if needed"""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False
    
    armature_name = armature_obj.name
    if "smush_blender_import" not in armature_name.lower():
        return False
    
    # Check if settings are already loaded
    if armature_obj.data.expykit_retarget.has_settings():
        ensure_armature_preset_tracked(armature_obj)
        return False  # Already has settings
    
    # Load Smash preset with proper custom bone handling
    try:
        # Temporarily set as active to load preset
        original_active = bpy.context.view_layer.objects.active
        bpy.context.view_layer.objects.active = armature_obj
        
        if load_preset_with_custom_bones('Smash.py', armature_obj):
            set_armature_active_preset(armature_obj, 'Smash.py')
            print(f"Auto-loaded Smash preset for armature: {armature_name}")
            
            # Restore original active object
            if original_active:
                bpy.context.view_layer.objects.active = original_active
            return True
    except Exception as e:
        print(f"Failed to auto-load Smash preset for {armature_name}: {e}")
    
    return False


@persistent
def auto_detect_smash_armature(scene):
    """Auto-load Smash preset when a smush_blender_import armature is selected"""
    global _last_detected_armature, _last_active_armature
    
    try:
        ob = bpy.context.object
        current_name = ob.name if ob and ob.type == 'ARMATURE' else None

        if current_name != _last_active_armature:
            _last_active_armature = current_name
            if ob and ob.type == 'ARMATURE':
                ensure_armature_preset_tracked(ob)
            sync_preset_menu_label(bpy.context)

        # Check active object
        if ob and ob.type == 'ARMATURE':
            armature_name = ob.name
            
            # Only process if we haven't already processed this armature
            if _last_detected_armature != armature_name:
                if check_and_load_smash_preset(ob):
                    _last_detected_armature = armature_name
        
        # Also check the "Bind To" target if set
        if hasattr(bpy.context.scene, 'expykit_bind_to') and bpy.context.scene.expykit_bind_to:
            target_armature = bpy.context.scene.expykit_bind_to
            if target_armature and target_armature.type == 'ARMATURE':
                check_and_load_smash_preset(target_armature)
    
    except Exception as e:
        # Silently fail to avoid spamming console
        pass


# Override preset execution to handle custom bones properly
class ULTIMATE_OT_execute_preset_retarget(bpy.types.Operator):
    """Apply a Bone Retarget Preset with Custom Bone Support"""
    bl_idname = "object.ultimate_armature_preset_apply"
    bl_label = "Apply Bone Retarget Preset"

    filepath: bpy.props.StringProperty(
        subtype='FILE_PATH',
        options={'SKIP_SAVE'},
    )
    menu_idname: bpy.props.StringProperty(
        name="Menu ID Name",
        description="ID name of the menu this was called from",
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        from os.path import basename, splitext
        filepath = self.filepath

        # change the menu title to the most recently chosen option
        preset_class = ui.VIEW3D_MT_retarget_presets
        preset_class.bl_label = bpy.path.display_name(basename(filepath), title_case=False)

        ext = splitext(filepath)[1].lower()

        if ext not in {".py", ".xml"}:
            self.report({'ERROR'}, "Unknown file type: %r" % ext)
            return {'CANCELLED'}

        if hasattr(preset_class, "reset_cb"):
            preset_class.reset_cb(context)

        if ext == ".py":
            try:
                # PRE-PROCESS: Extract and create custom bone properties
                custom_bones = load_custom_bones_from_preset(filepath)
                if custom_bones and context.object and context.object.type == 'ARMATURE':
                    skeleton = context.object.data.expykit_retarget
                    ensure_custom_bones_exist(skeleton, custom_bones)
                
                # Execute the preset file
                bpy.utils.execfile(filepath)
            except Exception as ex:
                self.report({'ERROR'}, "Failed to execute the preset: " + repr(ex))

        elif ext == ".xml":
            # Blender 5.0 made the bundled rna_xml module private by renaming
            # it to _rna_xml. Same module, same API.
            try:
                import rna_xml
            except ModuleNotFoundError:
                import _rna_xml as rna_xml
            rna_xml.xml_file_run(context,
                                 filepath,
                                 preset_class.preset_xml_map)

        if hasattr(preset_class, "post_cb"):
            preset_class.post_cb(context)

        preset_handler.validate_preset(context.object.data)

        settings = context.object.data.expykit_retarget
        guided.migrate_throw_from_custom(settings)
        preset_handler.reset_preset_names(settings)

        set_armature_active_preset(context.object, filepath)
        
        # Force UI refresh to show custom bones
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        return {'FINISHED'}


# Custom preset menu that uses our custom operator
class ULTIMATE_MT_retarget_presets(ui.VIEW3D_MT_retarget_presets):
    """Retarget presets menu with custom bone support"""
    bl_label = "Retarget Presets"  # Required for preset deletion to work
    preset_operator = "object.ultimate_armature_preset_apply"  # Use our custom operator


# Custom preset add/remove operator that properly references our custom menu
# Inherits from AddPresetBase to get proper preset saving behavior
from bl_operators.presets import AddPresetBase

class ULTIMATE_OT_add_preset_retarget(AddPresetBase, bpy.types.Operator):
    """Add or Remove a Bone Retarget Preset"""
    bl_idname = "object.ultimate_armature_preset_add"
    bl_label = "Add Bone Retarget Preset"
    
    # Point to OUR custom menu class (this fixes the bl_label error on delete)
    preset_menu = "ULTIMATE_MT_retarget_presets"
    
    overwrite: bpy.props.BoolProperty(
        name="Overwrite Existing",
        description="Replace the preset file if it already exists",
        default=True,
    )
    
    # Same preset_defines and preset_values as original
    preset_defines = [
        "skeleton = bpy.context.object.data.expykit_retarget"
    ]
    
    preset_values = [
        "skeleton.face",
        "skeleton.spine",
        "skeleton.right_arm",
        "skeleton.left_arm",
        "skeleton.right_leg",
        "skeleton.left_leg",
        "skeleton.left_fingers",
        "skeleton.right_fingers",
        "skeleton.right_arm_ik",
        "skeleton.left_arm_ik",
        "skeleton.right_leg_ik",
        "skeleton.left_leg_ik",
        "skeleton.custom",
        "skeleton.custom.name",
        "skeleton.root",
        "skeleton.throw",
        "skeleton.deform_preset"
    ]
    
    # Same preset directory as original expy_kit (armature/retarget)
    preset_subdir = os.path.join("armature", "retarget")

    def pre_cb(self, context):
        if context.object and context.object.type == 'ARMATURE':
            context.object.data.expykit_retarget.custom.sync_all_dynamic_props()

    def invoke(self, context, event):
        if self.remove_active or self.remove_name:
            preset_label = get_preset_display_label(context.object)
            if preset_label in ("Retarget Presets", "-- Current Settings --"):
                self.report({'WARNING'}, "No preset file selected to remove")
                return {'CANCELLED'}
            return context.window_manager.invoke_confirm(
                self,
                event,
                title=f'Delete preset "{preset_label}"?',
                confirm_text="Delete",
                icon='ERROR',
            )

        if context.object and context.object.type == 'ARMATURE':
            preset = context.object.data.expykit_retarget.active_preset
            if preset:
                from os.path import splitext, basename
                self.name = bpy.path.display_name(splitext(basename(preset))[0], title_case=False)
            elif ULTIMATE_MT_retarget_presets.bl_label not in ("Retarget Presets", "-- Current Settings --"):
                self.name = ULTIMATE_MT_retarget_presets.bl_label

        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "name")
        self.layout.prop(self, "overwrite")

    def execute(self, context):
        import os
        from bl_operators.presets import AddPresetBase, _is_path_readonly

        if hasattr(self, "pre_cb"):
            self.pre_cb(context)

        preset_menu_class = getattr(bpy.types, self.preset_menu)
        is_preset_add = not (self.remove_name or self.remove_active)

        if is_preset_add:
            name = self.name.strip()
            if not name:
                return {'FINISHED'}

            filename = AddPresetBase.as_filename(name)
            target_path = os.path.join("presets", self.preset_subdir)
            target_path = bpy.utils.user_resource('SCRIPTS', path=target_path, create=True)

            if not target_path:
                self.report({'WARNING'}, "Failed to create presets path")
                return {'CANCELLED'}

            preset_filepath = bpy.utils.preset_find(filename, self.preset_subdir, ext=".py")

            if _is_path_readonly(target_path) or (preset_filepath and not self.overwrite):
                self.report({'WARNING'}, f"Cannot create preset \"{name}\", as the name already exists")
                return {'CANCELLED'}

            filepath = preset_filepath or os.path.join(target_path, filename) + ".py"

            try:
                self._write_preset_file(filepath)
            except Exception as ex:
                self.report({'ERROR'}, f"Failed to write preset: {ex!r}")
                return {'CANCELLED'}

            preset_menu_class.bl_label = bpy.path.display_name(filename)
            set_armature_active_preset(context.object, os.path.basename(filepath))
            self.report({'INFO'}, f"Saved preset: {filename}")
            return {'FINISHED'}

        return AddPresetBase.execute(self, context)

    def _write_preset_file(self, filepath):
        import os
        from bl_operators.presets import AddPresetBase

        preset_menu_class = getattr(bpy.types, self.preset_menu)

        def rna_recursive_attr_expand(value, rna_path_step, level):
            if isinstance(value, bpy.types.PropertyGroup):
                properties_skip = {"rna_type"}
                for sub_value_attr in value.bl_rna.properties.keys():
                    if sub_value_attr in properties_skip:
                        continue
                    properties_skip.add(sub_value_attr)
                    sub_value = getattr(value, sub_value_attr)
                    rna_recursive_attr_expand(
                        sub_value,
                        f"{rna_path_step}.{sub_value_attr}",
                        level,
                    )
            elif type(value).__name__ == "bpy_prop_collection_idprop":
                file_preset.write(f"{rna_path_step}.clear()\n")
                for sub_value in value:
                    file_preset.write(f"item_sub_{level} = {rna_path_step}.add()\n")
                    rna_recursive_attr_expand(sub_value, f"item_sub_{level}", level + 1)
            else:
                try:
                    value = value[:]
                except Exception:
                    pass
                file_preset.write(f"{rna_path_step} = {value!r}\n")

        with open(filepath, "w", encoding="utf-8") as file_preset:
            file_preset.write("import bpy\n")

            namespace_globals = {"bpy": bpy}
            namespace_locals = {}

            for rna_path in self.preset_defines:
                exec(rna_path, namespace_globals, namespace_locals)
                file_preset.write(f"{rna_path}\n")
            file_preset.write("\n")

            for rna_path in self.preset_values:
                value = eval(rna_path, namespace_globals, namespace_locals)
                rna_recursive_attr_expand(value, rna_path, 1)

        preset_menu_class.bl_label = bpy.path.display_name(os.path.splitext(os.path.basename(filepath))[0])


class ULTIMATE_OT_map_bones_by_proximity(bpy.types.Operator):
    """Map bones whose heads are close between the active and reference armatures"""
    bl_idname = "object.ultimate_map_bones_by_proximity"
    bl_label = "Map Bones by Proximity"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.object or context.object.type != 'ARMATURE':
            return False
        ref = getattr(context.scene, 'expykit_nearest_bone_ref', None)
        return ref and ref.type == 'ARMATURE' and ref != context.object

    def execute(self, context):
        from .nearest_bone_mapper import (
            _compute_proximity_threshold,
            map_bones_by_proximity,
        )

        target = context.object
        reference = context.scene.expykit_nearest_bone_ref
        radius = getattr(context.scene, 'expykit_map_radius', 1.0)

        mapped_count, custom_count = map_bones_by_proximity(
            reference, target, radius_scale=radius
        )
        if mapped_count == 0 and custom_count == 0:
            threshold = _compute_proximity_threshold(reference, target, radius_scale=radius)
            self.report(
                {'WARNING'},
                f"No close bone pairs found (match distance {threshold:.3f}). "
                "Raise Match Radius or make sure both armatures overlap.",
            )
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            f"Mapped {mapped_count} preset bones and {custom_count} custom bones from '{reference.name}'",
        )
        return {'FINISHED'}


# We'll create a REPLACEMENT operator instead of patching
_original_constrain_class = None


def _bind_hint(column, explain, text):
    if explain:
        hint = column.row()
        hint.scale_y = 0.8
        hint.label(text=text, icon='INFO')


_BIND_SYNC = (
    ('src_preset', 'expykit_src_preset'),
    ('trg_preset', 'expykit_trg_preset'),
    ('match_transform', 'expykit_match_transform'),
    ('match_object_transform', 'expykit_match_object_transform'),
    ('fit_target_scale', 'expykit_fit_target_scale'),
    ('adjust_location', 'expykit_adjust_location'),
    ('loc_constraints', 'expykit_loc_constraints'),
    ('rot_constraints', 'expykit_rot_constraints'),
    ('scale_constraints', 'expykit_scale_constraints'),
    ('bind_floating', 'expykit_bind_floating'),
    ('math_look_at', 'expykit_math_look_at'),
    ('copy_IK_roll_hands', 'expykit_copy_IK_roll_hands'),
    ('copy_IK_roll_feet', 'expykit_copy_IK_roll_feet'),
    ('constraint_policy', 'expykit_constraint_policy'),
    ('only_selected', 'expykit_only_selected'),
    ('bind_by_name', 'expykit_bind_by_name'),
    ('name_prefix', 'expykit_name_prefix'),
    ('name_replace', 'expykit_name_replace'),
    ('name_replace_with', 'expykit_name_replace_with'),
    ('name_suffix', 'expykit_name_suffix'),
    ('constrain_root', 'expykit_constrain_root'),
    ('root_motion_bone', 'expykit_root_motion_bone'),
    ('no_finger_loc', 'expykit_no_finger_loc'),
    ('root_cp_loc_x', 'expykit_root_cp_loc_x'),
    ('root_cp_loc_y', 'expykit_root_cp_loc_y'),
    ('root_cp_loc_z', 'expykit_root_cp_loc_z'),
    ('root_cp_rot_x', 'expykit_root_cp_rot_x'),
    ('root_cp_rot_y', 'expykit_root_cp_rot_y'),
    ('root_cp_rot_z', 'expykit_root_cp_rot_z'),
    ('root_use_loc_min_x', 'expykit_root_use_loc_min_x'),
    ('root_use_loc_min_y', 'expykit_root_use_loc_min_y'),
    ('root_use_loc_min_z', 'expykit_root_use_loc_min_z'),
    ('root_loc_min_x', 'expykit_root_loc_min_x'),
    ('root_loc_min_y', 'expykit_root_loc_min_y'),
    ('root_loc_min_z', 'expykit_root_loc_min_z'),
    ('root_use_loc_max_x', 'expykit_root_use_loc_max_x'),
    ('root_use_loc_max_y', 'expykit_root_use_loc_max_y'),
    ('root_use_loc_max_z', 'expykit_root_use_loc_max_z'),
    ('root_loc_max_x', 'expykit_root_loc_max_x'),
    ('root_loc_max_y', 'expykit_root_loc_max_y'),
    ('root_loc_max_z', 'expykit_root_loc_max_z'),
    ('copy_scale', 'expykit_root_copy_scale'),
    ('ret_bones_collection', 'expykit_ret_bones_collection'),
)


def _copy_scene_to_operator(op, scene):
    syncing = getattr(guided, '_SYNCING_BIND_PROPS', False)
    guided._SYNCING_BIND_PROPS = True
    try:
        for op_attr, scene_attr in _BIND_SYNC:
            if not hasattr(scene, scene_attr):
                continue
            try:
                setattr(op, op_attr, getattr(scene, scene_attr))
            except Exception:
                pass
    finally:
        guided._SYNCING_BIND_PROPS = syncing


_ROOT_STAMP_ATTRS = (
    'constrain_root',
    'root_motion_bone',
    'root_cp_loc_x',
    'root_cp_loc_y',
    'root_cp_loc_z',
    'root_cp_rot_x',
    'root_cp_rot_y',
    'root_cp_rot_z',
    'root_use_loc_min_z',
)


def _mapped_root_bone(armature):
    """Root slot from an armature's retarget mapping, or '' if unset."""
    if armature is None or getattr(armature, 'type', None) != 'ARMATURE':
        return ""
    try:
        return (armature.data.expykit_retarget.root or "").strip()
    except Exception:
        return ""


def _bind_reference_armature(context, scene=None, constrained=None):
    """Armature constraints reference (Bind To) — not the one receiving constraints."""
    scene = scene or getattr(context, 'scene', None)
    bind_to = getattr(scene, 'expykit_bind_to', None) if scene is not None else None
    if bind_to is not None and getattr(bind_to, 'type', None) == 'ARMATURE':
        return bind_to
    active = getattr(context, 'active_object', None)
    if (
        active is not None
        and getattr(active, 'type', None) == 'ARMATURE'
        and active is not constrained
    ):
        return active
    return None


def _sync_root_motion_from_reference(op, scene, reference):
    """Force Root Animation bone from the Bind To mapping Root slot.

    That armature is the one constraints point at (not the constrained model).
    Always overwrite last-used operator values; leave empty when Root is unset.
    """
    bone = _mapped_root_bone(reference)
    try:
        op.root_motion_bone = bone
    except Exception:
        pass
    if scene is not None and hasattr(scene, 'expykit_root_motion_bone'):
        scene.expykit_root_motion_bone = bone
    return bone


def _apply_smash_root_defaults(op, scene=None, source=None):
    """The redo HUD reads operator RNA / last-used values, not the N-panel.

    ExpyKit stored constrain_root as No Root. Smash bind needs Bone mode.
    Root motion bone is handled by ``_sync_root_motion_from_reference``.
    """
    del source
    uninitialized = getattr(op, 'constrain_root', 'None') in {'None', ''}
    if uninitialized:
        op.constrain_root = 'Bone'
        op.root_cp_loc_x = True
        op.root_cp_loc_y = True
        op.root_cp_loc_z = True
        op.root_cp_rot_x = True
        op.root_cp_rot_y = True
        op.root_cp_rot_z = True
        op.root_use_loc_min_z = True
        if scene is not None:
            scene.expykit_constrain_root = 'Bone'
            if hasattr(scene, 'expykit_root_cp_rot_z'):
                scene.expykit_root_cp_rot_z = True
            if hasattr(scene, 'expykit_root_use_loc_min_z'):
                scene.expykit_root_use_loc_min_z = True


def _stamp_last_bind_props(context, op):
    wm = getattr(context, 'window_manager', None)
    if wm is None or not hasattr(wm, 'operator_properties_last'):
        return
    try:
        last = wm.operator_properties_last('armature.expykit_constrain_to_armature')
    except Exception:
        last = None
    if last is None:
        return
    for attr in _ROOT_STAMP_ATTRS:
        if hasattr(op, attr) and hasattr(last, attr):
            try:
                setattr(last, attr, getattr(op, attr))
            except Exception:
                pass


def _copy_operator_to_scene(op, scene):
    syncing = getattr(guided, '_SYNCING_BIND_PROPS', False)
    guided._SYNCING_BIND_PROPS = True
    try:
        for op_attr, scene_attr in _BIND_SYNC:
            if not hasattr(scene, scene_attr):
                continue
            try:
                setattr(scene, scene_attr, getattr(op, op_attr))
            except Exception:
                pass
    finally:
        guided._SYNCING_BIND_PROPS = syncing


def draw_binded_settings_ui(layout, context, show_presets=True, explain=False):
    """Shared drawing function for BOTH the Binded Settings panel AND the Bind to Active Armature dialog.
    This ensures they are literally the SAME UI editing the SAME properties."""
    scene = context.scene
    column = layout.column()
    
    _prop_indent = 0.15

    if explain:
        box = column.box()
        box.label(text="Set how the source armature drives the target, then click OK.", icon='HELP')
        column.separator()
    
    # Presets section
    if show_presets:
        row = column.row()
        row.prop(scene, 'expykit_src_preset', text="To Bind")
        _bind_hint(column, explain, "Preset used by the animated source armature.")
        
        row = column.row()
        row.prop(scene, 'expykit_trg_preset', text="Bind To")
        _bind_hint(column, explain, "Preset used by the target armature you are retargeting onto.")
        
        column.separator()

    # Conversion section
    row = column.row()
    row.label(text='Conversion')
    _bind_hint(column, explain, "How rest poses are aligned before constraints are added.")

    row = column.split(factor=_prop_indent, align=True)
    row.separator()
    col = row.column()
    col.prop(scene, 'expykit_match_transform', text='')
    col.prop(scene, 'expykit_match_object_transform')
    col.prop(scene, 'expykit_fit_target_scale')
    if scene.expykit_fit_target_scale != "--":
        col.prop(scene, 'expykit_adjust_location')

    if not scene.expykit_loc_constraints and scene.expykit_match_transform == 'Bone':
        col.label(text="'Copy Location' might be required", icon='ERROR')
    elif scene.expykit_fit_target_scale == '--' and scene.expykit_match_transform == 'Pose':
        col.label(text="'Fit height' might improve results", icon='ERROR')
    else:
        col.separator()

    # Constraints section
    column.separator()
    row = column.row()
    row.label(text='Constraints')
    _bind_hint(column, explain, "Copy Rotation is the usual Smash setup. Enable Copy Location only if bones should follow position too.")

    row = column.split(factor=_prop_indent, align=True)
    row.separator()

    constr_col = row.column()
    
    copy_loc_row = constr_col.row()
    copy_loc_row.prop(scene, 'expykit_loc_constraints')
    if scene.expykit_loc_constraints:
        copy_loc_row.prop(scene, 'expykit_no_finger_loc', text="Except Fingers")
    else:
        copy_loc_row.prop(scene, 'expykit_bind_floating', text="Only Floating")
    
    copy_rot_row = constr_col.row()
    copy_rot_row.prop(scene, 'expykit_rot_constraints')
    copy_rot_row.prop(scene, 'expykit_math_look_at')
    
    copy_scale_row = constr_col.row()
    copy_scale_row.prop(scene, 'expykit_scale_constraints')

    ik_aim_row = constr_col.row()
    ik_aim_row.prop(scene, 'expykit_copy_IK_roll_hands')
    ik_aim_row.prop(scene, 'expykit_copy_IK_roll_feet')

    constr_col.prop(scene, 'expykit_constraint_policy', text='')
    
    # Affect Bones section
    column.separator()
    row = column.row()
    row.label(text="Affect Bones")
    _bind_hint(column, explain, "Leave Only Selected off to bind the whole mapped skeleton. Also by Name matches leftover bones with the same name.")
    
    row = column.split(factor=_prop_indent, align=True)
    row.separator()
    col = row.column()
    col.prop(scene, 'expykit_only_selected')
    row.prop(scene, 'expykit_bind_by_name', text="Also by Name")
    if scene.expykit_bind_by_name:
        row = column.row()
        col = row.column()
        col.label(text="Prefix")
        col.prop(scene, 'expykit_name_prefix', text="")

        col = row.column()
        col.label(text="Replace:")
        col.prop(scene, 'expykit_name_replace', text="")

        col = row.column()
        col.label(text="With:")
        col.prop(scene, 'expykit_name_replace_with', text="")

        col = row.column()
        col.label(text="Suffix:")
        col.prop(scene, 'expykit_name_suffix', text="")

    # Root Animation section
    column.separator()
    row = column.row()
    row.label(text="Root Animation")
    _bind_hint(column, explain, "Bone uses the Root slot from the Bind To mapping. Leave empty if Root is unset.")
    row = column.split(factor=_prop_indent, align=True)
    row.separator()
    row.prop(scene, 'expykit_constrain_root', text="")

    if scene.expykit_constrain_root != 'None':
        row = column.split(factor=_prop_indent, align=True)
        row.label(text="")
        if context.active_object and context.active_object.type == 'ARMATURE':
            row.prop_search(scene, 'expykit_root_motion_bone',
                            context.active_object.data,
                            "bones", text="")

    if scene.expykit_constrain_root != 'None':
        row = column.row(align=True)
        row.label(text="Location")
        row.prop(scene, "expykit_root_cp_loc_x", text="X", toggle=True)
        row.prop(scene, "expykit_root_cp_loc_y", text="Y", toggle=True)
        row.prop(scene, "expykit_root_cp_loc_z", text="Z", toggle=True)

        if any((scene.expykit_root_cp_loc_x, scene.expykit_root_cp_loc_y, scene.expykit_root_cp_loc_z)):
            column.separator()

            if scene.expykit_root_cp_loc_x:
                row = column.row(align=True)
                row.prop(scene, "expykit_root_use_loc_min_x", text="Min X")
                subcol = row.column()
                subcol.prop(scene, "expykit_root_loc_min_x", text="")
                subcol.enabled = scene.expykit_root_use_loc_min_x
                row.separator()
                row.prop(scene, "expykit_root_use_loc_max_x", text="Max X")
                subcol = row.column()
                subcol.prop(scene, "expykit_root_loc_max_x", text="")
                subcol.enabled = scene.expykit_root_use_loc_max_x
                row.enabled = scene.expykit_root_cp_loc_x

            if scene.expykit_root_cp_loc_y:
                row = column.row(align=True)
                row.prop(scene, "expykit_root_use_loc_min_y", text="Min Y")
                subcol = row.column()
                subcol.prop(scene, "expykit_root_loc_min_y", text="")
                subcol.enabled = scene.expykit_root_use_loc_min_y
                row.separator()
                row.prop(scene, "expykit_root_use_loc_max_y", text="Max Y")
                subcol = row.column()
                subcol.prop(scene, "expykit_root_loc_max_y", text="")
                subcol.enabled = scene.expykit_root_use_loc_max_y
                row.enabled = scene.expykit_root_cp_loc_y

            if scene.expykit_root_cp_loc_z:
                row = column.row(align=True)
                row.prop(scene, "expykit_root_use_loc_min_z", text="Min Z")
                subcol = row.column()
                subcol.prop(scene, "expykit_root_loc_min_z", text="")
                subcol.enabled = scene.expykit_root_use_loc_min_z
                row.separator()
                row.prop(scene, "expykit_root_use_loc_max_z", text="Max Z")
                subcol = row.column()
                subcol.prop(scene, "expykit_root_loc_max_z", text="")
                subcol.enabled = scene.expykit_root_use_loc_max_z
                row.enabled = scene.expykit_root_cp_loc_z

            column.separator()

        row = column.row(align=True)
        row.label(text="Rotation")
        row.prop(scene, "expykit_root_cp_rot_x", text="X", toggle=True)
        row.prop(scene, "expykit_root_cp_rot_y", text="Y", toggle=True)
        row.prop(scene, "expykit_root_cp_rot_z", text="Z", toggle=True)

        row = column.row()
        row.prop(scene, "expykit_root_copy_scale")

        column.separator()

    column.separator()
    row = column.row()
    row.prop(scene, 'expykit_ret_bones_collection', text="Layer")


class ULTIMATE_OT_constrain_to_armature(operators.ConstrainToArmature):
    """REPLACEMENT for ConstrainToArmature that uses scene properties.
    Same bl_idname as the original. Inherits bind helpers so execute can run."""
    from_scene: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    # Prevent Blender from restoring a stale last-used "Trans" into the dialog.
    root_motion_bone: bpy.props.StringProperty(
        name="Root Motion",
        description="Root bone from the Bind To armature mapping (constraints reference this model)",
        default="",
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        scene = context.scene
        source, target = guided.bound_pair(scene)
        if getattr(scene, 'expykit_bind_is_active', False) and source and target:
            return True
        if len(context.selected_objects) != 2:
            return False
        if context.mode != 'POSE':
            return False
        for ob in context.selected_objects:
            if ob.type != 'ARMATURE':
                return False
        return True

    def invoke(self, context, event):
        scene = context.scene
        
        # Auto-detect and load presets
        try:
            to_bind = None
            for ob in context.selected_objects:
                if ob != context.active_object and ob.type == 'ARMATURE':
                    to_bind = ob
                    break
            if to_bind:
                check_and_load_smash_preset(to_bind)
            if context.active_object and context.active_object.type == 'ARMATURE':
                check_and_load_smash_preset(context.active_object)
        except Exception as e:
            print(f"Auto-detection failed: {e}")
        
        # Set presets in scene based on armature settings
        try:
            to_bind = next(ob for ob in context.selected_objects if ob != context.active_object)
            if to_bind.data.expykit_retarget.has_settings():
                scene.expykit_src_preset = '--Current--'
            if context.active_object.data.expykit_retarget.has_settings():
                scene.expykit_trg_preset = '--Current--'
        except:
            pass

        constrained = next((ob for ob in context.selected_objects if ob != context.active_object), None)
        reference = _bind_reference_armature(context, scene, constrained=constrained)
        first_bind = not bool(getattr(scene, 'expykit_bind_is_active', False))
        if first_bind:
            scene.expykit_constrain_root = 'Bone'
            scene.expykit_root_cp_loc_x = True
            scene.expykit_root_cp_loc_y = True
            scene.expykit_root_cp_loc_z = True
            scene.expykit_root_cp_rot_x = True
            scene.expykit_root_cp_rot_y = True
            scene.expykit_root_cp_rot_z = True
            if hasattr(scene, 'expykit_root_use_loc_min_z'):
                scene.expykit_root_use_loc_min_z = True
        # Root Animation bone = Bind To mapping Root (the armature constraints
        # reference). Never the constrained model's Root / stale "Trans".
        guided.apply_root_bind_defaults(
            scene, constrained, reference, force_root_bone=True
        )
        _copy_scene_to_operator(self, scene)
        _apply_smash_root_defaults(self, scene)
        _sync_root_motion_from_reference(self, scene, reference)
        _stamp_last_bind_props(context, self)

        if self.force_dialog:
            return context.window_manager.invoke_props_dialog(self, width=480)
        if event is not None:
            return context.window_manager.invoke_props_popup(self, event)
        return self.execute(context)

    def check(self, context):
        return True

    def draw(self, context):
        """Draw operator properties like the original Bind to Active Armature redo panel."""
        saved = self.force_dialog
        self.force_dialog = False
        try:
            super().draw(context)
        finally:
            self.force_dialog = saved

    def execute(self, context):
        scene = context.scene
        rebind = bool(self.from_scene or getattr(guided, '_REBIND_BUSY', False))
        source = next((ob for ob in context.selected_objects if ob != context.active_object), None)
        target = getattr(scene, 'expykit_bind_to', None) or context.active_object
        if not source or not target or getattr(source, 'type', None) != 'ARMATURE':
            bound_source, bound_target = guided.bound_pair(scene)
            if bound_source and bound_target:
                source, target = bound_source, bound_target
        if rebind:
            _copy_scene_to_operator(self, scene)
        else:
            _apply_smash_root_defaults(self, scene)
            _sync_root_motion_from_reference(self, scene, target)
        _stamp_last_bind_props(context, self)

        if not source or not target or getattr(source, 'type', None) != 'ARMATURE':
            bound_source, bound_target = guided.bound_pair(scene)
            if bound_source and bound_target:
                source, target = bound_source, bound_target
                guided.prepare_bind_selection(context, source, target)
        result = super().execute(context)

        if result and 'CANCELLED' not in result:
            if not getattr(guided, '_REBIND_BUSY', False):
                _copy_operator_to_scene(self, scene)
            _stamp_last_bind_props(context, self)
            # Expy Kit calls the active ``Bind To`` object the target, but it
            # creates the constraints on the other armature. Preserve that pair
            # explicitly and leave the constrained model selected for the user.
            driver = guided.bind_keep_target(scene, target)
            constrained = source if source != driver else None
            if constrained and driver:
                guided.remember_bind_pair(scene, constrained, driver)
            scene.expykit_guided_explain = False
            if scene.expykit_guided_phase == 'BIND':
                scene.expykit_guided_phase = 'BAKE'
                guided._invoke_later(bpy.ops.object.ultimate_guided_mode, step='CHECK')
            if not getattr(guided, '_REBIND_BUSY', False):
                if constrained:
                    guided.select_only_constrained(context, constrained, driver)
                    guided.schedule_select_only_constrained(constrained, driver)
                guided.tag_retargeting_redraw()

        return result


# Custom bind operator with auto-detection and auto pose mode
class ULTIMATE_OT_bind_armatures(bpy.types.Operator):
    """Bind armatures with automatic Smash preset detection and pose mode switching"""
    bl_idname = "object.ultimate_bind_armatures"
    bl_label = "Bind Armatures"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        # Allow binding even if not in pose mode - we'll switch automatically
        return (context.object and context.object.type == 'ARMATURE' and 
                context.scene.expykit_bind_to and 
                context.object != context.scene.expykit_bind_to)
    
    def _prepare_bind(self, context):
        source_armature = context.object
        target_armature = context.scene.expykit_bind_to
        scene = context.scene

        check_and_load_smash_preset(source_armature)
        check_and_load_smash_preset(target_armature)
        guided.migrate_throw_from_custom(source_armature.data.expykit_retarget)
        guided.migrate_throw_from_custom(target_armature.data.expykit_retarget)
        scene.expykit_constrain_root = 'Bone'
        scene.expykit_root_cp_loc_x = True
        scene.expykit_root_cp_loc_y = True
        scene.expykit_root_cp_loc_z = True
        scene.expykit_root_cp_rot_x = True
        scene.expykit_root_cp_rot_y = True
        scene.expykit_root_cp_rot_z = True
        if hasattr(scene, 'expykit_root_use_loc_min_z'):
            scene.expykit_root_use_loc_min_z = True
        guided.apply_root_bind_defaults(
            scene, source_armature, target_armature, force_root_bone=True
        )

        if context.mode != 'POSE':
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for ob in context.selected_objects:
                ob.select_set(False)
            source_armature.select_set(True)
            context.view_layer.objects.active = source_armature
            bpy.ops.object.mode_set(mode='POSE')

        for ob in context.selected_objects:
            ob.select_set(ob == source_armature)

        target_armature.select_set(True)
        context.view_layer.objects.active = target_armature

        if target_armature != source_armature:
            bpy.ops.object.mode_set(mode='POSE')

        if target_armature.animation_data and target_armature.animation_data.action:
            try:
                bpy.ops.object.expykit_action_to_range()
            except Exception:
                pass
        return {'FINISHED'}

    def invoke(self, context, event):
        self._prepare_bind(context)
        return bpy.ops.armature.expykit_constrain_to_armature('INVOKE_DEFAULT', force_dialog=True)

    def execute(self, context):
        self._prepare_bind(context)
        return bpy.ops.armature.expykit_constrain_to_armature('INVOKE_DEFAULT', force_dialog=True)


# Help operator for how to use retargeting
class ULTIMATE_OT_retargeting_help(bpy.types.Operator):
    """Show instructions for using the retargeting system"""
    bl_idname = "object.ultimate_retargeting_help"
    bl_label = "How to Use Expy Kit Retargeting"
    
    def execute(self, context):
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=500)
    
    def draw(self, context):
        layout = self.layout
        
        col = layout.column(align=True)
        col.label(text="Expy Kit Retargeting - Quick Guide", icon='INFO')
        col.separator()
        
        # Step 1
        box = layout.box()
        box.label(text="1. Set Up Source Armature (the one you want to retarget FROM):", icon='ARMATURE_DATA')
        col = box.column(align=True)
        col.label(text="   • Select your source armature")
        col.label(text="   • Load the animations you want to retarget")
        col.label(text="   • Enter Pose Mode")
        col.label(text="   • Choose or create a preset in 'Expy Mapping'")
        col.label(text="   • Map bones to the preset's skeleton structure")
        
        # Step 2
        box = layout.box()
        box.label(text="2. Set Up Target Armature (the one you want to retarget TO):", icon='ARMATURE_DATA')
        col = box.column(align=True)
        col.label(text="   • Select your target armature")
        col.label(text="   • Enter Pose Mode")
        col.label(text="   • Choose or create a matching preset")
        
        # Step 3
        box = layout.box()
        box.label(text="3. Bind Armatures:", icon='LINKED')
        col = box.column(align=True)
        col.label(text="   • Select your target armature")
        col.label(text="   • In 'Bind To' panel, choose the target armature")
        col.label(text="   • A panel will appear with 'To Bind' and 'Bind To' options;")
        col.label(text="     choose presets for each model as needed and click OK")
        col.label(text="   • Click 'Bind Armatures'")
        col.label(text="   • A panel will appear in the bottom-left viewport corner")
        col.label(text="   • Use it to: adjust time stretching, toggle constraints,")
        col.label(text="     pick the Root Animation bone from the Bind To Root mapping,")
        col.label(text="     set offsets at the top (e.g., 'Bone Offset',")
        col.label(text="     'Current Pose is Target Pose'), and preview in real-time")
        
        # Step 4 - Tweaking
        box = layout.box()
        box.label(text="4. Tweaking:", icon='OUTLINER_OB_ARMATURE')
        col = box.column(align=True)
        col.label(text="   • A new set of helper bones is created for the source armature")
        col.label(text="   • Find them in the Bone Collections; use them to fix imperfections")
        col.label(text="   • Every target bone has constraints you can adjust")
        col.label(text="     if you don't want certain motions from the source")
        
        # Step 5
        box = layout.box()
        box.label(text="5. Bake Animation:", icon='RENDER_ANIMATION')
        col = box.column(align=True)
        col.label(text="   • Select your target armature in Pose Mode")
        col.label(text="   • Right-click in the viewport")
        col.label(text="   • Navigate to: Expy Kit > Animation > Bake Constrained Actions")
        col.label(text="   • Animation will be converted to keyframes")
        col.label(text="   • You can now unbind and use the baked animation")
        
        # Tips
        layout.separator()
        box = layout.box()
        box.label(text="Tips:", icon='LIGHTPROBE_CUBEMAP')
        col = box.column(align=True)
        col.label(text="   • Custom Bones: Add unique bones not in standard skeleton")
        col.label(text="   • Use the eyedropper to quickly assign active bone")
        col.label(text="   • Smash armatures auto-load the Smash preset")
        col.label(text="   • Both armatures must use the same preset structure")


# Main Retargeting panel in Ultimate tab
class SUB_PT_retargeting_main(Panel):
    """Main Retargeting panel in Ultimate tab"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Retargeting'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 80

    @classmethod
    def poll(cls, context):
        # Always visible
        return True

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        col = box.column(align=True)
        col.label(text="Expy Kit Retargeting Tools", icon='ARMATURE_DATA')

        row = col.row()
        row.scale_y = 1.4
        row.operator(
            "object.ultimate_guided_mode",
            text=guided.guided_button_label(scene),
            icon='PLAY',
        )

        bake_row = col.row()
        bake_row.scale_y = 1.6
        bake_row.operator("armature.ultimate_bake_actions", text="Bake Actions", icon='RENDER_ANIMATION')


# Expy_kit panels moved to Ultimate tab, all as children of Retargeting
# These will replace the original panels

class ULTIMATE_PT_expy_retarget(ui.VIEW3D_PT_expy_retarget):
    """Expy Mapping panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "SUB_PT_retargeting_main"
    
    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True
    
    def draw(self, context):
        layout = self.layout

        # Add help button for active bone functionality
        row = layout.row()
        row.operator(ui.SetToActiveBoneHelpText.bl_idname, text="How to Set Active Bone", icon='HELP')
        layout.separator()

        sync_preset_menu_label(context)

        preset_label = get_preset_display_label(context.object)

        # Use our custom preset menu that handles custom bones
        split = layout.split(factor=0.75)
        split.menu(ULTIMATE_MT_retarget_presets.__name__, text=preset_label)
        row = split.row(align=True)
        row.operator(ULTIMATE_OT_add_preset_retarget.bl_idname, text="+")
        row.operator(ULTIMATE_OT_add_preset_retarget.bl_idname, text="-").remove_active = True

        layout.separator()
        box = layout.box()
        box.label(text="Map from Reference Armature", icon='AUTO')
        row = box.row(align=True)
        row.prop(context.scene, 'expykit_nearest_bone_ref', text="Reference")
        row.operator(ULTIMATE_OT_map_bones_by_proximity.bl_idname, text="Map by Proximity")
        box.prop(context.scene, 'expykit_map_radius', text="Match Radius", slider=True)
        box.label(
            text="Scales auto match distance. Raise if overlapping bones still miss.",
            icon='INFO',
        )


class ULTIMATE_PT_BindPanel(ui.VIEW3D_PT_BindPanel):
    """Bind To panel in Ultimate tab with custom bind operator"""
    bl_category = 'Ultimate'
    bl_parent_id = "SUB_PT_retargeting_main"
    bl_label = "Bind To"
    
    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, 'expykit_bind_to', text="")
        layout.operator("object.ultimate_bind_armatures")
        source, target = guided.bound_pair(scene)
        if source and target:
            layout.separator()
            draw_binded_settings_ui(layout, context, show_presets=True)
            row = layout.row()
            row.scale_y = 1.2
            row.operator("object.ultimate_apply_bind_settings", icon='FILE_REFRESH')


class ULTIMATE_PT_ActionsPanel(Panel):
    """Actions panel - contains Binding, Conversion, and Animation operators"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = "Actions"
    bl_parent_id = "SUB_PT_retargeting_main"
    bl_options = {'DEFAULT_CLOSED'}
    
    @classmethod
    def poll(cls, context):
        return True
    
    def draw(self, context):
        layout = self.layout
        
        if context.mode != 'POSE':
            layout.label(text="Enter Pose Mode for actions", icon='INFO')


class ULTIMATE_PT_ActionsBinding(Panel):
    """Binding sub-panel under Actions"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = "Binding"
    bl_parent_id = "ULTIMATE_PT_ActionsPanel"
    bl_options = {'DEFAULT_CLOSED'}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'
    
    def draw(self, context):
        layout = self.layout
        
        col = layout.column()
        col.operator(operators.ConstrainToArmature.bl_idname)
        col.operator(operators.ConstraintStatus.bl_idname)
        op = col.operator(operators.SelectConstrainedControls.bl_idname)
        op.select_type = 'constr'


class ULTIMATE_PT_ActionsConversion(Panel):
    """Conversion sub-panel under Actions"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = "Conversion"
    bl_parent_id = "ULTIMATE_PT_ActionsPanel"
    bl_options = {'DEFAULT_CLOSED'}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'
    
    def draw(self, context):
        layout = self.layout
        
        col = layout.column()
        col.operator(operators.ConvertGameFriendly.bl_idname)
        col.operator(operators.RevertDotBoneNames.bl_idname)
        col.operator(operators.ConvertBoneNaming.bl_idname)
        col.operator(operators.ExtractMetarig.bl_idname)
        col.operator(operators.CreateTransformOffset.bl_idname)


class ULTIMATE_PT_ActionsAnimation(Panel):
    """Animation sub-panel under Actions"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = "Animation"
    bl_parent_id = "ULTIMATE_PT_ActionsPanel"
    bl_options = {'DEFAULT_CLOSED'}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'
    
    def draw(self, context):
        layout = self.layout
        
        col = layout.column()
        col.operator(operators.ActionRangeToScene.bl_idname)
        col.operator_context = 'INVOKE_DEFAULT'
        col.operator(operators.RenameActionsFromFbxFiles.bl_idname)
        col.operator(operators.AddRootMotion.bl_idname)
        op = col.operator(operators.SelectConstrainedControls.bl_idname, text="Select Animated Controls")
        op.select_type = 'anim'


# Custom bake operator with "Bake Visible" option
class ULTIMATE_OT_bake_actions(bpy.types.Operator):
    """Bake Constrained Actions with multiple baking modes"""
    bl_idname = "armature.ultimate_bake_actions"
    bl_label = "Bake Actions"
    bl_description = "Bake Actions constrained from another Armature with selectable baking mode"
    bl_options = {'REGISTER', 'UNDO'}
    
    bake_mode: bpy.props.EnumProperty(
        name="Bake Mode",
        items=[
            ('CONSTRAINED', "Bake Constrained", "Original behavior - bake constrained actions"),
            ('VISIBLE', "Bake Visible", "Bake with visual keying and clear constraints"),
        ],
        default='CONSTRAINED'
    )
    
    clear_users_old: bpy.props.BoolProperty(
        name="Clear original Action Users",
        default=True
    )
    
    fake_user_new: bpy.props.BoolProperty(
        name="Save New Action User",
        default=True
    )
    
    exclude_deform: bpy.props.BoolProperty(
        name="Exclude deform bones",
        default=False
    )
    
    do_bake: bpy.props.BoolProperty(
        name="Bake and Exit",
        description="Bake driven motion and exit",
        default=True,
        options={'SKIP_SAVE'}
    )
    
    copy_visibility_fcurves: bpy.props.BoolProperty(
        name="Copy Vis Layers",
        description="Link SAP Data animations to the new retargeted animation",
        default=False
    )

    keep_ik_bones: bpy.props.BoolProperty(
        name="Keep IK Bones",
        description="Include IK control bones in the bake and preserve their keyframes (FootIK, KneeIK, HandIK, etc.)",
        default=True,
    )
    
    # Bake Visible specific options
    clear_constraints_after: bpy.props.BoolProperty(
        name="Clear Constraints After Bake",
        description="Remove constraints after baking",
        default=True
    )
    
    @classmethod
    def poll(cls, context):
        ob = context.object
        return bool(ob and ob.type == 'ARMATURE')
    
    def invoke(self, context, event):
        if context.mode != 'POSE':
            try:
                bpy.ops.object.mode_set(mode='POSE')
            except Exception:
                pass
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        column = layout.column()
        
        # Bake mode selector
        column.prop(self, "bake_mode")
        column.separator()
        
        if self.bake_mode == 'CONSTRAINED':
            # Original bake constrained mode UI
            from ...expy_kit import bone_utils
            
            for to_bake in context.selected_objects:
                if getattr(to_bake, "type", None) != "ARMATURE":
                    continue
                trg_ob = self._get_trg_ob(to_bake)
                if not trg_ob:
                    continue
                column.label(text=f"Baking from {trg_ob.name} to {to_bake.name}")
            
            if len(context.selected_objects) > 1:
                column.label(text="No need to select two Armatures anymore", icon='ERROR')
            
            column.label(text="Mouse cursor fills as a clock while baking")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "clear_users_old")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "fake_user_new")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "exclude_deform")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "copy_visibility_fcurves")

            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "keep_ik_bones")
        
        else:  # VISIBLE mode
            column.label(text="Bake Visible Mode:", icon='INFO')
            column.label(text="Parallel visual bake of all actions")
            column.label(text="Renames originals to _old, baked get original names")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "clear_users_old")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "fake_user_new")
            
            row = column.split(factor=0.30, align=True)
            row.label(text="")
            row.prop(self, "clear_constraints_after")
        
        column.separator()
        row = column.split(factor=0.30, align=True)
        row.label(text="")
        row.prop(self, "do_bake", toggle=True)
    
    def _get_trg_ob(self, ob):
        """Get target object from constrained bones"""
        from ...expy_kit import bone_utils

        # Bake dialog iterates context.selected_objects; meshes like Hair.001
        # are often selected and have no pose data.
        if ob is None or getattr(ob, "type", None) != "ARMATURE" or getattr(ob, "pose", None) is None:
            return None

        for pb in bone_utils.get_constrained_controls(armature_object=ob, use_deform=not self.exclude_deform):
            for constr in pb.constraints:
                try:
                    subtarget = constr.subtarget
                except AttributeError:
                    continue
                
                if subtarget.endswith("_RET"):
                    return constr.target
        return None
    
    def execute(self, context):
        if not self.do_bake:
            self.report({'INFO'}, "Enable 'Bake and Exit' to run the bake")
            return {'FINISHED'}
        
        if self.bake_mode == 'VISIBLE':
            return self._execute_bake_visible(context)
        else:
            return self._execute_bake_constrained(context)
    
    def _execute_bake_visible(self, context):
        """Bake using visual keying - bulk bakes all actions like the constrained mode"""
        from ..anim.fcurve_compat import collect_actions_for_bake
        from ...expy_kit.operators import resolve_bake_armature_pair
        
        ob = context.object
        
        if not ob or ob.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        action_armature, bake_armature = resolve_bake_armature_pair(ob)
        if action_armature is None or bake_armature is None:
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        if action_armature != bake_armature:
            print(f"Bake Visible: baking from {action_armature.name} to {bake_armature.name}")
        else:
            print(f"Bake Visible: baking {bake_armature.name}'s own actions")

        if not action_armature.animation_data:
            action_armature.animation_data_create()
        
        actions_to_bake = collect_actions_for_bake(action_armature, bake_armature, extra_armatures=[ob])
        
        if not actions_to_bake:
            self.report({'WARNING'}, "No actions found to bake")
            return {'CANCELLED'}
        
        total_actions = len(actions_to_bake)
        print(f"Bake Visible: found {total_actions} actions to bake")
        
        # Store which bones have constraints (to clear after all baking)
        bones_with_constraints = []
        if self.clear_constraints_after:
            for pb in bake_armature.pose.bones:
                if pb.constraints:
                    bones_with_constraints.append(pb.name)
        
        try:
            from .fast_bake import bake_visible_actions
            baked_count = bake_visible_actions(
                context,
                action_armature,
                bake_armature,
                actions_to_bake,
                fake_user_new=self.fake_user_new,
                clear_users_old=self.clear_users_old,
                keep_ik_bones=self.keep_ik_bones,
            )
            
            # Clear constraints after all baking is done
            if self.clear_constraints_after:
                for bone_name in bones_with_constraints:
                    try:
                        pbone = bake_armature.pose.bones[bone_name]
                        for constr in reversed(pbone.constraints):
                            pbone.constraints.remove(constr)
                    except KeyError:
                        continue
            
            self.report({'INFO'}, f"Bake Visible completed - {baked_count}/{total_actions} actions baked")
            self._hide_source_after_bake(context, action_armature, bake_armature)
            
        except Exception as e:
            self.report({'ERROR'}, f"Bake failed: {str(e)}")
            return {'CANCELLED'}
        
        finally:
            context.window.cursor_modal_restore()
        
        return {'FINISHED'}
    
    def _execute_bake_constrained(self, context):
        """Use the original expy_kit nla.bake constrained baker."""
        bake_kwargs = dict(
            clear_users_old=self.clear_users_old,
            fake_user_new=self.fake_user_new,
            exclude_deform=self.exclude_deform,
            copy_visibility_fcurves=self.copy_visibility_fcurves,
            keep_ik_bones=self.keep_ik_bones,
            do_bake=True,
        )
        try:
            result = bpy.ops.armature.expykit_bake_constrained_actions(
                'EXEC_DEFAULT',
                **bake_kwargs,
            )
        except TypeError:
            # Older bundled expy_kit builds may not expose keep_ik_bones yet.
            bake_kwargs.pop('keep_ik_bones', None)
            result = bpy.ops.armature.expykit_bake_constrained_actions(
                'EXEC_DEFAULT',
                **bake_kwargs,
            )
        if 'CANCELLED' in result:
            return {'CANCELLED'}
        ob = context.object
        from ...expy_kit.operators import resolve_bake_armature_pair
        source, target = resolve_bake_armature_pair(ob)
        self._hide_source_after_bake(context, source, target)
        return {'FINISHED'}

    def _hide_source_after_bake(self, context, source, target):
        scene = context.scene
        # Prefer the pair captured at bind time. Bake operators can change the
        # active object and can remove constraints, making post-bake discovery
        # ambiguous. The stored order is (constrained model, Bind To driver).
        constrained, driver = guided.bound_pair(scene)
        if not constrained or not driver:
            driver, constrained = source, target
        if constrained and driver and constrained != driver:
            guided.hide_source_keep_target(context, driver, constrained)
        scene.expykit_guided_phase = 'DONE'
        scene.expykit_bind_is_active = False
        guided._invalidate_smash_viewport()


class ULTIMATE_PT_retarget_spine(ui.VIEW3D_PT_expy_retarget_spine):
    """Core panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True


class ULTIMATE_PT_retarget_arms(ui.VIEW3D_PT_expy_retarget_arms):
    """Arms panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True


class ULTIMATE_PT_retarget_arms_IK(ui.VIEW3D_PT_expy_retarget_arms_IK):
    """Arms IK panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        return False


class ULTIMATE_PT_retarget_legs(ui.VIEW3D_PT_expy_retarget_leg):
    """Legs panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        return True


class ULTIMATE_PT_retarget_legs_IK(ui.VIEW3D_PT_expy_retarget_leg_IK):
    """Legs IK panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        return False


class ULTIMATE_PT_retarget_fingers(ui.VIEW3D_PT_expy_retarget_fingers):
    """Fingers panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        return True


class ULTIMATE_PT_retarget_face(ui.VIEW3D_PT_expy_retarget_face):
    """Face panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        return False


class ULTIMATE_PT_retarget_root(ui.VIEW3D_PT_expy_retarget_root):
    """Root panel in Ultimate tab"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"
    
    @classmethod
    def poll(cls, context):
        return True


class ULTIMATE_PT_retarget_custom(ui.VIEW3D_PT_expy_retarget_custom):
    """Custom Bones panel in Ultimate tab - first item under Expy Mapping"""
    bl_category = 'Ultimate'
    bl_parent_id = "ULTIMATE_PT_expy_retarget"  # Under Expy Mapping, but first
    
    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True
    
    def draw(self, context):
        # Call parent draw method
        super().draw(context)
        # Force UI refresh to update custom bones list immediately
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# List of custom panels to register (all in Ultimate tab under Retargeting)
custom_panels = [
    SUB_PT_retargeting_main,
    ULTIMATE_PT_BindPanel,  # Bind To + settings after bind
    ULTIMATE_PT_expy_retarget,  # Expy Mapping panel
    ULTIMATE_PT_retarget_custom,  # Custom Bones - first under Expy Mapping
    ULTIMATE_PT_retarget_spine,
    ULTIMATE_PT_retarget_arms,
    ULTIMATE_PT_retarget_arms_IK,
    ULTIMATE_PT_retarget_legs,
    ULTIMATE_PT_retarget_legs_IK,
    ULTIMATE_PT_retarget_fingers,
    ULTIMATE_PT_retarget_face,
    ULTIMATE_PT_retarget_root,
    ULTIMATE_PT_ActionsPanel,  # Actions panel
    ULTIMATE_PT_ActionsBinding,  # Binding sub-panel
    ULTIMATE_PT_ActionsConversion,  # Conversion sub-panel (actions)
    ULTIMATE_PT_ActionsAnimation,  # Animation sub-panel
]


def register():
    """Register the retargeting module and all expy_kit components"""
    print("  Registering Retargeting module (expy_kit integration)...")
    
    # Install expy_kit presets if not already installed
    try:
        preset_handler.install_presets()
    except Exception as e:
        print(f"  Warning: Could not install expy_kit presets: {e}")
    
    # Register expy_kit properties
    properties.register_classes()
    
    # Register expy_kit operators FIRST
    operators.register_classes()
    
    # NOW unregister the original ConstrainToArmature and register our replacement
    global _original_constrain_class
    try:
        ConstrainToArmature = operators.ConstrainToArmature
        _original_constrain_class = ConstrainToArmature
        # Unregister the original
        bpy.utils.unregister_class(ConstrainToArmature)
        # Register our replacement (same bl_idname)
        bpy.utils.register_class(ULTIMATE_OT_constrain_to_armature)
        print("  Replaced ConstrainToArmature with our custom version that uses scene properties")
    except Exception as e:
        print(f"  Warning: Could not replace constrain operator: {e}")
    
    # Register expy_kit preferences
    preferences.register_classes()
    
    # Register original expy_kit UI classes (menus, operators UI, etc)
    # But we'll unregister the panels and replace them with our versions
    ui.register_classes()
    
    # Register our custom operators and menu
    try:
        bpy.utils.register_class(ULTIMATE_OT_execute_preset_retarget)
        bpy.utils.register_class(ULTIMATE_OT_add_preset_retarget)
        bpy.utils.register_class(ULTIMATE_OT_map_bones_by_proximity)
        bpy.utils.register_class(ULTIMATE_MT_retarget_presets)
        bpy.utils.register_class(ULTIMATE_OT_bind_armatures)
        bpy.utils.register_class(ULTIMATE_OT_retargeting_help)
        bpy.utils.register_class(ULTIMATE_OT_bake_actions)
        for cls in guided.classes:
            bpy.utils.register_class(cls)
    except Exception as e:
        print(f"  Warning: Could not register custom operators/menu: {e}")
    
    # Register scene properties for Binded Settings panel.
    # Always replace so live-update callbacks attach after addon reload.
    for _op_attr, scene_attr in _BIND_SYNC:
        if hasattr(bpy.types.Scene, scene_attr):
            try:
                delattr(bpy.types.Scene, scene_attr)
            except Exception:
                pass
    _bind_update = guided.on_bind_setting_update

    # Preset properties (use the same enum callback as the operator)
    bpy.types.Scene.expykit_src_preset = bpy.props.EnumProperty(
        items=preset_handler.iterate_presets_with_current,
        name="To Bind",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_trg_preset = bpy.props.EnumProperty(
        items=preset_handler.iterate_presets_with_current,
        name="Bind To",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_match_transform = bpy.props.EnumProperty(
        items=[
            ('None', "- None -", "Don't match any transform"),
            ('Bone', "Bones Offset", "Account for difference between control and deform rest pose"),
            ('Pose', "Current Pose is target Rest Pose", "Armature was posed manually to match rest pose of target"),
            ('World', "Follow target Pose in world space", "Just copy target world positions"),
        ],
        name="Match Transform",
        default='None',
        update=_bind_update,
    )
    bpy.types.Scene.expykit_match_object_transform = bpy.props.BoolProperty(
        name="Match Object Transform",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_fit_target_scale = bpy.props.EnumProperty(
        name="Fit height",
        items=(
            ('--', '- None -', 'None'),
            ('head', 'head', 'head'),
            ('neck', 'neck', 'neck'),
            ('spine2', 'chest', 'spine2'),
            ('spine1', 'spine1', 'spine1'),
            ('spine', 'spine', 'spine'),
            ('hips', 'hips', 'hips'),
        ),
        default='--',
        update=_bind_update,
    )
    bpy.types.Scene.expykit_loc_constraints = bpy.props.BoolProperty(
        name="Copy Location",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_rot_constraints = bpy.props.BoolProperty(
        name="Copy Rotation",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_scale_constraints = bpy.props.BoolProperty(
        name="Copy Scale",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_bind_floating = bpy.props.BoolProperty(
        name="Only Floating",
        description="Always bind unparented bones Location and Rotation",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_math_look_at = bpy.props.BoolProperty(
        name="Fix direction",
        description="Correct chain direction based on mid limb",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_copy_IK_roll_hands = bpy.props.BoolProperty(
        name="Hands IK Roll",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_copy_IK_roll_feet = bpy.props.BoolProperty(
        name="Feet IK Roll",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_constraint_policy = bpy.props.EnumProperty(
        items=[
            ('skip', "Skip Existing Constraints", "Skip Bones that are constrained already"),
            ('disable', "Disable Existing Constraints", "Disable existing binding constraints and add new ones"),
            ('remove', "Delete Existing Constraints", "Delete existing binding constraints")
        ],
        name="Policy",
        default='skip',
        update=_bind_update,
    )
    bpy.types.Scene.expykit_only_selected = bpy.props.BoolProperty(
        name="Only Selected",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_bind_by_name = bpy.props.BoolProperty(
        name="Also by Name",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_name_prefix = bpy.props.StringProperty(
        name="Prefix",
        default="",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_name_replace = bpy.props.StringProperty(
        name="Replace",
        default="",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_name_replace_with = bpy.props.StringProperty(
        name="With",
        default="",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_name_suffix = bpy.props.StringProperty(
        name="Suffix",
        default="",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_constrain_root = bpy.props.EnumProperty(
        items=[
            ('None', "No Root", "Don't constrain root bone"),
            ('Bone', "Bone", "Constrain root to bone"),
            ('Object', "Object", "Constrain root to object")
        ],
        name="Root Animation",
        default='Bone',
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_motion_bone = bpy.props.StringProperty(
        name="Root Motion Bone",
        default="",
        update=_bind_update,
    )
    bpy.types.Scene.expykit_adjust_location = bpy.props.BoolProperty(
        name="Adjust location to new scale",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_no_finger_loc = bpy.props.BoolProperty(
        name="Except Fingers",
        description="Don't copy location for finger bones",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_cp_loc_x = bpy.props.BoolProperty(
        name="Root Copy Loc X",
        description="Copy Root X Location",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_cp_loc_y = bpy.props.BoolProperty(
        name="Root Copy Loc Y",
        description="Copy Root Y Location",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_cp_loc_z = bpy.props.BoolProperty(
        name="Root Copy Loc Z",
        description="Copy Root Z Location",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_cp_rot_x = bpy.props.BoolProperty(
        name="Root Copy Rot X",
        description="Copy Root X Rotation",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_cp_rot_y = bpy.props.BoolProperty(
        name="Root Copy Rot Y",
        description="Copy Root Y Rotation",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_cp_rot_z = bpy.props.BoolProperty(
        name="Root Copy Rot Z",
        description="Copy Root Z Rotation",
        default=True,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_root_use_loc_min_x = bpy.props.BoolProperty(
        name="Min X", default=False, update=_bind_update)
    bpy.types.Scene.expykit_root_use_loc_min_y = bpy.props.BoolProperty(
        name="Min Y", default=False, update=_bind_update)
    bpy.types.Scene.expykit_root_use_loc_min_z = bpy.props.BoolProperty(
        name="Min Z", default=True, update=_bind_update)
    bpy.types.Scene.expykit_root_loc_min_x = bpy.props.FloatProperty(
        name="Root Min X", default=0.0, update=_bind_update)
    bpy.types.Scene.expykit_root_loc_min_y = bpy.props.FloatProperty(
        name="Root Min Y", default=0.0, update=_bind_update)
    bpy.types.Scene.expykit_root_loc_min_z = bpy.props.FloatProperty(
        name="Root Min Z", default=0.0, update=_bind_update)
    bpy.types.Scene.expykit_root_use_loc_max_x = bpy.props.BoolProperty(
        name="Max X", default=False, update=_bind_update)
    bpy.types.Scene.expykit_root_use_loc_max_y = bpy.props.BoolProperty(
        name="Max Y", default=False, update=_bind_update)
    bpy.types.Scene.expykit_root_use_loc_max_z = bpy.props.BoolProperty(
        name="Max Z", default=False, update=_bind_update)
    bpy.types.Scene.expykit_root_loc_max_x = bpy.props.FloatProperty(
        name="Root Max X", default=0.0, update=_bind_update)
    bpy.types.Scene.expykit_root_loc_max_y = bpy.props.FloatProperty(
        name="Root Max Y", default=0.0, update=_bind_update)
    bpy.types.Scene.expykit_root_loc_max_z = bpy.props.FloatProperty(
        name="Root Max Z", default=0.0, update=_bind_update)
    bpy.types.Scene.expykit_root_copy_scale = bpy.props.BoolProperty(
        name="Copy Scale",
        description="Copy Scale from motion bone",
        default=False,
        update=_bind_update,
    )
    bpy.types.Scene.expykit_ret_bones_collection = bpy.props.StringProperty(
        name="Layer",
        default="Retarget Bones",
        update=_bind_update,
    )

    if not hasattr(bpy.types.Scene, 'expykit_map_radius'):
        bpy.types.Scene.expykit_map_radius = bpy.props.FloatProperty(
            name="Match Radius",
            description="How far apart source and target bones can be when mapping by proximity",
            default=1.0,
            min=0.25,
            max=4.0,
            soft_min=0.25,
            soft_max=4.0,
        )

    if not hasattr(bpy.types.Scene, 'expykit_guided_phase'):
        bpy.types.Scene.expykit_guided_phase = bpy.props.StringProperty(
            default='IDLE',
        )

    if not hasattr(bpy.types.Scene, 'expykit_guided_explain'):
        bpy.types.Scene.expykit_guided_explain = bpy.props.BoolProperty(
            default=False,
        )

    if not hasattr(bpy.types.Scene, 'expykit_bind_is_active'):
        bpy.types.Scene.expykit_bind_is_active = bpy.props.BoolProperty(
            default=False,
        )

    if not hasattr(bpy.types.Scene, 'expykit_bound_source'):
        bpy.types.Scene.expykit_bound_source = bpy.props.PointerProperty(
            type=bpy.types.Object,
        )

    if not hasattr(bpy.types.Scene, 'expykit_bound_target'):
        bpy.types.Scene.expykit_bound_target = bpy.props.PointerProperty(
            type=bpy.types.Object,
        )

    # Note: ConstrainToArmature was replaced with our custom version earlier
    
    # Unregister original panels
    original_panels = [
        ui.VIEW3D_PT_expy_retarget,
        ui.VIEW3D_PT_BindPanel,
        ui.VIEW3D_PT_expy_retarget_spine,
        ui.VIEW3D_PT_expy_retarget_arms,
        ui.VIEW3D_PT_expy_retarget_arms_IK,
        ui.VIEW3D_PT_expy_retarget_leg,
        ui.VIEW3D_PT_expy_retarget_leg_IK,
        ui.VIEW3D_PT_expy_retarget_fingers,
        ui.VIEW3D_PT_expy_retarget_face,
        ui.VIEW3D_PT_expy_retarget_root,
        ui.VIEW3D_PT_expy_retarget_custom,
        ui.VIEW3D_PT_expy_rename_candidates,
        ui.VIEW3D_PT_expy_rename_advanced,
    ]
    
    for panel in original_panels:
        try:
            bpy.utils.unregister_class(panel)
        except:
            pass

    # Drop the old duplicate "Bind to Active Armature" sub-panel if a previous
    # reload left it registered.
    for cls_name in ("ULTIMATE_PT_BindSettings",):
        old_panel = getattr(bpy.types, cls_name, None)
        if old_panel is not None:
            try:
                bpy.utils.unregister_class(old_panel)
            except Exception:
                pass
    
    # Register our custom panels
    for panel in custom_panels:
        try:
            bpy.utils.register_class(panel)
        except Exception as e:
            print(f"  Warning: Could not register panel {panel.__name__}: {e}")
    
    # Callback for when bind_to property changes
    def on_bind_to_update(self, context):
        """Check if the selected bind_to armature is a smash rig"""
        if self.expykit_bind_to and self.expykit_bind_to.type == 'ARMATURE':
            check_and_load_smash_preset(self.expykit_bind_to)
    
    # Register scene properties for binding
    if not hasattr(bpy.types.Scene, 'expykit_bind_to'):
        bpy.types.Scene.expykit_bind_to = bpy.props.PointerProperty(
            type=bpy.types.Object,
            name="Bind To",
            description="Target armature to bind to",
            poll=ui.poll_armature_bind_to,
            update=on_bind_to_update
        )

    if not hasattr(bpy.types.Scene, 'expykit_nearest_bone_ref'):
        bpy.types.Scene.expykit_nearest_bone_ref = bpy.props.PointerProperty(
            type=bpy.types.Object,
            name="Reference Armature",
            description="Armature with a configured preset to use as a spatial reference for bone mapping",
            poll=ui.poll_armature_bind_to,
        )
    
    # Register auto-detection handler for Smash armatures
    if auto_detect_smash_armature not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(auto_detect_smash_armature)
    
    print("  Retargeting module registered successfully!")


def unregister():
    """Unregister the retargeting module and all expy_kit components"""
    print("  Unregistering Retargeting module...")
    
    # Unregister auto-detection handler
    if auto_detect_smash_armature in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(auto_detect_smash_armature)
    
    # Unregister our custom panels
    for panel in reversed(custom_panels):
        try:
            bpy.utils.unregister_class(panel)
        except:
            pass
    old_dup = getattr(bpy.types, "ULTIMATE_PT_BindSettings", None)
    if old_dup is not None:
        try:
            bpy.utils.unregister_class(old_dup)
        except Exception:
            pass
    
    # Restore original ConstrainToArmature operator
    global _original_constrain_class
    if _original_constrain_class:
        try:
            # Unregister our replacement
            bpy.utils.unregister_class(ULTIMATE_OT_constrain_to_armature)
            # Re-register the original
            bpy.utils.register_class(_original_constrain_class)
            print("  Restored original ConstrainToArmature operator")
        except:
            pass
    
    # Unregister our custom operators and menu
    try:
        for cls in reversed(guided.classes):
            bpy.utils.unregister_class(cls)
        bpy.utils.unregister_class(ULTIMATE_OT_bake_actions)
        bpy.utils.unregister_class(ULTIMATE_OT_retargeting_help)
        bpy.utils.unregister_class(ULTIMATE_OT_bind_armatures)
        bpy.utils.unregister_class(ULTIMATE_MT_retarget_presets)
        bpy.utils.unregister_class(ULTIMATE_OT_map_bones_by_proximity)
        bpy.utils.unregister_class(ULTIMATE_OT_add_preset_retarget)
        bpy.utils.unregister_class(ULTIMATE_OT_execute_preset_retarget)
    except:
        pass
    
    # Unregister scene properties for Binded Settings panel
    scene_props = [
        'expykit_src_preset',
        'expykit_trg_preset',
        'expykit_match_transform',
        'expykit_match_object_transform',
        'expykit_fit_target_scale',
        'expykit_adjust_location',
        'expykit_loc_constraints',
        'expykit_rot_constraints',
        'expykit_scale_constraints',
        'expykit_bind_floating',
        'expykit_math_look_at',
        'expykit_copy_IK_roll_hands',
        'expykit_copy_IK_roll_feet',
        'expykit_constraint_policy',
        'expykit_only_selected',
        'expykit_bind_by_name',
        'expykit_name_prefix',
        'expykit_name_replace',
        'expykit_name_replace_with',
        'expykit_name_suffix',
        'expykit_constrain_root',
        'expykit_root_motion_bone',
        'expykit_no_finger_loc',
        'expykit_root_cp_loc_x',
        'expykit_root_cp_loc_y',
        'expykit_root_cp_loc_z',
        'expykit_root_cp_rot_x',
        'expykit_root_cp_rot_y',
        'expykit_root_cp_rot_z',
        'expykit_root_use_loc_min_x',
        'expykit_root_use_loc_min_y',
        'expykit_root_use_loc_min_z',
        'expykit_root_loc_min_x',
        'expykit_root_loc_min_y',
        'expykit_root_loc_min_z',
        'expykit_root_use_loc_max_x',
        'expykit_root_use_loc_max_y',
        'expykit_root_use_loc_max_z',
        'expykit_root_loc_max_x',
        'expykit_root_loc_max_y',
        'expykit_root_loc_max_z',
        'expykit_root_copy_scale',
        'expykit_ret_bones_collection',
        'expykit_map_radius',
        'expykit_guided_phase',
        'expykit_guided_explain',
        'expykit_bind_is_active',
        'expykit_bound_source',
        'expykit_bound_target',
    ]
    
    for prop in scene_props:
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    
    # Unregister original expy_kit UI classes
    try:
        ui.unregister_classes()
    except:
        pass
    
    # Unregister expy_kit preferences
    try:
        preferences.unregister_classes()
    except:
        pass
    
    # Unregister expy_kit operators
    try:
        operators.unregister_classes()
    except:
        pass
    
    # Unregister expy_kit properties
    try:
        properties.unregister_classes()
    except:
        pass
    
    # Remove scene properties
    if hasattr(bpy.types.Scene, 'expykit_nearest_bone_ref'):
        del bpy.types.Scene.expykit_nearest_bone_ref
    if hasattr(bpy.types.Scene, 'expykit_bind_to'):
        del bpy.types.Scene.expykit_bind_to
    
    # Reset global state
    global _last_detected_armature, _last_active_armature
    _last_detected_armature = None
    _last_active_armature = None
    
    print("  Retargeting module unregistered successfully!")
