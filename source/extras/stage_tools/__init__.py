from . import battlefield_ref
from . import light_nuanmb
from . import panel
from . import shpcanim


classes = (
    *battlefield_ref.classes,
    *light_nuanmb.classes,
    *shpcanim.classes,
    panel.SUB_PT_stage_tools,
)


def register():
    import bpy
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    if shpcanim.shpc_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(shpcanim.shpc_frame_change)
    handler = light_nuanmb._stage_light_depsgraph_update
    if handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(handler)


def unregister():
    import bpy
    if shpcanim.shpc_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(shpcanim.shpc_frame_change)
    handler = light_nuanmb._stage_light_depsgraph_update
    if handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(handler)
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
