import bpy

from ..anim.fcurve_compat import get_all_action_fcurves, remove_fcurve
from ..blender_compat import set_pose_bone_select


def get_ik_bone_names(armature_data):
    """IK control bones only (FootIK / HandIK / KneeIK / ArmIK), not every name with 'IK'."""
    from .create_animation_rig import _IK_BONE, canonical_bone_name

    names = []
    for bone in armature_data.bones:
        if _IK_BONE.match(canonical_bone_name(bone.name)):
            names.append(bone.name)
    return names


def present_ik_limbs(armature_object):
    """Return 'ARMS', 'LEGS', 'BOTH', or None based on IK bones that exist."""
    from .create_animation_rig import armature_has_ik

    has_arms = armature_has_ik(armature_object, "ARMS")
    has_legs = armature_has_ik(armature_object, "LEGS")
    if has_arms and has_legs:
        return "BOTH"
    if has_arms:
        return "ARMS"
    if has_legs:
        return "LEGS"
    return None


def _ik_constraint_chain_bone_names(armature_object, limbs="BOTH"):
    """FK bones actually driven by limb IK / Copy Rotation constraints.

    Arm IK uses chain_count=2 on Arm, so Shoulder must be included or the bake
    leaves the shoulder on old FK while the arm is baked — visible arm drift.
    """
    from .create_animation_rig import _iter_limb_ik_constraints

    names = set()
    for pose_bone, constraint in _iter_limb_ik_constraints(armature_object, limbs=limbs):
        if constraint.type == "IK":
            count = int(getattr(constraint, "chain_count", 0) or 0)
            if count <= 0:
                count = 32
            bone = pose_bone
            for _ in range(count):
                if bone is None:
                    break
                names.add(bone.name)
                bone = bone.parent
        elif constraint.type == "COPY_ROTATION":
            names.add(pose_bone.name)
    return names


def collect_fk_bone_names(armature_object, leg_bone_map=None, limbs=None):
    """FK bones to bake/clear — only for limbs that actually have IK on the rig."""
    if armature_object.data.get("sub_independent_ik"):
        from .ik_channels import chains
        return list(dict.fromkeys(n for _, names, _, _ in chains(armature_object, limbs or present_ik_limbs(armature_object) or 'BOTH') for n in names))
    from .create_animation_rig import _ik_driven_fk_bone_names, _ik_limb_kind

    if limbs is None:
        limbs = present_ik_limbs(armature_object)
    if not limbs:
        return []

    names = set(_ik_constraint_chain_bone_names(armature_object, limbs=limbs))

    if leg_bone_map and limbs in {"LEGS", "BOTH"}:
        for side_roles in leg_bone_map.values():
            names.update(value for value in side_roles.values() if value)
    else:
        parts = []
        if limbs in {"LEGS", "BOTH"}:
            parts.extend(("Leg", "Knee", "Foot"))
        if limbs in {"ARMS", "BOTH"}:
            # Shoulder is part of the Arm IK chain
            parts.extend(("Shoulder", "Arm", "Hand"))
        for side in ("L", "R"):
            for part in parts:
                names.add(f"{part}{side}")

    # Include extra-arm / custom bones that actually carry IK constraints
    for pose_bone in armature_object.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.type not in {"IK", "COPY_ROTATION"}:
                continue
            subtarget = constraint.subtarget or ""
            if "IK" not in subtarget:
                continue
            kind = _ik_limb_kind(subtarget)
            if kind is None:
                continue
            if limbs != "BOTH" and kind != limbs:
                continue
            names.add(pose_bone.name)

    for name in _ik_driven_fk_bone_names(armature_object, limbs=limbs):
        names.add(name)

    return [name for name in names if name in armature_object.data.bones]


def skip_ik_visual_bake(name):
    """Finger / slider / extra control bones must not be visual-baked with IK."""
    if not name:
        return False
    if name.startswith("Finger") or name.startswith("BL_"):
        return True
    return False


def _bone_chain_depth(pose_bone):
    depth = 0
    parent = pose_bone.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _key_pose_rotation(pose_bone, frame):
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=pose_bone.name)
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        pose_bone.keyframe_insert("rotation_axis_angle", frame=frame, group=pose_bone.name)
    else:
        pose_bone.keyframe_insert("rotation_euler", frame=frame, group=pose_bone.name)
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=pose_bone.name)


def _action_frame_range(armature_object, scene):
    from . import anim_layers_compat

    action, _slot = anim_layers_compat.viewport_driving_action(armature_object)
    if action is not None:
        fr = action.frame_range
        start, end = int(fr[0]), int(fr[1])
        if end >= start:
            return start, end
    return int(scene.frame_start), int(scene.frame_end)


def _prepare_frame_eval_for_bake(armature_object, limbs="BOTH"):
    """Evaluate this frame the same way the viewport does (IK/FK mutes + influence).

    Does not force every limb into pure IK — that was destroying FK arm animation
    and baking a different solve than what the user saw.
    """
    from .create_animation_rig import (
        _ensure_ik_influence_drivers,
        _ik_fk_props,
        _ik_mode_bucket,
        _ik_switch_factor,
        _iter_limb_ik_constraints,
        _neutralize_fk_pose_for_ik,
        _sync_ik_fk_fcurve_mutes,
    )

    _ensure_ik_influence_drivers(armature_object)
    for _pose_bone, constraint in _iter_limb_ik_constraints(armature_object, limbs=limbs):
        constraint.mute = False

    props = _ik_fk_props(armature_object)
    _sync_ik_fk_fcurve_mutes(armature_object)

    arms_f = _ik_switch_factor(props, "sub_use_ik_arms", 0.0)
    legs_f = _ik_switch_factor(props, "sub_use_ik_legs", 0.0)
    if limbs in {"ARMS", "BOTH"} and _ik_mode_bucket(arms_f) == "ik":
        _neutralize_fk_pose_for_ik(armature_object, limbs="ARMS")
    if limbs in {"LEGS", "BOTH"} and _ik_mode_bucket(legs_f) == "ik":
        _neutralize_fk_pose_for_ik(armature_object, limbs="LEGS")


def _visual_key_options():
    """Blender keyframe options for visual (constraint-aware) inserts."""
    try:
        return {'INSERTKEY_VISUAL'}
    except Exception:
        return set()


def _key_pose_bone_visual(pose_bone, frame):
    """Insert Loc/Rot/Scale keys from the constrained viewport pose (like manual Visual keying)."""
    opts = _visual_key_options()
    kwargs = {"frame": frame, "group": pose_bone.name}
    if opts:
        kwargs["options"] = opts
    pose_bone.keyframe_insert("location", **kwargs)
    pose_bone.keyframe_insert("scale", **kwargs)
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.keyframe_insert("rotation_quaternion", **kwargs)
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        pose_bone.keyframe_insert("rotation_axis_angle", **kwargs)
    else:
        pose_bone.keyframe_insert("rotation_euler", **kwargs)
        # Smash / export often also reads quaternion channels
        try:
            pose_bone.keyframe_insert("rotation_quaternion", **kwargs)
        except (RuntimeError, TypeError):
            pass


def _bones_for_visual_bake(armature_object, fk_bone_names):
    return [armature_object.pose.bones[n] for n in dict.fromkeys(fk_bone_names or [])
            if n in armature_object.pose.bones and not skip_ik_visual_bake(n)]


def bake_ik_driven_fk_visual(
    context,
    armature_object,
    fk_bone_names,
    frame_start,
    frame_end,
    limbs=None,
    clear_constraints=True,
):
    """Bake the live viewport pose with Visual keying (same as manual key-all).

    Constraints stay ON while INSERTKEY_VISUAL writes Loc/Rot/Scale every frame,
    then IK constraints are removed. Avoids matrix capture/replay, which drifted
    arms even when the viewport looked correct.
    """
    if armature_object.data.get("sub_independent_ik"):
        from .ik_channels import bake
        return bake(context, armature_object, fk_bone_names, frame_start, frame_end, clear_constraints)
    from . import anim_layers_compat
    from .create_animation_rig import (
        _iter_limb_ik_constraints,
        unmute_all_ik_fk_fcurves,
    )

    if limbs is None:
        limbs = present_ik_limbs(armature_object) or "BOTH"

    pose_bones = _bones_for_visual_bake(armature_object, fk_bone_names)
    if not pose_bones:
        return 0

    if context.mode != "POSE":
        bpy.ops.object.mode_set(mode="POSE")

    scene = context.scene
    start, end = int(frame_start), int(frame_end)
    original = scene.frame_current
    bone_names = [pb.name for pb in pose_bones]

    try:
        driving_before, _driving_slot = anim_layers_compat.viewport_driving_action(
            armature_object
        )
        with anim_layers_compat.bind_driving_action_for_bake(armature_object, context):
            anim_layers_compat.prepare_absolute_keys_on_layer(armature_object)

            # Select the bones we will key (matches the working manual workflow)
            wanted = set(bone_names)
            for bone in armature_object.pose.bones:
                set_pose_bone_select(bone, bone.name in wanted)

            keyed = 0
            for frame in range(start, end + 1):
                scene.frame_set(frame)
                _prepare_frame_eval_for_bake(armature_object, limbs=limbs)
                context.view_layer.update()
                for pose_bone in pose_bones:
                    # Re-fetch in case of rename / eval proxies
                    pb = armature_object.pose.bones.get(pose_bone.name)
                    if pb is None:
                        continue
                    _key_pose_bone_visual(pb, frame)
                    keyed += 1

            anim = armature_object.animation_data
            keyed_action = getattr(anim, "action", None) if anim else None
            keyed_slot = getattr(anim, "action_slot", None) if anim else None
            if keyed_action is not None and anim is not None:
                for track in getattr(anim, "nla_tracks", []) or []:
                    if not track.strips:
                        continue
                    strip = track.strips[0]
                    if strip.action in (None, driving_before, keyed_action):
                        strip.action = keyed_action
                        if keyed_slot is not None and hasattr(strip, "action_slot"):
                            try:
                                strip.action_slot = keyed_slot
                            except (AttributeError, TypeError, RuntimeError):
                                pass

            if clear_constraints:
                remove_ik_constraints_from_bones(armature_object, bone_names)
                # Also strip constraints from any FK bone that still has IK links
                for pose_bone, _constraint in list(
                    _iter_limb_ik_constraints(armature_object, limbs=limbs)
                ):
                    remove_ik_constraints_from_bones(armature_object, [pose_bone.name])
            unmute_all_ik_fk_fcurves(armature_object)
            return keyed
    finally:
        scene.frame_set(original)
        unmute_all_ik_fk_fcurves(armature_object)


def bake_action_visual(context, armature_object, frame_start, frame_end, bone_names=None, clear_constraints=False):
    """Bake listed (or default limb) bones 1:1 — never the whole rig."""
    limbs = present_ik_limbs(armature_object) or "BOTH"
    if bone_names is None:
        bone_names = collect_fk_bone_names(armature_object, limbs=limbs)
    return bake_ik_driven_fk_visual(
        context,
        armature_object,
        bone_names,
        frame_start,
        frame_end,
        limbs=limbs,
        clear_constraints=clear_constraints,
    )


def remove_ik_constraints_from_bones(armature_object, bone_names):
    """Remove only IK / Copy Rotation-to-IK constraints — leave other constraints."""
    for bone_name in bone_names:
        bone = armature_object.pose.bones.get(bone_name)
        if not bone:
            continue
        for constraint in list(bone.constraints):
            if constraint.type == "IK":
                bone.constraints.remove(constraint)
                continue
            if constraint.type == "COPY_ROTATION":
                sub = constraint.subtarget or ""
                if "IK" in sub:
                    bone.constraints.remove(constraint)


def remove_constraints_from_bones(armature_object, bone_names):
    """Back-compat: strip IK-related constraints from FK bones."""
    remove_ik_constraints_from_bones(armature_object, bone_names)


def remove_ik_fcurves_from_action(action, ik_bone_names):
    if not action or not ik_bone_names:
        return 0

    fcurves = get_all_action_fcurves(action)
    to_remove = []
    for fcurve in fcurves:
        path = fcurve.data_path or ""
        for ik_name in ik_bone_names:
            if f'pose.bones["{ik_name}"]' in path:
                to_remove.append(fcurve)
                break
        else:
            # Also strip keyed IK constraint channels on FK bones (pole_angle, etc.)
            if ".constraints[" in path and "pole_angle" in path:
                to_remove.append(fcurve)
            elif ".constraints[" in path and "influence" in path:
                # Only remove if it's an IK-driven FK bone path we baked
                pass

    for fcurve in to_remove:
        remove_fcurve(action, fcurve)
    return len(to_remove)


def delete_ik_bones_from_armature(armature_object):
    if armature_object.data.get("sub_independent_ik"):
        from .ik_channels import remove
        return remove(bpy.context, armature_object)
    ik_bone_names = get_ik_bone_names(armature_object.data)
    if not ik_bone_names:
        return []

    bpy.ops.object.mode_set(mode="EDIT")
    for bone_name in ik_bone_names:
        edit_bone = armature_object.data.edit_bones.get(bone_name)
        if edit_bone:
            armature_object.data.edit_bones.remove(edit_bone)
    bpy.ops.object.mode_set(mode="OBJECT")
    return ik_bone_names


def bake_and_clean_current_action(context, armature_object, leg_bone_map=None, remove_ik_rig=True, limbs=None):
    """Bake the viewport limb pose onto FK bones, then optionally strip the IK rig."""
    from . import anim_layers_compat
    from .create_animation_rig import (
        _iter_armature_actions,
        _remove_ik_fk_switch_keys,
        _remove_ik_influence_drivers,
        pause_ik_fk_mute_sync,
        unmute_all_ik_fk_fcurves,
    )

    limbs = limbs or present_ik_limbs(armature_object)
    if not limbs:
        return 0, []

    if armature_object.data.get('sub_independent_ik'):
        from . import ik_channels
        names = collect_fk_bone_names(armature_object, limbs=limbs)
        start, end = _action_frame_range(armature_object, context.scene)
        count = ik_channels.bake(context, armature_object, names, start, end)
        removed = ik_channels.remove(context, armature_object, limbs) if remove_ik_rig else []
        return count, removed

    ik_bone_names = get_ik_bone_names(armature_object.data)
    fk_bone_names = collect_fk_bone_names(
        armature_object, leg_bone_map=leg_bone_map, limbs=limbs
    )
    frame_start, frame_end = _action_frame_range(armature_object, context.scene)

    pause_ik_fk_mute_sync(True)
    try:
        if context.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")

        bake_ik_driven_fk_visual(
            context,
            armature_object,
            fk_bone_names,
            frame_start,
            frame_end,
            limbs=limbs,
        )

        removed_fcurves = 0
        for action in _iter_armature_actions(armature_object):
            removed_fcurves += remove_ik_fcurves_from_action(action, ik_bone_names)

        _remove_ik_fk_switch_keys(armature_object)
        _remove_ik_influence_drivers(armature_object)

        if remove_ik_rig:
            # Constraints already removed during bake; ensure leftovers are gone.
            remove_ik_constraints_from_bones(armature_object, fk_bone_names)
            with anim_layers_compat.anim_layers_paused():
                delete_ik_bones_from_armature(armature_object)

        unmute_all_ik_fk_fcurves(armature_object)
        if context.mode not in {"POSE", "OBJECT"}:
            bpy.ops.object.mode_set(mode="POSE")
        context.view_layer.update()
    finally:
        pause_ik_fk_mute_sync(False)

    return removed_fcurves, ik_bone_names


class SUB_OP_apply_ik_animation_operator(bpy.types.Operator):
    """Bake IK Animation to Original Bones and Remove IK Bones"""
    bl_description = 'Bake IK Animation to Original Bones and Remove IK Bones'
    bl_idname = "sub.apply_ik_animation"
    bl_label = "Apply IK Animation"
    bl_options = {"REGISTER", "UNDO"}

    limbs: bpy.props.EnumProperty(name="Limbs", items=(('AUTO', "All present IK", ""), ('LEGS', "Legs only", ""), ('ARMS', "Arms only", ""), ('BOTH', "Arms and legs", "")), default='AUTO')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        armature_object = context.object

        if not armature_object or armature_object.type != "ARMATURE":
            self.report({"ERROR"}, "No armature selected. Please select an armature in Object Mode.")
            return {"CANCELLED"}

        removed_fcurves, _ik_bone_names = bake_and_clean_current_action(
            context,
            armature_object,
            remove_ik_rig=True,
            limbs=None if self.limbs == 'AUTO' else self.limbs,
        )

        if removed_fcurves and not armature_object.data.get("sub_independent_ik"):
            self.report({"INFO"}, f"Removed {removed_fcurves} IK keyframe channels.")
        self.report({"INFO"}, "Animation baked to original bones and IK bones removed.")
        return {"FINISHED"}


class SUB_PT_apply_ik_animation_panel(bpy.types.Panel):
    """Creates a Panel in the 3D Viewport"""
    bl_label = "Apply IK Animation"
    bl_idname = "SUB_PT_apply_ik_animation_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "IK Bones"

    def draw(self, context):
        self.layout.use_property_decorate = False
        layout = self.layout
        layout.operator("sub.apply_ik_animation", text="Bake & Remove IK")

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


def register():
    bpy.utils.register_class(SUB_OP_apply_ik_animation_operator)
    bpy.utils.register_class(SUB_PT_apply_ik_animation_panel)


def unregister():
    bpy.utils.unregister_class(SUB_OP_apply_ik_animation_operator)
    bpy.utils.unregister_class(SUB_PT_apply_ik_animation_panel)


if __name__ == "__main__":
    register()
