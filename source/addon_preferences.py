import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, PropertyGroup


ADDON_MODULE_NAME = (__package__ or "").split(".")[0]


class SUB_PG_param_labels_path(PropertyGroup):
    name: StringProperty(name="Label", default="")
    path: StringProperty(
        name="Path",
        description="An additional ParamLabels CSV file that receives generated hashes",
        default="",
        subtype="FILE_PATH",
    )


class SUB_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_MODULE_NAME

    param_labels_paths: CollectionProperty(type=SUB_PG_param_labels_path)
    param_labels_paths_index: IntProperty(default=0)

    show_timeline_fps_shortcuts: BoolProperty(
        name="Show Timeline FPS Shortcuts",
        description="Show the configurable FPS buttons in the Timeline header",
        default=True,
    )
    fps_preset_1: IntProperty(name="FPS 1", default=5, min=1, max=1000)
    fps_preset_2: IntProperty(name="FPS 2", default=15, min=1, max=1000)
    fps_preset_3: IntProperty(name="FPS 3", default=30, min=1, max=1000)
    fps_preset_4: IntProperty(name="FPS 4", default=60, min=1, max=1000)

    show_nuanmb_extension_on_import: BoolProperty(
        name=".nuanmb",
        description="Keep the .nuanmb extension in imported Blender action names",
        default=False,
    )
    show_rawanim_extension_on_import: BoolProperty(
        name=".rawanim",
        description="Keep the .rawanim extension in imported Blender action names",
        default=True,
    )

    collection_preset_directory: StringProperty(
        name="Custom Collection Preset Directory",
        description="Directory used when the Collection Presets panel library is set to Custom",
        default="",
        subtype="DIR_PATH",
    )

    def draw(self, _context):
        layout = self.layout

        box = layout.box()
        box.label(text="Timeline FPS Shortcuts")
        box.prop(self, "show_timeline_fps_shortcuts")
        row = box.row(align=True)
        row.prop(self, "fps_preset_1")
        row.prop(self, "fps_preset_2")
        row.prop(self, "fps_preset_3")
        row.prop(self, "fps_preset_4")

        box = layout.box()
        box.label(text="Show Animation File Extension")
        box.prop(self, "show_nuanmb_extension_on_import")
        box.prop(self, "show_rawanim_extension_on_import")

        box = layout.box()
        box.label(text="Armature Collection Presets")
        box.prop(self, "collection_preset_directory")

        box = layout.box()
        box.label(text="Ultimate Sidebar Layout")
        box.label(
            text="Panel visibility and order live in the Panel Presets panel at the bottom of the Ultimate tab.",
            icon='INFO',
        )

        box = layout.box()
        box.label(text="Additional ParamLabels Files")
        controls = box.row(align=True)
        controls.operator("sub.add_param_labels_path", text="Add", icon="ADD")
        controls.operator("sub.remove_param_labels_path", text="Remove", icon="REMOVE")
        for item in self.param_labels_paths:
            box.prop(item, "path", text="")
        if not self.param_labels_paths:
            box.label(text="No additional files configured.", icon="INFO")


CLASSES = (
    SUB_PG_param_labels_path,
    SUB_AddonPreferences,
)


def get_addon_preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_MODULE_NAME)
    return addon.preferences if addon is not None else None


def fps_presets(context=None):
    prefs = get_addon_preferences(context)
    if prefs is None:
        return (5, 15, 30, 60)
    values = (
        prefs.fps_preset_1,
        prefs.fps_preset_2,
        prefs.fps_preset_3,
        prefs.fps_preset_4,
    )
    # Avoid duplicate buttons while preserving the configured order.
    return tuple(dict.fromkeys(int(value) for value in values))


def show_animation_extension_on_import(extension, context=None):
    """Return the configured import-name behavior, including safe defaults."""
    extension = extension.lower()
    prefs = get_addon_preferences(context)
    if extension == ".rawanim":
        return True if prefs is None else prefs.show_rawanim_extension_on_import
    if extension == ".nuanmb":
        return False if prefs is None else prefs.show_nuanmb_extension_on_import
    return False


def format_animation_name_on_import(stem, extension, context=None):
    """Format an imported action name using the per-format extension setting."""
    return stem + extension if show_animation_extension_on_import(extension, context) else stem


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
