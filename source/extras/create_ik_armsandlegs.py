import bpy
import mathutils
from mathutils import Vector
import math  # Import the math module
from . import fk_to_ik
from . import anim_layers_compat
from .ik_leg_placement import place_leg_ik_edit_bones
from ..blender_compat import assign_bone_to_collection, ensure_bone_collection

class SUB_OP_create_ik_bones_operator(bpy.types.Operator):
    """Generate IK Bones for Arms and Legs with Automatic Setup"""
    bl_description = 'Generate IK Bones for Arms and Legs with Automatic Setup'
    bl_idname = "sub.create_ik_bones"
    bl_label = "Create IK Bones Arms + Legs"
    bl_options = {'REGISTER', 'UNDO'}
    
    match_position: bpy.props.BoolProperty(
        name="Match IK to FK Position",
        description="Match IK bones position to FK bones after creation",
        default=True
    )

    def execute(self, context):
        with anim_layers_compat.anim_layers_paused():
            return self._execute_ik_create(context)

    def _execute_ik_create(self, context):
        from .create_animation_rig import find_target_armature
        from .ik_channels import create_controls
        obj = find_target_armature(context)
        if obj is None:
            self.report({'ERROR'}, "Select an armature")
            return {'CANCELLED'}
        count = create_controls(context, obj, 'BOTH')
        if not count:
            self.report({'ERROR'}, "No complete both chains found")
            return {'CANCELLED'}
        if self.match_position:
            fk_to_ik.invoke_position_match_dialog(cleanup_mode='BOTH')
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "match_position")

class SUB_PT_ik_bones_panel(bpy.types.Panel):
    """Creates a Panel in the 3D Viewport"""
    bl_label = "IK Bone Generator"
    bl_idname = "SUB_PT_ik_bones_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'IK Bones'

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

    def draw(self, context):
        self.layout.use_property_decorate = False
        layout = self.layout
        layout.operator("sub.create_ik_bones", text="Generate IK Bones")



def register():
    bpy.utils.register_class(SUB_OP_create_ik_bones_operator)
    bpy.utils.register_class(SUB_PT_ik_bones_panel)

def unregister():
    bpy.utils.unregister_class(SUB_OP_create_ik_bones_operator)
    bpy.utils.unregister_class(SUB_PT_ik_bones_panel)

if __name__ == "__main__":
    register()
