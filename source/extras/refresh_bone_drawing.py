import bpy
from bpy.types import Operator, Panel


_injected_ok = False
_hooked_panels = []


class SUB_OT_refresh_bone_drawing(Operator):
    bl_idname = "sub.refresh_bone_drawing"
    bl_label = "Refresh Bone Drawing"
    bl_description = (
        "Work around invisible imported bones by toggling and restoring each "
        "bone's Connected state"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == "ARMATURE"

    def execute(self, context):
        armature = context.object
        previous_mode = armature.mode
        context.view_layer.objects.active = armature
        armature.select_set(True)
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            for bone in armature.data.edit_bones:
                connected = bone.use_connect
                bone.use_connect = not connected
                bone.use_connect = connected
            bpy.ops.object.mode_set(mode=previous_mode)
        except Exception as exc:
            try:
                bpy.ops.object.mode_set(mode=previous_mode)
            except Exception:
                pass
            self.report({"ERROR"}, f"Bone drawing refresh failed: {exc}")
            return {"CANCELLED"}

        context.view_layer.update()
        self.report({"INFO"}, "Refreshed bone drawing without changing the rig.")
        return {"FINISHED"}


def _draw_button(layout):
    layout.separator()
    layout.operator(SUB_OT_refresh_bone_drawing.bl_idname, icon="FILE_REFRESH")


def draw_refresh_button(self, context):
    obj = context.object
    if obj is not None and obj.type == "ARMATURE":
        _draw_button(self.layout)


class SUB_PT_refresh_bone_drawing_fallback(Panel):
    bl_label = "Refresh Bone Drawing"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "data"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return not _injected_ok and obj is not None and obj.type == "ARMATURE"

    def draw(self, _context):
        self.layout.use_property_decorate = False
        _draw_button(self.layout)

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


def _viewport_display_panels():
    for name in dir(bpy.types):
        cls = getattr(bpy.types, name, None)
        if not isinstance(cls, type) or not issubclass(cls, Panel):
            continue
        if getattr(cls, "bl_space_type", None) != "PROPERTIES":
            continue
        if getattr(cls, "bl_region_type", None) != "WINDOW":
            continue
        if getattr(cls, "bl_context", None) != "data":
            continue
        if (getattr(cls, "bl_label", "") or "").strip().lower() == "viewport display":
            yield cls


def register():
    global _injected_ok, _hooked_panels
    bpy.utils.register_class(SUB_OT_refresh_bone_drawing)
    bpy.utils.register_class(SUB_PT_refresh_bone_drawing_fallback)
    _injected_ok = False
    _hooked_panels = []
    for panel in _viewport_display_panels():
        try:
            panel.append(draw_refresh_button)
            _hooked_panels.append(panel)
            _injected_ok = True
        except Exception:
            pass


def unregister():
    global _injected_ok, _hooked_panels
    for panel in _hooked_panels:
        try:
            panel.remove(draw_refresh_button)
        except Exception:
            pass
    _hooked_panels = []
    _injected_ok = False
    bpy.utils.unregister_class(SUB_PT_refresh_bone_drawing_fallback)
    bpy.utils.unregister_class(SUB_OT_refresh_bone_drawing)
