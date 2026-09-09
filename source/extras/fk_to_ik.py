import bpy
import math
import re
from mathutils import Vector

from ..anim.fcurve_compat import find_fcurve, get_fcurves, new_fcurve, remove_fcurve
from ..blender_compat import assign_action
from . import anim_layers_compat

_DUP_SUFFIX = re.compile(r'(?:\.\d{3})+$')
_ARM_FK = re.compile(r'^Arm([LR])(\d*)$')
_LEG_FK = re.compile(r'^Leg([LR])(\d*)$')


def _canonical_bone_name(name):
    return _DUP_SUFFIX.sub('', name) if name else name


def _bone_dup_suffix(name):
    match = _DUP_SUFFIX.search(name or '')
    return match.group(0) if match else ''


def iter_arm_fk_chains(armature_object):
    """Primary ArmL/ArmR plus extra Smash arms (ArmL2) and Blender .001 copies."""
    seen = set()
    for pose_bone in armature_object.pose.bones:
        match = _ARM_FK.match(_canonical_bone_name(pose_bone.name))
        if not match:
            continue
        side, digits = match.group(1), match.group(2) or ''
        suffix = _bone_dup_suffix(pose_bone.name)
        key = (side, digits, suffix)
        if key in seen:
            continue
        arm = armature_object.pose.bones.get(f'Arm{side}{digits}{suffix}')
        hand = armature_object.pose.bones.get(f'Hand{side}{digits}{suffix}')
        if arm is None or hand is None:
            continue
        seen.add(key)
        yield {
            'side': side,
            'digits': digits,
            'suffix': suffix,
            'id': f'{side}{digits}{suffix}',
            'shoulder': armature_object.pose.bones.get(f'Shoulder{side}{digits}{suffix}'),
            'arm': arm,
            'hand': hand,
            'hand_ik': armature_object.pose.bones.get(f'HandIK{side}{digits}{suffix}'),
            'arm_ik': armature_object.pose.bones.get(f'ArmIK{side}{digits}{suffix}'),
        }


def iter_leg_fk_chains(armature_object, custom=None):
    """Primary LegL/LegR plus extra Smash legs and Blender .001 copies."""
    seen = set()
    if custom:
        for side, names in custom.items():
            leg_name, knee_name, foot_name = names
            leg = armature_object.pose.bones.get(leg_name)
            knee = armature_object.pose.bones.get(knee_name)
            foot = armature_object.pose.bones.get(foot_name)
            suffix = _bone_dup_suffix(leg.name) if leg is not None else ''
            digits = ''
            if leg is not None:
                match = _LEG_FK.match(_canonical_bone_name(leg.name))
                if match:
                    digits = match.group(2) or ''
            foot_ik = (
                armature_object.pose.bones.get(f'FootIK{side}{digits}{suffix}')
                or armature_object.pose.bones.get(f'FootIK{side}')
            )
            knee_ik = (
                armature_object.pose.bones.get(f'KneeIK{side}{digits}{suffix}')
                or armature_object.pose.bones.get(f'KneeIK{side}')
            )
            if not all([leg, knee, foot, foot_ik, knee_ik]):
                continue
            key = (foot_ik.name, knee_ik.name)
            if key in seen:
                continue
            seen.add(key)
            yield {
                'side': side,
                'digits': digits,
                'suffix': suffix,
                'id': f'{side}{digits}{suffix}',
                'leg': leg,
                'knee': knee,
                'foot': foot,
                'foot_ik': foot_ik,
                'knee_ik': knee_ik,
            }

    for pose_bone in armature_object.pose.bones:
        match = _LEG_FK.match(_canonical_bone_name(pose_bone.name))
        if not match:
            continue
        side, digits = match.group(1), match.group(2) or ''
        suffix = _bone_dup_suffix(pose_bone.name)
        leg = armature_object.pose.bones.get(f'Leg{side}{digits}{suffix}')
        knee = armature_object.pose.bones.get(f'Knee{side}{digits}{suffix}')
        foot = armature_object.pose.bones.get(f'Foot{side}{digits}{suffix}')
        foot_ik = armature_object.pose.bones.get(f'FootIK{side}{digits}{suffix}')
        knee_ik = armature_object.pose.bones.get(f'KneeIK{side}{digits}{suffix}')
        if not all([leg, knee, foot, foot_ik, knee_ik]):
            continue
        key = (foot_ik.name, knee_ik.name)
        if key in seen:
            continue
        seen.add(key)
        yield {
            'side': side,
            'digits': digits,
            'suffix': suffix,
            'id': f'{side}{digits}{suffix}',
            'leg': leg,
            'knee': knee,
            'foot': foot,
            'foot_ik': foot_ik,
            'knee_ik': knee_ik,
        }


def _pole_follow_distance(mid_bone, pole_bone):
    rest = (pole_bone.bone.head_local - mid_bone.bone.head_local).length
    if rest > 0.05:
        return rest
    length = getattr(mid_bone, "length", 0.0) or 0.0
    return max(length * 3.0, 0.5)


def _set_pose_location(pose_bone, location):
    matrix = pose_bone.matrix.copy()
    matrix.translation = location
    pose_bone.matrix = matrix


def _key_pose_bone(pose_bone, frame):
    pose_bone.keyframe_insert("location", frame=frame, group=pose_bone.name)
    pose_bone.keyframe_insert("scale", frame=frame, group=pose_bone.name)
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=pose_bone.name)
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        pose_bone.keyframe_insert("rotation_axis_angle", frame=frame, group=pose_bone.name)
    else:
        pose_bone.keyframe_insert("rotation_euler", frame=frame, group=pose_bone.name)


# Function to invoke the position matching dialog that can be imported by other scripts
def invoke_position_match_dialog(cleanup_mode='LEGS'):
    bpy.ops.sub.fk_to_ik_transfer('INVOKE_DEFAULT', cleanup_mode=cleanup_mode)


def run_fk_to_ik_match_for_raw_import(context, cleanup_mode='LEGS'):
    """Match IK controls to FK on the current frame only (no dialogs)."""
    if context.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
    return bpy.ops.sub.fk_to_ik_transfer(
        'EXEC_DEFAULT',
        cleanup_mode=cleanup_mode,
        entire_animation=False,
    )

class SUB_OP_fk_to_ik_transfer(bpy.types.Operator):
    """Perfectly positions IK controls to match the FK bone positions"""
    bl_idname = "sub.fk_to_ik_transfer"
    bl_label = "Position IK Controls"
    bl_options = {'REGISTER', 'UNDO'}
    
    clean_animation: bpy.props.BoolProperty(
        name="Clean animation", default=False,
        description="Remove redundant FK and IK keys for the selected limbs (0.0001 channel tolerance)",
    )

    entire_animation: bpy.props.BoolProperty(
        name="Entire Animation",
        description="Apply to the entire animation instead of just the current frame",
        default=True
    )
    
    auto_keyframe: bpy.props.BoolProperty(
        name="Auto Keyframe",
        description="Automatically insert keyframes when applying to the entire animation",
        default=True
    )

    cleanup_mode: bpy.props.EnumProperty(
        name="Limbs",
        description="Which limbs to match and switch to IK (arms and legs stay independent)",
        items=(
            ('LEGS', "Legs", "Match and enable foot/knee IK only — arms stay on FK"),
            ('ARMS', "Arms", "Match and enable hand/elbow IK only — legs stay on FK"),
            ('BOTH', "Arms and Legs", "Match and enable IK on arms and legs"),
        ),
        default='LEGS',
        options={'SKIP_SAVE'},
    )

    remove_knee_frames: bpy.props.BoolProperty(
        name="Delete Knee/Leg FK Keys",
        description="Permanently delete Knee/Leg keys after matching. Leave off so IK/FK switch can restore the original animation",
        default=False
    )

    remove_arm_frames: bpy.props.BoolProperty(
        name="Delete Arm FK Keys",
        description="Permanently delete Arm keys after matching. Leave off so IK/FK switch can restore the original animation",
        default=False
    )

    # When removing leftover FK frames, allow the user to choose the reference frame
    reference_frame: bpy.props.EnumProperty(
        name="Reference Frame",
        description="Which frame to keep as the reference when cleaning leftover FK keyframes",
        items=(
            ('FIRST', "Keep First Frame", "Keep only the first frame's keys"),
            ('LAST', "Keep Last Frame", "Keep only the last frame's keys"),
        ),
        default='LAST'
    )

    reset_foot_bones: bpy.props.BoolProperty(
        name="Reset Foot FK Bones",
        description="Reset transforms and remove keyframes from Foot FK bones after IK transfer",
        default=False
    )

    custom_leg_bone_l: bpy.props.StringProperty(name="Leg L", default="LegL", options={'SKIP_SAVE'})
    custom_knee_bone_l: bpy.props.StringProperty(name="Knee L", default="KneeL", options={'SKIP_SAVE'})
    custom_foot_bone_l: bpy.props.StringProperty(name="Foot L", default="FootL", options={'SKIP_SAVE'})
    custom_leg_bone_r: bpy.props.StringProperty(name="Leg R", default="LegR", options={'SKIP_SAVE'})
    custom_knee_bone_r: bpy.props.StringProperty(name="Knee R", default="KneeR", options={'SKIP_SAVE'})
    custom_foot_bone_r: bpy.props.StringProperty(name="Foot R", default="FootR", options={'SKIP_SAVE'})

    show_progress: bpy.props.BoolProperty(
        name="Show Progress",
        default=True,
        options={'SKIP_SAVE', 'HIDDEN'},
    )

    def _fk_leg_bone(self, side, part):
        return getattr(self, f"custom_{part}_bone_{side.lower()}")

    def _leg_custom_map(self):
        return {
            "L": (self.custom_leg_bone_l, self.custom_knee_bone_l, self.custom_foot_bone_l),
            "R": (self.custom_leg_bone_r, self.custom_knee_bone_r, self.custom_foot_bone_r),
        }

    def _iter_legs(self, armature_object):
        return iter_leg_fk_chains(armature_object, self._leg_custom_map())

    def _should_key(self):
        return bool(self.auto_keyframe) or not self.entire_animation

    @classmethod
    def poll(cls, context):
        from .create_animation_rig import armature_has_ik, find_target_armature
        armature = find_target_armature(context)
        return armature is not None and armature_has_ik(armature)

    def process_frame(self, context):
        armature_object = context.object
        transfer_count = 0
        limbs = self.cleanup_mode

        constraint_states = {}
        for pose_bone in self._iter_ik_chain_bones(armature_object):
            for i, constraint in enumerate(pose_bone.constraints):
                if constraint.type not in {'IK', 'COPY_ROTATION'}:
                    continue
                constraint_states[(pose_bone.name, i)] = constraint.mute
                constraint.mute = True

        context.view_layer.update()

        original_fk_world_matrices = {}
        leg_chains = list(self._iter_legs(armature_object)) if limbs in {'LEGS', 'BOTH'} else []
        for chain in leg_chains:
            original_fk_world_matrices[f"Foot{chain['id']}"] = (
                armature_object.matrix_world @ chain['foot'].matrix
            )
        arm_chains = list(iter_arm_fk_chains(armature_object)) if limbs in {'ARMS', 'BOTH'} else []
        for chain in arm_chains:
            original_fk_world_matrices[f"Hand{chain['id']}"] = (
                armature_object.matrix_world @ chain['hand'].matrix
            )

        bones_to_keyframe = []
        for chain in leg_chains:
            transfer_count += self._place_leg_ik(armature_object, chain, bones_to_keyframe)

        for chain in arm_chains:
            if chain['hand_ik'] is None or chain['arm_ik'] is None:
                continue
            transfer_count += self._place_arm_ik(armature_object, chain, bones_to_keyframe)

        context.view_layer.update()

        for (bone_name, constraint_idx) in constraint_states:
            bone = armature_object.pose.bones.get(bone_name)
            if bone and constraint_idx < len(bone.constraints):
                bone.constraints[constraint_idx].mute = False

        context.view_layer.update()
        self._apply_limb_pole_angles(armature_object)

        world_inv = armature_object.matrix_world.inverted()
        for chain in leg_chains:
            key = f"Foot{chain['id']}"
            if key not in original_fk_world_matrices:
                continue
            chain['foot_ik'].matrix = world_inv @ original_fk_world_matrices[key]
            if chain['foot_ik'] not in bones_to_keyframe:
                bones_to_keyframe.append(chain['foot_ik'])

        for chain in arm_chains:
            hand_ik_bone = chain['hand_ik']
            key = f"Hand{chain['id']}"
            if hand_ik_bone is None or key not in original_fk_world_matrices:
                continue
            hand_ik_bone.matrix = world_inv @ original_fk_world_matrices[key]
            if hand_ik_bone not in bones_to_keyframe:
                bones_to_keyframe.append(hand_ik_bone)

        if limbs in {'ARMS', 'BOTH'}:
            self._refine_arm_pole_angles(armature_object)

        if self._should_key():
            current_frame = context.scene.frame_current
            if getattr(self, "_collect_keys", False):
                self._record_bone_keys(current_frame, bones_to_keyframe)
                self._record_pole_keys(armature_object, current_frame)
            else:
                for bone in bones_to_keyframe:
                    _key_pose_bone(bone, current_frame)
                self._key_limb_pole_angles(armature_object, current_frame)

        context.view_layer.update()
        return transfer_count

    def _place_leg_ik(self, armature_object, chain, bones_to_keyframe):
        """Place FootIK on the FK foot and KneeIK along the knee bend."""
        leg_bone = chain['leg']
        knee_bone = chain['knee']
        foot_bone = chain['foot']
        foot_ik_bone = chain['foot_ik']
        knee_ik_bone = chain['knee_ik']
        leg_pos = leg_bone.matrix.to_translation()
        knee_pos = knee_bone.matrix.to_translation()
        foot_pos = foot_bone.matrix.to_translation()
        foot_ik_bone.matrix = foot_bone.matrix.copy()

        chain_vec = foot_pos - leg_pos
        if chain_vec.length < 1e-6:
            chain_dir = Vector((0.0, 0.0, -1.0))
        else:
            chain_dir = chain_vec.normalized()
        knee_proj = leg_pos + chain_dir * chain_dir.dot(knee_pos - leg_pos)
        pole_dir = knee_pos - knee_proj
        if pole_dir.length < 0.001:
            pole_dir = self._fallback_pole_dir(armature_object, chain_dir, Vector((0.0, -1.0, 0.0)))
        else:
            pole_dir.normalize()

        _set_pose_location(
            knee_ik_bone,
            knee_pos + pole_dir * _pole_follow_distance(knee_bone, knee_ik_bone),
        )
        bones_to_keyframe.append(foot_ik_bone)
        bones_to_keyframe.append(knee_ik_bone)
        return 1

    def _fallback_pole_dir(self, armature_object, chain_dir, preferred_local):
        rot = armature_object.matrix_world.to_3x3()
        for local in (preferred_local, Vector((0.0, 0.0, 1.0)), Vector((1.0, 0.0, 0.0))):
            world = rot @ local
            pole_dir = world - world.project(chain_dir)
            if pole_dir.length > 0.01:
                return pole_dir.normalized()
        return Vector((0.0, 1.0, 0.0))

    def _place_arm_ik(self, armature_object, chain, bones_to_keyframe):
        """Place HandIK on the FK hand and ArmIK along the elbow bend."""
        shoulder_bone = chain['shoulder']
        arm_bone = chain['arm']
        hand_bone = chain['hand']
        hand_ik_bone = chain['hand_ik']
        arm_ik_bone = chain['arm_ik']
        if shoulder_bone:
            shoulder_pos = shoulder_bone.matrix.to_translation()
        else:
            shoulder_dir = arm_bone.matrix.to_translation() - hand_bone.matrix.to_translation()
            if shoulder_dir.length > 0.001:
                shoulder_dir.normalize()
            shoulder_pos = arm_bone.matrix.to_translation() + shoulder_dir * 1.0

        arm_pos = arm_bone.matrix.to_translation()
        hand_pos = hand_bone.matrix.to_translation()
        hand_ik_bone.matrix = hand_bone.matrix.copy()

        chain_vec = hand_pos - shoulder_pos
        if chain_vec.length < 1e-6:
            chain_dir = Vector((1.0, 0.0, 0.0))
        else:
            chain_dir = chain_vec.normalized()
        arm_proj = shoulder_pos + chain_dir * chain_dir.dot(arm_pos - shoulder_pos)
        pole_dir = arm_pos - arm_proj

        if pole_dir.length < 0.001:
            pole_dir = self._fallback_pole_dir(armature_object, chain_dir, Vector((0.0, 1.0, 0.0)))
        else:
            pole_dir.normalize()

        _set_pose_location(
            arm_ik_bone,
            arm_pos + pole_dir * _pole_follow_distance(arm_bone, arm_ik_bone),
        )
        bones_to_keyframe.append(hand_ik_bone)
        bones_to_keyframe.append(arm_ik_bone)
        return 1

    def _iter_ik_chain_bones(self, armature_object):
        yielded = set()
        names = []
        limbs = self.cleanup_mode
        if limbs in {'LEGS', 'BOTH'}:
            for chain in self._iter_legs(armature_object):
                names.extend((
                    chain['leg'].name, chain['knee'].name, chain['foot'].name,
                    chain['foot_ik'].name, chain['knee_ik'].name,
                ))
        if limbs in {'ARMS', 'BOTH'}:
            for chain in iter_arm_fk_chains(armature_object):
                names.extend(
                    bone.name for bone in (
                        chain['shoulder'], chain['arm'], chain['hand'],
                        chain['hand_ik'], chain['arm_ik'],
                    ) if bone is not None
                )
        for name in names:
            if name in yielded:
                continue
            bone = armature_object.pose.bones.get(name)
            if bone:
                yielded.add(name)
                yield bone

    def _find_ik_constraint(self, pose_bone):
        if pose_bone is None:
            return None
        for constraint in pose_bone.constraints:
            if constraint.type == 'IK':
                return constraint
        return None

    def _signed_angle(self, v1, v2, axis):
        if v1.length < 1e-6 or v2.length < 1e-6 or axis.length < 1e-6:
            return 0.0
        v1 = v1.normalized()
        v2 = v2.normalized()
        axis = axis.normalized()
        return math.atan2(axis.dot(v1.cross(v2)), v1.dot(v2))

    def _pole_angle_from_zero(self, knee_bone, knee_ik_bone, leg_bone, foot_bone, end_pos=None):
        """Pole angle that points the mid-joint at the pole, from the current pose.

        Does not require the constraint to be 0; the value is the remaining
        signed angle from the current mid-joint to the pole around the chain.
        """
        start = leg_bone.matrix.to_translation()
        if end_pos is None:
            end_pos = foot_bone.matrix.to_translation()
        chain_axis = end_pos - start
        if chain_axis.length < 1e-6:
            return 0.0
        chain_axis.normalize()

        def project(point):
            direction = point - start
            return direction - direction.dot(chain_axis) * chain_axis

        current = project(knee_bone.matrix.to_translation())
        target = project(knee_ik_bone.matrix.to_translation())
        if current.length < 1e-4 or target.length < 1e-4:
            return 0.0
        return self._signed_angle(current, target, chain_axis)

    def _arm_chain_end(self, arm_bone, hand_bone):
        tail = getattr(arm_bone, "tail", None)
        if tail is not None:
            return tail.copy()
        return hand_bone.matrix.to_translation()

    def _pole_alignment(self, mid_bone, pole_bone, start_bone, end_pos):
        start = start_bone.matrix.to_translation()
        chain_axis = end_pos - start
        if chain_axis.length < 1e-6:
            return 1.0
        chain_axis.normalize()
        current = mid_bone.matrix.to_translation() - start
        target = pole_bone.matrix.to_translation() - start
        current = current - current.dot(chain_axis) * chain_axis
        target = target - target.dot(chain_axis) * chain_axis
        if current.length < 1e-4 or target.length < 1e-4:
            return 1.0
        return current.normalized().dot(target.normalized())

    def _arm_pole_alignment(self, arm_bone, arm_ik_bone, shoulder_bone, hand_bone):
        return self._pole_alignment(
            arm_bone,
            arm_ik_bone,
            shoulder_bone,
            self._arm_chain_end(arm_bone, hand_bone),
        )

    def _iter_pole_constraints(self, armature_object):
        limbs = self.cleanup_mode
        if limbs in {'LEGS', 'BOTH'}:
            for chain in self._iter_legs(armature_object):
                constraint = self._find_ik_constraint(chain['knee'])
                if constraint is not None:
                    yield chain['knee'], constraint
        if limbs in {'ARMS', 'BOTH'}:
            for chain in iter_arm_fk_chains(armature_object):
                constraint = self._find_ik_constraint(chain['arm'])
                if constraint is not None:
                    yield chain['arm'], constraint

    def _apply_limb_pole_angles(self, armature_object):
        """Zero poles for the matched limbs, sample once, then solve independently."""
        limbs = self.cleanup_mode
        knee_jobs = []
        if limbs in {'LEGS', 'BOTH'}:
            for chain in self._iter_legs(armature_object):
                ik_constraint = self._find_ik_constraint(chain['knee'])
                if ik_constraint is None:
                    continue
                knee_jobs.append((chain, ik_constraint))

        arm_jobs = []
        if limbs in {'ARMS', 'BOTH'}:
            for chain in iter_arm_fk_chains(armature_object):
                arm_bone = chain['arm']
                arm_ik_bone = chain['arm_ik']
                shoulder_bone = chain['shoulder']
                hand_bone = chain['hand']
                ik_constraint = self._find_ik_constraint(arm_bone)
                if not all([arm_bone, arm_ik_bone, hand_bone, ik_constraint]):
                    continue
                if shoulder_bone is None:
                    ik_constraint.pole_angle = (
                        math.radians(-90.0) if chain['side'] == 'L' else 0.0
                    )
                    continue
                arm_jobs.append((chain, ik_constraint))

        if not knee_jobs and not arm_jobs:
            return

        for _chain, ik_constraint in knee_jobs:
            ik_constraint.pole_angle = 0.0
        for _chain, ik_constraint in arm_jobs:
            ik_constraint.pole_angle = 0.0
        bpy.context.view_layer.update()

        knee_signs = getattr(self, "_knee_pole_signs", None)
        if knee_signs is None:
            knee_signs = {}
            self._knee_pole_signs = knee_signs
        arm_signs = getattr(self, "_arm_pole_signs", None)
        if arm_signs is None:
            arm_signs = {}
            self._arm_pole_signs = arm_signs

        computed_knees = []
        for chain, ik_constraint in knee_jobs:
            angle = self._pole_angle_from_zero(
                chain['knee'], chain['knee_ik'], chain['leg'], chain['foot']
            )
            sign = knee_signs.get(chain['id'], 1.0)
            ik_constraint.pole_angle = sign * angle
            computed_knees.append((chain, ik_constraint, angle, sign))

        computed_arms = []
        for chain, ik_constraint in arm_jobs:
            end_pos = self._arm_chain_end(chain['arm'], chain['hand'])
            angle = self._pole_angle_from_zero(
                chain['arm'], chain['arm_ik'], chain['shoulder'], chain['hand'],
                end_pos=end_pos,
            )
            sign = arm_signs.get(chain['id'], 1.0)
            ik_constraint.pole_angle = sign * angle
            computed_arms.append((chain, ik_constraint, angle, sign))

        bpy.context.view_layer.update()

        for chain, ik_constraint, angle, sign in computed_knees:
            alignment = self._pole_alignment(
                chain['knee'], chain['knee_ik'], chain['leg'],
                chain['foot'].matrix.to_translation(),
            )
            if alignment < 0.85:
                sign = -sign
                knee_signs[chain['id']] = sign
                ik_constraint.pole_angle = sign * angle
            elif chain['id'] not in knee_signs:
                knee_signs[chain['id']] = sign

        for chain, ik_constraint, angle, sign in computed_arms:
            alignment = self._arm_pole_alignment(
                chain['arm'], chain['arm_ik'], chain['shoulder'], chain['hand']
            )
            if alignment < 0.85:
                sign = -sign
                arm_signs[chain['id']] = sign
                ik_constraint.pole_angle = sign * angle
            elif chain['id'] not in arm_signs:
                arm_signs[chain['id']] = sign

    def _refine_arm_pole_angles(self, armature_object):
        """Correct leftover elbow-to-pole error after the HandIK snap."""
        for _ in range(3):
            bpy.context.view_layer.update()
            proposals = []
            for chain in iter_arm_fk_chains(armature_object):
                arm_bone = chain['arm']
                arm_ik_bone = chain['arm_ik']
                shoulder_bone = chain['shoulder']
                hand_bone = chain['hand']
                ik_constraint = self._find_ik_constraint(arm_bone)
                if not all([arm_bone, arm_ik_bone, shoulder_bone, hand_bone, ik_constraint]):
                    continue
                delta = self._pole_angle_from_zero(
                    arm_bone,
                    arm_ik_bone,
                    shoulder_bone,
                    hand_bone,
                    end_pos=self._arm_chain_end(arm_bone, hand_bone),
                )
                if abs(delta) < 1e-5:
                    continue
                proposals.append((
                    ik_constraint,
                    delta,
                    ik_constraint.pole_angle,
                    self._arm_pole_alignment(arm_bone, arm_ik_bone, shoulder_bone, hand_bone),
                    arm_bone,
                    arm_ik_bone,
                    shoulder_bone,
                    hand_bone,
                ))
            if not proposals:
                break
            for ik_constraint, delta, original, _before, *_rest in proposals:
                ik_constraint.pole_angle = original + delta
            bpy.context.view_layer.update()
            retries = []
            for item in proposals:
                ik_constraint, delta, original, before, arm_bone, arm_ik_bone, shoulder_bone, hand_bone = item
                after = self._arm_pole_alignment(arm_bone, arm_ik_bone, shoulder_bone, hand_bone)
                if after + 1e-4 < before:
                    retries.append(item)
            if retries:
                for ik_constraint, delta, original, _before, *_rest in retries:
                    ik_constraint.pole_angle = original - delta
                bpy.context.view_layer.update()
                for ik_constraint, delta, original, before, arm_bone, arm_ik_bone, shoulder_bone, hand_bone in retries:
                    after = self._arm_pole_alignment(arm_bone, arm_ik_bone, shoulder_bone, hand_bone)
                    if after + 1e-4 < before:
                        ik_constraint.pole_angle = original

    def _key_limb_pole_angles(self, armature_object, frame):
        for _pose_bone, constraint in self._iter_pole_constraints(armature_object):
            try:
                constraint.keyframe_insert("pole_angle", frame=frame)
            except RuntimeError:
                pass

    def _record_pole_keys(self, armature_object, frame):
        store = self._pole_store
        for pose_bone, constraint in self._iter_pole_constraints(armature_object):
            path = f'pose.bones["{pose_bone.name}"].constraints["{constraint.name}"].pole_angle'
            store.setdefault(path, []).append((frame, constraint.pole_angle))

    def _record_bone_keys(self, frame, bones):
        store = self._key_store
        for bone in bones:
            entry = store.setdefault(bone.name, {
                "mode": bone.rotation_mode,
                "frames": [],
            })
            if bone.rotation_mode == 'QUATERNION':
                rotation = tuple(bone.rotation_quaternion)
            else:
                rotation = tuple(bone.rotation_euler)
            entry["frames"].append((
                frame,
                tuple(bone.location),
                rotation,
                tuple(bone.scale),
            ))

    def _flush_recorded_keys(self, armature_object):
        if not armature_object.animation_data:
            armature_object.animation_data_create()
        action = armature_object.animation_data.action
        if action is None:
            action = bpy.data.actions.new(name=f"{armature_object.name} IK Match")
            assign_action(armature_object.animation_data, action)

        for bone_name, entry in self._key_store.items():
            frames = entry["frames"]
            if not frames:
                continue
            frames.sort(key=lambda item: item[0])
            loc_values = [item[1] for item in frames]
            rot_values = [item[2] for item in frames]
            if entry["mode"] == 'QUATERNION':
                aligned = [list(rot_values[0])]
                for quat in rot_values[1:]:
                    prev = aligned[-1]
                    if (prev[0] * quat[0] + prev[1] * quat[1] + prev[2] * quat[2] + prev[3] * quat[3]) < 0.0:
                        aligned.append([-quat[0], -quat[1], -quat[2], -quat[3]])
                    else:
                        aligned.append(list(quat))
                rot_values = aligned
            scl_values = [item[3] for item in frames]
            frame_nums = [item[0] for item in frames]

            frame_min = frame_nums[0]
            frame_max = frame_nums[-1]

            def write_channel(data_path, index, values):
                fcurve = find_fcurve(action, data_path, index=index)
                if fcurve is None:
                    fcurve = new_fcurve(action, data_path, index=index, action_group=bone_name)
                else:
                    for i in range(len(fcurve.keyframe_points) - 1, -1, -1):
                        frame = fcurve.keyframe_points[i].co[0]
                        if frame_min - 0.001 <= frame <= frame_max + 0.001:
                            fcurve.keyframe_points.remove(fcurve.keyframe_points[i])
                for frame, value in zip(frame_nums, values):
                    fcurve.keyframe_points.insert(frame, value, options={'FAST'})
                fcurve.update()

            loc_path = f'pose.bones["{bone_name}"].location'
            for index in range(3):
                write_channel(loc_path, index, [value[index] for value in loc_values])

            if entry["mode"] == 'QUATERNION':
                rot_path = f'pose.bones["{bone_name}"].rotation_quaternion'
                for index in range(4):
                    write_channel(rot_path, index, [value[index] for value in rot_values])
            else:
                rot_path = f'pose.bones["{bone_name}"].rotation_euler'
                for index in range(3):
                    write_channel(rot_path, index, [value[index] for value in rot_values])

            scl_path = f'pose.bones["{bone_name}"].scale'
            for index in range(3):
                write_channel(scl_path, index, [value[index] for value in scl_values])

        for data_path, frames in getattr(self, "_pole_store", {}).items():
            if not frames:
                continue
            frames = sorted(frames, key=lambda item: item[0])
            frame_nums = [item[0] for item in frames]
            values = [item[1] for item in frames]
            frame_min = frame_nums[0]
            frame_max = frame_nums[-1]
            group = ""
            if 'pose.bones["' in data_path:
                group = data_path.split('pose.bones["', 1)[-1].split('"]', 1)[0]
            fcurve = find_fcurve(action, data_path, index=0)
            if fcurve is None:
                fcurve = new_fcurve(action, data_path, index=0, action_group=group)
            else:
                for i in range(len(fcurve.keyframe_points) - 1, -1, -1):
                    frame = fcurve.keyframe_points[i].co[0]
                    if frame_min - 0.001 <= frame <= frame_max + 0.001:
                        fcurve.keyframe_points.remove(fcurve.keyframe_points[i])
            for frame, value in zip(frame_nums, values):
                fcurve.keyframe_points.insert(frame, value, options={'FAST'})
            fcurve.update()

    def execute(self, context):
        # Pause Animation Layers during IK placement / many view_layer.update() calls
        with anim_layers_compat.anim_layers_paused():
            return self._execute_transfer(context)

    def _execute_transfer(self, context):
        from .create_animation_rig import find_target_armature, _activate_armature
        from .ik_channels import match
        obj = find_target_armature(context)
        if obj is None:
            return {'CANCELLED'}
        _activate_armature(context, obj)
        if context.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        try:
            count = match(context, obj, self.cleanup_mode, self.entire_animation, self.auto_keyframe or not self.entire_animation, clean=self.clean_animation, _batch=True)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Matched {count} limb poses; switch keys preserved." if self.clean_animation else f"Matched {count} limb poses; FK keys and switch keys preserved.")
        return {'FINISHED'}

    def _execute_transfer_body(self, context, armature_object):
        from .create_animation_rig import (
            _clear_fk_rest_hold,
            _iter_limb_ik_constraints,
            _set_ik_control_fcurves_muted,
            _set_ik_driven_fcurves_muted,
            _set_ik_enabled,
            pause_ik_fk_mute_sync,
        )
        from . import anim_layers_compat

        # Temporarily evaluate FK (unmute) and silence IK keys for matched limbs only
        limbs = self.cleanup_mode
        _clear_fk_rest_hold(armature_object)
        _set_ik_driven_fcurves_muted(armature_object, False, limbs=limbs)
        _set_ik_control_fcurves_muted(armature_object, True, limbs=limbs)
        for _pb, constraint in _iter_limb_ik_constraints(armature_object, limbs=limbs):
            constraint.mute = True

        self._knee_pole_signs = {}
        self._arm_pole_signs = {}
        self._pole_store = {}

        if self.entire_animation:
            original_frame = context.scene.frame_current
            start_frame = context.scene.frame_start
            end_frame = context.scene.frame_end
            
            total_frames = end_frame - start_frame + 1
            total_transfers = 0
            self._collect_keys = bool(self.auto_keyframe)
            self._key_store = {}
            
            if self.show_progress:
                context.window_manager.progress_begin(0, 100)
            
            try:
                with anim_layers_compat.bind_driving_action_for_bake(armature_object, context):
                    for frame_num in range(start_frame, end_frame + 1):
                        if self.show_progress:
                            progress = (frame_num - start_frame) / total_frames * 100
                            context.window_manager.progress_update(progress)

                        context.scene.frame_set(frame_num)
                        total_transfers += self.process_frame(context)

                    if self._collect_keys:
                        self._flush_recorded_keys(armature_object)
                    
                if self.show_progress:
                    context.window_manager.progress_end()
                
                context.scene.frame_set(original_frame)
                
                keep_frame = start_frame if self.reference_frame == 'FIRST' else end_frame
                if self._should_remove_knee_frames() and self.remove_knee_frames:
                    knee_leg_names = []
                    for chain in self._iter_legs(armature_object):
                        knee_leg_names.extend((chain['knee'].name, chain['leg'].name))
                    self.remove_fk_keyframes(context, keep_frame, knee_leg_names, "knee/leg")
                if self._should_remove_arm_frames() and self.remove_arm_frames:
                    arm_names = [chain['arm'].name for chain in iter_arm_fk_chains(armature_object)]
                    self.remove_fk_keyframes(context, keep_frame, arm_names or ["ArmL", "ArmR"], "arm")
                
                if self.reset_foot_bones:
                    self.reset_foot_bone_transforms(context)

                pause_ik_fk_mute_sync(False)
                _set_ik_enabled(context, armature_object, True, limbs=self.cleanup_mode)
                try:
                    from .anim_rig_extras import mark_ik_matched
                    mark_ik_matched(armature_object, self.cleanup_mode)
                except Exception:
                    pass
                self.report({'INFO'}, f"Successfully positioned IK controllers across {total_frames} frames")
                return {'FINISHED'}
                
            except Exception as e:
                if self.show_progress:
                    context.window_manager.progress_end()
                context.scene.frame_set(original_frame)
                self.report({'ERROR'}, f"Error processing animation: {str(e)}")
                return {'CANCELLED'}
        else:
            self._collect_keys = False
            with anim_layers_compat.bind_driving_action_for_bake(armature_object, context):
                try:
                    context.scene.frame_set(context.scene.frame_current)
                except Exception:
                    context.view_layer.update()
                transfer_count = self.process_frame(context)
            
            if transfer_count > 0:
                pause_ik_fk_mute_sync(False)
                _set_ik_enabled(context, armature_object, True, limbs=self.cleanup_mode)
                try:
                    from .anim_rig_extras import mark_ik_matched
                    mark_ik_matched(armature_object, self.cleanup_mode)
                except Exception:
                    pass
                self.report({'INFO'}, f"Successfully positioned {transfer_count} IK controllers")
            else:
                self.report({'WARNING'}, "No IK controllers could be positioned")
                
            return {'FINISHED'}
    
    def _should_remove_knee_frames(self):
        return self.cleanup_mode in {'LEGS', 'BOTH'}

    def _should_remove_arm_frames(self):
        return self.cleanup_mode in {'ARMS', 'BOTH'}

    def remove_fk_keyframes(self, context, frame_to_keep, bone_names, label):
        """Remove all keyframes from the given bones except the chosen reference frame"""
        armature_object = context.object
        bones_to_process = [
            armature_object.pose.bones.get(name) for name in bone_names
            if armature_object.pose.bones.get(name)
        ]
        
        if not bones_to_process:
            self.report({'WARNING'}, f"No {label} bones found to remove keyframes from")
            return
        
        # Ensure action exists
        if not armature_object.animation_data or not armature_object.animation_data.action:
            self.report({'WARNING'}, "No animation data found")
            return
        
        action = armature_object.animation_data.action
        bone_names = [bone.name for bone in bones_to_process]
        
        # Track the number of keyframes removed
        removed_count = 0
        
        # Find all FCurves associated with the requested bones
        fcurves_to_process = []
        for fcurve in get_fcurves(action):
            if fcurve.data_path.startswith('pose.bones["') and any(
                f'pose.bones["{bone_name}"]' in fcurve.data_path for bone_name in bone_names
            ):
                fcurves_to_process.append(fcurve)
        
        # For each FCurve, remove all keyframes except for the chosen frame
        for fcurve in fcurves_to_process:
            # Sort keyframes by frame
            keyframes = sorted(fcurve.keyframe_points, key=lambda kf: kf.co.x)
            
            # Skip if there's only one keyframe or none
            if len(keyframes) <= 1:
                continue
            
            # Decide which keyframe to keep (first or last) and move it to frame_to_keep
            keep_index = 0 if self.reference_frame == 'FIRST' else len(keyframes) - 1
            keep_kf = keyframes[keep_index]
            # Remove all other keyframes, starting from the end to avoid reindex issues
            for i in range(len(keyframes) - 1, -1, -1):
                if i == keep_index:
                    continue
                fcurve.keyframe_points.remove(keyframes[i])
                removed_count += 1
            
            # Move the kept keyframe to the exact reference frame
            if keep_kf.co.x != frame_to_keep:
                keep_kf.co.x = frame_to_keep
                keep_kf.handle_left.x = frame_to_keep - 0.5
                keep_kf.handle_right.x = frame_to_keep + 0.5
            fcurve.update()
        
        # Report the number of keyframes removed
        if removed_count > 0:
            self.report({'INFO'}, f"Removed {removed_count} keyframes from {label} bones, keeping only frame {int(frame_to_keep)}")
        else:
            self.report({'INFO'}, f"No {label} bone keyframes found to remove")
    
    def reset_foot_bone_transforms(self, context):
        """Reset transforms and remove keyframes from Foot FK bones after IK transfer"""
        armature_object = context.object
        
        # Foot bones to process
        foot_bones = [
            armature_object.pose.bones.get(self.custom_foot_bone_l),
            armature_object.pose.bones.get(self.custom_foot_bone_r),
        ]
        
        # Filter out None values (in case a bone doesn't exist)
        foot_bones = [bone for bone in foot_bones if bone]
        
        if not foot_bones:
            self.report({'WARNING'}, "No Foot bones found to reset")
            return
        
        # Reset transforms for each foot bone
        for bone in foot_bones:
            # Reset location
            bone.location = (0.0, 0.0, 0.0)
            
            # Reset rotation based on rotation mode
            if bone.rotation_mode == 'QUATERNION':
                bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            else:
                bone.rotation_euler = (0.0, 0.0, 0.0)
            
            # Reset scale
            bone.scale = (1.0, 1.0, 1.0)
        
        # Update the view layer to apply the transform resets
        context.view_layer.update()
        
        # Remove keyframes if animation data exists
        if armature_object.animation_data and armature_object.animation_data.action:
            action = armature_object.animation_data.action
            bone_names = [bone.name for bone in foot_bones]
            
            # Track the number of keyframes removed
            removed_count = 0
            
            # Find all FCurves associated with the foot bones and remove them
            fcurves_to_remove = []
            for fcurve in get_fcurves(action):
                # Parse the data path to check if it belongs to a foot bone
                if fcurve.data_path.startswith('pose.bones["') and any(bone_name in fcurve.data_path for bone_name in bone_names):
                    fcurves_to_remove.append(fcurve)
                    removed_count += len(fcurve.keyframe_points)
            
            # Remove the FCurves entirely
            for fcurve in fcurves_to_remove:
                remove_fcurve(action, fcurve)
            
            # Report the number of keyframes removed
            if removed_count > 0:
                self.report({'INFO'}, f"Reset foot bone transforms and removed {removed_count} keyframes")
            else:
                self.report({'INFO'}, "Reset foot bone transforms (no keyframes found to remove)")
        else:
            self.report({'INFO'}, "Reset foot bone transforms (no animation data found)")
        
        # Final update
        context.view_layer.update()

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.label(text="Match IK positions to FK bones?")
        layout.prop(self, "cleanup_mode", text="Limbs")
        layout.prop(self, "entire_animation")
        
        # Only show auto keyframe option if entire animation is selected
        if self.entire_animation:
            layout.prop(self, "auto_keyframe")
            layout.prop(self, "clean_animation")
            if not self.clean_animation:
                layout.label(text="FK keys are preserved; only IK channels are written.")


def register():
    bpy.utils.register_class(SUB_OP_fk_to_ik_transfer)

def unregister():
    bpy.utils.unregister_class(SUB_OP_fk_to_ik_transfer)

if __name__ == "__main__":
    register()
