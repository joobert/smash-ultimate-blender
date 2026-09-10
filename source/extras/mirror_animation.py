import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, BoolProperty
import logging
import mathutils

from ..anim.fcurve_compat import get_fcurves, get_all_action_fcurves, new_fcurve, find_fcurve, remove_fcurve, collect_actions_for_armatures
from ..blender_compat import is_armature_bone_selected, is_pose_bone_selected, assign_action
from .anim_flip import (
    collect_excluded_bone_names,
    collect_unchecked_custom_mirror_bones,
    create_mirror_map,
    extract_bone_name_from_path,
    find_custom_mirror_bones,
    keyframe_pose_bones,
    load_smash_pose_cache,
    mirror_evaluated_pose,
    should_exclude_bone_from_mirroring,
    smash_pose_data_from_armature,
    smash_pose_data_from_cache,
)

# Set up logging
logger = logging.getLogger(__name__)

# Extra axes kept for the operator UI. Y uses the same fcurve mirror path as X/Z.
NEGATE_DATA_PATH_XAXIS = (
    ('location', 0),
    ('rotation_quaternion', 2),
    ('rotation_quaternion', 3),
    ('rotation_euler', 2),
    ('rotation_euler', 1),
)

NEGATE_DATA_PATH_YAXIS = (
    ('location', 2),
    ('rotation_quaternion', 1),
    ('rotation_quaternion', 2),
    ('rotation_euler', 0),
    ('rotation_euler', 1),
)

NEGATE_DATA_PATH_ZAXIS = (
    ('location', 1),
    ('rotation_quaternion', 1),
    ('rotation_quaternion', 3),
    ('rotation_euler', 0),
    ('rotation_euler', 2),
)


#########################################################################################
# Mirror Action
#########################################################################################


def negate_fcurve(fcurve, only_active_frame=False, current_frame=None):
    for k in fcurve.keyframe_points:
        if only_active_frame and current_frame is not None:
            # Only negate keyframes at the current frame
            if abs(k.co[0] - current_frame) < 0.001:  # Small tolerance for floating point comparison
                k.co[1] = -k.co[1]
                k.handle_left[1] = -k.handle_left[1]
                k.handle_right[1] = -k.handle_right[1]
        else:
            # Negate all keyframes
            k.co[1] = -k.co[1]
            k.handle_left[1] = -k.handle_left[1]
            k.handle_right[1] = -k.handle_right[1]

def apply_global_mirror_transform(value, axis, object_matrix):
    """Apply global mirroring transformation considering object's world matrix"""
    if object_matrix is None:
        return -value
    
    # Create a vector for the value
    if axis == 'X':
        vec = mathutils.Vector((value, 0, 0))
    elif axis == 'Y':
        vec = mathutils.Vector((0, value, 0))
    elif axis == 'Z':
        vec = mathutils.Vector((0, 0, value))
    else:
        return -value
    
    # Transform to world space
    world_vec = object_matrix @ vec
    
    # Mirror in world space
    if axis == 'X':
        world_vec.x = -world_vec.x
    elif axis == 'Y':
        world_vec.y = -world_vec.y
    elif axis == 'Z':
        world_vec.z = -world_vec.z
    
    # Transform back to local space
    local_vec = object_matrix.inverted() @ world_vec
    
    # Return the appropriate component
    if axis == 'X':
        return local_vec.x
    elif axis == 'Y':
        return local_vec.y
    elif axis == 'Z':
        return local_vec.z
    
    return -value


def _apply_channel_negation(value, left_handle, right_handle, axis, mirror_space, object_matrix):
    if mirror_space != 'GLOBAL':
        return -value, -left_handle, -right_handle
    return (
        apply_global_mirror_transform(value, axis, object_matrix),
        apply_global_mirror_transform(left_handle, axis, object_matrix),
        apply_global_mirror_transform(right_handle, axis, object_matrix),
    )


def _selected_pose_bone_names(context):
    if not context or not context.active_object or context.active_object.type != 'ARMATURE':
        return set()
    armature_obj = context.active_object
    names = set()
    if context.selected_pose_bones:
        names.update(bone.name for bone in context.selected_pose_bones)
    if armature_obj.mode == 'POSE':
        names.update(
            pbone.name for pbone in armature_obj.pose.bones if is_pose_bone_selected(pbone)
        )
        active = context.active_pose_bone
        if active is not None and is_pose_bone_selected(active):
            names.add(active.name)
    else:
        names.update(
            bone.name for bone in armature_obj.data.bones
            if is_armature_bone_selected(armature_obj, bone)
        )
    return names


def _action_bone_names(act):
    names = set()
    for fcurve in get_fcurves(act):
        bone_name = extract_bone_name_from_path(fcurve.data_path)
        if bone_name:
            names.add(bone_name)
    return names


def _idle_library_pose_data(context, act):
    """Reuse stored Idle Pose nuanmb data when the action has no import cache."""
    import json
    action_name = act.name if act else ""
    candidates = []
    ssp = getattr(context.scene, "sub_scene_properties", None)
    if ssp is not None:
        for pose in getattr(ssp, "idle_pose_list", []):
            if pose.data and pose.name and pose.name in action_name:
                candidates.append(pose.data)
    if "idle_pose_data" in context.scene:
        candidates.append(context.scene["idle_pose_data"])
    for data in candidates:
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed:
            return parsed
    return None


def _should_use_smash_y_mirror(act, context, selected_bones_only, prefer_smash_y):
    """Use Smash pose flip for Y when requested and doing a full-body mirror."""
    return prefer_smash_y and not selected_bones_only


def _mirror_y_axis(action, use_smash_y, smash_y_kwargs, fcurve_kwargs):
    if use_smash_y:
        mirror_action_smash_y(action, **smash_y_kwargs)
        return True
    mirror_action(action, axis='Y', **fcurve_kwargs)
    return False


def get_armature_actions(armature_object):
    """Return pose actions for this armature, excluding SAP/_old backups."""
    return collect_actions_for_armatures([armature_object])


def _apply_hip_180_for_mirror_axis(armature, axis, only_active_frame, current_frame):
    if axis in ('X', 'Y', 'Z'):
        rotate_hip_180(armature, axis, only_active_frame=only_active_frame, current_frame=current_frame)
    elif axis == 'XY':
        rotate_hip_180(armature, 'X', only_active_frame=only_active_frame, current_frame=current_frame)
        rotate_hip_180(armature, 'Y', only_active_frame=only_active_frame, current_frame=current_frame)
    elif axis == 'XZ':
        rotate_hip_180(armature, 'X', only_active_frame=only_active_frame, current_frame=current_frame)
        rotate_hip_180(armature, 'Z', only_active_frame=only_active_frame, current_frame=current_frame)
    elif axis == 'YZ':
        rotate_hip_180(armature, 'Y', only_active_frame=only_active_frame, current_frame=current_frame)
        rotate_hip_180(armature, 'Z', only_active_frame=only_active_frame, current_frame=current_frame)
    elif axis == 'XYZ':
        rotate_hip_180(armature, 'X', only_active_frame=only_active_frame, current_frame=current_frame)
        rotate_hip_180(armature, 'Y', only_active_frame=only_active_frame, current_frame=current_frame)
        rotate_hip_180(armature, 'Z', only_active_frame=only_active_frame, current_frame=current_frame)


def apply_mirror_to_action(
    context,
    armature,
    action,
    axis,
    *,
    rotate_180=False,
    selected_bones_only=False,
    only_active_frame=False,
    mirror_space='LOCAL',
    include_fingers=False,
    smash_y_anim_flip=False,
    selected_bone_names=None,
):
    """Mirror one action on an armature. Returns (success, used_smash_y)."""
    if axis == 'O':
        return False, False
    if not action or not get_all_action_fcurves(action):
        return False, False

    current_frame = context.scene.frame_current if only_active_frame else None
    captured_bones = set(selected_bone_names or [])
    if selected_bones_only:
        if not captured_bones:
            captured_bones = _selected_pose_bone_names(context)
        if not captured_bones:
            return False, False

    smash_y_kwargs = dict(
        selected_bones_only=selected_bones_only,
        context=context,
        only_active_frame=only_active_frame,
        include_fingers=include_fingers,
        selected_bone_names=captured_bones if selected_bones_only else None,
    )
    fcurve_kwargs = dict(
        selected_bones_only=selected_bones_only,
        context=context,
        only_active_frame=only_active_frame,
        mirror_space=mirror_space,
        include_fingers=include_fingers,
        selected_bone_names=captured_bones if selected_bones_only else None,
    )
    use_smash_y = _should_use_smash_y_mirror(
        action, context, selected_bones_only, smash_y_anim_flip
    )
    used_smash_y = False

    if axis == 'Y':
        used_smash_y = _mirror_y_axis(action, use_smash_y, smash_y_kwargs, fcurve_kwargs)
    elif axis in ('X', 'Z'):
        mirror_action(action, axis=axis, **fcurve_kwargs)
    elif axis == 'XY':
        mirror_action(action, axis='X', **fcurve_kwargs)
        used_smash_y = _mirror_y_axis(action, use_smash_y, smash_y_kwargs, fcurve_kwargs)
    elif axis == 'XZ':
        mirror_action(action, axis='X', **fcurve_kwargs)
        mirror_action(action, axis='Z', **fcurve_kwargs)
    elif axis == 'YZ':
        used_smash_y = _mirror_y_axis(action, use_smash_y, smash_y_kwargs, fcurve_kwargs)
        mirror_action(action, axis='Z', **fcurve_kwargs)
    elif axis == 'XYZ':
        mirror_action(action, axis='X', **fcurve_kwargs)
        used_smash_y = _mirror_y_axis(action, use_smash_y, smash_y_kwargs, fcurve_kwargs)
        mirror_action(action, axis='Z', **fcurve_kwargs)

    if rotate_180:
        _apply_hip_180_for_mirror_axis(armature, axis, only_active_frame, current_frame)

    return True, used_smash_y


def _frames_for_action(act, only_active_frame, scene, selected_bone_names=None):
    if only_active_frame:
        return [scene.frame_current]

    if selected_bone_names:
        frames = sorted({
            int(round(keyframe.co[0]))
            for fcurve in get_fcurves(act)
            if extract_bone_name_from_path(fcurve.data_path) in selected_bone_names
            for keyframe in fcurve.keyframe_points
        })
        if frames:
            return frames

    frames = sorted({
        int(round(keyframe.co[0]))
        for fcurve in get_fcurves(act)
        for keyframe in fcurve.keyframe_points
    })
    return frames or [scene.frame_current]


def mirror_action_smash_y(
    act,
    selected_bones_only=False,
    context=None,
    only_active_frame=False,
    include_fingers=False,
    selected_bone_names=None,
):
    """
    Y-axis Smash Ultimate mirror: same Studio SB flip + importer as Idle Pose
    Library Mirrored. Prefers the Smash TRS cache written on nuanmb import.
    """
    if not context or not context.active_object or context.active_object.type != 'ARMATURE':
        print("Smash Y mirror requires an active armature")
        return

    armature = context.active_object
    excluded_bones = collect_excluded_bone_names(armature, include_fingers=include_fingers)
    ssp = getattr(context.scene, 'sub_scene_properties', None)
    if ssp is not None:
        excluded_bones |= collect_unchecked_custom_mirror_bones(
            armature, getattr(ssp, 'mirror_custom_bones', [])
        )
    source_bones = None
    target_bones = None
    in_place = False
    if selected_bones_only:
        selected_bones = set(selected_bone_names or [])
        if not selected_bones and context is not None:
            selected_bones = _selected_pose_bone_names(context)
        if not selected_bones:
            print("Warning: No bones selected for 'Selected Bones Only' mode")
            return
        source_bones = selected_bones
        target_bones = selected_bones
        in_place = True
        excluded_bones -= source_bones

    scene = context.scene
    smash_cache = load_smash_pose_cache(act)
    bone_filter = source_bones
    if bone_filter is None:
        animated_bones = _action_bone_names(act)
        bone_filter = animated_bones or None

    frames = _frames_for_action(
        act,
        only_active_frame,
        scene,
        selected_bone_names=source_bones,
    )

    original_frame = scene.frame_current
    for frame in frames:
        if scene.frame_current != frame:
            scene.frame_set(frame)
        context.view_layer.update()
        if in_place and source_bones:
            # Read the selected bones directly from the evaluated pose at this frame.
            pose_data = smash_pose_data_from_armature(armature, bone_filter=source_bones)
        else:
            pose_data = smash_pose_data_from_cache(smash_cache, frame) if smash_cache else None
            if pose_data is None:
                pose_data = _idle_library_pose_data(context, act)
        applied = mirror_evaluated_pose(
            armature,
            excluded_bones=excluded_bones,
            target_bones=target_bones,
            source_bones=source_bones,
            pose_data=pose_data,
            bone_filter=bone_filter,
            in_place=in_place,
        )
        keyframe_pose_bones(applied, frame)

    if scene.frame_current != original_frame:
        scene.frame_set(original_frame)


def mirror_action(
    act,
    axis='X',
    selected_bones_only=False,
    context=None,
    only_active_frame=False,
    mirror_space='LOCAL',
    include_fingers=True,
    selected_bone_names=None,
):
    
    if not (act and get_all_action_fcurves(act)):
        print("No Keyframes")
        return
    
    # Get current frame if only_active_frame is enabled
    current_frame = None
    if only_active_frame and context:
        current_frame = context.scene.frame_current
    
    # Get selected bone names if filtering is enabled
    selected_bone_names = set(selected_bone_names or [])
    if selected_bones_only and context and context.active_object and context.active_object.type == 'ARMATURE':
        if not selected_bone_names:
            armature_obj = context.active_object
            if context.selected_pose_bones:
                selected_bone_names = {bone.name for bone in context.selected_pose_bones}
            elif armature_obj.mode == 'POSE':
                selected_bone_names = {
                    pbone.name for pbone in armature_obj.pose.bones if is_pose_bone_selected(pbone)
                }
            else:
                selected_bone_names = {
                    bone.name for bone in armature_obj.data.bones
                    if is_armature_bone_selected(armature_obj, bone)
                }

        if not selected_bone_names:
            print("Warning: No bones selected for 'Selected Bones Only' mode")
            return

    selected_only_set = selected_bone_names if selected_bones_only else set()
    
    # Get armature for bone exclusion checks
    armature = context.active_object if context and context.active_object and context.active_object.type == 'ARMATURE' else None
    custom_skip = set()
    if context and armature is not None:
        ssp = getattr(context.scene, 'sub_scene_properties', None)
        if ssp is not None:
            custom_skip = collect_unchecked_custom_mirror_bones(
                armature, getattr(ssp, 'mirror_custom_bones', [])
            )
    
    # Get object transformation matrix for global mirroring
    object_matrix = None
    if mirror_space == 'GLOBAL' and context and context.active_object:
        object_matrix = context.active_object.matrix_world.copy()
    
    # create name map
    # strip attribute suffix eg. 'pose.bones["root"].location' -> 'pose.bones["root"]'
    bone_names = {fc.data_path.rsplit('.', 1)[0] for fc in get_fcurves(act) if '.' in fc.data_path}
    mirror_map = create_mirror_map(bone_names)

    if axis == 'X':
        negate_data_path_tuples = NEGATE_DATA_PATH_XAXIS
    elif axis == 'Y':
        negate_data_path_tuples = NEGATE_DATA_PATH_YAXIS
    elif axis == 'Z':
        negate_data_path_tuples = NEGATE_DATA_PATH_ZAXIS
    else:
        raise ValueError(f"Unsupported {axis=}")

    if only_active_frame and current_frame is not None:
        # Frame-specific mirroring: only affect keyframes at current frame
        # Step 1: Collect all source data first to prevent overwriting issues
        keyframe_data = []  # List of (source_path, target_path, array_index, value, left_handle, right_handle)
        
        for fc in get_all_action_fcurves(act):
            data_path = fc.data_path
            array_index = fc.array_index
            path, _dot, attribute = data_path.rpartition('.')
            
            bone_name = extract_bone_name_from_path(path)
            
            # Check if this bone should be excluded from mirroring
            if bone_name and bone_name not in selected_only_set and (
                should_exclude_bone_from_mirroring(bone_name, armature, include_fingers)
                or bone_name in custom_skip
            ):
                continue
            
            # Determine target data path (mirrored bone, or same bone when selected-only)
            target_data_path = data_path
            target_bone_name = bone_name
            if not selected_bones_only and path and (path in mirror_map):
                target_data_path = "".join((mirror_map[path], _dot, attribute))
                target_bone_name = extract_bone_name_from_path(mirror_map[path]) or bone_name
            
            # Check if SOURCE bone should be affected (selected bones only filter).
            if selected_bones_only and bone_name and bone_name not in selected_bone_names:
                continue
            
            # Find the keyframe at current frame
            current_kf = None
            for kf in fc.keyframe_points:
                if abs(kf.co[0] - current_frame) < 0.001:
                    current_kf = kf
                    break
            
            if current_kf is None:
                continue
                
            # Get the value and handles
            value = current_kf.co[1]
            left_handle = current_kf.handle_left[1]
            right_handle = current_kf.handle_right[1]
            
            # Studio SB anim_flip applies to Hip / Trans location as well.
            should_negate = (attribute, array_index) in negate_data_path_tuples
            
            if should_negate:
                value, left_handle, right_handle = _apply_channel_negation(
                    value, left_handle, right_handle, axis, mirror_space, object_matrix
                )
            
            # Store the data for later application
            keyframe_data.append((data_path, target_data_path, array_index, value, left_handle, right_handle))
        
        # Step 2: Apply all mirrored keyframes at once
        for source_path, target_path, array_index, value, left_handle, right_handle in keyframe_data:
            # Find or create target fcurve
            target_fc = None
            for fc_check in get_fcurves(act):
                if fc_check.data_path == target_path and fc_check.array_index == array_index:
                    target_fc = fc_check
                    break
            
            if target_fc is None:
                target_fc = new_fcurve(act, target_path, index=array_index)
            
            # Set keyframe at current frame
            target_fc.keyframe_points.insert(current_frame, value)
            
            # Update the keyframe handles
            for kf in target_fc.keyframe_points:
                if abs(kf.co[0] - current_frame) < 0.001:
                    kf.handle_left = (kf.handle_left[0], left_handle)
                    kf.handle_right = (kf.handle_right[0], right_handle)
                    break
                    
    else:
        # Full animation mirroring: collect all data first, delete fcurves, then create new ones
        # Step 1: Collect all fcurve data with mirrored values
        fcurve_data = []  # List of (target_path, array_index, keyframe_values, action_group)
        paths_to_remove = set()  # (data_path, array_index)
        
        for fc in get_all_action_fcurves(act):
            data_path = fc.data_path
            array_index = fc.array_index

            # bone curves are 'pose.bones["root"].location'
            # objects curves are simply 'location'
            path, _dot, attribute = data_path.rpartition('.')
            
            bone_name = extract_bone_name_from_path(path)
            
            # Check if this bone should be excluded from mirroring
            if bone_name and bone_name not in selected_only_set and (
                should_exclude_bone_from_mirroring(bone_name, armature, include_fingers)
                or bone_name in custom_skip
            ):
                continue
            
            # Determine target path (mirrored bone, or same bone when selected-only)
            target_data_path = data_path
            target_bone_name = bone_name
            
            if not selected_bones_only and path and (path in mirror_map):
                target_data_path = "".join((mirror_map[path], _dot, attribute))
                target_bone_name = extract_bone_name_from_path(mirror_map[path]) or bone_name
            
            # Check if SOURCE bone should be affected (selected bones only filter).
            if selected_bones_only and bone_name and bone_name not in selected_bone_names:
                continue
            
            # Studio SB anim_flip applies to Hip / Trans location as well.
            should_negate = (attribute, array_index) in negate_data_path_tuples
            
            if selected_bones_only and target_data_path == data_path:
                for kf in fc.keyframe_points:
                    if should_negate:
                        kf.co[1] = -kf.co[1]
                        kf.handle_left[1] = -kf.handle_left[1]
                        kf.handle_right[1] = -kf.handle_right[1]
                continue
            
            # Collect all keyframe data from this fcurve
            keyframe_values = []
            for kf in fc.keyframe_points:
                frame = kf.co[0]
                value = kf.co[1]
                left_handle_x = kf.handle_left[0]
                left_handle_y = kf.handle_left[1]
                right_handle_x = kf.handle_right[0]
                right_handle_y = kf.handle_right[1]
                interpolation = kf.interpolation
                
                if should_negate:
                    value, left_handle_y, right_handle_y = _apply_channel_negation(
                        value, left_handle_y, right_handle_y, axis, mirror_space, object_matrix
                    )
                
                keyframe_values.append((frame, value, left_handle_x, left_handle_y, right_handle_x, right_handle_y, interpolation))
            
            # Store the mirrored data
            fcurve_data.append((target_data_path, array_index, keyframe_values, target_bone_name))
            paths_to_remove.add((data_path, array_index))
        
        # Step 2: Remove all source fcurves by path (safe on Blender 5 layered actions)
        for data_path, array_index in paths_to_remove:
            existing_fc = find_fcurve(act, data_path, index=array_index)
            if existing_fc:
                remove_fcurve(act, existing_fc)
        
        # Step 3: Create new fcurves with mirrored data
        for target_path, array_index, keyframe_values, action_group_name in fcurve_data:
            # Check if target fcurve already exists (can happen with selected_bones_only 
            # when target bone wasn't selected but source was)
            existing_fc = find_fcurve(act, target_path, index=array_index)
            if existing_fc:
                remove_fcurve(act, existing_fc)
            
            # Create new fcurve
            new_fc = new_fcurve(act, target_path, index=array_index, action_group=action_group_name)
            
            # Add all keyframes
            for frame, value, left_x, left_y, right_x, right_y, interpolation in keyframe_values:
                kf = new_fc.keyframe_points.insert(frame, value)
                kf.handle_left = (left_x, left_y)
                kf.handle_right = (right_x, right_y)
                kf.interpolation = interpolation


#########################################################################################
# Hip Bone 180 Rotation
#########################################################################################

def find_hip_bone(armature):
    """Find the hip bone in the armature"""
    if not armature or armature.type != 'ARMATURE':
        return None
    
    # Common hip bone names
    hip_keywords = ['hip', 'pelvis', 'root']
    
    for bone in armature.pose.bones:
        bone_name_lower = bone.name.lower()
        for keyword in hip_keywords:
            if keyword in bone_name_lower:
                return bone
    
    return None

def rotate_hip_180(armature, axis, only_active_frame=False, current_frame=None):
    """Rotate hip bone 180 degrees on specified axis"""
    hip_bone = find_hip_bone(armature)
    
    if not hip_bone:
        print("No hip bone found for 180 degree rotation")
        return False
    
    import math
    rotation_180 = math.radians(180)
    
    # Store original mode and ensure we're in pose mode
    original_mode = bpy.context.mode
    if original_mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')
    
    if only_active_frame and current_frame is not None:
        # Only rotate at current frame
        if hip_bone.rotation_mode == 'QUATERNION':
            # Create rotation quaternion and apply it
            if axis == 'X':
                rot_quat = mathutils.Quaternion((1, 0, 0), rotation_180)
            elif axis == 'Y':
                rot_quat = mathutils.Quaternion((0, 1, 0), rotation_180)
            elif axis == 'Z':
                rot_quat = mathutils.Quaternion((0, 0, 1), rotation_180)
            hip_bone.rotation_quaternion = rot_quat @ hip_bone.rotation_quaternion
            hip_bone.keyframe_insert(data_path="rotation_quaternion", frame=current_frame, group=hip_bone.name)
        else:
            # Apply 180 degree rotation to euler
            if axis == 'X':
                hip_bone.rotation_euler[0] += rotation_180
            elif axis == 'Y':
                hip_bone.rotation_euler[1] += rotation_180
            elif axis == 'Z':
                hip_bone.rotation_euler[2] += rotation_180
            hip_bone.keyframe_insert(data_path="rotation_euler", frame=current_frame, group=hip_bone.name)
    else:
        # Rotate for entire animation
        action = armature.animation_data.action if armature.animation_data else None
        if not action:
            print("No action found for hip bone rotation")
            return False
        
        # Find all frames with keyframes for the hip bone
        hip_frames = set()
        for fcurve in get_fcurves(action):
            if f'pose.bones["{hip_bone.name}"]' in fcurve.data_path and 'rotation' in fcurve.data_path:
                for keyframe in fcurve.keyframe_points:
                    hip_frames.add(int(keyframe.co[0]))
        
        # If no keyframes found, apply to current frame only
        if not hip_frames:
            hip_frames = {current_frame or 1}
        
        # Store original frame
        original_frame = bpy.context.scene.frame_current
        
        # Apply rotation to each frame
        for frame in hip_frames:
            bpy.context.scene.frame_set(frame)
            
            if hip_bone.rotation_mode == 'QUATERNION':
                # Create rotation quaternion and apply it
                if axis == 'X':
                    rot_quat = mathutils.Quaternion((1, 0, 0), rotation_180)
                elif axis == 'Y':
                    rot_quat = mathutils.Quaternion((0, 1, 0), rotation_180)
                elif axis == 'Z':
                    rot_quat = mathutils.Quaternion((0, 0, 1), rotation_180)
                hip_bone.rotation_quaternion = rot_quat @ hip_bone.rotation_quaternion
                hip_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame, group=hip_bone.name)
            else:
                if axis == 'X':
                    hip_bone.rotation_euler[0] += rotation_180
                elif axis == 'Y':
                    hip_bone.rotation_euler[1] += rotation_180
                elif axis == 'Z':
                    hip_bone.rotation_euler[2] += rotation_180
                hip_bone.keyframe_insert(data_path="rotation_euler", frame=frame, group=hip_bone.name)
        
        # Restore original frame
        bpy.context.scene.frame_set(original_frame)
    
    # Restore original mode
    if original_mode != 'POSE':
        bpy.ops.object.mode_set(mode=original_mode)
    
    print(f"Applied 180 degree rotation to hip bone '{hip_bone.name}' on {axis}-axis")
    return True


#########################################################################################
# OPERATORS
#########################################################################################


class SUB_OT_mirror_action(Operator):
    """Mirror/flip animation on selected axis"""
    bl_description = 'Mirror/flip animation on selected axis'
    bl_idname = "sub.mirror_action"
    bl_label = "Mirror Action"
    bl_options = {"REGISTER","UNDO"}

    axis : EnumProperty(
        name="Axis",
        description="Select mirror axis",
        default='Y',
        items = (
            ('X', 'X', "X axis"),
            ('Y', 'Y', "Y axis"),
            ('Z', 'Z', "Z axis"),
            ('XY', 'XY', "Both XY axes"),
            ('XZ', 'XZ', "Both XZ axes"),
            ('YZ', 'YZ', "Both YZ axes"),
            ('XYZ', 'XYZ', "All XYZ axes"),
            ('O', 'Original', "Original"),
        )
    )

    rotate_180 : BoolProperty(
        name="180 Rotate",
        description="Rotate hip bone 180 degrees on selected axis after mirroring. Leave off to match Idle Pose Library Mirrored.",
        default=False
    )

    selected_bones_only : BoolProperty(
        name="Selected Bones Only",
        description="Mirror only the selected bones in place (does not swap L/R sides)",
        default=False
    )

    only_active_frame : BoolProperty(
        name="Only Active Frame",
        description="Mirror only keyframes at the current frame",
        default=False
    )

    include_fingers : BoolProperty(
        name="Include Fingers",
        description="Include finger bones (FingerL11, FingerR23, etc.) in the mirroring process. Off matches Idle Pose Library Mirrored.",
        default=False
    )

    smash_y_anim_flip : BoolProperty(
        name="Smash Y Anim Flip",
        description="Use Studio SB anim_flip for Y-axis (matches Idle Pose Library Mirrored). Off uses fcurve mirroring like X/Z",
        default=False,
    )

    selected_bone_names: bpy.props.StringProperty(
        name="Selected Bone Names",
        description="Pose bones captured when the operator was invoked",
        default="",
        options={'HIDDEN'},
    )


    @classmethod
    def poll(cls, context):
        return context.active_object

    @staticmethod
    def _parse_bone_names(name_string):
        if not name_string:
            return set()
        return {name for name in name_string.split("|") if name}

    @staticmethod
    def _format_bone_names(bone_names):
        return "|".join(sorted(bone_names))

    def invoke(self, context, event):
        if context.active_object and context.active_object.type == 'ARMATURE':
            self.selected_bone_names = self._format_bone_names(
                _selected_pose_bone_names(context)
            )
        else:
            self.selected_bone_names = ""
        ssp = getattr(context.scene, "sub_scene_properties", None)
        if ssp is not None:
            self.smash_y_anim_flip = ssp.mirror_smash_y_anim_flip
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "axis")
        layout.prop(self, "rotate_180")
        layout.prop(self, "selected_bones_only")
        layout.prop(self, "only_active_frame")
        layout.prop(self, "include_fingers")
        if self.axis in ('Y', 'XY', 'YZ', 'XYZ'):
            layout.prop(self, "smash_y_anim_flip")
            if self.selected_bones_only and self.smash_y_anim_flip:
                layout.label(text="Selected bones always use fcurve Y mirror", icon='INFO')

        if self.selected_bones_only:
            selected = self._parse_bone_names(self.selected_bone_names)
            if selected:
                layout.label(text=f"Bones: {', '.join(sorted(selected))}", icon='BONE_DATA')
            else:
                layout.label(text="Select pose bones first", icon='ERROR')

    def execute(self, context):

        if not context.active_object.animation_data:
            self.report({"ERROR"}, "No Animation Data")
            return {'CANCELLED'}
        if not context.active_object.animation_data.action:
            self.report({"ERROR"}, "No Action assigned")
            return {'CANCELLED'}
        if not get_fcurves(context.active_object.animation_data.action):
            self.report({"ERROR"}, "No Keyframes")
            return {'CANCELLED'}

        armature = context.active_object
        ssp = context.scene.sub_scene_properties
        ssp.mirror_smash_y_anim_flip = self.smash_y_anim_flip

        captured_bones = self._parse_bone_names(self.selected_bone_names)
        if self.selected_bones_only and armature.type == 'ARMATURE':
            selected_bones = captured_bones or _selected_pose_bone_names(context)
            if not selected_bones:
                self.report({"WARNING"}, "No bones selected. Select bones in Pose mode first.")
                return {'CANCELLED'}
            captured_bones = selected_bones

        action = armature.animation_data.action
        success, used_smash_y = apply_mirror_to_action(
            context,
            armature,
            action,
            self.axis,
            rotate_180=self.rotate_180,
            selected_bones_only=self.selected_bones_only,
            only_active_frame=self.only_active_frame,
            mirror_space=ssp.mirror_space,
            include_fingers=self.include_fingers,
            smash_y_anim_flip=self.smash_y_anim_flip,
            selected_bone_names=captured_bones if self.selected_bones_only else None,
        )
        if not success:
            self.report({"ERROR"}, "No Keyframes")
            return {'CANCELLED'}

        if used_smash_y:
            message = f"Action mirrored on {self.axis}-axis using Smash anim_flip."
        else:
            message = f"Action mirrored on {self.axis}-axis using {ssp.mirror_space.lower()} space!"
        if self.rotate_180:
            message += f" Hip rotated 180° on {self.axis}-axis."
        self.report({"INFO"}, message)
        return {'FINISHED'}


class SUB_OT_mirror_all_actions(Operator):
    """Mirror every loaded animation on the active armature"""
    bl_description = 'Mirror every loaded animation on the active armature'
    bl_idname = "sub.mirror_all_actions"
    bl_label = "Mirror All Loaded Animations"
    bl_options = {"REGISTER", "UNDO"}

    axis : EnumProperty(
        name="Axis",
        description="Select mirror axis",
        default='Y',
        items = (
            ('X', 'X', "X axis"),
            ('Y', 'Y', "Y axis"),
            ('Z', 'Z', "Z axis"),
            ('XY', 'XY', "Both XY axes"),
            ('XZ', 'XZ', "Both XZ axes"),
            ('YZ', 'YZ', "Both YZ axes"),
            ('XYZ', 'XYZ', "All XYZ axes"),
        )
    )

    rotate_180 : BoolProperty(
        name="180 Rotate",
        description="Rotate hip bone 180 degrees on selected axis after mirroring",
        default=False
    )

    selected_bones_only : BoolProperty(
        name="Selected Bones Only",
        description="Mirror only the selected bones in place on every animation",
        default=False
    )

    only_active_frame : BoolProperty(
        name="Only Active Frame",
        description="Mirror only keyframes at the current frame",
        default=False
    )

    include_fingers : BoolProperty(
        name="Include Fingers",
        description="Include finger bones in the mirroring process",
        default=False
    )

    smash_y_anim_flip : BoolProperty(
        name="Smash Y Anim Flip",
        description="Use Studio SB anim_flip for Y-axis on standard Smash rigs",
        default=False,
    )

    selected_bone_names: bpy.props.StringProperty(
        name="Selected Bone Names",
        description="Pose bones captured when the operator was invoked",
        default="",
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj and obj.type == 'ARMATURE' and context.mode in {'OBJECT', 'POSE'}

    def invoke(self, context, event):
        if context.object and context.object.type == 'ARMATURE':
            self.selected_bone_names = SUB_OT_mirror_action._format_bone_names(
                _selected_pose_bone_names(context)
            )
        else:
            self.selected_bone_names = ""
        ssp = getattr(context.scene, "sub_scene_properties", None)
        if ssp is not None:
            self.smash_y_anim_flip = ssp.mirror_smash_y_anim_flip
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        actions = get_armature_actions(context.object) if context.object else []
        layout.label(text=f"Will process {len(actions)} animation(s)", icon='RENDER_ANIMATION')
        layout.prop(self, "axis")
        layout.prop(self, "rotate_180")
        layout.prop(self, "selected_bones_only")
        layout.prop(self, "only_active_frame")
        layout.prop(self, "include_fingers")
        if self.axis in ('Y', 'XY', 'YZ', 'XYZ'):
            layout.prop(self, "smash_y_anim_flip")
            if self.selected_bones_only and self.smash_y_anim_flip:
                layout.label(text="Selected bones always use fcurve Y mirror", icon='INFO')
        if self.selected_bones_only:
            selected = SUB_OT_mirror_action._parse_bone_names(self.selected_bone_names)
            if selected:
                layout.label(text=f"Bones: {', '.join(sorted(selected))}", icon='BONE_DATA')
            else:
                layout.label(text="Select pose bones first", icon='ERROR')

    def execute(self, context):
        armature = context.object
        ssp = context.scene.sub_scene_properties
        actions = get_armature_actions(armature)
        if not actions:
            self.report({"ERROR"}, "No animations found for this armature")
            return {'CANCELLED'}

        captured_bones = SUB_OT_mirror_action._parse_bone_names(self.selected_bone_names)
        if self.selected_bones_only:
            selected_bones = captured_bones or _selected_pose_bone_names(context)
            if not selected_bones:
                self.report({"WARNING"}, "No bones selected. Select bones in Pose mode first.")
                return {'CANCELLED'}
            captured_bones = selected_bones

        ssp.mirror_smash_y_anim_flip = self.smash_y_anim_flip

        if not armature.animation_data:
            armature.animation_data_create()

        original_action = armature.animation_data.action
        original_frame = context.scene.frame_current
        context.view_layer.objects.active = armature
        armature.select_set(True)

        total_actions = len(actions)
        context.window_manager.progress_begin(0, total_actions)
        context.window.cursor_modal_set("WAIT")

        processed = 0
        skipped = 0
        try:
            for action_index, action in enumerate(actions):
                context.window_manager.progress_update(action_index / total_actions)
                assign_action(armature.animation_data, action)
                success, _used_smash_y = apply_mirror_to_action(
                    context,
                    armature,
                    action,
                    self.axis,
                    rotate_180=self.rotate_180,
                    selected_bones_only=self.selected_bones_only,
                    only_active_frame=self.only_active_frame,
                    mirror_space=ssp.mirror_space,
                    include_fingers=self.include_fingers,
                    smash_y_anim_flip=self.smash_y_anim_flip,
                    selected_bone_names=captured_bones if self.selected_bones_only else None,
                )
                if success:
                    processed += 1
                else:
                    skipped += 1
        finally:
            context.window_manager.progress_end()
            context.window.cursor_modal_restore()
            if original_action:
                assign_action(armature.animation_data, original_action)
            context.scene.frame_set(original_frame)

        if processed == 0:
            self.report({"ERROR"}, "No animations were mirrored")
            return {'CANCELLED'}

        message = f"Mirrored {processed}/{total_actions} animation(s) on {self.axis}-axis"
        if skipped:
            message += f" ({skipped} skipped, no keyframes)"
        self.report({"INFO"}, message)
        return {'FINISHED'}


class SUB_UL_mirror_custom_bones(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "include", text="")
        row.label(text=item.name, translate=False)


class SUB_OT_find_custom_mirror_bones(Operator):
    """List bones that are not part of a normal Smash Ultimate armature"""
    bl_description = 'List bones that are not part of a normal Smash Ultimate armature'
    bl_idname = "sub.find_custom_mirror_bones"
    bl_label = "Find Custom Bones"
    bl_options = {'REGISTER'}

    def execute(self, context):
        armature = context.active_object
        if not armature or armature.type != 'ARMATURE':
            self.report({'WARNING'}, "Select an armature first")
            return {'CANCELLED'}

        ssp = context.scene.sub_scene_properties
        previous = {item.name: item.include for item in ssp.mirror_custom_bones}
        custom_names = find_custom_mirror_bones(armature)
        ssp.mirror_custom_bones.clear()
        for name in custom_names:
            item = ssp.mirror_custom_bones.add()
            item.name = name
            item.include = previous.get(name, False)
        ssp.mirror_custom_bones_index = 0
        ssp.mirror_custom_armature_name = armature.name
        if custom_names:
            self.report({'INFO'}, f"Found {len(custom_names)} custom bone(s). Check the ones to mirror.")
        else:
            self.report({'INFO'}, "No custom bones found on this armature")
        return {'FINISHED'}


class SUB_OT_mirror_custom_bones_set_all(Operator):
    """Check or uncheck every custom bone in the list"""
    bl_description = 'Check or uncheck every custom bone in the list'
    bl_idname = "sub.mirror_custom_bones_set_all"
    bl_label = "Set All Custom Bones"
    bl_options = {'REGISTER'}

    include: BoolProperty(default=True)

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        if not ssp.mirror_custom_bones:
            self.report({'WARNING'}, "Find custom bones first")
            return {'CANCELLED'}
        for item in ssp.mirror_custom_bones:
            item.include = self.include
        return {'FINISHED'}


#########################################################################################
# REGISTER/UNREGISTER
#########################################################################################


classes = (
    SUB_OT_mirror_action,
    SUB_OT_mirror_all_actions,
    SUB_UL_mirror_custom_bones,
    SUB_OT_find_custom_mirror_bones,
    SUB_OT_mirror_custom_bones_set_all,
)

def register():
    logger.info("Registering mirror_animation.py classes")
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
            logger.info(f"Successfully registered {cls.__name__}")
        except Exception as e:
            logger.error(f"Failed to register {cls.__name__}: {str(e)}")

def unregister():
    logger.info("Unregistering mirror_animation.py classes")
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
            logger.info(f"Successfully unregistered {cls.__name__}")
        except Exception as e:
            logger.error(f"Failed to unregister {cls.__name__}: {str(e)}")


if __name__ == "__main__":
    register() 