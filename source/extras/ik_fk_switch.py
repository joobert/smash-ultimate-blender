import bpy
from bpy.props import BoolProperty

class SUB_OP_simple_ik_control(bpy.types.Operator):
    """Simple IK control - just toggle IK constraints on/off"""
    bl_description = 'Simple IK control - just toggle IK constraints on/off'
    bl_idname = "sub.simple_ik_control"
    bl_label = "Toggle IK Control"
    bl_options = {'REGISTER', 'UNDO'}
    
    enable_ik: BoolProperty(
        name="Enable IK",
        description="Enable IK control (disable to use direct bone manipulation)",
        default=True
    )
    
    @classmethod
    def poll(cls, context):
        return context.object and context.object.type == 'ARMATURE'
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "enable_ik")
        
        if self.enable_ik:
            layout.label(text="IK Control Enabled", icon="CONSTRAINT_BONE")
        else:
            layout.label(text="Direct Bone Control", icon="BONE_DATA")
    
    def execute(self, context):
        armature_object = context.object
        
        if not armature_object or armature_object.type != 'ARMATURE':
            self.report({'ERROR'}, "No active armature selected")
            return {'CANCELLED'}
        
        # Simple approach: just toggle IK constraint influences
        influence = 1.0 if self.enable_ik else 0.0
        
        for side in ["L", "R"]:
            # Toggle IK constraints on intermediate bones (Arm/Knee)
            for bone_name in [f"Arm{side}", f"Knee{side}"]:
                bone = armature_object.pose.bones.get(bone_name)
                if bone:
                    for constraint in bone.constraints:
                        if constraint.type == 'IK':
                            constraint.influence = influence
                            constraint.mute = (influence < 0.5)
            
            # Toggle rotation constraints on end bones (Hand/Foot)
            for bone_name in [f"Hand{side}", f"Foot{side}"]:
                bone = armature_object.pose.bones.get(bone_name)
                if bone:
                    for constraint in bone.constraints:
                        if constraint.type == 'COPY_ROTATION':
                            constraint.influence = influence
                            constraint.mute = (influence < 0.5)
        
        # Force update
        armature_object.update_tag()
        bpy.context.view_layer.update()
        
        if self.enable_ik:
            self.report({'INFO'}, "IK control enabled")
        else:
            self.report({'INFO'}, "Direct bone control enabled")
        
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SUB_OP_simple_ik_control)

def unregister():
    bpy.utils.unregister_class(SUB_OP_simple_ik_control)

if __name__ == "__main__":
    register()