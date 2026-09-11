import bpy
import mathutils
from mathutils import Vector
import math
from . import fk_to_ik
from . import anim_layers_compat
from .ik_leg_placement import place_leg_ik_edit_bones
from ..blender_compat import assign_bone_to_collection, ensure_bone_collection

class SUB_OP_create_foot_ik_operator(bpy.types.Operator):
    """Generate Foot and Knee IK Bones with Constraints"""
    bl_description = 'Generate Foot and Knee IK Bones with Constraints'
    bl_idname = "sub.create_foot_ik"
    bl_label = "Create Foot IK Bones"
    bl_options = {'REGISTER', 'UNDO'}
    
    match_position: bpy.props.BoolProperty(
        name="Match IK to FK Position",
        description="Match IK bones position to FK bones after creation",
        default=True
    )

    def execute(self, context):
        # Pause Animation Layers handlers while editing armature topology
        with anim_layers_compat.anim_layers_paused():
            return self._execute_ik_create(context)

    def _execute_ik_create(self, context):
        from .create_animation_rig import find_target_armature
        from .ik_channels import create_controls
        obj = find_target_armature(context)
        if obj is None:
            self.report({'ERROR'}, "Select an armature")
            return {'CANCELLED'}
        count = create_controls(context, obj, 'LEGS')
        if not count:
            self.report({'ERROR'}, "No complete legs chains found")
            return {'CANCELLED'}
        if self.match_position:
            fk_to_ik.invoke_position_match_dialog(cleanup_mode='LEGS')
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "match_position")


class SUB_PT_foot_ik_panel(bpy.types.Panel):
    """Creates a Panel in the 3D Viewport"""
    bl_label = "Foot IK Bone Generator"
    bl_idname = "SUB_PT_foot_ik_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'IK Bones'

    def draw(self, context):
        self.layout.use_property_decorate = False
        layout = self.layout
        layout.operator("sub.create_foot_ik", text="Generate Foot IK Bones")



def register():
    bpy.utils.register_class(SUB_OP_create_foot_ik_operator)
    bpy.utils.register_class(SUB_PT_foot_ik_panel)


def unregister():
    bpy.utils.unregister_class(SUB_OP_create_foot_ik_operator)
    bpy.utils.unregister_class(SUB_PT_foot_ik_panel)


if __name__ == "__main__":
    register()