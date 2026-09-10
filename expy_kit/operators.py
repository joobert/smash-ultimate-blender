from math import pi
import os
import typing
import re

import bpy
from bpy.props import BoolProperty
from bpy.props import EnumProperty
from bpy.props import FloatProperty
from bpy.props import IntProperty
from bpy.props import StringProperty
from bpy.props import CollectionProperty
from bpy.props import FloatVectorProperty

from bpy_extras.io_utils import ImportHelper

from itertools import chain

from .rig_mapping import bone_mapping
from . import preset_handler
from . import bone_utils
from . import fbx_helper
from ..source.anim.fcurve_compat import (
    get_fcurves,
    get_all_action_fcurves,
    remove_fcurve,
    action_matches_id,
    action_frame_range_safe,
    collect_actions_for_bake,
)
from ..source.anim.raw_anim import normalize_anim_stem
from ..source.blender_compat import set_pose_bone_select, assign_action

from mathutils import Vector
from mathutils import Matrix


# Global variables to track SAP sync state
_last_source_actions = {}  # Track last action for each source armature
_sap_sync_pairs = []  # Store sync pairs globally

def _sap_sync_timer_func():
    """Timer function to handle real-time SAP Data synchronization"""
    try:
        global _sap_sync_pairs
        if not _sap_sync_pairs:
            return None  # Stop timer if no sync pairs
        
        # Check each sync pair for action changes
        for sync_pair in _sap_sync_pairs:
            source_obj_name = sync_pair.get('source_object')
            target_obj_name = sync_pair.get('target_object')
            clean_source_name = sync_pair.get('clean_source_name')
            clean_target_name = sync_pair.get('clean_target_name')
            
            if not all([source_obj_name, target_obj_name, clean_source_name, clean_target_name]):
                continue
            
            # Get source and target objects
            source_obj = bpy.data.objects.get(source_obj_name)
            target_obj = bpy.data.objects.get(target_obj_name)
            
            if not source_obj or not target_obj:
                continue
            
            # Check if source has current action
            if not (source_obj.animation_data and source_obj.animation_data.action):
                continue
            
            current_action = source_obj.animation_data.action
            current_action_name = current_action.name
            
            # Check if this action has changed since last check
            last_action = _last_source_actions.get(source_obj_name)
            if last_action == current_action_name:
                continue  # No change
            
            # Update our tracking
            _last_source_actions[source_obj_name] = current_action_name
            
            # Clean action name
            if "|" in current_action_name:
                clean_action_name = current_action_name.split("|")[-1]
            else:
                clean_action_name = current_action_name
            
            # Look for corresponding target SAP action
            target_sap_action_name = f"{clean_target_name} {clean_action_name} SAP Data"
            target_sap_action = bpy.data.actions.get(target_sap_action_name)
            
            if target_sap_action:
                # Ensure target has armature data animation data
                if not target_obj.data.animation_data:
                    target_obj.data.animation_data_create()
                
                # Set the target's SAP action
                target_obj.data.animation_data.action = target_sap_action
                print(f"SAP Sync: Set '{target_obj_name}' SAP action to '{target_sap_action_name}'")
            else:
                print(f"SAP Sync: No matching SAP action found for '{target_sap_action_name}'")
        
        return 0.1  # Check again in 0.1 seconds
        
    except Exception as e:
        print(f"SAP Sync timer error: {e}")
        return 0.1  # Continue despite errors


CONSTR_STATUS = (
    ('enable', "Enable", "Enable All Constraints"),
    ('disable', "Disable", "Disable All Constraints"),
    ('remove', "Remove", "Remove All Constraints")
)


CONSTR_TYPES = bpy.types.PoseBoneConstraints.bl_rna.functions['new'].parameters['type'].enum_items.keys()
CONSTR_TYPES.append('ALL_TYPES')


class ConstraintStatus(bpy.types.Operator):
    """Disable/Enable bone constraints."""
    bl_description = 'Disable/Enable bone constraints.'
    bl_idname = "object.expykit_set_constraints_status"
    bl_label = "Enable/disable constraints"
    bl_options = {'REGISTER', 'UNDO'}

    set_status: EnumProperty(items=CONSTR_STATUS,
                             name="Status",
                             default='enable')

    selected_only: BoolProperty(name="Only Selected",
                                default=False)
    
    constr_type: EnumProperty(items=[(ct, ct.replace('_', ' ').title(), ct) for ct in CONSTR_TYPES],
                              name="Constraint Type",
                              default='ALL_TYPES')

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.mode != 'POSE':
            return False
        if context.object.type != 'ARMATURE':
            return False

        return True

    def execute(self, context):
        bones = context.selected_pose_bones if self.selected_only else context.object.pose.bones
        if self.set_status == 'remove':
            for bone in bones:
                for constr in reversed(bone.constraints):
                    if self.constr_type != 'ALL_TYPES' and constr.type != self.constr_type:
                        continue

                    bone.constraints.remove(constr)
        else:
            for bone in bones:
                for constr in bone.constraints:
                    if self.constr_type != 'ALL_TYPES' and constr.type != self.constr_type:
                        continue

                    constr.mute = self.set_status == 'disable'

        return {'FINISHED'}


class SelectConstrainedControls(bpy.types.Operator):
    bl_idname = "armature.expykit_select_constrained_ctrls"
    bl_label = "Select constrained controls"
    bl_description = "Select bone controls with constraints or animations"
    bl_options = {'REGISTER', 'UNDO'}

    select_type: EnumProperty(items=[
        ('constr', "Constrained", "Select constrained controls"),
        ('anim', "Animated", "Select animated controls"),
    ],
        name="Select if",
        default='constr')
    
    skip_deform: BoolProperty(name="Skip Deform Bones", default=True)
    has_shape: BoolProperty(name="Only Control shapes", default=True)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.mode != 'POSE':
            return False
        if context.object.type != 'ARMATURE':
            return False

        return True

    def execute(self, context):
        ob = context.object

        if self.select_type == 'constr':
            for pb in bone_utils.get_constrained_controls(ob, unselect=True, use_deform=not self.skip_deform):
                set_pose_bone_select(pb, bool(pb.custom_shape) if self.has_shape else True)

        elif self.select_type == 'anim':
            if not ob.animation_data:
                return {'FINISHED'}
            if not ob.animation_data.action:
                return {'FINISHED'}

            for fc in get_fcurves(ob.animation_data.action):
                bone_name = crv_bone_name(fc)
                if not bone_name:
                    continue
                try:
                    bone = ob.data.bones[bone_name]
                except KeyError:
                    continue
                bone.select = True

        return {'FINISHED'}


class RevertDotBoneNames(bpy.types.Operator):
    """Reverts dots in bones that have renamed by Unreal Engine"""
    bl_description = 'Reverts dots in bones that have renamed by Unreal Engine'
    bl_idname = "object.expykit_dot_bone_names"
    bl_label = "Revert dots in Names (from UE4 renaming)"
    bl_options = {'REGISTER', 'UNDO'}

    sideletters_only: BoolProperty(name="Only Side Letters",
                                   description="i.e. '_L' to '.L'",
                                   default=True)

    selected_only: BoolProperty(name="Only Selected",
                                default=False)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.mode != 'POSE':
            return False
        return context.object.type == 'ARMATURE'

    def execute(self, context):
        bones = context.selected_pose_bones if self.selected_only else context.object.pose.bones

        if self.sideletters_only:
            for bone in bones:
                for side in ("L", "R"):
                    if bone.name[:-1].endswith("_{0}_00".format(side)):
                        bone.name = bone.name.replace("_{0}_00".format(side), ".{0}.00".format(side))
                    elif bone.name.endswith("_{0}".format(side)):
                        bone.name = bone.name[:-2] + ".{0}".format(side)
        else:
            for bone in bones:
                bone.name = bone.name.replace('_', '.')

        return {'FINISHED'}


class ConvertBoneNaming(bpy.types.Operator):
    """Convert Bone Names between Naming Convention"""
    bl_description = 'Convert Bone Names between Naming Convention'
    bl_idname = "object.expykit_convert_bone_names"
    bl_label = "Convert Bone Names"
    bl_options = {'REGISTER', 'UNDO'}

    src_preset: EnumProperty(items=preset_handler.iterate_presets_with_current,
                             name="Source Preset",
                             )

    trg_preset: EnumProperty(items=preset_handler.iterate_presets,
                             name="Target Preset",
                             )

    strip_prefix: BoolProperty(
        name="Strip Prefix",
        description="Remove prefix when found",
        default=True
    )

    anim_tracks: BoolProperty(
        name="Convert Animations",
        description="Convert Animation Tracks",
        default=True
    )

    replace_existing: BoolProperty(
        name="Take Over Existing Names",
        description='Bones already named after Target Preset will get ".001" suffix',
        default=True
    )

    prefix_separator: StringProperty(
        name="Prefix Separator",
        description="Separator between prefix and name, i.e: MyCharacter:head",
        default=":"
    )

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.mode != 'POSE':
            return False
        if context.object.type != 'ARMATURE':
            return False

        return True

    @staticmethod
    def convert_presets(src_settings, target_settings):
        src_skeleton = preset_handler.get_preset_skel(src_settings)
        trg_skeleton = preset_handler.get_preset_skel(target_settings)

        return src_skeleton, trg_skeleton

    @staticmethod
    def convert_settings(current_settings, target_settings, validate=True):
        src_settings = preset_handler.PresetSkeleton()
        src_settings.copy(current_settings)

        src_skeleton = preset_handler.get_settings_skel(src_settings)
        trg_skeleton = preset_handler.set_preset_skel(target_settings, validate)

        return src_skeleton, trg_skeleton

    @staticmethod
    def rename_bones(context, src_skeleton, trg_skeleton, separator="", replace_existing=False, skip_ik=False):
        # FIXME: separator should not be necessary anymore, as it is handled at preset validation
        bone_names_map = src_skeleton.conversion_map(trg_skeleton, skip_ik=skip_ik)

        if separator:
            for bone in context.object.data.bones:
                if separator not in bone.name:
                    continue

                bone.name = bone.name.rsplit(separator, 1)[1]

        additional_bones = {}
        for src_name, trg_name in bone_names_map.items():
            if not trg_name:
                continue
            if not src_name:
                continue
            try:
                src_bone = context.object.data.bones.get(src_name, None)
            except SystemError:
                continue

            if not src_bone:
                continue

            if replace_existing:
                pre_existing_bone = context.object.data.bones.get(trg_name, None)
                if pre_existing_bone:
                    pre_existing_name = pre_existing_bone.name
                    pre_existing_bone.name = f"{trg_name}.001"
                    additional_bones[pre_existing_name] = pre_existing_bone.name
                                          
            src_bone.name = trg_name

        bone_names_map.update(additional_bones)
        return bone_names_map

    def execute(self, context):
        if self.src_preset == "--Current--":
            current_settings = context.object.data.expykit_retarget
            trg_settings = preset_handler.PresetSkeleton()
            trg_settings.copy(current_settings)
            src_skeleton, trg_skeleton = self.convert_settings(trg_settings, self.trg_preset, validate=False)

            set_preset = False
        else:
            src_skeleton, trg_skeleton = self.convert_presets(self.src_preset, self.trg_preset)

            set_preset = True

        if all((src_skeleton, trg_skeleton, src_skeleton != trg_skeleton)):
            if self.anim_tracks:
                actions = [action for action in bpy.data.actions if validate_actions(action, context.object.path_resolve)]
            else:
                actions = []

            bone_names_map = self.rename_bones(context, src_skeleton, trg_skeleton,
                                               self.prefix_separator if self.strip_prefix else "",
                                               self.replace_existing)

            if context.object.animation_data and context.object.data.animation_data:
                for driver in chain(context.object.animation_data.drivers, context.object.data.animation_data.drivers):
                    try:
                        driver_bone = driver.data_path.split('"')[1]
                    except IndexError:
                        continue

                    try:
                        trg_name = bone_names_map[driver_bone]
                    except KeyError:
                        continue

                    driver.data_path = driver.data_path.replace('bones["{0}"'.format(driver_bone),
                                                                'bones["{0}"'.format(trg_name))

            for action in actions:
                for fc in get_fcurves(action):
                    try:
                        track_bone = fc.data_path.split('"')[1]
                    except IndexError:
                        continue

                    if self.strip_prefix and self.prefix_separator in track_bone:
                        stripped_bone = track_bone.rsplit(self.prefix_separator, 1)[1]
                    else:
                        stripped_bone = track_bone

                    try:
                        trg_name = bone_names_map[stripped_bone]
                    except KeyError:
                        continue

                    fc.data_path = fc.data_path.replace('bones["{0}"'.format(track_bone),
                                                        'bones["{0}"'.format(trg_name))

            if set_preset:
                preset_handler.set_preset_skel(self.trg_preset)
            else:
                preset_handler.validate_preset(bpy.context.active_object.data, separator=self.prefix_separator)

        if bpy.app.version[0] > 2:
            # blender 3.0 objects do not immediately update renamed vertex groups
            for ob in bone_utils.iterate_rigged_obs(context.object):
                ob.data.update()

        return {'FINISHED'}


_offset_preview_session = {}


def _offset_container_scale_update(self, context):
    """Live viewport preview while the scale slider is dragged."""
    if _offset_preview_session.get('finalizing'):
        return

    arm_name = _offset_preview_session.get('armature')
    if not arm_name:
        return

    arm_ob = bpy.data.objects.get(arm_name)
    if arm_ob is None:
        return

    preview_scale = max(self.container_scale, 1e-8)
    base_scale = Vector(_offset_preview_session.get('base_scale', (1.0, 1.0, 1.0)))
    arm_ob.scale = base_scale * preview_scale

    met_name = _offset_preview_session.get('metarig')
    if met_name:
        metarig = bpy.data.objects.get(met_name)
        if metarig:
            met_base = Vector(_offset_preview_session.get('met_base_scale', (1.0, 1.0, 1.0)))
            metarig.scale = met_base * preview_scale

    if context.screen:
        for area in context.screen.areas:
            area.tag_redraw()

    view_layer = getattr(context, 'view_layer', None)
    if view_layer is not None:
        view_layer.update()


class CreateTransformOffset(bpy.types.Operator):
    """Scale the Character and setup an Empty to preserve final transform"""
    bl_description = 'Scale the Character and setup an Empty to preserve final transform'
    bl_idname = "object.expykit_create_offset"
    bl_label = "Create Scale Offset"
    bl_options = {'REGISTER', 'UNDO'}

    container_name: StringProperty(name="Name", description="Name of the transform container", default="EMP-Offset")
    container_scale: FloatProperty(
        name="Scale",
        description="Scale of the transform container",
        default=0.01,
        min=1e-8,
        update=_offset_container_scale_update,
    )
    fix_animations: BoolProperty(
        name="Fix Animations",
        description="Apply offset to character animations when Execute and Exit is pressed",
        default=True,
    )
    fix_constraints: BoolProperty(name="Fix Constraints", description="Apply Offset to character constraints", default=True)
    do_parent: BoolProperty(
        name="Execute and Exit",
        description="Bake the chosen scale, fix animations, create the offset empty, and finish",
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    _allowed_modes = ['OBJECT', 'POSE']

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.object.parent:
            return False
        if context.object.type != 'ARMATURE':
            return False
        if context.mode not in cls._allowed_modes:
            return False

        return True

    def check(self, context):
        return True

    def invoke(self, context, event):
        if self.do_parent:
            _offset_preview_session['finalizing'] = True
            result = self.execute(context)
            _offset_preview_session.clear()
            return result

        arm_ob = context.object
        _offset_preview_session.clear()
        _offset_preview_session.update({
            'active': True,
            'armature': arm_ob.name,
            'base_scale': tuple(arm_ob.scale),
        })

        metarig = self._find_metarig(arm_ob)
        if metarig:
            _offset_preview_session['metarig'] = metarig.name
            _offset_preview_session['met_base_scale'] = tuple(metarig.scale)

        _offset_container_scale_update(self, context)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        column = layout.column()

        row = column.split(factor=0.2, align=True)
        row.label(text="Name")
        row.prop(self, 'container_name', text="")

        row = column.split(factor=0.2, align=True)
        row.label(text="Scale")
        row.prop(self, "container_scale", text="")

        row = column.split(factor=0.2, align=True)
        row.label(text="")
        row.prop(self, "fix_animations")

        row = column.split(factor=0.2, align=True)
        row.label(text="")
        row.prop(self, "fix_constraints")

        column.separator()
        row = column.row()
        row.scale_y = 1.4
        op = row.operator(self.bl_idname, text="Execute and Exit", icon='CHECKMARK')
        op.container_name = self.container_name
        op.container_scale = self.container_scale
        op.fix_animations = self.fix_animations
        op.fix_constraints = self.fix_constraints
        op.do_parent = True

    @staticmethod
    def _find_metarig(arm_ob):
        try:
            return next(
                ob for ob in bpy.data.objects
                if ob.type == 'ARMATURE' and ob.data.rigify_target_rig == arm_ob
            )
        except (StopIteration, AttributeError):
            return None

    def _apply_scale_finalize(self, context, arm_ob):
        scale = self.container_scale
        scale_mat = Matrix.Scale(scale, 4)
        base_scale = Vector(_offset_preview_session.get('base_scale', (1.0, 1.0, 1.0)))

        arm_world = arm_ob.matrix_world.copy()
        arm_loc, arm_rot, _arm_scale = arm_world.decompose()

        arm_ob.scale = base_scale
        arm_ob.data.transform(scale_mat)
        arm_ob.update_tag()
        arm_ob.matrix_world = Matrix.LocRotScale(arm_loc, arm_rot, base_scale)

        metarig = self._find_metarig(arm_ob)
        if metarig:
            met_base = Vector(_offset_preview_session.get('met_base_scale', (1.0, 1.0, 1.0)))
            met_world = metarig.matrix_world.copy()
            met_loc, met_rot, _met_scale = met_world.decompose()

            metarig.scale = met_base
            metarig.data.transform(scale_mat)
            metarig.update_tag()
            metarig.matrix_world = Matrix.LocRotScale(met_loc, met_rot, met_base)

        if self.fix_constraints:
            for pbone in arm_ob.pose.bones:
                for constr in pbone.constraints:
                    if constr.type == 'STRETCH_TO':
                        constr.rest_length *= scale
                    elif constr.type == 'LIMIT_DISTANCE':
                        constr.distance *= scale
                    elif constr.type == 'ACTION':
                        if constr.target == arm_ob and constr.transform_channel.startswith('LOCATION'):
                            if constr.target_space != 'WORLD':
                                constr.min *= scale
                                constr.max *= scale
                    elif constr.type == 'LIMIT_LOCATION' and constr.owner_space != 'WORLD':
                        constr.min_x *= scale
                        constr.min_y *= scale
                        constr.min_z *= scale

                        constr.max_x *= scale
                        constr.max_y *= scale
                        constr.max_z *= scale

        rigged = (ob for ob in bpy.data.objects if
                  next((mod for mod in ob.modifiers if mod.type == 'ARMATURE' and mod.object == context.object),
                       None))

        for ob in rigged:
            if ob.data.shape_keys:
                ob.scale *= scale
            else:
                ob.data.transform(scale_mat)
            for mod in ob.modifiers:
                if mod.type == 'DISPLACE':
                    mod.strength *= scale
                elif mod.type == 'SOLIDIFY':
                    mod.thickness *= scale

        if self.fix_animations:
            path_resolve = arm_ob.path_resolve

            for action in bpy.data.actions:
                if not validate_actions(action, path_resolve):
                    continue

                for fc in get_fcurves(action):
                    data_path = fc.data_path

                    if not data_path.endswith('location'):
                        continue

                    for kf in fc.keyframe_points:
                        kf.co[1] *= scale

        emp_ob = bpy.data.objects.new(self.container_name, None)
        context.collection.objects.link(emp_ob)
        emp_ob.matrix_world = Matrix.LocRotScale(arm_loc, arm_rot, Vector((1.0, 1.0, 1.0)))
        arm_ob.parent = emp_ob
        arm_ob.matrix_world = Matrix.LocRotScale(arm_loc, arm_rot, base_scale)

        if metarig:
            metarig.parent = emp_ob
            metarig.matrix_world = Matrix.LocRotScale(met_loc, met_rot, met_base)

        return {'FINISHED'}

    def execute(self, context):
        arm_ob = context.object

        if self.do_parent:
            return self._apply_scale_finalize(context, arm_ob)

        _offset_container_scale_update(self, context)
        return {'FINISHED'}


class ExtractMetarig(bpy.types.Operator):
    """Create Metarig from current object"""
    bl_idname = "object.expykit_extract_metarig"
    bl_label = "Extract Metarig"
    bl_description = "Create Metarig from current object"
    bl_options = {'REGISTER', 'UNDO'}

    rig_preset: EnumProperty(items=preset_handler.iterate_presets_with_current,
                             name="Rig Type",
                             )

    offset_knee: FloatProperty(name='Offset Knee',
                               default=0.0)

    offset_elbow: FloatProperty(name='Offset Elbow',
                                default=0.0)

    offset_fingers: FloatVectorProperty(name='Offset Fingers')

    no_face: BoolProperty(name='No face bones',
                          default=True)

    rigify_names: BoolProperty(name='Use rigify names',
                               default=True)

    assign_metarig: BoolProperty(name='Assign metarig',
                                 default=True,
                                 description='Rigify will generate to the active object')

    forward_spine_roll: BoolProperty(name='Align spine frontally', default=True,
                                     description='Spine Z will face the Y axis')

    apply_transforms: BoolProperty(name='Apply Transform', default=True,
                                   description='Apply current transforms before extraction')

    def draw(self, context):
        layout = self.layout
        column = layout.column()

        # if not context.active_object.data.expykit_retarget.has_settings():
        row = column.row()
        row.prop(self, 'rig_preset', text="Rig Type")

        row = column.split(factor=0.5, align=True)
        row.label(text="Offset Knee")
        row.prop(self, 'offset_knee', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="Offset Elbow")
        row.prop(self, 'offset_elbow', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="Offset Fingers")
        row.prop(self, 'offset_fingers', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="No Face Bones")
        row.prop(self, 'no_face', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="Use Rigify Names")
        row.prop(self, 'rigify_names', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="Assign Metarig")
        row.prop(self, 'assign_metarig', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="Align spine frontally")
        row.prop(self, 'forward_spine_roll', text='')

        row = column.split(factor=0.5, align=True)
        row.label(text="Apply Transform")
        row.prop(self, 'apply_transforms', text='')

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if 'rigify' not in context.preferences.addons:
            return False
        if context.mode != 'POSE':
            return False
        if context.object.type != 'ARMATURE':
            return False

        return True

    def execute(self, context):
        src_object = context.object
        src_armature = context.object.data

        if self.rig_preset == "--Current--":
            current_settings = context.object.data.expykit_retarget

            if current_settings.deform_preset and current_settings.deform_preset != '--':
                deform_preset = current_settings.deform_preset

                src_skeleton = preset_handler.set_preset_skel(deform_preset)
                current_settings = src_skeleton
            else:
                src_settings = preset_handler.PresetSkeleton()
                src_settings.copy(current_settings)
                src_skeleton = preset_handler.get_settings_skel(src_settings)
        else:
            src_skeleton = preset_handler.set_preset_skel(self.rig_preset)
            current_settings = context.object.data.expykit_retarget

        if not src_skeleton:
            return {'FINISHED'}

        # TODO: remove action, bring to rest pose
        if self.apply_transforms:
            rigged = (ob for ob in bpy.data.objects if
                      next((mod for mod in ob.modifiers if mod.type == 'ARMATURE' and mod.object == src_object),
                           None))
            for ob in rigged:
                ob.data.transform(src_object.matrix_local)

            src_armature.transform(src_object.matrix_local)
            src_object.matrix_local = Matrix()

        met_skeleton = bone_mapping.RigifyMeta()

        if self.rigify_names:
            # check if doesn't contain rigify deform bones already
            bones_needed = met_skeleton.spine.hips, met_skeleton.spine.spine
            if not [b for b in bones_needed if b in src_armature.bones]:
                # Converted settings should not be validated yet, as bones have not been renamed
                src_skeleton, trg_skeleton = ConvertBoneNaming.convert_settings(current_settings, 'Rigify_Deform.py', validate=False)
                ConvertBoneNaming.rename_bones(context, src_skeleton, trg_skeleton, skip_ik=True)
                src_skeleton = bone_mapping.RigifySkeleton()

                for name_attr in ('left_eye', 'right_eye'):
                    bone_name = getattr(src_skeleton.face, name_attr)

                    if bone_name not in src_armature.bones and bone_name[4:] in src_armature.bones:
                        # fix eye bones lacking "DEF-" prefix on b3.2
                        setattr(src_skeleton.face, name_attr, bone_name[4:])

                    if src_skeleton.face.super_copy:
                        # supercopy def bones start with DEF-
                        bone_name = getattr(src_skeleton.face, name_attr)

                        if not bone_name.startswith('DEF-'):
                            new_name = f"DEF-{bone_name}"
                            try:
                                context.object.data.bones[bone_name].name = new_name
                            except KeyError:
                                pass
                            else:
                                setattr(src_skeleton.face, name_attr, new_name)


        # bones that have rigify attr will be copied when the metarig is in edit mode
        additional_bones = [(b.name, b.rigify_type) for b in src_object.pose.bones if b.rigify_type]

        try:
            metarig = next(ob for ob in bpy.data.objects if ob.type == 'ARMATURE' and ob.data.rigify_target_rig == src_object)
        except AttributeError:
            self.report({'WARNING'}, 'Rigify Add-On not enabled')
            return {'CANCELLED'}
        except StopIteration:
            create_metarig = True
            met_armature = bpy.data.armatures.new('metarig')
            metarig = bpy.data.objects.new("metarig", met_armature)
            try:
                metarig.data.rigify_rig_basename = src_object.name
            except AttributeError:
                # removed in rigify 0.6.4
                pass

            context.collection.objects.link(metarig)
        else:
            met_armature = metarig.data
            create_metarig = False

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')

        metarig.select_set(True)
        context.view_layer.objects.active = metarig
        bpy.ops.object.mode_set(mode='EDIT')

        if create_metarig:
            from rigify.metarigs import human
            human.create(metarig)

        def match_meta_bone(met_bone_group, src_bone_group, bone_attr, axis=None):
            try:
                met_bone = met_armature.edit_bones[getattr(met_bone_group, bone_attr)]
                src_bone_name = getattr(src_bone_group, bone_attr)
                src_bone = src_armature.bones.get(src_bone_name, None)
            except KeyError:
                return

            if not src_bone:
                self.report({'WARNING'}, f"{bone_attr}, {src_bone_name} not found in {src_armature}")
                return

            met_bone.head = src_bone.head_local
            met_bone.tail = src_bone.tail_local

            if met_bone.parent and met_bone.use_connect:
                bone_dir = met_bone.vector.normalized()
                parent_dir = met_bone.parent.vector.normalized()

                if bone_dir.dot(parent_dir) < -0.6:
                    self.report({'WARNING'}, f"{met_bone.name} is not aligned with its parent")
                    # TODO

            if axis:
                met_bone.roll = bone_utils.ebone_roll_to_vector(met_bone, axis)
            else:
                src_x_axis = Vector((0.0, 0.0, 1.0)) @ src_bone.matrix_local.inverted().to_3x3()
                src_x_axis.normalize()
                met_bone.roll = bone_utils.ebone_roll_to_vector(met_bone, src_x_axis)

            return met_bone

        for bone_attr in ['hips', 'spine', 'spine1', 'spine2', 'neck', 'head']:
            if self.forward_spine_roll:
                align = Vector((0.0, -1.0, 0.0))
            else:
                align = None
            match_meta_bone(met_skeleton.spine, src_skeleton.spine, bone_attr, axis=align)

        for bone_attr in ['shoulder', 'arm', 'forearm', 'hand']:
            match_meta_bone(met_skeleton.right_arm, src_skeleton.right_arm, bone_attr)
            match_meta_bone(met_skeleton.left_arm, src_skeleton.left_arm, bone_attr)

        for bone_attr in ['upleg', 'leg', 'foot', 'toe']:
            match_meta_bone(met_skeleton.right_leg, src_skeleton.right_leg, bone_attr)
            match_meta_bone(met_skeleton.left_leg, src_skeleton.left_leg, bone_attr)

        rigify_face_bones = bone_mapping.rigify_face_bones
        for bone_attr in ['left_eye', 'right_eye', 'jaw']:
            met_bone = match_meta_bone(met_skeleton.face, src_skeleton.face, bone_attr)
            if met_bone:
                try:
                    rigify_face_bones.remove(met_skeleton.face[bone_attr])
                except ValueError:
                    pass

                if src_skeleton.face.super_copy:
                    metarig.pose.bones[met_bone.name].rigify_type = "basic.super_copy"
                    # FIXME: sometimes eye bone group is not renamed accordingly
                    # TODO: then maybe change jaw shape to box

        try:
            right_leg = met_armature.edit_bones[met_skeleton.right_leg.leg]
            left_leg = met_armature.edit_bones[met_skeleton.left_leg.leg]
        except KeyError:
            pass
        else:
            offset = Vector((0.0, self.offset_knee, 0.0))
            for bone in right_leg, left_leg:
                bone.head += offset

            try:
                right_knee = met_armature.edit_bones[met_skeleton.right_arm.forearm]
                left_knee = met_armature.edit_bones[met_skeleton.left_arm.forearm]
            except KeyError:
                pass
            else:
                offset = Vector((0.0, self.offset_elbow, 0.0))

                for bone in right_knee, left_knee:
                    bone.head += offset

        def match_meta_fingers(met_bone_group, src_bone_group, bone_attr):
            met_bone_names = getattr(met_bone_group, bone_attr)
            src_bone_names = getattr(src_bone_group, bone_attr)

            if not src_bone_names:
                print(bone_attr, "not found in", src_armature)
                return
            if not met_bone_names:
                print(bone_attr, "not found in", src_armature)
                return

            if 'thumb' not in bone_attr:
                try:
                    met_bone = met_armature.edit_bones[met_bone_names[0]]
                    src_bone = src_armature.bones.get(src_bone_names[0], None)
                except KeyError:
                    pass
                else:
                    if src_bone:
                        palm_bone = met_bone.parent

                        palm_bone.tail = src_bone.head_local
                        hand_bone = palm_bone.parent
                        palm_bone.head = hand_bone.head * 0.75 + src_bone.head_local * 0.25
                        palm_bone.roll = 0

            for met_bone_name, src_bone_name in zip(met_bone_names, src_bone_names):
                try:
                    met_bone = met_armature.edit_bones[met_bone_name]
                    src_bone = src_armature.bones[src_bone_name]
                except KeyError:
                    print("source bone not found", src_bone_name)
                    continue

                met_bone.head = src_bone.head_local
                try:
                    met_bone.tail = src_bone.children[0].head_local
                except IndexError:
                    bone_utils.align_to_closer_axis(src_bone, met_bone)

                met_bone.roll = 0.0

                src_z_axis = Vector((0.0, 0.0, 1.0)) @ src_bone.matrix_local.to_3x3()

                inv_rot = met_bone.matrix.to_3x3().inverted()
                trg_z_axis = src_z_axis @ inv_rot
                dot_z = (met_bone.z_axis @ met_bone.matrix.inverted()).dot(trg_z_axis)
                met_bone.roll = dot_z * pi

                offset_fingers = Vector(self.offset_fingers) @ src_bone.matrix_local.to_3x3()
                if met_bone.head.x < 0:  # Right side
                    offset_fingers /= -100
                else:
                    offset_fingers /= 100

                if met_bone.parent.name in met_bone_names and met_bone.children:
                    met_bone.head += offset_fingers
                    met_bone.tail += offset_fingers

        for bone_attr in ['thumb', 'index', 'middle', 'ring', 'pinky']:
            match_meta_fingers(met_skeleton.right_fingers, src_skeleton.right_fingers, bone_attr)
            match_meta_fingers(met_skeleton.left_fingers, src_skeleton.left_fingers, bone_attr)

        try:
            met_armature.edit_bones['spine.003'].tail = met_armature.edit_bones['spine.004'].head
            met_armature.edit_bones['spine.005'].head = (met_armature.edit_bones['spine.004'].head + met_armature.edit_bones['spine.006'].head) / 2
        except KeyError:
            pass

        # find foot vertices
        foot_verts = {}
        foot_ob = None
        # pick object with most foot verts
        for ob in bone_utils.iterate_rigged_obs(src_object):
            if src_skeleton.left_leg.foot not in ob.vertex_groups:
                continue
            grouped_verts = bone_utils.get_group_verts(ob, src_skeleton.left_leg.foot, threshold=0.8)
            if len(grouped_verts) > len(foot_verts):
                foot_verts = grouped_verts
                foot_ob = ob

        if foot_verts:
            # find rear verts (heel)
            rearest_y = max([foot_ob.data.vertices[v].co[1] for v in foot_verts])
            leftmost_x = max([foot_ob.data.vertices[v].co[0] for v in foot_verts])  # FIXME: we should counter rotate verts for more accuracy
            rightmost_x = min([foot_ob.data.vertices[v].co[0] for v in foot_verts])

            for side in 'L', 'R':
                # invert left/right vertices when we switch sides
                leftmost_x, rightmost_x = rightmost_x, leftmost_x

                heel_bone = met_armature.edit_bones['heel.02.' + side]

                heel_bone.head.y = rearest_y
                heel_bone.tail.y = rearest_y

                if heel_bone.head.x > 0:
                    heel_head = leftmost_x
                    heel_tail = rightmost_x
                else:
                    heel_head = rightmost_x * -1
                    heel_tail = leftmost_x * -1
                heel_bone.head.x = heel_head
                heel_bone.tail.x = heel_tail

                try:
                    spine_bone = met_armature.edit_bones['spine']
                    pelvis_bone = met_armature.edit_bones['pelvis.' + side]
                except KeyError:
                    pass
                else:
                    pelvis_bone.head = spine_bone.head
                    pelvis_bone.tail.z = spine_bone.tail.z

                try:
                    spine_bone = met_armature.edit_bones['spine.003']
                    breast_bone = met_armature.edit_bones['breast.' + side]
                except KeyError:
                    pass
                else:
                    breast_bone.head.z = spine_bone.head.z
                    breast_bone.tail.z = spine_bone.head.z

        if self.no_face:
            for bone_name in rigify_face_bones:
                try:
                    face_bone = met_armature.edit_bones[bone_name]
                except KeyError:
                    continue

                met_armature.edit_bones.remove(face_bone)

        for src_name, src_attr in additional_bones:
            new_bone_name = bone_utils.copy_bone_to_arm(src_object, metarig, src_name, suffix="")

            if 'chain' in src_attr:  # TODO: also fingers
                # working around weird bug: sometimes src_armature.bones causes KeyError even if the bone is there
                bone = next((b for b in src_armature.bones if b.name == src_name), None)

                new_parent_name = new_bone_name
                while bone:
                    # optional: use connect
                    try:
                        bone = bone.children[0]
                    except IndexError:
                        break

                    child_bone_name = bone_utils.copy_bone_to_arm(src_object, metarig, bone.name, suffix="")
                    child_bone = met_armature.edit_bones[child_bone_name]
                    child_bone.parent = met_armature.edit_bones[new_parent_name]
                    child_bone.use_connect = True

                    bone.name = f"DEF-{bone.name}"
                    new_parent_name = child_bone_name

            try:
                bone = next((b for b in src_armature.bones if b.name == src_name), None)
                
                if bone:
                    if bone.parent:
                        # FIXME: should use mapping to get parent bone name
                        parent_name = bone.parent.name.replace('DEF-', '')
                        met_armature.edit_bones[new_bone_name].parent = met_armature.edit_bones[parent_name]
                    if ".raw_" in src_attr:
                        met_armature.edit_bones[new_bone_name].use_deform = bone.use_deform
                    elif bone.name.startswith('DEF-'):
                        # already a DEF, need to strip that from metarig bone instead
                        met_armature.edit_bones[new_bone_name].name = new_bone_name.replace("DEF-", '')
                    else:
                        bone.name = f'DEF-{bone.name}'
            except KeyError:
                self.report({'WARNING'}, "bones not found in target, perhaps wrong preset?")
                continue

        bpy.ops.object.mode_set(mode='POSE')
        # now we can copy the stored rigify attrs
        for src_name, src_attr in additional_bones:
            src_meta = src_name[4:] if src_name.startswith('DEF-') else src_name
            metarig.pose.bones[src_meta].rigify_type = src_attr
            # TODO: should copy rigify options of specific types as well

        if current_settings.left_leg.upleg_twist_02 or current_settings.left_leg.leg_twist_02:
            metarig.pose.bones['thigh.L']['rigify_parameters']['segments'] = 3

        if current_settings.right_leg.upleg_twist_02 or current_settings.right_leg.leg_twist_02:
            metarig.pose.bones['thigh.R']['rigify_parameters']['segments'] = 3
        
        if current_settings.left_arm.arm_twist_02 or current_settings.left_arm.forearm_twist_02:
            metarig.pose.bones['upper_arm.L']['rigify_parameters']['segments'] = 3
        
        if current_settings.right_arm.arm_twist_02 or current_settings.right_arm.forearm_twist_02:
            metarig.pose.bones['upper_arm.R']['rigify_parameters']['segments'] = 3

        if self.assign_metarig:
            met_armature.rigify_target_rig = src_object

        metarig.parent = src_object.parent

        return {'FINISHED'}


class ActionRangeToScene(bpy.types.Operator):
    """Set Playback range to current action Start/End"""
    bl_idname = "object.expykit_action_to_range"
    bl_label = "Action Range to Scene"
    bl_description = "Match scene range with current action range"
    bl_options = {'REGISTER', 'UNDO'}

    _allowed_modes_ = ['POSE', 'OBJECT']

    @classmethod
    def poll(cls, context):
        obj = context.object

        if not obj:
            return False
        if obj.mode not in cls._allowed_modes_:
            return False
        if not obj.animation_data:
            return False
        if not obj.animation_data.action:
            return False

        return True

    def execute(self, context):
        action_range = context.object.animation_data.action.frame_range

        scn = context.scene
        scn.frame_start = int(action_range[0])
        scn.frame_end = int(action_range[1])

        try:
            bpy.ops.action.view_all()
        except RuntimeError:
            # we are not in a timeline context, let's look for one in the screen
            for window in context.window_manager.windows:
                screen = window.screen
                for area in screen.areas:
                    if area.type == 'DOPESHEET_EDITOR':
                        for region in area.regions:
                            if region.type == 'WINDOW':
                                with context.temp_override(window=window,
                                                           area=area,
                                                           region=region):
                                    bpy.ops.action.view_all()
                                break
                        break
        return {'FINISHED'}


class ActionEndToLastKeyframe(bpy.types.Operator):
    """Set end frame to last keyframe of current action"""
    bl_idname = "object.expykit_action_end_to_last_keyframe"
    bl_label = "To Frame Range"
    bl_description = "Set end frame to last keyframe of current action"
    bl_options = {'REGISTER', 'UNDO'}

    _allowed_modes_ = ['POSE', 'OBJECT']

    @classmethod
    def poll(cls, context):
        obj = context.object

        if not obj:
            return False
        if obj.mode not in cls._allowed_modes_:
            return False
        if not obj.animation_data:
            return False
        if not obj.animation_data.action:
            return False

        return True

    def execute(self, context):
        action = context.object.animation_data.action
        
        # Find the last keyframe in the action
        last_keyframe = 0
        for fcurve in get_fcurves(action):
            if fcurve.keyframe_points:
                for keyframe in fcurve.keyframe_points:
                    if keyframe.co[0] > last_keyframe:
                        last_keyframe = keyframe.co[0]
        
        if last_keyframe > 0:
            context.scene.frame_end = int(last_keyframe)
            self.report({'INFO'}, f"End frame set to {int(last_keyframe)}")
        else:
            self.report({'WARNING'}, "No keyframes found in current action")

        return {'FINISHED'}


class MergeHeadTails(bpy.types.Operator):
    """Connect head/tails when closer than given max distance"""
    bl_idname = "armature.expykit_merge_head_tails"
    bl_label = "Merge Head/Tails"
    bl_description = "Connect head/tails when closer than given max distance"
    bl_options = {'REGISTER', 'UNDO'}

    at_child_head: BoolProperty(
        name="Match at child head",
        description="Bring parent's tail to match child head when possible",
        default=True
    )

    min_distance: FloatProperty(
        name="Distance",
        description="Max Distance for merging",
        default=0.0
    )

    selected_only: BoolProperty(name="Only Selected",
                                default=False)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj:
            return False
        if obj.mode != 'EDIT':
            return False
        if obj.type != 'ARMATURE':
            return False

        return True

    def execute(self, context):
        if self.selected_only:
            selected_names = [bone.name for bone in context.selected_bones]
            bones = [bone for bone in context.object.data.edit_bones if bone.name in selected_names]
        else:
            bones = context.object.data.edit_bones

        for bone in bones:
            if bone.use_connect:
                continue
            if not bone.parent:
                continue

            distance = (bone.parent.tail - bone.head).length
            if distance <= self.min_distance:
                if self.at_child_head and len(bone.parent.children) == 1:
                    bone.parent.tail = bone.head

                bone.use_connect = True

        context.object.update_from_editmode()

        return {'FINISHED'}


def mute_fcurves(obj: bpy.types.Object, channel_name: str):
    action = obj.animation_data.action
    if not action:
        return
    
    for fc in get_fcurves(action):
        if fc.data_path == channel_name:
            fc.mute = True

def limit_scale(obj):
    constr = obj.constraints.new('LIMIT_SCALE')
    
    constr.owner_space = 'LOCAL'
    constr.min_x = obj.scale[0]
    constr.min_y = obj.scale[1]
    constr.min_z = obj.scale[2]

    constr.max_x = obj.scale[0]
    constr.max_y = obj.scale[1]
    constr.max_z = obj.scale[2]

    constr.use_min_x = True
    constr.use_min_y = True
    constr.use_min_z = True

    constr.use_max_x = True
    constr.use_max_y = True
    constr.use_max_z = True


class ConvertGameFriendly(bpy.types.Operator):
    """Convert Rigify (0.5) rigs to a Game Friendly hierarchy"""
    bl_idname = "armature.expykit_convert_gamefriendly"
    bl_label = "Rigify Game Friendly"
    bl_description = "Make the rigify deformation bones a one root rig"
    bl_options = {'REGISTER', 'UNDO'}

    keep_backup: BoolProperty(
        name="Backup",
        description="Keep copy of datablock",
        default=True
    )
    rename: StringProperty(
        name="Rename",
        description="Rename rig to 'Armature'",
        default="Armature"
    )
    eye_bones: BoolProperty(
        name="Keep eye bones",
        description="Activate 'deform' for eye bones",
        default=True
    )
    limit_scale: BoolProperty(
        name="Limit Spine Scale",
        description="Limit scale on the spine deform bones",
        default=True
    )
    disable_bendy: BoolProperty(
        name="Disable B-Bones",
        description="Disable Bendy-Bones",
        default=True
    )
    fix_tail: BoolProperty(
        name="Invert Tail",
        description="Reverse the tail direction so that it spawns from hip",
        default=True
    )
    reparent_twist: BoolProperty(
        name="Dispossess Twist Bones",
        description="Rearrange Twist Hierarchy in limbs for in game IK",
        default=True
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj:
            return False
        if obj.mode != 'POSE':
            return False
        if obj.type != 'ARMATURE':
            return False
        return bool(context.active_object.data.get("rig_id"))

    def execute(self, context):
        ob = context.active_object
        if self.keep_backup:
            backup_data = ob.data.copy()
            backup_data.name = ob.name + "_GameUnfriendly_backup"
            backup_data.use_fake_user = True

        if self.rename:
            ob.name = self.rename
            ob.data.name = self.rename

            try:
                metarig = next(
                    obj for obj in bpy.data.objects if obj.type == 'ARMATURE' and obj.data.rigify_target_rig == ob)
            except (StopIteration, AttributeError):  # Attribute Error if Rigify is not loaded
                pass
            else:
                try:
                    metarig.data.rigify_rig_basename = self.rename
                except AttributeError:
                    # Removed in rigify 0.6.4
                    pass

        if self.eye_bones and 'DEF-eye.L' not in ob.pose.bones:
            # Old rigify face: eyes deform is disabled
            # FIXME: the 'DEF-eye.L' condition should be checked on invoke
            try:
                # Oddly, changes to use_deform are not kept
                ob.pose.bones["MCH-eye.L"].bone.use_deform = True
                ob.pose.bones["MCH-eye.R"].bone.use_deform = True
            except KeyError:
                pass

        bpy.ops.object.mode_set(mode='EDIT')
        num_reparents = bone_utils.gamefriendly_hierarchy(ob, fix_tail=self.fix_tail, limit_scale=self.limit_scale)

        if self.reparent_twist:
            arm_bones = ["DEF-upper_arm", "DEF-forearm", "DEF-hand"]
            leg_bones = ["DEF-thigh", "DEF-shin", "DEF-foot"]
            for side in ".L", ".R":
                for bone_names in arm_bones, leg_bones:
                    parent_bone = ob.data.edit_bones[bone_names.pop(0) + side] 
                    for bone in bone_names:
                        e_bone = ob.data.edit_bones[bone + side]
                        e_bone.use_connect = False

                        e_bone.parent = parent_bone
                        parent_bone = e_bone

                        num_reparents += 1

        bpy.ops.object.mode_set(mode='POSE')

        if self.disable_bendy:
            for bone in ob.data.bones:
                bone.bbone_segments = 1
                # TODO: disable bbone drivers

        self.report({'INFO'}, f'{num_reparents} bones were re-parented')
        return {'FINISHED'}


class ConstrainToArmature(bpy.types.Operator):
    bl_idname = "armature.expykit_constrain_to_armature"
    bl_label = "Bind to Active Armature"
    bl_description = "Constrain bones of selected armatures to active armature"
    bl_options = {'REGISTER', 'UNDO'}

    src_preset: EnumProperty(items=preset_handler.iterate_presets_with_current,
                             name="To Bind",
                             options={'SKIP_SAVE'}
                             )

    trg_preset: EnumProperty(items=preset_handler.iterate_presets_with_current,
                             name="Bind To",
                             options={'SKIP_SAVE'}
                             )
    
    only_selected: BoolProperty(name="Only Selected", default=False, description="Bind only selected bones")
    
    bind_by_name: BoolProperty(name="Bind bones by name", default=True)
    name_prefix: StringProperty(name="Add prefix to name", default="")
    name_replace: StringProperty(name="Replace in name", default="")
    name_replace_with: StringProperty(name="Replace in name with", default="")
    name_suffix: StringProperty(name="Add suffix to name", default="")

    if bpy.app.version[0] < 4:
        ret_bones_layer: IntProperty(name="Layer",
                                    min=0, max=29, default=24,
                                    description="Armature Layer to use for connection bones")
        use_legacy_index = True
    else:
        ret_bones_collection: StringProperty(name="Layer",
                                             default="Retarget Bones",
                                             description="Armature collection to use for connection bones")
        use_legacy_index = False

    match_transform: EnumProperty(items=[
        ('None', "- None -", "Don't match any transform"),
        ('Bone', "Bones Offset", "Account for difference between control and deform rest pose (Requires similar proportions and Y bone-axis)"),
        ('Pose', "Current Pose is target Rest Pose", "Armature was posed manually to match rest pose of target"),
        ('World', "Follow target Pose in world space", "Just copy target world positions (Same bone orient, different rest pose)"),
    ],
        name="Match Transform",
        default='None')
    
    match_object_transform: BoolProperty(name="Match Object Transform", default=True)

    math_look_at: BoolProperty(name="Fix direction",
                               description="Correct chain direction based on mid limb (Useful for IK)",
                               default=False)
    
    copy_IK_roll_hands: BoolProperty(name="Hands IK Roll",
                            description="USe IK target roll from source armature (Useful for IK)",
                            default=False)
    
    copy_IK_roll_feet: BoolProperty(name="Feet IK Roll",
                            description="USe IK target roll from source armature (Useful for IK)",
                            default=False)
    
    fit_target_scale: EnumProperty(name="Fit height",
                                   items=(('--', '- None -', 'None'),
                                          ('head', 'head', 'head'),
                                          ('neck', 'neck', 'neck'),
                                          ('spine2', 'chest', 'spine2'),
                                          ('spine1', 'spine1', 'spine1'),
                                          ('spine', 'spine', 'spine'),
                                          ('hips', 'hips', 'hips'),
                                          ),
                                    default='--',
                                    description="Fit height of the target Armature at selected bone")
    adjust_location: BoolProperty(default=True, name="Adjust location to new scale")

    constrain_root: EnumProperty(items=[
        ('None', "No Root", "Don't constrain root bone"),
        ('Bone', "Bone", "Constrain root to bone"),
        ('Object', "Object", "Constrain root to object")
    ],
        name="Constrain Root",
        default='Bone')

    loc_constraints: BoolProperty(name="Copy Location",
                                  description="Use Location Constraint when binding",
                                  default=False)
    
    rot_constraints: BoolProperty(name="Copy Rotation",
                                  description="Use Rotation Constraint when binding",
                                  default=True)
    
    scale_constraints: BoolProperty(name="Copy Scale",
                                   description="Use Scale Constraint when binding",
                                   default=False)
    
    # Removed copy_visibility_tracks property as requested
    
    constraint_policy: EnumProperty(items=[
        ('skip', "Skip Existing Constraints", "Skip Bones that are constrained already"),
        ('disable', "Disable Existing Constraints", "Disable existing binding constraints and add new ones"),
        ('remove', "Delete Existing Constraints", "Delete existing binding constraints")
        ],
        name="Policy",
        description="Action to take with existing constraints",
        default='skip'
        )

    bind_floating: BoolProperty(name="Bind Floating",
                                description="Always bind unparented bones Location and Rotation",
                                default=True)

    root_motion_bone: StringProperty(name="Root Motion",
                                     description="Constrain Root bone to Hip motion",
                                     default="")

    root_cp_loc_x: BoolProperty(name="Root Copy Loc X", description="Copy Root X Location", default=True)
    root_cp_loc_y: BoolProperty(name="Root Copy Loc y", description="Copy Root Y Location", default=True)
    root_cp_loc_z: BoolProperty(name="Root Copy Loc Z", description="Copy Root Z Location", default=True)

    root_use_loc_min_x: BoolProperty(name="Use Root Min X", description="Minimum Root X", default=False)
    root_use_loc_min_y: BoolProperty(name="Use Root Min Y", description="Minimum Root Y", default=False)
    root_use_loc_min_z: BoolProperty(name="Use Root Min Z", description="Minimum Root Z", default=True)

    root_loc_min_x: FloatProperty(name="Root Min X", description="Minimum Root X", default=0.0)
    root_loc_min_y: FloatProperty(name="Root Min Y", description="Minimum Root Y", default=0.0)
    root_loc_min_z: FloatProperty(name="Root Min Z", description="Minimum Root Z", default=0.0)

    root_use_loc_max_x: BoolProperty(name="Use Root Max X", description="Maximum Root X", default=False)
    root_use_loc_max_y: BoolProperty(name="Use Root Max Y", description="Maximum Root Y", default=False)
    root_use_loc_max_z: BoolProperty(name="Use Root Max Z", description="Maximum Root Z", default=False)

    root_loc_max_x: FloatProperty(name="Root Max X", description="Maximum Root X", default=0.0)
    root_loc_max_y: FloatProperty(name="Root Max Y", description="Maximum Root Y", default=0.0)
    root_loc_max_z: FloatProperty(name="Root Max Z", description="Maximum Root Z", default=0.0)

    root_cp_rot_x: BoolProperty(name="Root Copy Rot X", description="Copy Root X Rotation", default=True)
    root_cp_rot_y: BoolProperty(name="Root Copy Rot y", description="Copy Root Y Rotation", default=True)
    root_cp_rot_z: BoolProperty(name="Root Copy Rot Z", description="Copy Root Z Rotation", default=True)

    copy_scale: BoolProperty(name="Copy Scale", description="Copy Scale from motion bone", default=False)

    no_finger_loc: BoolProperty(default=False, name="No Finger Location")

    prefix_separator: StringProperty(
        name="Prefix Separator",
        description="Separator between prefix and name, i.e: MyCharacter:head",
        default=":"
    )

    force_dialog: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    
    _autovars_unset = True
    _constrained_root = None

    _prop_indent = 0.15
    
    @property
    def _bind_constraints(self):
        constrs = []
        if self.loc_constraints:
            constrs.append('COPY_LOCATION')
        if self.rot_constraints:
            constrs.append('COPY_ROTATION')
        if self.scale_constraints:
            constrs.append('COPY_SCALE')

        return constrs

    @classmethod
    def poll(cls, context):
        if len(context.selected_objects) != 2:
            return False
        if context.mode != 'POSE':
            return False
        for ob in context.selected_objects:
            if ob.type != 'ARMATURE':
                return False

        return True

    def invoke(self, context, event):
        # Set to use current Expy Kit settings if found
        to_bind = next(ob for ob in context.selected_objects if ob != context.active_object)

        if to_bind.data.expykit_retarget.has_settings():
            self.src_preset = '--Current--'
        if context.active_object.data.expykit_retarget.has_settings():
            self.trg_preset = '--Current--'

        if self.force_dialog:
            return context.window_manager.invoke_props_dialog(self)

        return self.execute(context)        

    def draw(self, context):
        layout = self.layout
        column = layout.column()

        row = column.row()
        row.prop(self, 'src_preset', text="To Bind")
    
        row = column.row()
        row.prop(self, 'trg_preset', text="Bind To")

        if self.force_dialog:
            return

        column.separator()
        row = column.row()
        row.label(text='Conversion')

        row = column.split(factor=self._prop_indent, align=True)
        row.separator()
        col = row.column()
        col.prop(self, 'match_transform', text='')
        col.prop(self, 'match_object_transform')
        col.prop(self, 'fit_target_scale')
        if self.fit_target_scale != "--":
            col.prop(self, 'adjust_location')

        if not self.loc_constraints and self.match_transform == 'Bone':
            col.label(text="'Copy Location' might be required", icon='ERROR')
        elif self.fit_target_scale == '--' and self.match_transform == 'Pose':
            col.label(text="'Fit height' might improve results", icon='ERROR')
        else:
            col.separator()

        column.separator()
        row = column.row()
        row.label(text='Constraints')

        row = column.row()
        row = column.split(factor=self._prop_indent, align=True)
        row.separator()

        constr_col = row.column()
        
        copy_loc_row = constr_col.row()
        copy_loc_row.prop(self, 'loc_constraints')
        if self.loc_constraints:
            copy_loc_row.prop(self, 'no_finger_loc', text="Except Fingers")
        else:
            copy_loc_row.prop(self, 'bind_floating', text="Only Floating")
        
        copy_rot_row = constr_col.row()
        copy_rot_row.prop(self, 'rot_constraints')
        copy_rot_row.prop(self, 'math_look_at')
        
        copy_scale_row = constr_col.row()
        copy_scale_row.prop(self, 'scale_constraints')

        # Removed copy_visibility_tracks UI as requested

        ik_aim_row = constr_col.row()
        ik_aim_row.prop(self, 'copy_IK_roll_hands')
        ik_aim_row.prop(self, 'copy_IK_roll_feet')

        row = column.split(factor=self._prop_indent, align=True)
        constr_col.prop(self, 'constraint_policy', text='')
        
        column.separator()
        row = column.row()
        row.label(text="Affect Bones")
        
        row = column.row()
        row = column.split(factor=self._prop_indent, align=True)
        row.separator()
        col = row.column()
        col.prop(self, 'only_selected')
        row.prop(self, 'bind_by_name', text="Also by Name")
        if self.bind_by_name:
            row = column.row()
            col = row.column()
            col.label(text="Prefix")
            col.prop(self, 'name_prefix', text="")

            col = row.column()
            col.label(text="Replace:")
            col.prop(self, 'name_replace', text="")

            col = row.column()
            col.label(text="With:")
            col.prop(self, 'name_replace_with', text="")

            col = row.column()
            col.label(text="Suffix:")
            col.prop(self, 'name_suffix', text="")

        column.separator()
        row = column.row()
        row.label(text="Root Animation")
        row = column.split(factor=self._prop_indent, align=True)
        row.separator()
        row.prop(self, 'constrain_root', text="")

        if self.constrain_root != 'None':
            row = column.split(factor=self._prop_indent, align=True)
            row.label(text="")
            row.prop_search(self, 'root_motion_bone',
                            context.active_object.data,
                            "bones", text="")

        if self.constrain_root != 'None':
            row = column.row(align=True)
            row.label(text="Location")
            row.prop(self, "root_cp_loc_x", text="X", toggle=True)
            row.prop(self, "root_cp_loc_y", text="Y", toggle=True)
            row.prop(self, "root_cp_loc_z", text="Z", toggle=True)

            if any((self.root_cp_loc_x, self.root_cp_loc_y, self.root_cp_loc_z)):
                column.separator()

                # Min/Max X
                if self.root_cp_loc_x:
                    row = column.row(align=True)
                    row.prop(self, "root_use_loc_min_x", text="Min X")

                    subcol = row.column()
                    subcol.prop(self, "root_loc_min_x", text="")
                    subcol.enabled = self.root_use_loc_min_x

                    row.separator()
                    row.prop(self, "root_use_loc_max_x", text="Max X")
                    subcol = row.column()
                    subcol.prop(self, "root_loc_max_x", text="")
                    subcol.enabled = self.root_use_loc_max_x
                    row.enabled = self.root_cp_loc_x

                # Min/Max Y
                if self.root_cp_loc_y:
                    row = column.row(align=True)
                    row.prop(self, "root_use_loc_min_y", text="Min Y")

                    subcol = row.column()
                    subcol.prop(self, "root_loc_min_y", text="")
                    subcol.enabled = self.root_use_loc_min_y

                    row.separator()
                    row.prop(self, "root_use_loc_max_y", text="Max Y")
                    subcol = row.column()
                    subcol.prop(self, "root_loc_max_y", text="")
                    subcol.enabled = self.root_use_loc_max_y
                    row.enabled = self.root_cp_loc_y

                # Min/Max Z
                if self.root_cp_loc_z:
                    row = column.row(align=True)
                    row.prop(self, "root_use_loc_min_z", text="Min Z")

                    subcol = row.column()
                    subcol.prop(self, "root_loc_min_z", text="")
                    subcol.enabled = self.root_use_loc_min_z

                    row.separator()
                    row.prop(self, "root_use_loc_max_z", text="Max Z")
                    subcol = row.column()
                    subcol.prop(self, "root_loc_max_z", text="")
                    subcol.enabled = self.root_use_loc_max_z
                    row.enabled = self.root_cp_loc_z

                column.separator()

            row = column.row(align=True)
            row.label(text="Rotation")
            row.prop(self, "root_cp_rot_x", text="X", toggle=True)
            row.prop(self, "root_cp_rot_y", text="Y", toggle=True)
            row.prop(self, "root_cp_rot_z", text="Z", toggle=True)
            
            row = column.row()
            row.prop(self, "copy_scale")

            column.separator()

        column.separator()
        if self.use_legacy_index:
            row = column.split(factor=self._prop_indent, align=True)
            row.separator()
            row.prop(self, 'ret_bones_layer')
        else:
            row = column.row()
            row.prop(self, 'ret_bones_collection', text="Layer")

    def _bone_bound_already(self, bone):
        for constr in bone.constraints:
            if constr.type in self._bind_constraints:
                return True

        return False

    def _add_limit_constraintss(self, ob, rot=True, loc=True, scale=False):
        limit_constraints = []
        if self.match_transform == 'Pose':
            return limit_constraints

        if rot:
            limit_rot = ob.constraints.new('LIMIT_ROTATION')
            limit_rot.use_limit_x = True
            limit_rot.use_limit_y = True
            limit_rot.use_limit_z = True

            limit_constraints.append(limit_rot)

        def limit_all(constr):
            constr.use_min_x = True
            constr.use_min_y = True
            constr.use_min_z = True
            constr.use_max_x = True
            constr.use_max_y = True
            constr.use_max_z = True

        if loc:
            limit_loc = ob.constraints.new('LIMIT_LOCATION')
            limit_all(limit_loc)
            limit_constraints.append(limit_loc)

        if scale:
            limit_scale = ob.constraints.new('LIMIT_SCALE')
            limit_scale.min_x = 1.0
            limit_scale.min_y = 1.0
            limit_scale.min_z = 1.0
            
            limit_scale.max_x = 1.0
            limit_scale.max_y = 1.0
            limit_scale.max_z = 1.0

            limit_all(limit_scale)
            limit_constraints.append(limit_scale)

        return limit_constraints
    
    def _setup_sap_sync(self, source_armature, target_armature):
        """
        Set up real-time SAP Data synchronization between source and target armatures.
        When the source armature's main action changes, the target's SAP Data action will automatically update.
        """
        # Store the sync relationship in a global dictionary for the sync handler to use
        global _sap_sync_pairs
        if '_sap_sync_pairs' not in globals():
            _sap_sync_pairs = []
        
        # Clean object names for proper SAP action lookup
        clean_source_name = source_armature.name.split(".")[0] if "." in source_armature.name else source_armature.name
        clean_target_name = target_armature.name.split(".")[0] if "." in target_armature.name else target_armature.name
        
        # Store the sync pair
        sync_pair = {
            'source_object': source_armature.name,
            'target_object': target_armature.name,
            'clean_source_name': clean_source_name,
            'clean_target_name': clean_target_name
        }
        
        # Remove any existing sync pair for this target to avoid duplicates
        _sap_sync_pairs = [
            pair for pair in _sap_sync_pairs 
            if pair.get('target_object') != target_armature.name
        ]
        
        _sap_sync_pairs.append(sync_pair)
        
        # Set up initial sync if source has a current action
        if (source_armature.animation_data and source_armature.animation_data.action):
            current_action = source_armature.animation_data.action
            
            # Clean action name
            action_name = current_action.name
            if "|" in action_name:
                clean_action_name = action_name.split("|")[-1]
            else:
                clean_action_name = action_name
            
            # Look for corresponding target SAP action.
            # Try both the clean target name and the full object name (which may include suffix like .001)
            target_prefix_candidates = []
            try:
                target_prefix_candidates.append(target_armature.name)
            except Exception:
                pass
            target_prefix_candidates.append(clean_target_name)

            target_sap_action = None
            chosen_name = None
            for prefix in target_prefix_candidates:
                candidate_name = f"{prefix} {clean_action_name} SAP Data"
                target_sap_action = bpy.data.actions.get(candidate_name)
                if target_sap_action:
                    chosen_name = candidate_name
                    break
            
            if target_sap_action:
                # Ensure target has armature data animation data
                if not target_armature.data.animation_data:
                    target_armature.data.animation_data_create()
                
                # Set the target's SAP action
                target_armature.data.animation_data.action = target_sap_action
                print(f"  Initial sync: Set target SAP action to '{chosen_name}'")
        
        # Start the sync handler if not already running
        if not self._is_sap_sync_handler_active():
            self._start_sap_sync_handler()
    
    def _is_sap_sync_handler_active(self):
        """Check if the SAP sync handler is already active"""
        return bpy.app.timers.is_registered(_sap_sync_timer_func)
    
    def _start_sap_sync_handler(self):
        """Start the SAP sync timer handler"""
        if not bpy.app.timers.is_registered(_sap_sync_timer_func):
            bpy.app.timers.register(_sap_sync_timer_func, first_interval=0.1, persistent=True)
            print("  Started SAP sync timer")
    
    def execute(self, context):
        # force_dialog limits drawn properties and is no longer required
        self.force_dialog = False

        trg_ob = context.active_object

        if self.trg_preset == '--':
            return {'FINISHED'}
        if self.src_preset == '--':
            return {'FINISHED'}

        if self.trg_preset == '--Current--' and trg_ob.data.expykit_retarget.has_settings():
            trg_settings = trg_ob.data.expykit_retarget
            trg_skeleton = preset_handler.get_settings_skel(trg_settings)
        else:
            trg_skeleton = preset_handler.set_preset_skel(self.trg_preset)

            if not trg_skeleton:
                return {'FINISHED'}

        cp_suffix = 'RET'
        prefix = ""

        fit_scale = False
        if self.fit_target_scale != '--':
            try:
                trg_bone = trg_ob.pose.bones[getattr(trg_skeleton.spine, self.fit_target_scale)]
            except KeyError:
                pass
            else:
                fit_scale = True
                trg_height = (trg_ob.matrix_world @ trg_bone.bone.head_local)

        for ob in context.selected_objects:
            if ob == trg_ob:
                continue

            src_settings = ob.data.expykit_retarget
            if self.src_preset == '--Current--' and ob.data.expykit_retarget.has_settings():    
                if not src_settings.has_settings():
                    return {'FINISHED'}
                src_skeleton = preset_handler.get_settings_skel(src_settings)
            else:
                src_skeleton = preset_handler.get_preset_skel(self.src_preset, src_settings)
                if not src_skeleton:
                    return {'FINISHED'}

            if fit_scale:
                ob_height = (ob.matrix_world @ ob.pose.bones[getattr(src_skeleton.spine, self.fit_target_scale)].bone.head_local)
                height_ratio = ob_height[2] / trg_height[2]
                
                mute_fcurves(trg_ob, 'scale')
                trg_ob.scale *= height_ratio
                limit_scale(trg_ob)

                if self.adjust_location:
                    # scale location animation to avoid offset
                    trg_action = trg_ob.animation_data.action
                    for fc in get_fcurves(trg_action):
                        data_path = fc.data_path

                        if not data_path.endswith('location'):
                            continue

                        for kf in fc.keyframe_points:
                            kf.co[1] /= height_ratio

            bone_names_map = src_skeleton.conversion_map(trg_skeleton)
            def_skeleton = preset_handler.get_preset_skel(src_settings.deform_preset)
            if def_skeleton:
                deformation_map = src_skeleton.conversion_map(def_skeleton)
            else:
                deformation_map = None

            if self.bind_by_name:
                # Look for bones present in both
                for bone in ob.pose.bones:
                    bone_name = bone.name
                    bone_look_up = self.name_prefix + bone_name.replace(self.name_replace, self.name_replace_with) + self.name_suffix
                    if bone_look_up in bone_names_map:
                        continue
                    if bone_utils.is_pose_bone_all_locked(bone):
                        continue
                    if bone_look_up in trg_ob.pose.bones:
                        bone_names_map[bone_name] = bone_look_up

            look_ats = {}

            if self.constrain_root == 'None':
                try:
                    del bone_names_map[src_skeleton.root]
                except KeyError:
                    pass
                self._constrained_root = None
            elif self.constrain_root == 'Bone':
                bone_names_map[src_skeleton.root] = self.root_motion_bone
            
            if self.only_selected:
                b_names = list(bone_names_map.keys())
                for b_name in b_names:
                    if not b_name:
                        continue
                    try:
                        bone = ob.data.bones[b_name]
                    except KeyError:
                        continue

                    if not bone.select:
                        del bone_names_map[b_name]

            # hacky, but will do it: keep target armature in place during binding
            limit_constraints = self._add_limit_constraintss(trg_ob, scale=self.scale_constraints)
            
            if not self.use_legacy_index:
                try:
                    ret_collection = trg_ob.data.collections[self.ret_bones_collection]
                except KeyError:
                    ret_collection = trg_ob.data.collections.new(self.ret_bones_collection)
                    ret_collection.is_visible = False

            # create Retarget bones
            bpy.ops.object.mode_set(mode='EDIT')
            for src_name, trg_name in bone_names_map.items():
                if not src_name:
                    continue

                if self.constraint_policy == 'skip':
                    try:
                        pb = ob.pose.bones[src_name]
                    except KeyError:
                        pass
                    else:
                        if self._bone_bound_already(pb):
                            continue

                is_object_root = src_name == src_skeleton.root and self.constrain_root == 'Object'
                if not trg_name and not is_object_root:
                    continue

                trg_name = str(prefix) + str(trg_name)

                new_bone_name = bone_utils.copy_bone_to_arm(ob, trg_ob, src_name, suffix=cp_suffix)
                if not new_bone_name:
                    continue
                try:
                    new_parent = trg_ob.data.edit_bones[trg_name]
                except KeyError:
                    if is_object_root:
                        new_parent = None
                    else:
                        self.report({'WARNING'}, f"{trg_name} not found in target")
                        continue

                new_bone = trg_ob.data.edit_bones[new_bone_name]
                new_bone.parent = new_parent

                if self.match_transform == 'Bone':
                    # counter deformation bone transform

                    if deformation_map:
                        try:
                            def_bone = ob.data.edit_bones[deformation_map[src_name]]
                        except KeyError:
                            def_bone = ob.data.edit_bones[src_name]
                    else:
                        def_bone = ob.data.edit_bones[src_name]

                    try:
                        trg_ed_bone = trg_ob.data.edit_bones[trg_name]
                    except KeyError:
                        continue

                    new_bone.transform(def_bone.matrix.inverted())

                    # even transform
                    if self.match_object_transform:
                        new_bone.transform(ob.matrix_world)
                    # counter target transform
                    new_bone.transform(trg_ob.matrix_world.inverted())
                    
                    # align target temporarily
                    trg_roll = trg_ed_bone.roll
                    trg_ed_bone.roll = bone_utils.ebone_roll_to_vector(trg_ed_bone, def_bone.z_axis)

                    # bring under trg_bone
                    new_bone.transform(trg_ed_bone.matrix)

                    # restore target orient
                    trg_ed_bone.roll = trg_roll

                    new_bone.roll = bone_utils.ebone_roll_to_vector(trg_ed_bone, def_bone.z_axis)
                elif self.match_transform == 'Pose':
                    new_bone.matrix = ob.pose.bones[src_name].matrix
                    if self.match_object_transform:
                        new_bone.transform(ob.matrix_world)
                    new_bone.transform(trg_ob.matrix_world.inverted_safe())
                elif self.match_transform == 'World':
                    new_bone.head = new_bone.parent.head
                    new_bone.tail = new_bone.parent.tail
                    new_bone.roll = new_bone.parent.roll
                    if self.match_object_transform:
                        new_bone.transform(ob.matrix_world)
                else:
                    src_bone = ob.data.bones[src_name]
                    src_z_axis_neg = Vector((0.0, 0.0, 1.0)) @ src_bone.matrix_local.inverted().to_3x3()
                    src_z_axis_neg.normalize()

                    new_bone.roll = bone_utils.ebone_roll_to_vector(new_bone, src_z_axis_neg)

                    if self.match_object_transform:
                        new_bone.transform(ob.matrix_world)
                        new_bone.transform(trg_ob.matrix_world.inverted())

                if self.copy_IK_roll_hands:
                    if src_name in (src_skeleton.right_arm_ik.hand,
                                    src_skeleton.left_arm_ik.hand):

                        src_ik = ob.data.bones[src_name]
                        new_bone.roll = bone_utils.ebone_roll_to_vector(new_bone, src_ik.z_axis)
                if self.copy_IK_roll_feet:
                    if src_name in (src_skeleton.left_leg_ik.foot,
                                    src_skeleton.right_leg_ik.foot):

                        src_ik = ob.data.bones[src_name]
                        new_bone.roll = bone_utils.ebone_roll_to_vector(new_bone, src_ik.z_axis)

                if self.use_legacy_index:
                    new_bone.layers[self.ret_bones_layer] = True
                    for i, L in enumerate(new_bone.layers):
                        # FIXME: should be util function
                        if i == self.ret_bones_layer:
                            continue
                        new_bone.layers[i] = False
                else:
                    for coll in new_bone.collections:
                        coll.unassign(new_bone)
                    ret_collection.assign(new_bone)

                if self.math_look_at:
                    if src_name == src_skeleton.right_arm_ik.arm:
                        start_bone_name = trg_skeleton.right_arm_ik.forearm
                    elif src_name == src_skeleton.left_arm_ik.arm:
                        start_bone_name = trg_skeleton.left_arm_ik.forearm
                    elif src_name == src_skeleton.right_leg_ik.upleg:
                        start_bone_name = trg_skeleton.right_leg_ik.leg
                    elif src_name == src_skeleton.left_leg_ik.upleg:
                        start_bone_name = trg_skeleton.left_leg_ik.leg
                    else:
                        start_bone_name = ""

                    if start_bone_name:
                        start_bone = trg_ob.data.edit_bones[prefix + start_bone_name]

                        look_bone = trg_ob.data.edit_bones.new(start_bone_name + '_LOOK')
                        look_bone.head = start_bone.head
                        look_bone.tail = 2 * start_bone.head - start_bone.tail
                        look_bone.parent = start_bone

                        look_ats[src_name] = look_bone.name

                        if self.use_legacy_index:
                            look_bone.layers[self.ret_bones_layer] = True
                            for i, L in enumerate(look_bone.layers):
                                # FIXME: should be util function
                                if i == self.ret_bones_layer:
                                    continue
                                look_bone.layers[i] = False
                        else:
                            for coll in look_bone.collections:
                                coll.unissign(look_bone)
                            ret_collection.assign(look_bone)
                            
            for constr in limit_constraints:
                trg_ob.constraints.remove(constr)

            bpy.ops.object.mode_set(mode='POSE')

            for src_name, trg_name in look_ats.items():
                ret_bone = trg_ob.pose.bones[f'{src_name}_{cp_suffix}']
                constr = ret_bone.constraints.new(type='LOCKED_TRACK')

                constr.head_tail = 1.0
                constr.target = trg_ob
                constr.subtarget = trg_name
                constr.lock_axis = 'LOCK_Y'
                constr.track_axis = 'TRACK_NEGATIVE_Z'

            left_finger_bones = list(chain(*src_skeleton.left_fingers.values()))
            right_finger_bones = list(chain(*src_skeleton.right_fingers.values()))

            for src_name in bone_names_map.keys():
                if not src_name:
                    continue
                if src_name == src_skeleton.root:
                    if self.constrain_root == "None":
                        continue
                    if self.constrain_root == "Bone" and not self.root_motion_bone:
                        continue
                try:
                    src_pbone = ob.pose.bones[src_name]
                except KeyError:
                    continue

                if self._bone_bound_already(src_pbone):
                    if self.constraint_policy == 'skip':
                       continue
                    
                    if self.constraint_policy == 'disable':
                        for constr in src_pbone.constraints:
                            if constr.type in self._bind_constraints:
                                constr.mute = True
                    elif self.constraint_policy == 'remove':
                        for constr in reversed(src_pbone.constraints):
                            if constr.type in self._bind_constraints:
                                src_pbone.constraints.remove(constr)
                    # TODO: should unconstrain mid bones to!

                if not self.loc_constraints and self.bind_floating and is_bone_floating(src_pbone, src_skeleton.spine.hips):
                    constr_types = ['COPY_LOCATION', 'COPY_ROTATION']
                    if self.scale_constraints:
                        constr_types.append('COPY_SCALE')
                elif self.no_finger_loc and (src_name in left_finger_bones or src_name in right_finger_bones):
                    constr_types = ['COPY_ROTATION']
                    if self.scale_constraints:
                        constr_types.append('COPY_SCALE')
                else:
                    constr_types = self._bind_constraints

                for constr_type in constr_types:
                    constr = src_pbone.constraints.new(type=constr_type)
                    constr.target = trg_ob

                    subtarget_name = f'{src_name}_{cp_suffix}'
                    if subtarget_name in trg_ob.data.bones:
                        constr.subtarget = subtarget_name

                if self.constrain_root == 'Bone' and src_name == src_skeleton.root:
                    self._constrained_root = src_pbone

            if self.constrain_root == 'Object' and self.root_motion_bone:
                constr_types = ['COPY_LOCATION']
                if any([self.root_cp_rot_x, self.root_cp_rot_y, self.root_cp_rot_z]):
                    constr_types.append('COPY_ROTATION')
                if self.scale_constraints:
                    constr_types.append('COPY_SCALE')
                for constr_type in constr_types:
                    constr = ob.constraints.new(type=constr_type)
                    constr.target = trg_ob

                    constr.subtarget = self.root_motion_bone

                self._constrained_root = ob

            if self._constrained_root:
                if any((self.root_use_loc_min_x, self.root_use_loc_min_y, self.root_use_loc_min_z,
                        self.root_use_loc_max_x, self.root_use_loc_max_y, self.root_use_loc_max_z))\
                        or not all((self.root_cp_loc_x, self.root_cp_loc_y, self.root_cp_loc_z)):

                    constr = self._constrained_root.constraints.new('LIMIT_LOCATION')

                    constr.use_min_x = self.root_use_loc_min_x or not self.root_cp_loc_x
                    constr.use_min_y = self.root_use_loc_min_y or not self.root_cp_loc_y
                    constr.use_min_z = self.root_use_loc_min_z or not self.root_cp_loc_z

                    constr.use_max_x = self.root_use_loc_max_x or not self.root_cp_loc_x
                    constr.use_max_y = self.root_use_loc_max_y or not self.root_cp_loc_y
                    constr.use_max_z = self.root_use_loc_max_z or not self.root_cp_loc_z

                    constr.min_x = self.root_loc_min_x if self.root_cp_loc_x and self.root_use_loc_min_x else 0.0
                    constr.min_y = self.root_loc_min_y if self.root_cp_loc_y and self.root_use_loc_min_y else 0.0
                    constr.min_z = self.root_loc_min_z if self.root_cp_loc_z and self.root_use_loc_min_z else 0.0

                    constr.max_x = self.root_loc_max_x if self.root_cp_loc_x and self.root_use_loc_max_x else 0.0
                    constr.max_y = self.root_loc_max_y if self.root_cp_loc_y and self.root_use_loc_max_y else 0.0
                    constr.max_z = self.root_loc_max_z if self.root_cp_loc_z and self.root_use_loc_max_z else 0.0

            if self._constrained_root and not all((self.root_cp_rot_x, self.root_cp_rot_y, self.root_cp_rot_z)):
                constr = self._constrained_root.constraints.new('LIMIT_ROTATION')

                constr.use_limit_x = not self.root_cp_rot_x
                constr.use_limit_y = not self.root_cp_rot_y
                constr.use_limit_z = not self.root_cp_rot_z

            # Removed copy_visibility_tracks functionality as requested

        return {'FINISHED'}


class ClearSAPSync(bpy.types.Operator):
    """Clear all SAP synchronization pairs"""
    bl_idname = "armature.expykit_clear_sap_sync"
    bl_label = "Clear SAP Sync"
    bl_description = "Clear all real-time SAP Data synchronization pairs"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        global _sap_sync_pairs
        count = len(_sap_sync_pairs)
        _sap_sync_pairs.clear()
        if count > 0:
            self.report({'INFO'}, f"Cleared {count} SAP sync pairs")
        else:
            self.report({'INFO'}, "No SAP sync pairs to clear")
        return {'FINISHED'}


def validate_actions(action: bpy.types.Action, armature_or_path_resolve):
    return action_matches_id(action, armature_or_path_resolve)


def find_retarget_source_armature(target_ob):
    """Source armature constrained to target _RET bones (Expy bind layout)."""
    if not target_ob or target_ob.type != 'ARMATURE':
        return None
    for candidate in bpy.data.objects:
        if candidate.type != 'ARMATURE' or candidate == target_ob:
            continue
        for pb in candidate.pose.bones:
            for constr in pb.constraints:
                if constr.target != target_ob:
                    continue
                subtarget = getattr(constr, 'subtarget', '') or ''
                if subtarget.endswith('_RET'):
                    return candidate
    return None


def resolve_bake_armature_pair(ob):
    """
    Return (action_armature, bake_armature) for constrained baking.

    After Expy bind, the source armature is constrained to the target's _RET
    bones. Baking with the target selected assigns actions on the source and
    writes keyframes onto the target deform bones.
    """
    if not ob or ob.type != 'ARMATURE':
        return None, None

    source_ob = find_retarget_source_armature(ob)
    if source_ob is not None:
        return source_ob, ob

    for pb in bone_utils.get_constrained_controls(armature_object=ob, use_deform=True):
        for constr in pb.constraints:
            subtarget = getattr(constr, 'subtarget', '') or ''
            if subtarget.endswith('_RET') and constr.target and constr.target.type == 'ARMATURE':
                return constr.target, ob

    return ob, ob


def action_eligible_for_bake(action, action_armature, bake_armature):
    if action is None:
        return False
    name = action.name
    if "SAP Data" in name or "_old" in name:
        return False
    if action_armature and action_matches_id(action, action_armature):
        return True
    if bake_armature and action_matches_id(action, bake_armature):
        return True
    return False


def uniquify_action_name(name):
    """Return an unused action name, adding .001 suffixes when needed."""
    if name not in bpy.data.actions:
        return name
    stem, dot, suffix = name.rpartition(".")
    if dot and suffix.isdigit():
        base = stem
        start = int(suffix) + 1
    else:
        base = name
        start = 1
    index = start
    while True:
        candidate = f"{base}.{index:03d}"
        if candidate not in bpy.data.actions:
            return candidate
        index += 1


def _clean_action_stem(action_name):
    if "|" in action_name:
        return action_name.split("|")[-1]
    return action_name


def _desired_baked_action_name(original_name):
    """Return the preferred baked action name without Blender duplicate suffixes."""
    return normalize_anim_stem(_clean_action_stem(original_name))


def select_bones_for_visual_bake(
    action_armature,
    bake_armature,
    *,
    use_deform=True,
    keep_ik_bones=False,
):
    """Select bones for a visual constrained bake pass.

    Always bake the full skeleton, including constraint helper bones. Partial selection
    leaves ancestor bones at rest on playback and causes the rig to float or
    drift vertically. keep_ik_bones is kept for API compatibility but no longer
    changes selection because the full skeleton is always baked.
    """
    del action_armature, use_deform, keep_ik_bones  # full-skeleton bake; args kept for callers

    bone_names: list[str] = []
    for pb in bake_armature.pose.bones:
        set_pose_bone_select(pb, True)
        bone_names.append(pb.name)
    return bone_names


def _ensure_armature_evaluable(armature):
    """Make sure an armature still evaluates while another armature is being baked."""
    if armature is None:
        return
    armature.hide_viewport = False
    armature.hide_set(False)


class SourceObjectBakeLock:
    """Keep a source armature object fixed while its actions drive a target bake.

    Retarget binds often add object-level copy constraints from the target root
    bone (for example Trans). While nla.bake steps frames, those constraints or
    object location keys in the source action can shift the whole source object
    and corrupt the visual bake on the target.
    """

    _active: dict[str, dict] = {}

    def __init__(self, armature_object):
        self.armature_object = armature_object
        self._key = armature_object.name if armature_object else ""

    def __enter__(self):
        ob = self.armature_object
        if ob is None or ob.type != 'ARMATURE':
            return self

        state = self._active.get(self._key)
        if state is not None:
            state["depth"] += 1
            return self

        muted_constraints = []
        for constr in ob.constraints:
            muted_constraints.append((constr, constr.mute))
            constr.mute = True

        self._active[self._key] = {
            "depth": 1,
            "object": ob,
            "matrix": ob.matrix_world.copy(),
            "muted_constraints": muted_constraints,
        }

        if _source_object_bake_lock_handler not in bpy.app.handlers.frame_change_pre:
            bpy.app.handlers.frame_change_pre.append(_source_object_bake_lock_handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        ob = self.armature_object
        if ob is None or ob.type != 'ARMATURE':
            return False

        state = self._active.get(self._key)
        if state is None:
            return False

        state["depth"] -= 1
        if state["depth"] > 0:
            return False

        for constr, was_muted in state["muted_constraints"]:
            constr.mute = was_muted
        state["object"].matrix_world = state["matrix"]
        del self._active[self._key]
        if not SourceObjectBakeLock._active:
            if _source_object_bake_lock_handler in bpy.app.handlers.frame_change_pre:
                bpy.app.handlers.frame_change_pre.remove(_source_object_bake_lock_handler)
        return False


def _source_object_bake_lock_handler(_scene):
    for state in SourceObjectBakeLock._active.values():
        ob = state.get("object")
        matrix = state.get("matrix")
        if ob is not None and matrix is not None:
            ob.matrix_world = matrix


def strip_ret_fcurves_from_action(action):
    """Remove helper _RET bone channels from a baked action."""
    if not action:
        return 0
    removed = 0
    for fc in list(get_all_action_fcurves(action)):
        if not fc.data_path.startswith('pose.bones['):
            continue
        bone_name = fc.data_path.split('"')[1] if '"' in fc.data_path else None
        if bone_name and bone_name.endswith("_RET"):
            remove_fcurve(action, fc)
            removed += 1
    return removed


def _pose_bone_chain_depth(pose_bone):
    depth = 0
    parent = pose_bone.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _mute_all_pose_constraints(armature):
    """Mute every pose-bone constraint; return [(constraint, was_muted), ...]."""
    backup = []
    for pose_bone in armature.pose.bones:
        for constraint in pose_bone.constraints:
            backup.append((constraint, constraint.mute))
            constraint.mute = True
    return backup


def _restore_pose_constraint_mutes(backup):
    for constraint, was_muted in backup:
        try:
            constraint.mute = was_muted
        except ReferenceError:
            pass


def _key_pose_bone_transform(pose_bone, frame):
    name = pose_bone.name
    pose_bone.keyframe_insert('location', frame=frame, group=name)
    pose_bone.keyframe_insert('scale', frame=frame, group=name)
    if pose_bone.rotation_mode == 'QUATERNION':
        pose_bone.keyframe_insert('rotation_quaternion', frame=frame, group=name)
    elif pose_bone.rotation_mode == 'AXIS_ANGLE':
        pose_bone.keyframe_insert('rotation_axis_angle', frame=frame, group=name)
    else:
        pose_bone.keyframe_insert('rotation_euler', frame=frame, group=name)


def _set_action_keys_linear(action):
    for fcurve in get_all_action_fcurves(action):
        for keyframe in fcurve.keyframe_points:
            keyframe.interpolation = 'LINEAR'


def _fix_quaternion_continuity(action, bone_names):
    """Avoid quaternion double-cover flips between consecutive baked frames."""
    for bone_name in bone_names:
        curves = []
        for index in range(4):
            path = f'pose.bones["{bone_name}"].rotation_quaternion'
            curve = next(
                (
                    fcurve
                    for fcurve in get_all_action_fcurves(action)
                    if fcurve.data_path == path and fcurve.array_index == index
                ),
                None,
            )
            curves.append(curve)
        if any(curve is None or not curve.keyframe_points for curve in curves):
            continue
        count = min(len(curve.keyframe_points) for curve in curves)
        prev = None
        for key_i in range(count):
            values = [curves[axis].keyframe_points[key_i].co[1] for axis in range(4)]
            if prev is not None and (
                prev[0] * values[0]
                + prev[1] * values[1]
                + prev[2] * values[2]
                + prev[3] * values[3]
            ) < 0.0:
                for axis in range(4):
                    curves[axis].keyframe_points[key_i].co[1] = -values[axis]
                values = [-v for v in values]
            prev = values


def clear_baked_pose_and_object_constraints(bake_armature, bone_names, action_armature=None):
    """Remove constraints after the full pose AND object transform bake."""
    if bake_armature is None:
        return
    for bone_name in bone_names:
        pose_bone = bake_armature.pose.bones.get(bone_name)
        if pose_bone is None:
            continue
        for constraint in reversed(pose_bone.constraints):
            pose_bone.constraints.remove(constraint)

    for constraint in reversed(list(bake_armature.constraints)):
        bake_armature.constraints.remove(constraint)


def _sample_constrained_visual_matrices(
    context,
    bake_armature,
    bone_names,
    frame_start,
    frame_end,
):
    """Capture armature-space pose matrices with all constraints still active."""
    pose_bones = [
        bake_armature.pose.bones[name]
        for name in bone_names
        if name in bake_armature.pose.bones
    ]
    if not pose_bones:
        return None, None, None

    ordered = sorted(pose_bones, key=_pose_bone_chain_depth)
    by_depth = {}
    for pose_bone in ordered:
        by_depth.setdefault(_pose_bone_chain_depth(pose_bone), []).append(pose_bone)

    scene = context.scene
    samples = []
    for frame in range(frame_start, frame_end + 1):
        scene.frame_set(frame)
        context.view_layer.update()
        samples.append({pose_bone.name: pose_bone.matrix.copy() for pose_bone in ordered})
    return samples, ordered, by_depth


def _apply_visual_matrices_and_key(
    context,
    bake_armature,
    samples,
    by_depth,
    frame_start,
    frame_end,
):
    """Mute constraints, apply sampled matrices, key real local channels."""
    mute_backup = _mute_all_pose_constraints(bake_armature)
    scene = context.scene
    try:
        for frame, visuals in zip(range(frame_start, frame_end + 1), samples):
            scene.frame_set(frame)
            for depth in sorted(by_depth):
                for pose_bone in by_depth[depth]:
                    pose_bone.matrix = visuals[pose_bone.name]
                context.view_layer.update()
                for pose_bone in by_depth[depth]:
                    _key_pose_bone_transform(pose_bone, frame)
    finally:
        _restore_pose_constraint_mutes(mute_backup)


def bake_one_constrained_action(
    context,
    action_armature,
    bake_armature,
    source_action,
    constr_bone_names,
    *,
    fake_user_new=True,
    use_retarget_clean=False,
    for_visible_bake=False,
    lock_source_object=True,
):
    """
    Bake one source action onto bake_armature.

    Creates a dedicated target action before writing keys so bulk bakes do not
    overwrite the previous result on the target armature.
    """
    if source_action is None or not constr_bone_names:
        return None

    original_name = source_action.name

    if action_armature.animation_data is None:
        action_armature.animation_data_create()
    if bake_armature.animation_data is None:
        bake_armature.animation_data_create()

    _ensure_armature_evaluable(action_armature)
    _ensure_armature_evaluable(bake_armature)

    action_armature.select_set(False)
    bake_armature.select_set(True)
    context.view_layer.objects.active = bake_armature
    if context.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')

    baked_action = None
    same_armature = action_armature == bake_armature

    fr_start, fr_end = action_frame_range_safe(source_action)
    frame_start = int(fr_start)
    frame_end = int(fr_end)
    if frame_end < frame_start:
        frame_end = frame_start

    scene = context.scene
    prev_frame = scene.frame_current
    prev_frame_start = scene.frame_start
    prev_frame_end = scene.frame_end
    scene.frame_start = frame_start
    scene.frame_end = frame_end

    def _run_bake_pass():
        nonlocal baked_action

        assign_action(action_armature.animation_data, source_action)
        # Destination channels must not mix with the constrained preview we sample.
        if not same_armature:
            assign_action(bake_armature.animation_data, None)
        scene.frame_set(frame_start)
        context.view_layer.update()

        # Sample first while the driving action is still assigned. Same-armature
        # bakes would lose that action if we swapped to the empty bake action early.
        samples, _ordered, by_depth = _sample_constrained_visual_matrices(
            context,
            bake_armature,
            constr_bone_names,
            frame_start,
            frame_end,
        )
        if not samples:
            baked_action = None
            return

        temp_action_name = uniquify_action_name(f".expykit_bake_{original_name}")
        baked_action = bpy.data.actions.new(temp_action_name)
        baked_action.use_fake_user = fake_user_new
        assign_action(bake_armature.animation_data, baked_action)

        try:
            _apply_visual_matrices_and_key(
                context,
                bake_armature,
                samples,
                by_depth,
                frame_start,
                frame_end,
            )
        except Exception:
            assign_action(bake_armature.animation_data, None)
            if baked_action.users == 0:
                bpy.data.actions.remove(baked_action)
            baked_action = None
            raise
        finally:
            scene.frame_set(prev_frame)
            scene.frame_start = prev_frame_start
            scene.frame_end = prev_frame_end

    try:
        if lock_source_object and not same_armature:
            with SourceObjectBakeLock(action_armature):
                _run_bake_pass()
        else:
            _run_bake_pass()
    except Exception as error:
        print(f"Bake failed for '{original_name}': {error}")
        scene.frame_set(prev_frame)
        scene.frame_start = prev_frame_start
        scene.frame_end = prev_frame_end
        return None

    if baked_action is None or not bake_armature.animation_data or not bake_armature.animation_data.action:
        if baked_action is not None and baked_action.users == 0:
            bpy.data.actions.remove(baked_action)
        return None

    baked_action = bake_armature.animation_data.action
    if not get_all_action_fcurves(baked_action):
        assign_action(bake_armature.animation_data, None)
        if baked_action.users == 0:
            bpy.data.actions.remove(baked_action)
        return None

    _set_action_keys_linear(baked_action)
    _fix_quaternion_continuity(baked_action, constr_bone_names)

    if for_visible_bake or use_retarget_clean:
        clean_baked_action(
            source_action,
            baked_action,
            bake_armature,
            baked_bone_names=constr_bone_names,
        )
        if for_visible_bake:
            strip_ret_fcurves_from_action(baked_action)
    else:
        clean_baked_action(source_action, baked_action, bake_armature)

    source_action.name = f"{original_name}_old"
    baked_action.name = uniquify_action_name(_desired_baked_action_name(original_name))
    baked_action.use_fake_user = fake_user_new

    # The bake pass assigns each source action to the driving armature while it
    # evaluates the constraints. Without explicitly clearing that assignment,
    # the final source action remains active after being renamed to ``_old``.
    # Keep the backup datablock, but stop it from evaluating after the bake.
    if (
        action_armature != bake_armature
        and action_armature.animation_data
        and action_armature.animation_data.action == source_action
    ):
        assign_action(action_armature.animation_data, None)
    assign_action(bake_armature.animation_data, None)

    return baked_action


def clean_baked_action(original_action, baked_action, target_armature, baked_bone_names=None):
    """
    Clean up the baked action by removing animation data for bones that weren't 
    animated in the original action. This prevents pose contamination from other animations.
    Bones with "_RET" suffix are preserved to maintain pose between animations.

    When baking across different armatures (retargeting), pass baked_bone_names so
    cleanup keeps the constrained destination bones instead of source bone names.
    """
    if not original_action or not baked_action:
        return

    if baked_bone_names is not None:
        allowed_bones = set(baked_bone_names)
        bones_to_clean = set()
        for fc in get_all_action_fcurves(baked_action):
            if fc.data_path.startswith('pose.bones['):
                bone_name = fc.data_path.split('"')[1] if '"' in fc.data_path else None
                if bone_name and bone_name not in allowed_bones and not bone_name.endswith("_RET"):
                    bones_to_clean.add(bone_name)
    else:
        # Get bones that were actually animated in the original action
        original_animated_bones = set()
        for fc in get_all_action_fcurves(original_action):
            if fc.data_path.startswith('pose.bones['):
                bone_name = fc.data_path.split('"')[1] if '"' in fc.data_path else None
                if bone_name:
                    original_animated_bones.add(bone_name)

        bones_to_clean = set()
        for fc in get_all_action_fcurves(baked_action):
            if fc.data_path.startswith('pose.bones['):
                bone_name = fc.data_path.split('"')[1] if '"' in fc.data_path else None
                if bone_name and bone_name not in original_animated_bones:
                    if not bone_name.endswith("_RET"):
                        bones_to_clean.add(bone_name)
    
    # Remove F-curves for bones that weren't originally animated (excluding _RET bones)
    for bone_name in bones_to_clean:
        for fc in list(get_all_action_fcurves(baked_action)):
            if fc.data_path.startswith(f'pose.bones["{bone_name}"]'):
                remove_fcurve(baked_action, fc)
    
    print(f"Cleaned baked action: removed animation data for {len(bones_to_clean)} bones that weren't animated in original (preserved _RET bones)")


class BakeConstrainedActions(bpy.types.Operator):
    bl_idname = "armature.expykit_bake_constrained_actions"
    bl_label = "Bake Constrained Actions"
    bl_description = "Bake Actions constrained from another Armature. No need to select two armatures"
    bl_options = {'REGISTER', 'UNDO'}

    clear_users_old: BoolProperty(name="Detach Source Action After Bake",
                                  description="Detach the active source action; preserve renamed originals and their other users",
                                  default=True)

    fake_user_new: BoolProperty(name="Save New Action User",
                                default=True)
    
    exclude_deform: BoolProperty(name="Exclude deform bones", default=False)

    do_bake: BoolProperty(name="Bake and Exit", description="Bake driven motion and exit",
                          default=False, options={'SKIP_SAVE'})
    
    copy_visibility_fcurves: BoolProperty(name="Copy Vis Layers", 
                                        description="Link SAP Data animations to the new retargeted animation instead of the _old one", 
                                        default=False)

    keep_ik_bones: BoolProperty(
        name="Keep IK Bones",
        description="Include IK control bones in the bake and preserve their keyframes (FootIK, KneeIK, HandIK, etc.)",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        column = layout.column()

        for to_bake in context.selected_objects:
            action_armature, bake_armature = resolve_bake_armature_pair(to_bake)
            if action_armature and bake_armature and action_armature != bake_armature:
                column.label(text=f"Baking from {action_armature.name} to {bake_armature.name}")
            elif bake_armature:
                column.label(text=f"Baking {bake_armature.name}")

        if len(context.selected_objects) > 1:
            column.label(text="No need to select two Armatures anymore", icon='ERROR')

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
        row.prop(self, "copy_visibility_fcurves") # Add the new checkbox here

        row = column.split(factor=0.30, align=True)
        row.label(text="")
        row.prop(self, "keep_ik_bones")

        row = column.split(factor=0.30, align=True)
        row.label(text="")
        row.prop(self, "do_bake", toggle=True)

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'

    def get_trg_ob(self, ob: bpy.types.Object) -> bpy.types.Object:
        action_armature, _bake_armature = resolve_bake_armature_pair(ob)
        if action_armature and action_armature != ob:
            return action_armature
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

        sel_obs = [ob for ob in context.selected_objects if ob and ob.type == 'ARMATURE']
        if not sel_obs and context.object and context.object.type == 'ARMATURE':
            sel_obs = [context.object]

        bake_pairs = []
        for ob in sel_obs:
            action_armature, bake_armature = resolve_bake_armature_pair(ob)
            if action_armature and bake_armature and not any(
                pair[1] == bake_armature for pair in bake_pairs
            ):
                bake_pairs.append((action_armature, bake_armature, ob))

        actions_by_pair = []
        total_actions = 0
        for action_armature, bake_armature, selected_ob in bake_pairs:
            actions_to_bake = collect_actions_for_bake(
                action_armature,
                bake_armature,
                extra_armatures=[selected_ob],
            )
            actions_by_pair.append((action_armature, bake_armature, actions_to_bake))
            total_actions += len(actions_to_bake)

        if total_actions == 0:
            self.report({'WARNING'}, "No actions found to bake")
            return {'CANCELLED'}

        print(f"Bake Constrained: found {total_actions} actions to bake")

        try:
            from ..source.retargeting.fast_bake import bake_visual_actions, load_baked_action

            baked_total = 0
            for action_armature, bake_armature, actions_to_bake in actions_by_pair:
                if not action_armature.animation_data:
                    action_armature.animation_data_create()
                if bake_armature.animation_data is None:
                    bake_armature.animation_data_create()

                context.view_layer.objects.active = bake_armature
                bake_armature.select_set(True)
                if context.mode != 'POSE':
                    bpy.ops.object.mode_set(mode='POSE')

                constr_bone_names = select_bones_for_visual_bake(
                    action_armature,
                    bake_armature,
                    use_deform=not self.exclude_deform,
                    keep_ik_bones=self.keep_ik_bones,
                )

                if not constr_bone_names:
                    self.report({'WARNING'}, f"No bones to bake on {bake_armature.name}")
                    continue

                if not actions_to_bake:
                    continue

                pairs = bake_visual_actions(
                    context,
                    action_armature,
                    bake_armature,
                    actions_to_bake,
                    fake_user_new=self.fake_user_new,
                    clear_users_old=self.clear_users_old,
                    bone_names=constr_bone_names,
                    interp_name="LINEAR",
                    parallel=True,
                    log_label="Bake Constrained",
                    use_retarget_clean=True,
                )
                baked_total += len(pairs)
                first_baked = pairs[0][1] if pairs else None

                if self.copy_visibility_fcurves:
                    if hasattr(action_armature.data, 'sub_anim_properties') and hasattr(bake_armature.data, 'sub_anim_properties'):
                        sync_vis_and_mat_tracks(action_armature.data, bake_armature.data)

                    target_armature_name = bake_armature.name
                    source_armature_name = action_armature.name

                    for sap_action in bpy.data.actions:
                        if "SAP Data" not in sap_action.name:
                            continue
                        source_prefix_with_space = f"{source_armature_name} "
                        if not sap_action.name.startswith(source_prefix_with_space):
                            continue
                        suffix = sap_action.name[len(source_prefix_with_space):]
                        sap_action.name = f"{target_armature_name} {suffix}"

                    try:
                        dup_pattern = re.compile(rf"^{re.escape(target_armature_name)}(?:\s+\.\d+)+\s+(?P<rest>.+)$")
                    except Exception:
                        dup_pattern = None
                    if dup_pattern is not None:
                        for sap_action in bpy.data.actions:
                            if "SAP Data" not in sap_action.name:
                                continue
                            match = dup_pattern.match(sap_action.name)
                            if not match:
                                continue
                            sap_action.name = f"{target_armature_name} {match.group('rest')}"

                    for _old_action, baked_action in pairs:
                        sap_data_action_name = f"{target_armature_name} {baked_action.name} SAP Data"
                        sap_data_action = bpy.data.actions.get(sap_data_action_name)
                        if sap_data_action:
                            if not bake_armature.data.animation_data:
                                bake_armature.data.animation_data_create()
                            bake_armature.data.animation_data.action = sap_data_action

                clear_baked_pose_and_object_constraints(
                    bake_armature,
                    constr_bone_names,
                    action_armature=action_armature,
                )

                if first_baked is not None:
                    try:
                        load_baked_action(bake_armature, first_baked)
                    except Exception:
                        assign_action(bake_armature.animation_data, first_baked)

            if baked_total:
                self.report({'INFO'}, f"Bake Constrained completed - {baked_total} actions baked")
            else:
                self.report({'WARNING'}, "No actions were baked")
        except Exception as error:
            self.report({'ERROR'}, f"Bake failed: {error}")
            return {'CANCELLED'}

        return {'FINISHED'}


def crv_bone_name(fcurve):
    p_bone_prefix = 'pose.bones['
    if not fcurve.data_path.startswith(p_bone_prefix):
        return
    data_path = fcurve.data_path
    return data_path[len(p_bone_prefix):].rsplit('"]', 1)[0].strip('"[')


def is_bone_floating(bone, hips_bone_name):
    binding_constrs = ['COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE', 'COPY_TRANSFORMS']
    while bone.parent:
        if bone.parent.name == hips_bone_name:
            return False
        for constr in bone.constraints:
            if constr.type in binding_constrs:
                return False
        bone = bone.parent

    return True


def add_loc_key(bone, frame, options):
    bone.keyframe_insert('location', index=0, frame=frame, options=options)
    bone.keyframe_insert('location', index=1, frame=frame, options=options)
    bone.keyframe_insert('location', index=2, frame=frame, options=options)


def get_rot_ani_path(to_animate):
    if to_animate.rotation_mode == 'QUATERNION':
        return 'rotation_quaternion', 4
    if to_animate.rotation_mode == 'AXIS_ANGLE':
        return 'rotation_axis_angle', 4
    
    return 'rotation_euler', 3


def add_loc_rot_key(bone, frame, options):
    add_loc_key(bone, frame, options)

    mode, channels = get_rot_ani_path(bone)
    for i in range(channels):
        bone.keyframe_insert(mode, index=i, frame=frame, options=options)


def add_scale_key(bone, frame, options):
    for i in range(3):
        bone.keyframe_insert('scale', index=i, frame=frame, options=options)


def add_loc_rot_scale_key(bone, frame, options):
    add_loc_key(bone, frame, options)
    add_scale_key(bone, frame, options)

    mode, channels = get_rot_ani_path(bone)
    for i in range(channels):
        bone.keyframe_insert(mode, index=i, frame=frame, options=options)


class AddRootMotion(bpy.types.Operator):
    bl_idname = "armature.expykit_add_rootmotion"
    bl_label = "Transfer Root Motion"
    bl_description = "Bring Motion to Root Bone"
    bl_options = {'REGISTER', 'UNDO'}

    rig_preset: EnumProperty(items=preset_handler.iterate_presets,
                             name="Target Preset")

    motion_bone: StringProperty(name="Motion",
                                description="Constrain Root bone to Hip motion",
                                default="")

    root_motion_bone: StringProperty(name="Root Motion",
                                     description="Constrain Root bone to Hip motion",
                                     default="")

    new_anim_suffix: StringProperty(name="Suffix",
                                    default="_RM",
                                    description="Suffix of the duplicate animation, leave empty to overwrite")

    obj_or_bone: EnumProperty(items=[
        ('object', "Object", "Transfer Root Motion To Object"),
        ('bone', "Bone", "Transfer Root Motion To Bone")],
                              name="Object/Bone", default='bone')

    keep_offset: BoolProperty(name="Keep Offset", default=True)
    offset_type: EnumProperty(items=[
        ('start', "Action Start", "Offset to Start Pose"),
        ('end', "Action End", "Offset to Match End Pose"),
        ('rest', "Rest Pose", "Offset to Match Rest Pose")],
                              name="Offset",
                              default='rest')

    root_cp_loc_x: BoolProperty(name="Root Copy Loc X", description="Copy Root X Location", default=False)
    root_cp_loc_y: BoolProperty(name="Root Copy Loc y", description="Copy Root Y Location", default=True)
    root_cp_loc_z: BoolProperty(name="Root Copy Loc Z", description="Copy Root Z Location", default=False)

    root_use_loc_min_x: BoolProperty(name="Use Root Min X", description="Minimum Root X", default=False)
    root_use_loc_min_y: BoolProperty(name="Use Root Min Y", description="Minimum Root Y", default=False)
    root_use_loc_min_z: BoolProperty(name="Use Root Min Z", description="Minimum Root Z", default=True)

    root_loc_min_x: FloatProperty(name="Root Min X", description="Minimum Root X", default=0.0)
    root_loc_min_y: FloatProperty(name="Root Min Y", description="Minimum Root Y", default=0.0)
    root_loc_min_z: FloatProperty(name="Root Min Z", description="Minimum Root Z", default=0.0)

    root_use_loc_max_x: BoolProperty(name="Use Root Max X", description="Maximum Root X", default=False)
    root_use_loc_max_y: BoolProperty(name="Use Root Max Y", description="Maximum Root Y", default=False)
    root_use_loc_max_z: BoolProperty(name="Use Root Max Z", description="Maximum Root Z", default=False)

    root_loc_max_x: FloatProperty(name="Root Max X", description="Maximum Root X", default=0.0)
    root_loc_max_y: FloatProperty(name="Root Max Y", description="Maximum Root Y", default=0.0)
    root_loc_max_z: FloatProperty(name="Root Max Z", description="Maximum Root Z", default=0.0)

    root_cp_rot_x: BoolProperty(name="Root Copy Rot X", description="Copy Root X Rotation", default=True)
    root_cp_rot_y: BoolProperty(name="Root Copy Rot y", description="Copy Root Y Rotation", default=True)
    root_cp_rot_z: BoolProperty(name="Root Copy Rot Z", description="Copy Root Z Rotation", default=False)
    
    copy_scale: BoolProperty(name="Copy Scale", description="Copy Scale from motion bone", default=False)

    _armature = None
    _prop_indent = 0.15

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.mode != 'POSE':
            return False
        if context.object.type != 'ARMATURE':
            return False
        if not context.object.animation_data:
            return False
        if not context.object.animation_data.action:
            return False
        return True

    def draw(self, context):
        layout = self.layout
        column = layout.column()

        if not context.object.data.expykit_retarget.has_settings():
            row = column.row()
            row.prop(self, 'rig_preset', text="Rig Type:")

        row = column.split(factor=self._prop_indent, align=True)
        row.label(text="From")
        row.prop_search(self, 'motion_bone',
                        context.active_object.data,
                        "bones", text="")

        split = column.split(factor=self._prop_indent, align=True)
        split.label(text="To")

        col = split.column()
        col.prop(self, 'obj_or_bone', expand=True)
        
        col.prop_search(self, 'root_motion_bone',
                        context.active_object.data,
                        "bones", text="")

        row = column.split(factor=self._prop_indent, align=True)
        row.label(text="Suffix:")
        row.prop(self, 'new_anim_suffix', text="")

        column.separator()

        row = column.row(align=False)
        row.prop(self, "keep_offset")
        subcol = row.column()
        subcol.prop(self, "offset_type", text="Match ")
        subcol.enabled = self.keep_offset

        row = column.row(align=True)
        row.label(text="Location")
        row.prop(self, "root_cp_loc_x", text="X", toggle=True)
        row.prop(self, "root_cp_loc_y", text="Y", toggle=True)
        row.prop(self, "root_cp_loc_z", text="Z", toggle=True)

        row = column.row(align=True)
        row.label(text="Rotation Plane")
        row.prop(self, "root_cp_rot_x", text="X", toggle=True)
        row.prop(self, "root_cp_rot_y", text="Y", toggle=True)
        row.prop(self, "root_cp_rot_z", text="Z", toggle=True)
        
        row = column.row()
        row.prop(self, "copy_scale")

        column.separator()

        # Min/Max X
        row = column.row(align=True)
        row.prop(self, "root_use_loc_min_x", text="Min X")

        subcol = row.column()
        subcol.prop(self, "root_loc_min_x", text="")
        subcol.enabled = self.root_use_loc_min_x

        row.separator()
        row.prop(self, "root_use_loc_max_x", text="Max X")
        subcol = row.column()
        subcol.prop(self, "root_loc_max_x", text="")
        subcol.enabled = self.root_use_loc_max_x
        row.enabled = self.root_cp_loc_x

        # Min/Max Y
        row = column.row(align=True)
        row.prop(self, "root_use_loc_min_y", text="Min Y")

        subcol = row.column()
        subcol.prop(self, "root_loc_min_y", text="")
        subcol.enabled = self.root_use_loc_min_y

        row.separator()
        row.prop(self, "root_use_loc_max_y", text="Max Y")
        subcol = row.column()
        subcol.prop(self, "root_loc_max_y", text="")
        subcol.enabled = self.root_use_loc_max_y
        row.enabled = self.root_cp_loc_y

        # Min/Max Z
        row = column.row(align=True)
        row.prop(self, "root_use_loc_min_z", text="Min Z")

        subcol = row.column()
        subcol.prop(self, "root_loc_min_z", text="")
        subcol.enabled = self.root_use_loc_min_z

        row.separator()
        row.prop(self, "root_use_loc_max_z", text="Max Z")
        subcol = row.column()
        subcol.prop(self, "root_loc_max_z", text="")
        subcol.enabled = self.root_use_loc_max_z
        row.enabled = self.root_cp_loc_z

    def _set_defaults(self, rig_settings):
        if not rig_settings:
            return False

        if not self.root_motion_bone:
            self.root_motion_bone = rig_settings.root

        if not self.motion_bone:
            self.motion_bone = rig_settings.spine.hips
        
        return(bool(self.motion_bone))

    def invoke(self, context, event):
        """Fill root and hips field according to character settings"""
        self._rootmo_transfs = []
        self._rootbo_transfs = []
        self._hip_bone_transfs = []
        self._all_floating_mats = []
        
        self._stored_motion_bone = ""
        self._stored_motion_type = self.obj_or_bone
        self._transforms_stored = False

        rig_settings = context.object.data.expykit_retarget
        if self._set_defaults(rig_settings):
            self._store_transforms(context)

        return self.execute(context)

    def _get_floating_bones(self, context):
        arm_ob = context.active_object
        skeleton = preset_handler.get_settings_skel(arm_ob.data.expykit_retarget)
        
        # TODO: check controls with animation curves instead
        def consider_bone(b_name):
            if b_name == self.root_motion_bone:
                return False
            return b_name in arm_ob.pose.bones

        rig_bones = [arm_ob.pose.bones[b_name] for b_name in skeleton.bone_names() if b_name and consider_bone(b_name)]
        return list([bone for bone in rig_bones if is_bone_floating(bone, self.motion_bone)])

    def _clear_cache(self):
        self._all_floating_mats.clear()
        self._hip_bone_transfs.clear()
        self._rootmo_transfs.clear()

        self._rootbo_transfs.clear()

    def _store_transforms(self, context):
        self._clear_cache()
        arm_ob = context.active_object
        
        root_bone = arm_ob.pose.bones[self.root_motion_bone]
        hip_bone = arm_ob.pose.bones[self.motion_bone]
        floating_bones = self._get_floating_bones(context)

        start, end = self._get_start_end(context)

        current_position = arm_ob.data.pose_position

        if self.offset_type == 'start':
            context.scene.frame_set(start)
        elif self.offset_type == 'end':
            context.scene.frame_set(end)
        else:
            arm_ob.data.pose_position = 'REST'

        start_mat_inverse = hip_bone.matrix.inverted()
        
        context.scene.frame_set(start)
        arm_ob.data.pose_position = current_position

        for frame_num in range(start, end + 1):
            context.scene.frame_set(frame_num)

            self._all_floating_mats.append(list([b.matrix.copy() for b in floating_bones]))
            self._hip_bone_transfs.append(hip_bone.matrix.copy())
            self._rootmo_transfs.append(hip_bone.matrix @ start_mat_inverse)

            if self.obj_or_bone == 'object' and root_bone:
                self._rootbo_transfs.append(root_bone.matrix.copy())

        self._stored_motion_bone = self.motion_bone
        self._stored_motion_type = self.obj_or_bone
        self._transforms_stored = True

    def _cache_dirty(self):
        if self._stored_motion_bone != self.motion_bone:
            return True
        if self._stored_motion_type != self.obj_or_bone:
            return True

        return False

    def execute(self, context):
        rig_settings = context.object.data.expykit_retarget
        if not rig_settings.has_settings():
            rig_settings = preset_handler.set_preset_skel(self.rig_preset)
            self._set_defaults(rig_settings)
        if not self.root_motion_bone:
            return {'FINISHED'}
        if not self.motion_bone:
            return {'FINISHED'}

        armature = context.active_object
        if self.new_anim_suffix:
            action_dupli = armature.animation_data.action.copy()

            action_name = armature.animation_data.action.name
            action_dupli.name = f'{action_name}{self.new_anim_suffix}'
            action_dupli.use_fake_user = armature.animation_data.action.use_fake_user
            armature.animation_data.action = action_dupli

        if self._cache_dirty():
            self._store_transforms(context)
            
        if not self._transforms_stored:
            self.report({'WARNING'}, "No transforms stored")

        self.action_offs(context)
        return {'FINISHED'}

    @staticmethod
    def _get_start_end(context):
        action = context.active_object.animation_data.action
        start, end = action.frame_range
        
        return int(start), int(end)

    def action_offs(self, context):
        start, end = self._get_start_end(context)
        current = context.scene.frame_current

        hips_bone_name = self.motion_bone
        hip_bone = context.active_object.pose.bones[hips_bone_name]

        if self.keep_offset and self.offset_type == 'end':
            context.scene.frame_set(end)
            end_mat = hip_bone.matrix.copy()
        else:
            end_mat = Matrix()

        context.scene.frame_set(start)
        start_mat = hip_bone.matrix.copy()
        start_mat_inverse = start_mat.inverted()

        if self.keep_offset:
            if self.offset_type == 'rest':
                offset_mat = context.active_object.data.bones[hip_bone.name].matrix_local.inverted()
            elif self.offset_type == 'start':
                offset_mat = start_mat_inverse
            elif self.offset_type == 'end':
                offset_mat = end_mat.inverted()
        else:
            offset_mat = Matrix()

        root_bone_name = self.root_motion_bone

        if self.obj_or_bone == 'object':
            root_bone = context.active_object
        else:
            try:
                root_bone = context.active_object.pose.bones[root_bone_name]
            except (TypeError, KeyError):
                self.report({'WARNING'}, f"{root_bone_name} not found in target")
                return {'FINISHED'}
        
        bpy.context.scene.frame_set(start)
        keyframe_options = {'INSERTKEY_VISUAL', 'INSERTKEY_CYCLE_AWARE'}
        add_loc_rot_key(root_bone, start, keyframe_options)

        root_matrix = root_bone.matrix if self.obj_or_bone == 'bone' else context.active_object.matrix_world
        for i, frame_num in enumerate(range(start, end + 1)):
            bpy.context.scene.frame_set(frame_num)

            rootmo_transf = self._hip_bone_transfs[i] @ offset_mat
            if self.root_cp_loc_x:
                if self.root_use_loc_min_x:
                    rootmo_transf[0][3] = max(rootmo_transf[0][3], self.root_loc_min_x)
                if self.root_use_loc_max_x:
                    rootmo_transf[0][3] = min(rootmo_transf[0][3], self.root_loc_max_x)
            else:
                rootmo_transf[0][3] = root_matrix[0][3]
            if self.root_cp_loc_y:
                if self.root_use_loc_min_y:
                    rootmo_transf[1][3] = max(rootmo_transf[1][3], self.root_loc_min_y)
                if self.root_use_loc_max_y:
                    rootmo_transf[1][3] = min(rootmo_transf[1][3], self.root_loc_max_y)
            else:
                rootmo_transf[1][3] = root_matrix[1][3]
            if self.root_cp_loc_z:
                if self.root_use_loc_min_z:
                    rootmo_transf[2][3] = max(rootmo_transf[2][3], self.root_loc_min_z)
                if self.root_use_loc_max_z:
                    rootmo_transf[2][3] = min(rootmo_transf[2][3], self.root_loc_max_z)
            else:
                rootmo_transf[2][3] = root_matrix[2][3]

            if not all((self.root_cp_rot_x, self.root_cp_rot_y, self.root_cp_rot_z)):
                if self.root_cp_rot_x + self.root_cp_rot_y + self.root_cp_rot_z < 2:
                    # need at least two axis to make this work, don't use rotation
                    no_rot = Matrix()
                    no_rot[0][3] = rootmo_transf[0][3]
                    no_rot[1][3] = rootmo_transf[1][3]
                    no_rot[2][3] = rootmo_transf[2][3]

                    rootmo_transf = no_rot
                else:
                    rootmo_transf.transpose()
                    root_transp = root_matrix.transposed()

                    if not self.root_cp_rot_z:
                        # XY plane
                        rootmo_transf[1][2] = root_transp[1][2]
                        rootmo_transf[0][2] = root_transp[0][2]

                        y_axis = rootmo_transf[1].to_3d()
                        y_axis.normalize()

                        x_axis = y_axis.cross(root_transp[2].to_3d())
                        x_axis.normalize()

                        z_axis = x_axis.cross(y_axis)
                        z_axis.normalize()
                    elif not self.root_cp_rot_x:
                        # ZY plane
                        rootmo_transf[1][0] = root_transp[1][0]
                        rootmo_transf[2][0] = root_transp[2][0]

                        z_axis = rootmo_transf[2].to_3d().normalized()
                        up = root_transp[1].to_3d()
                        x_axis = up.cross(z_axis).normalized()
                        y_axis = z_axis.cross(x_axis)
                        y_axis.normalize()
                    else:
                        # XZ plane
                        rootmo_transf[2][1] = root_transp[2][1]
                        rootmo_transf[0][1] = root_transp[0][1]

                        z_axis = rootmo_transf[2].to_3d().normalized()
                        up = root_transp[1].to_3d()
                        x_axis = up.cross(z_axis).normalized()
                        y_axis = z_axis.cross(x_axis)

                    rootmo_transf[0] = x_axis.to_4d()
                    rootmo_transf[1] = y_axis.to_4d()
                    rootmo_transf[2] = z_axis.to_4d()

                    rootmo_transf.transpose()

            if self.obj_or_bone == 'object':
                root_bone.matrix_world = rootmo_transf
            else:
                root_bone.matrix = rootmo_transf
            if self.copy_scale:
                add_loc_rot_scale_key(root_bone, frame_num, keyframe_options)
            else:
                add_loc_rot_key(root_bone, frame_num, keyframe_options)

        floating_bones = self._get_floating_bones(context)
        for i, frame_num in enumerate(range(start, end + 1)):
            bpy.context.scene.frame_set(frame_num)

            if self.obj_or_bone == 'object' and self.root_motion_bone:
                context.active_object.pose.bones[self.root_motion_bone].matrix = root_bone.matrix_world.inverted() @ context.active_object.pose.bones[self.root_motion_bone].matrix

            floating_mats = self._all_floating_mats[i]
            for bone, mat in zip(floating_bones, floating_mats):
                if self.obj_or_bone == 'object':
                    # TODO: should get matrix at frame 0
                    mat = root_bone.matrix_world.inverted() @ mat

                    bone.matrix = mat
                if self.copy_scale:
                    add_loc_rot_scale_key(bone, frame_num, set())
                else:
                    add_loc_rot_key(bone, frame_num, set())

        bpy.context.scene.frame_set(current)


class ActionNameCandidates(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name Candidate", default="")


class RenameActionsFromFbxFiles(bpy.types.Operator, ImportHelper):
    bl_idname = "armature.expykit_rename_actions_fbx"
    bl_label = "Rename Actions from fbx data..."
    bl_description = "Rename Actions from candidate fbx files"
    bl_options = {'PRESET', 'UNDO'}

    directory: StringProperty()

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})

    files: CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
    )

    contains: StringProperty(name="Containing", default="|")
    starts_with: StringProperty(name="Starting with", default="Action")

    def execute(self, context):
        fbx_durations = dict()
        for f in self.files:
            fbx_path = os.path.join(self.directory, f.name)
            local_time = fbx_helper.get_fbx_local_time(fbx_path)
            if not local_time:
                continue

            duration = fbx_helper.convert_from_fbx_duration(*local_time)
            duration = round(duration, 5)
            duration = str(duration)
            action_name = os.path.splitext(f.name[:-3])[0]

            try:
                fbx_durations[duration].append(action_name)
            except KeyError:  # entry doesn'exist yet
                fbx_durations[duration] = action_name
            except AttributeError:  # existing entry is not a list
                current = fbx_durations[duration]
                fbx_durations[duration] = [current, action_name]

        path_resolve = context.object.path_resolve
        for action in bpy.data.actions:
            skip_action = True

            if self.contains and self.contains in action.name:
                skip_action = False
            if skip_action and self.starts_with and action.name.startswith(self.starts_with):
                skip_action = False
            
            if skip_action:
                continue

            if not validate_actions(action, path_resolve):
                continue

            start, end = action.frame_range
            ac_duration = end - start
            ac_duration /= context.scene.render.fps
            ac_duration = round(ac_duration, 5)
            ac_duration = str(ac_duration)

            try:
                fbx_match = fbx_durations[ac_duration]
            except KeyError:
                continue

            if not fbx_match:
                continue
            if isinstance(fbx_match, typing.List):
                for name in fbx_match:
                    entry = action.expykit_name_candidates.add()
                    entry.name = name
                continue

            action.name = fbx_match

        return {'FINISHED'}


def register_classes():
    bpy.utils.register_class(ActionRangeToScene)
    bpy.utils.register_class(ActionEndToLastKeyframe)
    bpy.utils.register_class(ConstraintStatus)
    bpy.utils.register_class(SelectConstrainedControls)
    bpy.utils.register_class(ConvertBoneNaming)
    bpy.utils.register_class(ConvertGameFriendly)
    bpy.utils.register_class(ExtractMetarig)
    bpy.utils.register_class(MergeHeadTails)
    bpy.utils.register_class(RevertDotBoneNames)
    bpy.utils.register_class(ConstrainToArmature)
    bpy.utils.register_class(BakeConstrainedActions)
    bpy.utils.register_class(ClearSAPSync)
    bpy.utils.register_class(RenameActionsFromFbxFiles)
    bpy.utils.register_class(CreateTransformOffset)
    bpy.utils.register_class(AddRootMotion)
    bpy.utils.register_class(ActionNameCandidates)

    bpy.types.Action.expykit_name_candidates = bpy.props.CollectionProperty(type=ActionNameCandidates)


def unregister_classes():
    # Clean up SAP sync timer and data
    if bpy.app.timers.is_registered(_sap_sync_timer_func):
        bpy.app.timers.unregister(_sap_sync_timer_func)
    
    # Clear sync pairs and tracking data
    global _last_source_actions, _sap_sync_pairs
    _last_source_actions.clear()
    _sap_sync_pairs.clear()

    del bpy.types.Action.expykit_name_candidates

    bpy.utils.unregister_class(ActionRangeToScene)
    bpy.utils.unregister_class(ActionEndToLastKeyframe)
    bpy.utils.unregister_class(ConstraintStatus)
    bpy.utils.unregister_class(SelectConstrainedControls)
    bpy.utils.unregister_class(ConvertBoneNaming)
    bpy.utils.unregister_class(ConvertGameFriendly)
    bpy.utils.unregister_class(ExtractMetarig)
    bpy.utils.unregister_class(MergeHeadTails)
    bpy.utils.unregister_class(RevertDotBoneNames)
    bpy.utils.unregister_class(ConstrainToArmature)
    bpy.utils.unregister_class(BakeConstrainedActions)
    bpy.utils.unregister_class(ClearSAPSync)
    bpy.utils.unregister_class(RenameActionsFromFbxFiles)
    bpy.utils.unregister_class(CreateTransformOffset)
    bpy.utils.unregister_class(AddRootMotion)
    bpy.utils.unregister_class(ActionNameCandidates)

# --- Utility: Sync vis/material track entries from source to target armature data ---
def sync_vis_and_mat_tracks(source_data, target_data):
    """
    Ensure target_data has all visibility and material track entries present in source_data.
    """
    # Sync visibility tracks
    if hasattr(source_data, 'sub_anim_properties') and hasattr(target_data, 'sub_anim_properties'):
        src_vis = source_data.sub_anim_properties.vis_track_entries
        trg_vis = target_data.sub_anim_properties.vis_track_entries
        for src_track in src_vis:
            # Try to find by name
            found = False
            for trg_track in trg_vis:
                if trg_track.name == src_track.name:
                    found = True
                    break
            if not found:
                new_track = trg_vis.add()
                new_track.name = src_track.name
                # Only copy value if the attribute exists
                if hasattr(src_track, 'value') and hasattr(new_track, 'value'):
                    new_track.value = src_track.value
    
    # Sync material tracks
    if hasattr(source_data, 'sub_anim_properties') and hasattr(target_data, 'sub_anim_properties'):
        src_mat = getattr(source_data.sub_anim_properties, 'mat_tracks', None)
        trg_mat = getattr(target_data.sub_anim_properties, 'mat_tracks', None)
        if src_mat is not None and trg_mat is not None:
            for src_track in src_mat:
                found = False
                for trg_track in trg_mat:
                    if trg_track.name == src_track.name:
                        found = True
                        break
                if not found:
                    new_track = trg_mat.add()
                    new_track.name = src_track.name
                    # Only copy value if the attribute exists
                    if hasattr(src_track, 'value') and hasattr(new_track, 'value'):
                        new_track.value = src_track.value
                    # For material tracks, we might need to copy other attributes
                    # Check for common material track attributes
                    for attr in ['material', 'slot', 'enabled']:
                        if hasattr(src_track, attr) and hasattr(new_track, attr):
                            setattr(new_track, attr, getattr(src_track, attr))
# --- End utility ---
