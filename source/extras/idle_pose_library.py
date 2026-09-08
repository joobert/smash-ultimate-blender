import bpy
import json
from pathlib import Path
from bpy.types import Operator
from bpy.props import StringProperty

from ...dependencies import ssbh_data_py
from ..anim.import_anim import get_hierarchy_order
from .anim_flip import apply_smash_node_to_bone, collect_excluded_bone_names, mirror_smash_pose_data
from .mirror_animation import rotate_hip_180
from . import anim_layers_compat


def get_predefined_poses():
    """Get list of predefined pose names"""
    return [
        ("a00wait1", "a00wait1", "Basic wait animation"),
        ("a02run", "a02run", "Run animation"),
        ("a05squatwait", "a05squatwait", "Squat wait animation"),
        ("a04fall", "a04fall", "Fall animation"),
        ("a04fallaerial", "a04fallaerial", "Aerial fall animation"),
        ("a04fallspecial", "a04fallspecial", "Special fall animation"),
    ]

def initialize_predefined_poses(context):
    """Refresh predefined poses from the current animation folder."""
    ssp = context.scene.sub_scene_properties
    
    refresh_idle_poses(ssp)


def refresh_idle_poses(ssp):
    """Rebuild file-backed entries from the active folder; retain saved custom poses."""
    selected = (ssp.idle_pose_list[ssp.idle_pose_list_index].name
                if 0 <= ssp.idle_pose_list_index < len(ssp.idle_pose_list) else '')
    predefined = {pose[0] for pose in get_predefined_poses()}
    for index in reversed(range(len(ssp.idle_pose_list))):
        pose = ssp.idle_pose_list[index]
        if pose.name in predefined and not pose.is_custom:
            ssp.idle_pose_list.remove(index)
    folder = ssp.animation_import_folder_path
    available = ({p.stem.casefold() for p in Path(bpy.path.abspath(folder)).glob('*.nuanmb')}
                 if folder else set())
    existing_names = [pose.name for pose in ssp.idle_pose_list]
    
    # Add missing predefined poses
    for name, _, description in get_predefined_poses():
        if name not in existing_names and name.casefold() in available:
            new_pose = ssp.idle_pose_list.add()
            new_pose.name = name
            new_pose.data = ""  # Empty data indicates predefined pose
    ssp.idle_pose_list_index = next(
        (i for i, pose in enumerate(ssp.idle_pose_list) if pose.name == selected), 0)

def apply_pose_with_options(context, pose_data_str, include_trans=True, mirrored=False, rotate_180=False):
    """Apply pose data with the specified options"""
    armature = context.active_object
    current_frame = context.scene.frame_current
    
    if not armature or armature.type != 'ARMATURE':
        return {'CANCELLED'}, "No armature selected"
    
    if not pose_data_str:
        return {'CANCELLED'}, "No pose data available"
    
    try:
        reordered_bones = get_hierarchy_order(list(armature.pose.bones))
        
        # Get the stored pose data
        pose_data = json.loads(pose_data_str)
        
        # Same Smash-space flip as Mirror Animation / Studio SB (Hip and Trans included).
        if mirrored:
            excluded_bones = collect_excluded_bone_names(armature, include_fingers=False)
            pose_data = mirror_smash_pose_data(pose_data, excluded_bones=excluded_bones)
        
        # Create a mapping for quick access to stored node data
        bone_to_node_data = {}
        for bone in armature.pose.bones:
            if bone.name in pose_data:
                # Skip Trans bone if include_trans is False
                if not include_trans and bone.name == "Trans":
                    continue
                
                bone_to_node_data[bone] = pose_data[bone.name]
        
        # Process bones in hierarchy order using the shared Smash→Blender apply path
        for bone in reordered_bones:
            if bone not in bone_to_node_data:
                continue
            apply_smash_node_to_bone(bone, bone_to_node_data[bone])
        
        additive_layer = anim_layers_compat.is_non_base_anim_layer(armature)
        bones_to_key = list(bone_to_node_data.keys()) if additive_layer else list(armature.pose.bones)

        # On upper Anim Layers, key absolute values under REPLACE first, then
        # convert to ADD offsets so the base animation still plays underneath.
        if additive_layer:
            anim_layers_compat.prepare_absolute_keys_on_layer(armature)

        keyframed_count = 0
        for bone in bones_to_key:
            bone.keyframe_insert(data_path="location", frame=current_frame, group=bone.name)
            
            if bone.rotation_mode == 'QUATERNION':
                bone.keyframe_insert(data_path="rotation_quaternion", frame=current_frame, group=bone.name)
            else:
                bone.keyframe_insert(data_path="rotation_euler", frame=current_frame, group=bone.name)
            
            bone.keyframe_insert(data_path="scale", frame=current_frame, group=bone.name)
            keyframed_count += 1
        
        additive_note = ""
        if additive_layer and bones_to_key:
            keyed, err = anim_layers_compat.make_pose_additive_on_active_layer(
                context, armature, bones_to_key
            )
            if err:
                return {'CANCELLED'}, f"Idle pose applied but additive convert failed: {err}"
            additive_note = f" (additive on layer {armature.als.layer_index}, Blend=Add, {keyed} bones)"
        
        # Update the view and ensure pose is fully applied before mirroring
        context.view_layer.update()
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        
        # Small delay to ensure pose is fully processed
        import time
        time.sleep(0.01)
        
        # No need for additional mirroring - it's already done in the pose data if requested
        
        # Apply 180 rotate if requested (after pose application and mirroring)
        if rotate_180 and armature.animation_data and armature.animation_data.action:
            if additive_layer:
                anim_layers_compat.prepare_absolute_keys_on_layer(armature)
            rotate_hip_180(armature, 'Y', only_active_frame=True, current_frame=current_frame)
            # Re-convert after 180 so Hip stays additive on upper layers
            if additive_layer and bones_to_key:
                anim_layers_compat.make_pose_additive_on_active_layer(
                    context, armature, bones_to_key
                )
        
        # Update the view
        for area in context.screen.areas:
            area.tag_redraw()
        
        return {'FINISHED'}, f"Applied idle pose to frame {current_frame}{additive_note}"
        
    except Exception as e:
        return {'CANCELLED'}, f"Error applying idle pose: {str(e)}"

class SUB_OP_store_idle_pose(Operator):
    bl_idname = "sub.store_idle_pose"
    bl_label = "Store Idle Pose"
    bl_description = "Store the first frame of an idle animation for later use"
    bl_options = {'REGISTER', 'UNDO'}
    
    filter_glob: StringProperty(
        default='*.nuanmb',
        options={'HIDDEN'}
    )
    filepath: StringProperty(subtype="FILE_PATH")
    
    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE' or context.mode == 'OBJECT') and context.active_object and context.active_object.type == 'ARMATURE'
    
    def invoke(self, context, event):
        # Check if we have a current animation folder from the animation importer
        ssp = context.scene.sub_scene_properties
        if hasattr(ssp, 'animation_import_folder_path') and ssp.animation_import_folder_path:
            # Set the filepath to start in the animation folder
            self.filepath = ssp.animation_import_folder_path
        
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    def execute(self, context):
        armature = context.active_object
        
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}
        
        # Store the filepath to access it later
        context.scene["idle_pose_filepath"] = self.filepath
        
        try:
            # Read the animation data
            ssbh_anim_data = ssbh_data_py.anim_data.read_anim(self.filepath)
            
            # Get the transform group
            transform_group = None
            for group in ssbh_anim_data.groups:
                if group.group_type.name == 'Transform':
                    transform_group = group
                    break
            
            if not transform_group:
                self.report({'ERROR'}, "No transform data found in animation")
                return {'CANCELLED'}
            
            # Store first frame pose data in scene properties
            pose_data = {}
            
            for node in transform_group.nodes:
                # Check if the bone exists in the armature
                if node.name in armature.pose.bones:
                    # Only process if there are values (should always be true)
                    if node.tracks and len(node.tracks) > 0 and len(node.tracks[0].values) > 0:
                        # Get the first frame's data
                        track = node.tracks[0]
                        
                        if len(track.values) > 0:
                            transform = track.values[0]
                            
                            # Store the serializable data
                            transform_data = {
                                "scale": [transform.scale[0], transform.scale[1], transform.scale[2]],
                                "rotation": [transform.rotation[0], transform.rotation[1], transform.rotation[2], transform.rotation[3]],
                                "translation": [transform.translation[0], transform.translation[1], transform.translation[2]],
                                "flags": {
                                    "override_translation": track.transform_flags.override_translation,
                                    "override_rotation": track.transform_flags.override_rotation,
                                    "override_scale": track.transform_flags.override_scale,
                                    "compensate_scale": track.compensate_scale
                                }
                            }
                            
                            # Store the data
                            pose_data[node.name] = transform_data
            
            # Store the pose data in a custom property as a JSON string
            context.scene["idle_pose_data"] = json.dumps(pose_data)
            
            # Store animation name for display
            context.scene["idle_pose_name"] = Path(self.filepath).stem
            
            # Add to the idle pose list
            ssp = context.scene.sub_scene_properties
            pose_name = Path(self.filepath).stem
            
            # Check if pose already exists in the list
            existing_pose = None
            for pose in ssp.idle_pose_list:
                if pose.name == pose_name:
                    existing_pose = pose
                    break
            
            if existing_pose:
                # Update existing pose
                existing_pose.data = json.dumps(pose_data)
                existing_pose.is_custom = True
            else:
                # Add new pose
                new_pose = ssp.idle_pose_list.add()
                new_pose.name = pose_name
                new_pose.data = json.dumps(pose_data)
                new_pose.is_custom = True
            
            self.report({'INFO'}, f"Successfully stored idle pose from {Path(self.filepath).name}")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Error reading animation file: {str(e)}")
            return {'CANCELLED'}


class SUB_OP_apply_idle_pose(Operator):
    bl_idname = "sub.apply_idle_pose"
    bl_label = "Apply Idle Pose"
    bl_description = "Apply the stored idle pose to the current frame"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE' or context.mode == 'OBJECT') and \
               context.active_object and context.active_object.type == 'ARMATURE' and \
               "idle_pose_data" in context.scene
    
    def execute(self, context):
        if "idle_pose_data" not in context.scene:
            self.report({'ERROR'}, "No idle pose data stored. Please store an idle pose first.")
            return {'CANCELLED'}
        
        # Get options from scene properties
        ssp = context.scene.sub_scene_properties
        include_trans = ssp.idle_pose_include_trans
        mirrored = ssp.idle_pose_mirrored
        rotate_180 = ssp.idle_pose_180_rotate
        
        # Get the stored pose data
        pose_data_str = context.scene["idle_pose_data"]
        
        # Apply the pose using the helper function
        result, message = apply_pose_with_options(context, pose_data_str, include_trans, mirrored, rotate_180)
        
        if result == {'FINISHED'}:
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, message)
        
        return result


class SUB_OP_apply_idle_pose_from_list(Operator):
    bl_idname = "sub.apply_idle_pose_from_list"
    bl_label = "Apply Selected Idle Pose"
    bl_description = "Apply the selected idle pose from the dropdown list"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE' or context.mode == 'OBJECT') and \
               context.active_object and context.active_object.type == 'ARMATURE'
    
    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        
        # Refresh saved scenes and pick up files added since the folder changed.
        initialize_predefined_poses(context)
        
        if not ssp.idle_pose_list:
            self.report({'ERROR'}, "No poses available in the library")
            return {'CANCELLED'}
        
        if ssp.idle_pose_list_index >= len(ssp.idle_pose_list):
            self.report({'ERROR'}, "Invalid pose selection")
            return {'CANCELLED'}
        
        selected_pose = ssp.idle_pose_list[ssp.idle_pose_list_index]
        
        # Check if this is a predefined pose without data
        pose_data_str = selected_pose.data
        if not selected_pose.is_custom and selected_pose.name in {p[0] for p in get_predefined_poses()}:
            # This is a predefined pose that needs to be loaded from animation file
            # Try to find the animation file in the animation import folder
            if hasattr(ssp, 'animation_import_folder_path') and ssp.animation_import_folder_path:
                animation_path = Path(bpy.path.abspath(ssp.animation_import_folder_path)) / f"{selected_pose.name}.nuanmb"
                if animation_path.exists():
                    # Load the animation data
                    try:
                        ssbh_anim_data = ssbh_data_py.anim_data.read_anim(str(animation_path))
                        
                        # Get the transform group
                        transform_group = None
                        for group in ssbh_anim_data.groups:
                            if group.group_type.name == 'Transform':
                                transform_group = group
                                break
                        
                        if not transform_group:
                            self.report({'ERROR'}, f"No transform data found in {selected_pose.name}")
                            return {'CANCELLED'}
                        
                        # Extract pose data
                        pose_data = {}
                        armature = context.active_object
                        
                        for node in transform_group.nodes:
                            # Check if the bone exists in the armature
                            if node.name in armature.pose.bones:
                                # Only process if there are values (should always be true)
                                if node.tracks and len(node.tracks) > 0 and len(node.tracks[0].values) > 0:
                                    # Get the first frame's data
                                    track = node.tracks[0]
                                    
                                    if len(track.values) > 0:
                                        transform = track.values[0]
                                        
                                        # Store the serializable data
                                        transform_data = {
                                            "scale": [transform.scale[0], transform.scale[1], transform.scale[2]],
                                            "rotation": [transform.rotation[0], transform.rotation[1], transform.rotation[2], transform.rotation[3]],
                                            "translation": [transform.translation[0], transform.translation[1], transform.translation[2]],
                                            "flags": {
                                                "override_translation": track.transform_flags.override_translation,
                                                "override_rotation": track.transform_flags.override_rotation,
                                                "override_scale": track.transform_flags.override_scale,
                                                "compensate_scale": track.compensate_scale
                                            }
                                        }
                                        
                                        # Store the data
                                        pose_data[node.name] = transform_data
                        
                        # Keep file-backed data local so it never outlives its folder context.
                        pose_data_str = json.dumps(pose_data)
                        
                    except Exception as e:
                        self.report({'ERROR'}, f"Error loading {selected_pose.name}: {str(e)}")
                        return {'CANCELLED'}
                else:
                    self.report({'ERROR'}, f"Animation file {selected_pose.name}.nuanmb not found in {ssp.animation_import_folder_path}")
                    return {'CANCELLED'}
            else:
                self.report({'ERROR'}, f"No animation folder path set. Please import a model first or store a custom pose.")
                return {'CANCELLED'}
        
        # Get options from scene properties
        include_trans = ssp.idle_pose_include_trans
        mirrored = ssp.idle_pose_mirrored
        rotate_180 = ssp.idle_pose_180_rotate
        
        # Apply the pose using the helper function
        result, message = apply_pose_with_options(context, pose_data_str, include_trans, mirrored, rotate_180)
        
        if result == {'FINISHED'}:
            self.report({'INFO'}, f"Applied '{selected_pose.name}' pose - {message.split('-')[-1].strip()}")
        else:
            self.report({'ERROR'}, message)
        
        return result
