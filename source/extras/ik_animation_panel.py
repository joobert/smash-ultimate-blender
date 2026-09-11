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
        self.layout.use_property_decorate = False
        layout = self.layout
        layout.use_property_split = False

        # IK/FK switching section
        box = layout.box()
        box.label(text="Pose Controls", icon="CONSTRAINT_BONE")

        row = box.row(align=True)
        row.scale_y = 1.0
        if hasattr(bpy.types, 'SUB_OP_quick_switch_ik_fk'):
            row.operator("sub.quick_switch_ik_fk", text="Switch IK/FK", icon="ARROW_LEFTRIGHT")

        row = box.row(align=True)
        if hasattr(bpy.types, 'SUB_OP_advanced_ik_fk_control'):
            row.operator("sub.advanced_ik_fk_control", text="IK/FK Settings", icon="CONSTRAINT_BONE")

        row = box.row(align=True)
        row.operator("sub.toggle_ik_influence", text="Toggle IK Influence", icon="MODIFIER")
        from .create_animation_rig import find_target_armature
        arm = find_target_armature(context)
        if arm and arm.data.get('sub_independent_ik'):
            from .anim_rig_extras import _draw_ik_stretch_rows
            stretch_box = box.box()
            stretch_box.label(text='Independent IK')
            _draw_ik_stretch_rows(stretch_box, arm)

        # Setup is used less often than posing; keep it out of the daily workflow.
        header, setup = layout.panel("sub_ik_setup", default_closed=True)
        header.label(text="Rig Setup", icon="BONE_DATA")
        if setup:
            col = setup.column(align=True)
            col.operator("sub.create_ik_bones", text="Create Arms + Legs", icon="CONSTRAINT_BONE")
            row = col.row(align=True)
            row.operator("sub.create_arm_ik", text="Arms Only")
            row.operator("sub.create_foot_ik", text="Legs Only")
            from . import custom_ik
            custom_ik.draw(setup, context, arm)

        # Animation Tools section
        from . import ik_floor_contact
        ik_floor_contact.draw(layout, context, arm)

        box = layout.box()
        box.label(text="Bake Animation", icon="ANIM")

        col = box.column(align=True)
        col.operator("sub.apply_ik_animation", text="Bake & Remove IK/FK", icon="RENDER_ANIMATION")

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


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
