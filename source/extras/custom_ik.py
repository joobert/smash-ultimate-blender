"""User-defined parent-chain IK and portable JSON presets."""
import json
from pathlib import Path

import bpy

from . import ik_channels
from .create_animation_rig import find_target_armature


def preset_dir():
    return Path(bpy.utils.user_resource('SCRIPTS', path='presets/smash_custom_ik', create=True))


class SUB_PG_custom_ik(bpy.types.PropertyGroup):
    expanded: bpy.props.BoolProperty(default=False)
    name: bpy.props.StringProperty(name='Setup Name', default='Custom Limb')
    root: bpy.props.StringProperty(name='Root')
    middle: bpy.props.StringProperty(name='Bend Bone')
    end: bpy.props.StringProperty(name='End Bone')
    kind: bpy.props.EnumProperty(
        name='Switch Group',
        items=[('ARMS', 'Arms', ''), ('LEGS', 'Legs', '')],
    )


def definition(settings):
    return {key: getattr(settings, key) for key in ('name', 'root', 'middle', 'end', 'kind')}


class SUB_OP_custom_ik_create(bpy.types.Operator):
    bl_idname = 'sub.custom_ik_create'
    bl_label = 'Create Custom IK'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = find_target_armature(context)
        settings = context.scene.sub_custom_ik
        if obj is None:
            self.report({'ERROR'}, 'Select an armature')
            return {'CANCELLED'}
        names = ik_channels.chain_path(obj, settings.root, settings.end, settings.middle)
        if not names:
            self.report({'ERROR'}, 'Choose a root, bend and end on one parent chain, in that order')
            return {'CANCELLED'}
        try:
            records = json.loads(obj.get('sub_custom_ik_chains', '[]'))
        except (TypeError, ValueError):
            records = []
        for _, existing, _, _ in ik_channels.chains(obj):
            if set(names) & set(existing):
                self.report({'ERROR'}, 'This chain overlaps existing IK; remove that setup first')
                return {'CANCELLED'}
        tag = bpy.path.clean_name(settings.name.strip())[:32] or 'Custom'
        target = 'SUB_Custom_' + tag + '_Target'
        pole = 'SUB_Custom_' + tag + '_Pole'
        if target in obj.data.bones or pole in obj.data.bones:
            self.report({'ERROR'}, 'Controls with this setup name already exist; choose another name')
            return {'CANCELLED'}
        record = definition(settings)
        record.update(target=target, pole=pole)
        records.append(record)
        obj['sub_custom_ik_chains'] = json.dumps(records)
        try:
            ik_channels.create_controls(context, obj, settings.kind, custom_only=True)
        except Exception:
            records.pop()
            obj['sub_custom_ik_chains'] = json.dumps(records)
            raise
        self.report({'INFO'}, f'Created IK with {len(names) - 1} segments; uses the {settings.kind.lower()} IK/FK switch')
        return {'FINISHED'}


class SUB_OP_custom_ik_save(bpy.types.Operator):
    bl_idname = 'sub.custom_ik_save'
    bl_label = 'Save Custom IK Preset'

    def execute(self, context):
        settings = context.scene.sub_custom_ik
        name = bpy.path.clean_name(settings.name.strip())[:64]
        if not name:
            self.report({'ERROR'}, 'Enter a setup name')
            return {'CANCELLED'}
        path = preset_dir() / (name + '.json')
        try:
            path.write_text(json.dumps({'version': 1, **definition(settings)}, indent=2), encoding='utf-8')
        except OSError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Saved {path}')
        return {'FINISHED'}


class SUB_OP_custom_ik_load(bpy.types.Operator):
    bl_idname = 'sub.custom_ik_load'
    bl_label = 'Load Custom IK Preset'
    bl_options = {'UNDO'}

    filename: bpy.props.StringProperty()

    def execute(self, context):
        try:
            if Path(self.filename).name != self.filename:
                raise ValueError('Invalid preset filename')
            data = json.loads((preset_dir() / self.filename).read_text(encoding='utf-8'))
            if data.get('version') != 1 or data.get('kind') not in {'ARMS', 'LEGS'}:
                raise ValueError('Unsupported preset')
            if not all(isinstance(data.get(key), str) for key in ('name', 'root', 'middle', 'end')):
                raise ValueError('Invalid bone names')
            for key in ('name', 'root', 'middle', 'end', 'kind'):
                setattr(context.scene.sub_custom_ik, key, data[key])
        except (OSError, ValueError, TypeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class SUB_MT_custom_ik_presets(bpy.types.Menu):
    bl_label = 'Custom IK Presets'
    bl_idname = 'SUB_MT_custom_ik_presets'

    def draw(self, context):
        files = sorted(preset_dir().glob('*.json'))
        for path in files:
            self.layout.operator('sub.custom_ik_load', text=path.stem).filename = path.name
        if not files:
            self.layout.label(text='No saved presets')


def draw(layout, context, obj):
    settings = context.scene.sub_custom_ik
    box = layout.box()
    row = box.row()
    row.prop(
        settings,
        'expanded',
        text='Custom IK Bones',
        icon='TRIA_DOWN' if settings.expanded else 'TRIA_RIGHT',
        emboss=False,
    )
    if not settings.expanded:
        return
    box.prop(settings, 'name')
    if obj:
        for key in ('root', 'middle', 'end'):
            box.prop_search(settings, key, obj.data, 'bones')
    box.prop(settings, 'kind')
    box.operator('sub.custom_ik_create')
    row = box.row(align=True)
    row.operator('sub.custom_ik_save', text='Save Preset')
    row.menu('SUB_MT_custom_ik_presets', text='Load Preset')


CLASSES = (
    SUB_PG_custom_ik,
    SUB_OP_custom_ik_create,
    SUB_OP_custom_ik_save,
    SUB_OP_custom_ik_load,
    SUB_MT_custom_ik_presets,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sub_custom_ik = bpy.props.PointerProperty(type=SUB_PG_custom_ik)


def unregister():
    del bpy.types.Scene.sub_custom_ik
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
