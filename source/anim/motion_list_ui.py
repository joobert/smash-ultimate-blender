"""Exporter settings and review controls for motion-list updates."""
from pathlib import Path
import shutil

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from . import motion_list


def default_converter():
    path = shutil.which('yamlist')
    if path:
        return path
    path = Path.home() / '.cargo' / 'bin' / 'yamlist.exe'
    return str(path) if path.is_file() else ''


class SUB_PG_motion_list(bpy.types.PropertyGroup):
    enabled: BoolProperty(name='Update Motion List on Export', default=False,
        description='Update the matching motion only after its animation is saved successfully')
    filepath: StringProperty(name='Motion List', subtype='FILE_PATH',
        description='Choose motion_list.bin, .yml, or .yaml; leave empty to search beside the animation and up to /motion')
    converter: StringProperty(name='yamlist', subtype='FILE_PATH', default=default_converter(),
        description='yamlist executable for binary conversion; leave empty to use the bundled binary codec')
    key: StringProperty(name='Motion Key',
        description='Optional move name or Hash40 to disambiguate a shared animation; required when creating a new entry')
    template: StringProperty(name='New Entry Template',
        description='Existing motion key to copy for a new move, including game/effect/sound scripts; review those scripts in the list before use')
    cancel_mode: EnumProperty(name='Cancel Frame', default='LENGTH', items=[
        ('KEEP', 'Keep Existing', 'Preserve the entry cancel frame, including the zero sentinel'),
        ('LENGTH', 'Exported Length', 'Set cancel frame to the exported final frame index (end minus start); must fit 0–255'),
        ('CUSTOM', 'Custom', 'Use the cancel frame entered below')],
        description='Choose how export updates the fighter cancel frame')
    cancel: IntProperty(name='Cancel Frame Value', min=0, max=255,
        description='Fighter cancel frame; zero retains the game-defined sentinel behavior')
    override_blend: BoolProperty(name='Override Blend Frames',
        description='Replace blend frames on exported entries; otherwise preserve their existing values')
    blend: IntProperty(name='Blend Frames', min=0, max=255,
        description='Number of frames used to blend into this motion')
    override_flags: BoolProperty(name='Override Flags',
        description='Replace all fourteen documented motion flags with the values below; otherwise preserve each entry')
    status: StringProperty(name='Loaded Entry', description='Most recently loaded motion-list entry and frame values')


for _flag in motion_list.FLAGS:
    SUB_PG_motion_list.__annotations__['flag_' + _flag] = BoolProperty(
        name=_flag.replace('_', ' ').title(),
        description={
            'turn': 'Enable the motion turn flag', 'loop': 'Enable looping for this motion',
            'move': 'Enable the motion movement flag',
            'fix_trans': 'Enable fixed translation for this motion',
            'fix_rot': 'Enable fixed rotation for this motion',
            'fix_scale': 'Enable fixed scale for this motion',
        }.get(_flag, f'Preserve or set the undocumented game bit {_flag}; its meaning is unknown'))


def resolve_path(settings, animation_path):
    if settings.filepath:
        path = Path(bpy.path.abspath(settings.filepath))
        if not path.is_file():
            raise ValueError(f'Motion list does not exist: {path}')
        return path
    path = motion_list.discover(animation_path)
    if path is None:
        raise ValueError('No motion_list.bin/.yml/.yaml found beside the animation or up to /motion')
    return path


def prepare(settings, animation_path, final_frame):
    if not settings.enabled:
        return None
    path = resolve_path(settings, animation_path)
    expected = path.read_bytes()
    converter = bpy.path.abspath(settings.converter) if settings.converter else ''
    doc = motion_list.load(path, converter)
    cancel = (final_frame if settings.cancel_mode == 'LENGTH' else
              settings.cancel if settings.cancel_mode == 'CUSTOM' else None)
    flags = {flag: getattr(settings, 'flag_' + flag) for flag in motion_list.FLAGS} if settings.override_flags else None
    doc = motion_list.update(doc, Path(animation_path).name, cancel=cancel,
        blend=settings.blend if settings.override_blend else None,
        flags=flags, key=settings.key, template=settings.template)
    return path, doc, expected, converter


def commit(prepared, operator):
    if prepared is None:
        return
    path, doc, expected, converter = prepared
    motion_list.save(path, doc, expected=expected, converter=converter)
    if operator is not None:
        operator.report({'INFO'}, f'Updated {path.name}; original backup: {path.name}.bak')


class SUB_OP_load_motion_list(bpy.types.Operator):
    bl_idname = 'sub.load_motion_list'
    bl_label = 'Read Matching Entry'
    bl_description = 'Find the current animation in the motion list and load its cancel frame, blend frames, and flags for review'

    def execute(self, context):
        settings = context.scene.sub_motion_list
        settings.status = ''
        try:
            obj = context.active_object
            if not obj or not obj.animation_data or not obj.animation_data.action:
                raise ValueError('Select an object with an active animation')
            from .export_anim import ensure_nuanmb_filename, sanitize_filename
            name = ensure_nuanmb_filename(sanitize_filename(obj.animation_data.action.name))
            ssp = context.scene.sub_scene_properties
            folder = ssp.animation_import_folder_path or ssp.last_anim_export_dir or ssp.last_anim_import_dir
            path = resolve_path(settings, Path(bpy.path.abspath(folder)) / name)
            doc = motion_list.load(path, bpy.path.abspath(settings.converter) if settings.converter else '')
            keys = motion_list.matching_keys(doc, name)
            if settings.key:
                keys = [key for key in keys if motion_list.hash40(key) == motion_list.hash40(settings.key)]
            if len(keys) != 1:
                raise ValueError('Expected one matching motion; specify a motion key for shared animations')
            entry = doc['list'][keys[0]]
            settings.blend = entry['blend_frames']
            settings.cancel = (entry.get('extra') or {}).get('cancel_frame', 0)
            for flag in motion_list.FLAGS:
                setattr(settings, 'flag_' + flag, entry['flags'].get(flag, False))
            settings.filepath = str(path)
            key = hex(keys[0]) if isinstance(keys[0], int) else keys[0]
            cancel = settings.cancel if entry.get('extra') is not None else 'not available'
            settings.status = f'{key}: cancel {cancel}, blend {settings.blend}'
            self.report({'INFO'}, settings.status)
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


def draw_settings(layout, context, *, reader=True):
    settings = context.scene.sub_motion_list
    box = layout.box()
    box.use_property_split = True
    box.use_property_decorate = False
    box.prop(settings, 'enabled')
    col = box.column()
    col.prop(settings, 'filepath')
    col.prop(settings, 'converter')
    col.prop(settings, 'key')
    if reader:
        col.operator('sub.load_motion_list', icon='FILE_REFRESH')
    if settings.status:
        col.label(text='Last read: ' + settings.status, icon='INFO')
    col = box.column()
    col.enabled = settings.enabled
    col.prop(settings, 'cancel_mode')
    if settings.cancel_mode == 'CUSTOM':
        col.prop(settings, 'cancel')
    col.prop(settings, 'override_blend')
    if settings.override_blend:
        col.prop(settings, 'blend')
    col.prop(settings, 'override_flags')
    if settings.override_flags:
        grid = col.grid_flow(columns=2, align=True)
        for flag in motion_list.FLAGS:
            grid.prop(settings, 'flag_' + flag)
    col.prop(settings, 'template')
    if settings.template:
        col.label(text='New entries copy template scripts. Review before use.', icon='INFO')


class SUB_PT_motion_list(bpy.types.Panel):
    bl_label = 'Motion List'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_parent_id = 'SUB_PT_export_anim'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        self.layout.use_property_decorate = False
        draw_settings(self.layout, context)

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


classes = (SUB_PG_motion_list, SUB_OP_load_motion_list, SUB_PT_motion_list)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sub_motion_list = PointerProperty(type=SUB_PG_motion_list)


def unregister():
    del bpy.types.Scene.sub_motion_list
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
