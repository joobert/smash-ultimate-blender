import bpy
import mathutils
from mathutils import Vector
import bmesh
from math import degrees, radians, atan2, pi

class SUB_OP_align_ik_pole_angle(bpy.types.Operator):
    """Calculate pole angles to make IK pose match the current FK pose.
    
    Uses FK bone positions as the target: projects FK bone direction and pole 
    direction onto the plane perpendicular to the FK middle bone, then calculates 
    the pole angle needed. Perfect for seamless FK-to-IK transitions."""
    bl_description = 'Calculate pole angles to make IK pose match the current FK pose.'
    bl_idname = "sub.align_ik_pole_angle"
    bl_label = "Align IK to FK Pose"
    bl_options = {'REGISTER', 'UNDO'}
    
    entire_animation: bpy.props.BoolProperty(
        name="Entire Animation",
        description="Apply to the entire animation instead of just the current frame",
        default=False
    )
    
    auto_keyframe: bpy.props.BoolProperty(
        name="Auto Keyframe",
        description="Automatically insert keyframes when applying to the entire animation",
        default=True
    )
    
    include_arms: bpy.props.BoolProperty(
        name="Include Arms",
        description="Apply pole angle correction to arm IK constraints",
        default=True
    )
    
    include_legs: bpy.props.BoolProperty(
        name="Include Legs", 
        description="Apply pole angle correction to leg IK constraints",
        default=True
    )
    
    additive_mode: bpy.props.BoolProperty(
        name="Additive Mode",
        description="Add calculated angle to current pole angle (recommended). When disabled, sets pole angle absolutely",
        default=True
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "include_arms")
        layout.prop(self, "include_legs")
        layout.separator()
        layout.prop(self, "additive_mode")
        layout.separator()
        layout.prop(self, "entire_animation")
        
        # Only show auto keyframe option if entire animation is selected
        if self.entire_animation:
            layout.prop(self, "auto_keyframe")

    def execute(self, context):
        armature_object = context.object
        
        if not armature_object or armature_object.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}
        
        # Switch to pose mode if not already
        if context.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        
        # Determine frame range
        if self.entire_animation:
            frame_start = bpy.context.scene.frame_start
            frame_end = bpy.context.scene.frame_end
        else:
            frame_start = frame_end = bpy.context.scene.frame_current
        
        # Apply pole angle corrections for each frame in range
        total_corrections = 0
        for frame in range(frame_start, frame_end + 1):
            bpy.context.scene.frame_set(frame)
            # Ensure the frame is fully updated before calculating
            bpy.context.view_layer.update()
            
            frame_corrections = self.align_pole_angles_frame(armature_object)
            total_corrections += frame_corrections
            
            if self.entire_animation and self.auto_keyframe and frame_corrections > 0:
                self.insert_pole_angle_keyframes(armature_object)
        
        if self.entire_animation:
            self.report({'INFO'}, f"Applied {total_corrections} pole angle corrections across frames {frame_start}-{frame_end}")
        else:
            self.report({'INFO'}, f"Applied {total_corrections} pole angle corrections for current frame")
        
        return {'FINISHED'}
    
    def align_pole_angles_frame(self, armature_object):
        """Calculate and apply correct pole angles for current frame"""
        # Ensure scene is updated for current frame
        bpy.context.view_layer.update()
        
        corrections_applied = 0
        sides = ["L", "R"]
        
        for side in sides:
            if self.include_arms:
                if self.correct_arm_pole_angle(armature_object, side):
                    corrections_applied += 1
            
            if self.include_legs:
                if self.correct_leg_pole_angle(armature_object, side):
                    corrections_applied += 1
        
        # Update scene again after applying corrections
        bpy.context.view_layer.update()
        
        return corrections_applied
    
    def correct_arm_pole_angle(self, armature_object, side):
        """Adjust pole angle so Arm bone head matches ArmFK bone head in world space."""
        arm_bone = armature_object.pose.bones.get(f"Arm{side}")
        arm_fk_bone = armature_object.pose.bones.get(f"ArmFK{side}")
        hand_bone = armature_object.pose.bones.get(f"Hand{side}")
        arm_ik_pole = armature_object.pose.bones.get(f"ArmIK{side}")
        if not all([arm_bone, arm_fk_bone, hand_bone, arm_ik_pole]):
            return False
        ik_constraint = None
        for constraint in arm_bone.constraints:
            if constraint.type == 'IK' and constraint.name == "IK_Constraint":
                ik_constraint = constraint
                break
        if not ik_constraint:
            return False
        # Calculate pole angle so Arm head matches ArmFK head
        # Use world space positions
        arm_head = armature_object.matrix_world @ arm_bone.head
        arm_tail = armature_object.matrix_world @ arm_bone.tail
        hand_tail = armature_object.matrix_world @ hand_bone.tail
        pole_head = armature_object.matrix_world @ arm_ik_pole.head
        fk_head = armature_object.matrix_world @ arm_fk_bone.head
        # Calculate the angle between the current arm->hand direction and the desired arm->fk direction
        arm_vec = (arm_tail - arm_head).normalized()
        current_vec = (hand_tail - arm_head).normalized()
        desired_vec = (fk_head - arm_head).normalized()
        # Project both onto the plane perpendicular to the arm_vec
        def project_on_plane(v, n):
            return v - v.project(n)
        current_proj = project_on_plane(current_vec, arm_vec).normalized()
        desired_proj = project_on_plane(desired_vec, arm_vec).normalized()
        angle = current_proj.angle(desired_proj)
        cross = current_proj.cross(desired_proj)
        if cross.dot(arm_vec) < 0:
            angle = -angle
        # Set the pole angle to the current value plus the adjustment
        ik_constraint.pole_angle += angle
        return True

    def correct_leg_pole_angle(self, armature_object, side):
        """Adjust pole angle so Knee bone head matches KneeFK bone head in world space."""
        knee_bone = armature_object.pose.bones.get(f"Knee{side}")
        knee_fk_bone = armature_object.pose.bones.get(f"KneeFK{side}")
        foot_bone = armature_object.pose.bones.get(f"Foot{side}")
        knee_ik_pole = armature_object.pose.bones.get(f"KneeIK{side}")
        if not all([knee_bone, knee_fk_bone, foot_bone, knee_ik_pole]):
            return False
        ik_constraint = None
        for constraint in knee_bone.constraints:
            if constraint.type == 'IK' and constraint.name == "IK_Constraint":
                ik_constraint = constraint
                break
        if not ik_constraint:
            return False
        # Calculate pole angle so Knee head matches KneeFK head
        # Use world space positions
        knee_head = armature_object.matrix_world @ knee_bone.head
        knee_tail = armature_object.matrix_world @ knee_bone.tail
        foot_tail = armature_object.matrix_world @ foot_bone.tail
        pole_head = armature_object.matrix_world @ knee_ik_pole.head
        fk_head = armature_object.matrix_world @ knee_fk_bone.head
        # Calculate the angle between the current knee->foot direction and the desired knee->fk direction
        knee_vec = (knee_tail - knee_head).normalized()
        current_vec = (foot_tail - knee_head).normalized()
        desired_vec = (fk_head - knee_head).normalized()
        # Project both onto the plane perpendicular to the knee_vec
        def project_on_plane(v, n):
            return v - v.project(n)
        current_proj = project_on_plane(current_vec, knee_vec).normalized()
        desired_proj = project_on_plane(desired_vec, knee_vec).normalized()
        angle = current_proj.angle(desired_proj)
        cross = current_proj.cross(desired_proj)
        if cross.dot(knee_vec) < 0:
            angle = -angle
        # Set the pole angle to the current value plus the adjustment
        ik_constraint.pole_angle += angle
        return True
    
    def calculate_pole_angle_clean(self, bone_head, bone_tail, ik_tail, pole_location):
        """
        Clean pole angle calculation based on proven script.
        All positions should be in world space.
        """
        # Vector from head to tail of main bone
        bone_vector = (bone_tail - bone_head).normalized()
        # Vector from tail to IK bone tail (targeted)
        ik_vector = (ik_tail - bone_tail).normalized()
        # Vector from tail to pole
        pole_vector = (pole_location - bone_tail).normalized()

        # Project pole_vector and ik_vector onto plane perpendicular to bone_vector
        def project_on_plane(v, n):
            return v - v.project(n)

        ik_proj = project_on_plane(ik_vector, bone_vector).normalized()
        pole_proj = project_on_plane(pole_vector, bone_vector).normalized()

        angle = ik_proj.angle(pole_proj)
        cross = ik_proj.cross(pole_proj)

        # If the cross product points in the opposite direction, invert angle
        if cross.dot(bone_vector) < 0:
            angle = -angle

        return angle
    
    def calculate_pole_angle(self, start_bone, middle_bone, end_bone, pole_target, armature_object):
        """Calculate the pole angle needed to make IK match the current FK pose"""
        if not all([middle_bone, end_bone, pole_target]):
            return None
        
        try:
            # Get the corresponding FK bones - this is the key insight!
            # We want IK to match the FK pose, so calculate based on FK bone positions
            
            # Determine the side (L or R) from the bone name
            side = None
            if middle_bone.name.endswith('L'):
                side = 'L'
            elif middle_bone.name.endswith('R'):
                side = 'R'
            else:
                # Try to extract side from bone names
                for s in ['L', 'R']:
                    if s in middle_bone.name:
                        side = s
                        break
            
            if not side:
                return None
            
            # Get FK bone names based on whether this is arm or leg
            if "Arm" in middle_bone.name or "arm" in middle_bone.name.lower():
                # Arm chain
                fk_start_name = f"ShoulderFK{side}"
                fk_middle_name = f"ArmFK{side}"
                fk_end_name = f"HandFK{side}"
            elif "Knee" in middle_bone.name or "knee" in middle_bone.name.lower():
                # Leg chain  
                fk_start_name = f"LegFK{side}"
                fk_middle_name = f"KneeFK{side}"
                fk_end_name = f"FootFK{side}"
            else:
                return None
            
            # Get the FK bones
            fk_start = armature_object.pose.bones.get(fk_start_name)
            fk_middle = armature_object.pose.bones.get(fk_middle_name)
            fk_end = armature_object.pose.bones.get(fk_end_name)
            
            if not all([fk_middle, fk_end]):
                return None
            
            # Calculate pole angle based on FK bone positions (desired pose)
            # Use FK bone world space positions
            middle_head = armature_object.matrix_world @ fk_middle.head
            middle_tail = armature_object.matrix_world @ fk_middle.tail
            end_tail = armature_object.matrix_world @ fk_end.tail
            
            # Get pole target location in world space
            pole_head = armature_object.matrix_world @ pole_target.head
            
            # Use the clean pole angle calculation with FK positions
            pole_angle = self.calculate_pole_angle_clean(middle_head, middle_tail, end_tail, pole_head)
            return pole_angle
            
        except Exception as e:
            return None
    
    def insert_pole_angle_keyframes(self, armature_object):
        """Insert keyframes for pole angles that were modified"""
        sides = ["L", "R"]
        
        for side in sides:
            if self.include_arms:
                arm_bone = armature_object.pose.bones.get(f"Arm{side}")
                if arm_bone:
                    for constraint in arm_bone.constraints:
                        if constraint.type == 'IK' and constraint.name == "IK_Constraint":
                            constraint.keyframe_insert(data_path="pole_angle", group=arm_bone.name)
                            break
            
            if self.include_legs:
                knee_bone = armature_object.pose.bones.get(f"Knee{side}")
                if knee_bone:
                    for constraint in knee_bone.constraints:
                        if constraint.type == 'IK' and constraint.name == "IK_Constraint":
                            constraint.keyframe_insert(data_path="pole_angle", group=knee_bone.name)
                            break


def register():
    bpy.utils.register_class(SUB_OP_align_ik_pole_angle)


def unregister():
    bpy.utils.unregister_class(SUB_OP_align_ik_pole_angle)


if __name__ == "__main__":
    register() 