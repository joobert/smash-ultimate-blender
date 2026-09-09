"""Preview and downsample the active Ultimate material's texture images."""
import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, StringProperty

from .default_textures import generated_default_texture_name_value


def target_dimensions(size, steps):
    return tuple(max(1, int(value) // (2 ** steps)) for value in size)


def skip_reason(image):
    if image.name in generated_default_texture_name_value:
        return "Built-in default texture"
    if image.library:
        return "Linked image is read-only"
    if image.source not in {'FILE', 'GENERATED'} or image.type not in {'IMAGE', 'UV_TEST'} or image.is_multiview:
        return "Only single still images are supported"
    if not all(image.size):
        return "Image data unavailable"
    if tuple(image.size) == (1, 1):
        return "Already 1 x 1"
    return ""


class SUB_PG_texture_optimization(bpy.types.PropertyGroup):
    image_name: StringProperty()

    @property
    def image(self):
        # Operator properties cannot contain ID pointers, even inside collections.
        return bpy.data.images.get(self.image_name)
    selected: BoolProperty(name="Optimize", default=True)
    steps: IntProperty(
        name="Halving Steps", default=1, min=0, max=16,
        description="Halve width and height for each step; 0 keeps the current size",
    )


class SUB_OP_optimize_textures(bpy.types.Operator):
    bl_idname = "sub.optimize_textures"
    bl_label = "Optimize Textures"
    bl_description = "Preview and halve texture dimensions for the active Ultimate material"
    bl_options = {'UNDO'}

    textures: CollectionProperty(type=SUB_PG_texture_optimization)

    @classmethod
    def poll(cls, context):
        material = getattr(context.object, 'active_material', None)
        return bool(material and material.sub_matl_data.shader_label)

    def prepare(self, context):
        self.textures.clear()
        seen = set()
        for texture in context.object.active_material.sub_matl_data.textures:
            image = texture.image
            if image is None or image.as_pointer() in seen:
                continue
            seen.add(image.as_pointer())
            item = self.textures.add()
            item.image_name = image.name
            item.selected = not skip_reason(image)

    def invoke(self, context, event):
        self.prepare(context)
        if not self.textures:
            self.report({'WARNING'}, "This material has no assigned texture images")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=600)

    def check(self, context):
        return True

    def draw(self, context):
        layout = self.layout
        layout.label(text="Each step halves width and height (minimum 1 pixel).")
        before = after = 0
        for item in self.textures:
            image = item.image
            if image is None:
                continue
            reason = skip_reason(image)
            box = layout.box()
            row = box.row(align=True)
            row.enabled = not reason
            row.prop(item, 'selected', text=image.name)
            controls = row.row()
            controls.enabled = item.selected
            controls.prop(item, 'steps')
            width, height = image.size
            target = target_dimensions(image.size, item.steps if item.selected and not reason else 0)
            box.label(text=f"{width} x {height}  ->  {target[0]} x {target[1]}")
            if reason:
                box.label(text=reason, icon='INFO')
            elif item.selected:
                before += width * height
                after += target[0] * target[1]
        if before:
            layout.label(text=f"Selected texture pixels: {100 * (1 - after / before):.1f}% fewer")
        layout.label(text="Shared images also change in other materials.", icon='INFO')
        layout.label(text="Results are packed; save the blend file to keep them.")

    def execute(self, context):
        if not self.textures:
            self.prepare(context)
        changed = 0
        seen = set()
        for item in self.textures:
            image = item.image
            if not item.selected or image is None or image.as_pointer() in seen:
                continue
            seen.add(image.as_pointer())
            if skip_reason(image):
                continue
            target = target_dimensions(image.size, item.steps)
            if target == tuple(image.size):
                continue
            try:
                image.scale(*target)
                changed += 1
                image.update()
                image.pack()
            except Exception as error:
                self.report({'WARNING'}, f"{image.name}: {error}")
        if not changed:
            self.report({'WARNING'}, "No textures were resized")
            return {'CANCELLED'}
        if context.screen:
            for area in context.screen.areas:
                area.tag_redraw()
        self.report({'INFO'}, f"Resized {changed} texture(s). Save the blend file to keep changes.")
        return {'FINISHED'}


classes = (SUB_PG_texture_optimization, SUB_OP_optimize_textures)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
