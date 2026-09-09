import bpy
from bpy.types import Panel
from bpy.props import BoolProperty

class SUB_PT_ik_animation_tools(Panel):
    """Creates an IK Tools Panel within the Animation Tools category"""
    bl_label = "IK Tools"
    bl_idname = "SUB_PT_ik_animation_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_parent_id = "SUB_PT_animation_tools"

    @classmethod
    def poll(cls, context):
        modes = ['POSE', 'OBJECT', 'EDIT_ARMATURE']
        return context.mode in modes

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False

        # IK/FK switching section
        box = layout.box()
        box.label(text="IK/FK Control:", icon="CONSTRAINT_BONE")

        row = box.row(align=True)
        row.scale_y = 1.2
        if hasattr(bpy.types, 'SUB_OP_quick_switch_ik_fk'):
            row.operator("sub.quick_switch_ik_fk", text="Switch IK/FK", icon="ARROW_LEFTRIGHT")

        row = box.row(align=True)
        if hasattr(bpy.types, 'SUB_OP_advanced_ik_fk_control'):
            row.operator("sub.advanced_ik_fk_control", text="Advanced IK/FK Control", icon="CONSTRAINT_BONE")

        row = box.row(align=True)
        row.operator("sub.toggle_ik_influence", text="Toggle IK Influence", icon="MODIFIER")
        from .create_animation_rig import find_target_armature
        arm = find_target_armature(context)
        if arm and arm.data.get('sub_independent_ik'):
            row = box.row(align=True)
            row.prop(arm.data, 'sub_ik_stretch_arms', text='IK Stretch Arms')
            row.prop(arm.data, 'sub_ik_stretch_legs', text='IK Stretch Legs')

        # IK Setup section
        box = layout.box()
        box.label(text="IK Setup:", icon="BONE_DATA")

        col = box.column(align=True)
        col.operator("sub.create_ik_bones", text="Create IK Bones (Arms + Legs)", icon="CONSTRAINT_BONE")
        col.operator("sub.create_arm_ik", text="Create Arm IK Bones", icon="CONSTRAINT_BONE")
        col.operator("sub.create_foot_ik", text="Create Foot IK Bones", icon="CONSTRAINT_BONE")
        if hasattr(bpy.types, 'SUB_OP_quick_switch_ik_fk'):
            col.operator("sub.quick_switch_ik_fk", text="Switch IK/FK", icon="ARROW_LEFTRIGHT")

        # Animation Tools section
        box = layout.box()
        box.label(text="Animation Tools:", icon="ANIM")

        col = box.column(align=True)
        col.operator("sub.apply_ik_animation", text="Bake & Remove IK/FK", icon="RENDER_ANIMATION")


# Property to store panel expansion state
def register_properties():
    bpy.types.Scene.ik_animation_panel_expanded = BoolProperty(
        name="IK Animation Panel Expanded",
        description="Whether the IK Animation panel is expanded",
        default=True
    )


def unregister_properties():
    if hasattr(bpy.types.Scene, 'ik_animation_panel_expanded'):
        del bpy.types.Scene.ik_animation_panel_expanded


def register():
    bpy.utils.register_class(SUB_PT_ik_animation_tools)
    register_properties()


def unregister():
    bpy.utils.unregister_class(SUB_PT_ik_animation_tools)
    unregister_properties()


if __name__ == "__main__":
    register()
