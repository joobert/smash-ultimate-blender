import bpy
import os
from bpy.props import BoolProperty, CollectionProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, PropertyGroup


ADDON_MODULE_NAME = (__package__ or "").split(".")[0]


class SUB_PG_param_labels_path(PropertyGroup):
    name: StringProperty(name="Label", default="", description='Display name for this additional parameter-label file')
    path: StringProperty(
        name="Path",
        description="An additional ParamLabels CSV file that receives generated hashes",
        default="",
        subtype="FILE_PATH",
    )


class SUB_PG_model_export_path(PropertyGroup):
    model_folder: StringProperty(name="Model Source Folder", subtype='DIR_PATH',
        description='Imported model folder associated with this export destination')
    export_folder: StringProperty(name="Export Folder", subtype='DIR_PATH',
        description='Destination used when exporting models imported from the associated source folder')


class SUB_OP_model_export_path(bpy.types.Operator):
    bl_description = 'Choose the destination folders used by the model exporter'
    bl_idname = 'sub.model_export_path'
    bl_label = 'Edit Model Export Folders'
    index: IntProperty(default=-1)

    def execute(self, context):
        prefs = get_addon_preferences(context)
        if prefs is None:
            return {'CANCELLED'}
        if self.index < 0:
            item = prefs.model_export_paths.add()
            obj = context.scene.sub_scene_properties.model_export_arma
            if obj is not None:
                item.model_folder = model_source_folder(obj)
        elif self.index < len(prefs.model_export_paths):
            prefs.model_export_paths.remove(self.index)
        return {'FINISHED'}


class SUB_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_MODULE_NAME

    default_vanilla_nusktb_folder: StringProperty(name="Default Vanilla .nusktb Folder", subtype='DIR_PATH',
        description='Initial folder when browsing for an original game skeleton reference')
    default_model_export_folder: StringProperty(name="Default Model Export Folder", subtype='DIR_PATH',
        description='Initial model export destination when no per-model folder is configured')
    model_export_paths: CollectionProperty(type=SUB_PG_model_export_path)

    param_labels_paths: CollectionProperty(type=SUB_PG_param_labels_path)
    param_labels_paths_index: IntProperty(default=0)

    show_timeline_fps_shortcuts: BoolProperty(
        name="Show Timeline FPS Shortcuts",
        description="Show the configurable FPS buttons in the Timeline header",
        default=True,
    )
    fps_preset_1: IntProperty(name="FPS 1", default=5, min=1, max=1000, description='Playback frame rate assigned to the first timeline shortcut')
    fps_preset_2: IntProperty(name="FPS 2", default=15, min=1, max=1000, description='Playback frame rate assigned to the second timeline shortcut')
    fps_preset_3: IntProperty(name="FPS 3", default=30, min=1, max=1000, description='Playback frame rate assigned to the third timeline shortcut')
    fps_preset_4: IntProperty(name="FPS 4", default=60, min=1, max=1000, description='Playback frame rate assigned to the fourth timeline shortcut')

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
        box.label(text="Model Workflow Folders")
        box.prop(self, 'default_vanilla_nusktb_folder')
        box.prop(self, 'default_model_export_folder')
        box.label(text="Match the imported model's source folder to its export folder.")
        box.operator('sub.model_export_path', text='Add Model Folder', icon='ADD')
        for index, item in enumerate(self.model_export_paths):
            entry = box.box()
            entry.prop(item, 'model_folder')
            row = entry.row()
            row.prop(item, 'export_folder')
            row.operator('sub.model_export_path', text='', icon='X').index = index

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
    SUB_PG_model_export_path,
    SUB_OP_model_export_path,
    SUB_AddonPreferences,
)


def get_addon_preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_MODULE_NAME)
    return addon.preferences if addon is not None else None


def model_source_folder(obj):
    if obj is None:
        return ''
    return obj.get('sub_smash_model_folder', '') or obj.data.get('sub_smash_model_folder', '')


def model_export_folder(obj, context=None):
    prefs = get_addon_preferences(context)
    if prefs is None:
        return ''
    def normalized(path):
        return os.path.normcase(os.path.normpath(bpy.path.abspath(path)))
    source = model_source_folder(obj)
    if source:
        for item in prefs.model_export_paths:
            if item.model_folder and item.export_folder and normalized(item.model_folder) == normalized(source):
                return bpy.path.abspath(item.export_folder)
    return bpy.path.abspath(prefs.default_model_export_folder) if prefs.default_model_export_folder else ''


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
