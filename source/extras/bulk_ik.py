import math

import bpy
import mathutils
from mathutils import Vector

from ..blender_compat import assign_action, assign_bone_to_collection, ensure_bone_collection
from ..anim.fcurve_compat import collect_actions_for_armatures
from . import anim_layers_compat
from .apply_ik_animation import (
    bake_action_visual,
    collect_fk_bone_names,
    delete_ik_bones_from_armature,
    get_ik_bone_names,
    remove_constraints_from_bones,
    remove_ik_fcurves_from_action,
)

def _leg_bone_names(ssp):
    return {
        "L": {
            "leg": ssp.bulk_ik_leg_l,
            "knee": ssp.bulk_ik_knee_l,
            "foot": ssp.bulk_ik_foot_l,
        },
        "R": {
            "leg": ssp.bulk_ik_leg_r,
            "knee": ssp.bulk_ik_knee_r,
            "foot": ssp.bulk_ik_foot_r,
        },
    }


def _validate_leg_bones(armature_data, bone_names):
    missing = []
    for side, roles in bone_names.items():
        for role, name in roles.items():
            if not name:
                missing.append(f"{role} ({side})")
            elif name not in armature_data.bones:
                missing.append(name)
    return missing


def _leg_ik_exists(armature_data):
    for side in ("L", "R"):
        if f"FootIK{side}" not in armature_data.bones or f"KneeIK{side}" not in armature_data.bones:
            return False
    return True


def ensure_leg_ik_rig(armature_object, bone_names):
    """Create foot/knee IK bones and constraints using custom FK bone names."""
    with anim_layers_compat.anim_layers_paused():
        return _ensure_leg_ik_rig_impl(armature_object, bone_names)


def _ensure_leg_ik_rig_impl(armature_object, bone_names):
    """Create foot/knee IK bones and constraints using custom FK bone names."""
    if _leg_ik_exists(armature_object.data):
        return True

    armature = armature_object.data
    ik_scale_factor = 1.5
    created_any = False

    bpy.ops.object.mode_set(mode="EDIT")

    for side in ("L", "R"):
        names = bone_names[side]
        leg_bone = armature.edit_bones.get(names["leg"])
        knee_bone = armature.edit_bones.get(names["knee"])
        foot_bone = armature.edit_bones.get(names["foot"])

        if not all([leg_bone, knee_bone, foot_bone]):
            continue

        fk_knee_pos = knee_bone.head
        char_forward_local = Vector((0.0, -1.0, 0.0))
        thigh_vec = leg_bone.tail - leg_bone.head
        thigh_dir = thigh_vec.normalized() if thigh_vec.length > 0.001 else Vector((0, 0, 1))

        pole_dir_initial = char_forward_local - char_forward_local.project(thigh_dir)
        if pole_dir_initial.length < 0.01:
            char_up_local = Vector((0.0, 0.0, 1.0))
            pole_dir_initial = char_up_local - char_up_local.project(thigh_dir)
            if pole_dir_initial.length < 0.01:
                char_right_local = Vector((1.0, 0.0, 0.0))
                pole_dir_initial = char_right_local - char_right_local.project(thigh_dir)

        if pole_dir_initial.length > 0.001:
            pole_dir_initial.normalize()
        else:
            pole_dir_initial = Vector((0.0, -1.0, 0.0))

        pole_distance_factor = 0.75
        actual_pole_distance = leg_bone.length * pole_distance_factor
        if actual_pole_distance < 0.1:
            actual_pole_distance = 0.5

        knee_ik_bone = armature.edit_bones.new(f"KneeIK{side}")
        knee_ik_bone.head = fk_knee_pos + pole_dir_initial * actual_pole_distance
        pole_bone_length = max(leg_bone.length * 0.2, 0.2) * ik_scale_factor
        knee_ik_bone.tail = knee_ik_bone.head + pole_dir_initial * pole_bone_length
        if pole_dir_initial.length > 0.001:
            knee_ik_bone.align_roll(pole_dir_initial)

        foot_ik_bone = armature.edit_bones.new(f"FootIK{side}")
        foot_ik_bone.head = knee_bone.tail
        foot_ik_length = foot_bone.length if foot_bone.length > 0.01 else leg_bone.length * 0.3
        foot_ik_length *= ik_scale_factor
        if foot_ik_length < 0.1:
            foot_ik_length = 0.3
        foot_fk_dir = (
            (foot_bone.tail - foot_bone.head).normalized()
            if foot_bone.length > 0.001
            else Vector((0, 0, -1))
        )
        foot_ik_bone.tail = foot_ik_bone.head + foot_fk_dir * foot_ik_length
        foot_ik_bone.roll = math.radians(90.0)
        created_any = True

    bpy.ops.object.mode_set(mode="POSE")

    if not created_any:
        return False

    for side in ("L", "R"):
        names = bone_names[side]
        knee_pose = armature_object.pose.bones.get(names["knee"])
        foot_pose = armature_object.pose.bones.get(names["foot"])
        foot_ik_target = armature_object.pose.bones.get(f"FootIK{side}")
        knee_pole_target = armature_object.pose.bones.get(f"KneeIK{side}")

        if not all([knee_pose, foot_pose, foot_ik_target, knee_pole_target]):
            continue

        if not any(c.type == "IK" for c in knee_pose.constraints):
            knee_ik_constraint = knee_pose.constraints.new("IK")
            knee_ik_constraint.name = "IK_Constraint"
            knee_ik_constraint.target = armature_object
            knee_ik_constraint.subtarget = foot_ik_target.name
            knee_ik_constraint.pole_target = armature_object
            knee_ik_constraint.pole_subtarget = knee_pole_target.name
            knee_ik_constraint.chain_count = 2
            knee_ik_constraint.pole_angle = 0.0

        if not any(c.type == "COPY_ROTATION" for c in foot_pose.constraints):
            foot_rot_constraint = foot_pose.constraints.new("COPY_ROTATION")
            foot_rot_constraint.target = armature_object
            foot_rot_constraint.subtarget = foot_ik_target.name

        foot_ik_bone = armature_object.pose.bones.get(f"FootIK{side}")
        foot_fk = armature_object.pose.bones.get(names["foot"])
        if foot_ik_bone and foot_fk:
            foot_ik_bone.matrix = foot_fk.matrix.copy()

    for bone in armature.bones:
        if "IK" in bone.name:
            bone.color.palette = "THEME01"

    ik_bone_collection = ensure_bone_collection(armature, "FootIK Bones")
    for bone in armature.bones:
        if "IK" in bone.name:
            assign_bone_to_collection(ik_bone_collection, bone)

    return True


def get_armature_actions(armature_object):
    """Return pose actions for this armature, excluding SAP/_old backups."""
    return collect_actions_for_armatures([armature_object])


def _draw_bulk_ik_bone_pickers(layout, context, ssp):
    box = layout.box()
    box.label(text="Left Leg Bones:")
    row = box.row(align=True)
    row.prop_search(ssp, "bulk_ik_leg_l", context.object.data, "bones", text="Leg")
    op = row.operator(SUB_OP_bulk_ik_pick_bone.bl_idname, text="", icon="EYEDROPPER")
    op.target_property = "bulk_ik_leg_l"
    row = box.row(align=True)
    row.prop_search(ssp, "bulk_ik_knee_l", context.object.data, "bones", text="Knee")
    op = row.operator(SUB_OP_bulk_ik_pick_bone.bl_idname, text="", icon="EYEDROPPER")
    op.target_property = "bulk_ik_knee_l"
    row = box.row(align=True)
    row.prop_search(ssp, "bulk_ik_foot_l", context.object.data, "bones", text="Foot")
    op = row.operator(SUB_OP_bulk_ik_pick_bone.bl_idname, text="", icon="EYEDROPPER")
    op.target_property = "bulk_ik_foot_l"

    box = layout.box()
    box.label(text="Right Leg Bones:")
    row = box.row(align=True)
    row.prop_search(ssp, "bulk_ik_leg_r", context.object.data, "bones", text="Leg")
    op = row.operator(SUB_OP_bulk_ik_pick_bone.bl_idname, text="", icon="EYEDROPPER")
    op.target_property = "bulk_ik_leg_r"
    row = box.row(align=True)
    row.prop_search(ssp, "bulk_ik_knee_r", context.object.data, "bones", text="Knee")
    op = row.operator(SUB_OP_bulk_ik_pick_bone.bl_idname, text="", icon="EYEDROPPER")
    op.target_property = "bulk_ik_knee_r"
    row = box.row(align=True)
    row.prop_search(ssp, "bulk_ik_foot_r", context.object.data, "bones", text="Foot")
    op = row.operator(SUB_OP_bulk_ik_pick_bone.bl_idname, text="", icon="EYEDROPPER")
    op.target_property = "bulk_ik_foot_r"


def _update_progress_cursor(context, progress):
    wm = context.window_manager
    wm.progress_update(progress)
    if progress < 0.25:
        context.window.cursor_modal_set("WAIT")
    elif progress < 0.5:
        context.window.cursor_modal_set("CROSSHAIR")
    elif progress < 0.75:
        context.window.cursor_modal_set("MOVE_X")
    else:
        context.window.cursor_modal_set("MOVE_Y")


class SUB_OP_bulk_ik_pick_bone(bpy.types.Operator):
    bl_idname = "sub.bulk_ik_pick_bone"
    bl_label = "Pick Bone"
    bl_description = "Assign the selected pose/edit bone to this Bulk IK slot"
    bl_options = {"REGISTER", "UNDO"}

    target_property: bpy.props.StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        ssp = context.scene.sub_scene_properties
        active_obj = context.active_object
        if not active_obj or active_obj.type != "ARMATURE":
            self.report({"ERROR"}, "Active object must be an armature")
            return {"CANCELLED"}

        selected_bone_name = None
        if context.mode == "POSE":
            if context.active_pose_bone:
                selected_bone_name = context.active_pose_bone.name
            else:
                selected = [bone for bone in active_obj.pose.bones if bone.bone.select]
                if selected:
                    selected_bone_name = selected[0].name
        elif context.mode == "EDIT_ARMATURE":
            if context.active_bone:
                selected_bone_name = context.active_bone.name
            else:
                selected = [bone for bone in active_obj.data.edit_bones if bone.select]
                if selected:
                    selected_bone_name = selected[0].name

        if not selected_bone_name:
            self.report({"ERROR"}, "No bone selected in pose or edit mode")
            return {"CANCELLED"}

        if not hasattr(ssp, self.target_property):
            return {"CANCELLED"}

        setattr(ssp, self.target_property, selected_bone_name)
        self.report({"INFO"}, f"Assigned '{selected_bone_name}'")
        return {"FINISHED"}


class SUB_OP_bulk_ik_match_all(bpy.types.Operator):
    """Create leg IK and match FK to IK for every loaded animation on the armature"""
    bl_description = 'Create leg IK and match FK to IK for every loaded animation on the armature'
    bl_idname = "sub.bulk_ik_match_all"
    bl_label = "Run Bulk IK"
    bl_options = {"REGISTER", "UNDO"}

    remove_knee_frames: bpy.props.BoolProperty(
        name="Delete Knee/Leg FK Keyframes",
        description="Permanently delete Knee/Leg keys after matching. Leave off so the original FK animation is kept (muted while IK is on)",
        default=False,
    )

    reference_frame: bpy.props.EnumProperty(
        name="Reference Frame",
        items=(
            ("FIRST", "Keep First Frame", "Keep only the first frame's keys"),
            ("LAST", "Keep Last Frame", "Keep only the last frame's keys"),
        ),
        default="LAST",
    )

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj and obj.type == "ARMATURE" and context.mode in {"OBJECT", "POSE"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        ssp = context.scene.sub_scene_properties
        actions = get_armature_actions(context.object) if context.object else []
        layout.label(text=f"Will process {len(actions)} animation(s)")
        layout.prop(self, "remove_knee_frames")
        if self.remove_knee_frames:
            layout.prop(self, "reference_frame")
        _draw_bulk_ik_bone_pickers(layout, context, ssp)

    def execute(self, context):
        armature_object = context.object
        ssp = context.scene.sub_scene_properties
        bone_names = _leg_bone_names(ssp)

        missing = _validate_leg_bones(armature_object.data, bone_names)
        if missing:
            self.report({"ERROR"}, f"Missing bones: {', '.join(missing)}")
            return {"CANCELLED"}

        actions = get_armature_actions(armature_object)
        if not actions:
            self.report({"ERROR"}, "No animations found for this armature")
            return {"CANCELLED"}

        original_action = None
        if armature_object.animation_data:
            original_action = armature_object.animation_data.action
        original_frame = context.scene.frame_current
        original_frame_start = context.scene.frame_start
        original_frame_end = context.scene.frame_end

        if not armature_object.animation_data:
            armature_object.animation_data_create()

        context.view_layer.objects.active = armature_object
        armature_object.select_set(True)

        if not ensure_leg_ik_rig(armature_object, bone_names):
            self.report({"ERROR"}, "Failed to create leg IK bones")
            return {"CANCELLED"}

        if context.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")

        total_actions = len(actions)
        context.window_manager.progress_begin(0, total_actions)
        context.window.cursor_modal_set("WAIT")

        processed = 0
        try:
            for action_index, action in enumerate(actions):
                progress = action_index / total_actions
                _update_progress_cursor(context, progress)

                assign_action(armature_object.animation_data, action)
                fr_start = int(action.frame_range[0])
                fr_end = int(action.frame_range[1])
                context.scene.frame_start = fr_start
                context.scene.frame_end = fr_end

                result = bpy.ops.sub.fk_to_ik_transfer(
                    "EXEC_DEFAULT",
                    entire_animation=True,
                    auto_keyframe=True,
                    cleanup_mode="LEGS",
                    remove_knee_frames=self.remove_knee_frames,
                    remove_arm_frames=False,
                    reference_frame=self.reference_frame,
                    reset_foot_bones=False,
                    show_progress=False,
                    custom_leg_bone_l=ssp.bulk_ik_leg_l,
                    custom_knee_bone_l=ssp.bulk_ik_knee_l,
                    custom_foot_bone_l=ssp.bulk_ik_foot_l,
                    custom_leg_bone_r=ssp.bulk_ik_leg_r,
                    custom_knee_bone_r=ssp.bulk_ik_knee_r,
                    custom_foot_bone_r=ssp.bulk_ik_foot_r,
                )
                if "FINISHED" not in result:
                    self.report({"WARNING"}, f"Skipped or failed on action: {action.name}")
                    continue
                processed += 1

            _update_progress_cursor(context, 1.0)
            self.report({"INFO"}, f"Bulk IK complete: {processed}/{total_actions} animations")
            return {"FINISHED"}

        except Exception as exc:
            self.report({"ERROR"}, f"Bulk IK failed: {exc}")
            return {"CANCELLED"}

        finally:
            context.window_manager.progress_end()
            context.window.cursor_modal_restore()
            context.scene.frame_set(original_frame)
            context.scene.frame_start = original_frame_start
            context.scene.frame_end = original_frame_end
            if original_action and armature_object.animation_data:
                assign_action(armature_object.animation_data, original_action)


class SUB_OP_bulk_ik_bake_all(bpy.types.Operator):
    """Bake IK to FK for every loaded animation, then remove the IK rig"""
    bl_description = 'Bake IK to FK for every loaded animation, then remove the IK rig'
    bl_idname = "sub.bulk_ik_bake_all"
    bl_label = "Bulk Bake & Remove IK"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj and obj.type == "ARMATURE" and context.mode in {"OBJECT", "POSE"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        ssp = context.scene.sub_scene_properties
        actions = get_armature_actions(context.object) if context.object else []
        layout.label(text=f"Will bake {len(actions)} animation(s) to FK bones")
        layout.label(text="IK bones and constraints are removed after all bakes finish")
        _draw_bulk_ik_bone_pickers(layout, context, ssp)

    def execute(self, context):
        armature_object = context.object
        ssp = context.scene.sub_scene_properties
        bone_names = _leg_bone_names(ssp)

        actions = get_armature_actions(armature_object)
        if not actions:
            self.report({"ERROR"}, "No animations found for this armature")
            return {"CANCELLED"}

        if not get_ik_bone_names(armature_object.data):
            self.report({"ERROR"}, "No IK bones found. Run Bulk IK or create IK bones first.")
            return {"CANCELLED"}

        original_action = None
        if armature_object.animation_data:
            original_action = armature_object.animation_data.action
        original_frame = context.scene.frame_current
        original_frame_start = context.scene.frame_start
        original_frame_end = context.scene.frame_end

        if not armature_object.animation_data:
            armature_object.animation_data_create()

        context.view_layer.objects.active = armature_object
        armature_object.select_set(True)

        if context.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")

        ik_bone_names = get_ik_bone_names(armature_object.data)
        fk_bone_names = collect_fk_bone_names(armature_object, bone_names)
        total_actions = len(actions)
        context.window_manager.progress_begin(0, total_actions)
        context.window.cursor_modal_set("WAIT")

        processed = 0
        removed_fcurves_total = 0
        try:
            for action_index, action in enumerate(actions):
                progress = action_index / total_actions
                _update_progress_cursor(context, progress)

                assign_action(armature_object.animation_data, action)
                fr_start = int(action.frame_range[0])
                fr_end = int(action.frame_range[1])
                context.scene.frame_start = fr_start
                context.scene.frame_end = fr_end

                bake_action_visual(context, armature_object, fr_start, fr_end)
                removed_fcurves_total += remove_ik_fcurves_from_action(action, ik_bone_names)
                processed += 1

            remove_constraints_from_bones(armature_object, fk_bone_names)
            delete_ik_bones_from_armature(armature_object)

            _update_progress_cursor(context, 1.0)
            self.report(
                {"INFO"},
                f"Bulk bake complete: {processed}/{total_actions} animations, "
                f"removed {removed_fcurves_total} IK channels",
            )
            return {"FINISHED"}

        except Exception as exc:
            self.report({"ERROR"}, f"Bulk bake failed: {exc}")
            return {"CANCELLED"}

        finally:
            context.window_manager.progress_end()
            context.window.cursor_modal_restore()
            context.scene.frame_set(original_frame)
            context.scene.frame_start = original_frame_start
            context.scene.frame_end = original_frame_end
            if original_action and armature_object.animation_data:
                assign_action(armature_object.animation_data, original_action)


def register():
    bpy.utils.register_class(SUB_OP_bulk_ik_pick_bone)
    bpy.utils.register_class(SUB_OP_bulk_ik_match_all)
    bpy.utils.register_class(SUB_OP_bulk_ik_bake_all)


def unregister():
    bpy.utils.unregister_class(SUB_OP_bulk_ik_bake_all)
    bpy.utils.unregister_class(SUB_OP_bulk_ik_match_all)
    bpy.utils.unregister_class(SUB_OP_bulk_ik_pick_bone)
