"""Per-animation motion-list controls, shown under Ultimate Animation Data."""
from pathlib import Path

import bpy
from bpy.props import BoolProperty, IntProperty, PointerProperty, StringProperty
from . import hash_labels, motion_list


def _mark_synced(self, context):
    self.synced = True


def _reset_auto_sync(self, context):
    """Re-arm the tracker so toggling on syncs the current action right away."""
    _auto_synced_actions.clear()


def _set_override_path(self, context):
    """Seed the override field with the path auto-detection currently uses."""
    if not self.override_path:
        return
    paths = motion_list.discover(_probe_path(context))
    if paths:
        self.filepath = str(paths[0].resolve())


# Actions already auto-synced this session, keyed by armature name.
_auto_synced_actions = {}


class SUB_PG_action_motion(bpy.types.PropertyGroup):
    """Per-animation motion values, stored on the action like its pose markers."""
    blend_frames: IntProperty(name='Blend Frames', min=0, max=255, update=_mark_synced,
        description='Number of frames used to blend into this motion')
    flag_turn: BoolProperty(name='Turn', update=_mark_synced,
        description='Enable the motion turn flag')
    flag_loop: BoolProperty(name='Loop', update=_mark_synced,
        description='Enable looping for this motion')
    flag_move: BoolProperty(name='Move', update=_mark_synced,
        description='Enable the motion movement flag')
    synced: BoolProperty(name='Synced',
        description='These values came from the motion list or were edited, so export may write them')


class SUB_PG_motion_list(bpy.types.PropertyGroup):
    enabled: BoolProperty(name='Update Motion List on Export', default=False,
        description='Update the matching motion only after its animation is saved successfully')
    filepath: StringProperty(name='Motion List', subtype='FILE_PATH',
        description='Choose motion_list.bin, .yml, or .yaml; leave empty to search beside the animation and up to /motion')
    override_path: BoolProperty(name='Override Path',
        description='Choose the motion list by hand instead of detecting it beside the animation',
        update=_set_override_path)
    auto_sync: BoolProperty(name='Motion List Auto-Sync', default=True,
        description='Load the matching motion-list entry automatically whenever the active action changes',
        update=_reset_auto_sync)
    status: StringProperty(name='Loaded Entry',
        description='Most recently loaded motion-list entry and frame values')
    status_motion: StringProperty(name='Motion',
        description='Name of the most recently loaded motion-list entry')
    status_source: StringProperty(name='Source',
        description='File the most recent entry was read from')
    status_cancel: StringProperty(name='Cancel Frame',
        description='Cancel frame stored in the most recent entry')
    status_blend: StringProperty(name='Blend Frames',
        description='Blend frames stored in the most recent entry')
    status_flags: StringProperty(name='Flags',
        description='Flags stored in the most recent entry')


CANCEL_FRAME_MARKER = 'Cancel Frame'


def cancel_marker(action):
    if action is None:
        return None
    return action.pose_markers.get(CANCEL_FRAME_MARKER)


def cancel_frame(action):
    """Raw marker frame; no scene start offset is applied."""
    marker = cancel_marker(action)
    return None if marker is None else int(marker.frame)


def active_action(context):
    obj = context.active_object
    if obj is None or obj.animation_data is None:
        return None
    return obj.animation_data.action


def resolve_paths(settings, animation_path):
    """Explicit override targets one file; detection returns every format."""
    if settings.override_path and settings.filepath:
        path = Path(bpy.path.abspath(settings.filepath))
        if not path.is_file():
            raise ValueError(f'Motion list does not exist: {path}')
        return [path]
    paths = motion_list.discover(animation_path)
    if not paths:
        raise ValueError('No motion_list.yml/.yaml/.bin found beside the animation or up to /motion')
    return paths


def prepare(settings, animation_path, action):
    """Validate the edit before the animation is written; commit only after."""
    if not settings or not settings.enabled:
        return None
    paths = resolve_paths(settings, animation_path)
    doc, source, originals = motion_list.load_all(paths)
    cancel = cancel_frame(action)
    values = getattr(action, 'sub_motion', None) if action is not None else None
    blend = values.blend_frames if values is not None and values.synced else None
    flags = None
    if values is not None and values.synced:
        flags = {'turn': values.flag_turn, 'loop': values.flag_loop, 'move': values.flag_move}
    doc = motion_list.update(doc, Path(animation_path).name,
                             cancel=cancel, blend=blend, flags=flags)
    return originals, doc, source


def commit(prepared, operator):
    if prepared is None:
        return
    originals, doc, source = prepared
    motion_list.save_all(originals, doc)
    if operator is not None:
        names = ', '.join(sorted(path.name for path in originals))
        operator.report({'INFO'}, f'Updated {names} from {source.name}; originals backed up as .bak')


class SUB_OP_set_cancel_frame_marker(bpy.types.Operator):
    bl_idname = 'sub.set_cancel_frame_marker'
    bl_label = 'Set Cancel Marker'
    bl_description = 'Place the cancel frame marker on the active action at the current frame'

    @classmethod
    def poll(cls, context):
        return active_action(context) is not None

    def execute(self, context):
        action = active_action(context)
        marker = cancel_marker(action)
        if marker is None:
            marker = action.pose_markers.new(CANCEL_FRAME_MARKER)
        marker.frame = context.scene.frame_current
        self.report({'INFO'}, f'Cancel frame marker at {marker.frame}')
        return {'FINISHED'}


class SUB_OP_clear_cancel_frame_marker(bpy.types.Operator):
    bl_idname = 'sub.clear_cancel_frame_marker'
    bl_label = 'Clear Cancel Marker'
    bl_description = 'Remove the cancel frame marker so export keeps the existing cancel frame'

    @classmethod
    def poll(cls, context):
        return cancel_marker(active_action(context)) is not None

    def execute(self, context):
        action = active_action(context)
        action.pose_markers.remove(cancel_marker(action))
        return {'FINISHED'}


def load_into_action(context):
    """Read the matching motion-list entry onto the active action.

    Raises ValueError when the action, the file, or the entry cannot be resolved.
    """
    settings = context.scene.sub_motion_list
    _clear_status(settings)
    action = active_action(context)
    if action is None:
        raise ValueError('Select an object with an active animation')
    from .export_anim import ensure_nuanmb_filename, sanitize_filename
    name = ensure_nuanmb_filename(sanitize_filename(action.name))
    ssp = context.scene.sub_scene_properties
    folder = ssp.animation_import_folder_path or ssp.last_anim_export_dir or ssp.last_anim_import_dir
    paths = resolve_paths(settings, Path(bpy.path.abspath(folder)) / name)
    doc, source, _ = motion_list.load_all(paths)
    keys = motion_list.matching_keys(doc, name)
    if len(keys) != 1:
        raise ValueError(f'Expected one motion referencing {name}; found {len(keys)}')
    entry = doc['list'][keys[0]]
    values = action.sub_motion
    values.blend_frames = entry['blend_frames']
    values.flag_turn = entry['flags'].get('turn', False)
    values.flag_loop = entry['flags'].get('loop', False)
    values.flag_move = entry['flags'].get('move', False)
    values.synced = True
    extra = entry.get('extra')
    cancel = str(extra['cancel_frame']) if extra else 'not available'
    on = [label for label, flag in (('Turn', values.flag_turn), ('Loop', values.flag_loop),
                                    ('Move', values.flag_move)) if flag]
    settings.status_motion = hash_labels.label(keys[0])
    settings.status_source = source.name
    settings.status_cancel = cancel
    settings.status_blend = str(values.blend_frames)
    settings.status_flags = ', '.join(on) if on else 'none'
    settings.status = (f'{settings.status_motion} in {source.name}: '
                       f'cancel {cancel}, blend {values.blend_frames}')
    return settings.status


def _clear_status(settings):
    settings.status = ''
    settings.status_motion = ''
    settings.status_source = ''
    settings.status_cancel = ''
    settings.status_blend = ''
    settings.status_flags = ''


def auto_sync_active_action(context):
    """Best-effort sync used by the action-change handlers; never raises or reports."""
    try:
        settings = getattr(context.scene, 'sub_motion_list', None)
        if settings is None or not settings.auto_sync:
            return
        obj = context.active_object
        action = active_action(context)
        if obj is None or action is None:
            return
        if _auto_synced_actions.get(obj.name) == action.name:
            return
        _auto_synced_actions[obj.name] = action.name
        try:
            load_into_action(context)
        except Exception:
            # A missing motion list is normal while animating; stay quiet.
            _clear_status(settings)
    except Exception:
        pass


class SUB_OP_load_motion_list(bpy.types.Operator):
    bl_idname = 'sub.load_motion_list'
    bl_label = 'Sync From Motion List'
    bl_description = 'Load the matching entry blend frames and flags onto the active action'

    def execute(self, context):
        try:
            self.report({'INFO'}, load_into_action(context))
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


def _probe_path(context):
    """Stand-in animation path used to detect the motion list before an export."""
    ssp = context.scene.sub_scene_properties
    folder = ssp.animation_import_folder_path or ssp.last_anim_export_dir or ssp.last_anim_import_dir
    return Path(bpy.path.abspath(folder or '//')) / 'probe.nuanmb'


def _entry(grid, label, value):
    """One label/value line: label flush left, value anchored to the right edge."""
    split = grid.row(align=True).split(factor=0.35)
    left = split.row()
    left.alignment = 'LEFT'
    left.label(text=label)
    right = split.row()
    right.alignment = 'RIGHT'
    right.label(text=value)


def _draw_entry_box(layout, context, settings):
    """The values last read from the motion list, then how that file is chosen."""
    box = layout.box()
    grid = box.column(align=True)
    problem = ''
    try:
        resolve_paths(settings, _probe_path(context))
    except Exception as error:
        problem = str(error)

    if settings.status_motion:
        _entry(grid, 'Motion', settings.status_motion)
        _entry(grid, 'Cancel frame', settings.status_cancel)
        _entry(grid, 'Blend frames', settings.status_blend)
        _entry(grid, 'Flags', settings.status_flags)
    elif not problem:
        grid.label(text='No entry read yet.', icon='INFO')
    if problem:
        grid.label(text=problem, icon='ERROR')

    box.separator()
    box.prop(settings, 'override_path')
    if not settings.override_path:
        return
    box.prop(settings, 'filepath', text='')
    note = box.column(align=True)
    note.scale_y = 0.85
    note.label(text='Only this file is written; siblings are untouched.', icon='INFO')


class SUB_PT_motion_list(bpy.types.Panel):
    bl_label = 'Ultimate Motion List'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'data'
    bl_options = {'DEFAULT_CLOSED'}
    bl_parent_id = 'SUB_PT_sub_smush_anim_data_main'

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        settings = context.scene.sub_motion_list
        layout.prop(settings, 'enabled')

        _draw_entry_box(layout, context, settings)

        action = active_action(context)
        if action is None:
            layout.label(text='No active action on this armature.', icon='INFO')
        else:
            column = layout.column()
            column.enabled = settings.enabled
            frame = cancel_frame(action)
            if frame is None:
                column.label(text='No cancel marker - entry value kept', icon='MARKER')
            elif 0 <= frame <= 255:
                column.label(text=f'Cancel frame: {frame} (marker)', icon='MARKER_HLT')
            else:
                column.label(text=f'Cancel frame {frame} is outside 0-255', icon='ERROR')
            column.use_property_split = True
            row = column.row(align=True)
            row.operator('sub.set_cancel_frame_marker', icon='MARKER_HLT')
            row.operator('sub.clear_cancel_frame_marker', icon='X')
            values = action.sub_motion
            column.prop(values, 'blend_frames')
            flags = column.row(align=True)
            flags.prop(values, 'flag_turn', toggle=True)
            flags.prop(values, 'flag_loop', toggle=True)
            flags.prop(values, 'flag_move', toggle=True)
            if not values.synced:
                column.label(text='Not synced; export keeps the entry values.', icon='INFO')

        layout.operator('sub.load_motion_list', icon='FILE_REFRESH')



classes = (SUB_PG_action_motion, SUB_PG_motion_list, SUB_OP_set_cancel_frame_marker,
           SUB_OP_clear_cancel_frame_marker, SUB_OP_load_motion_list, SUB_PT_motion_list)


@bpy.app.handlers.persistent
def motion_list_auto_sync_handler(scene, depsgraph=None):
    """Runs independently of SAP auto-sync; it no-ops unless the action changed."""
    auto_sync_active_action(bpy.context)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sub_motion_list = PointerProperty(type=SUB_PG_motion_list)
    # Per-animation values live on the action, beside its cancel-frame pose marker.
    bpy.types.Action.sub_motion = PointerProperty(type=SUB_PG_action_motion)
    handlers = bpy.app.handlers.depsgraph_update_post
    if motion_list_auto_sync_handler not in handlers:
        handlers.append(motion_list_auto_sync_handler)


def unregister():
    handlers = bpy.app.handlers.depsgraph_update_post
    if motion_list_auto_sync_handler in handlers:
        handlers.remove(motion_list_auto_sync_handler)
    _auto_synced_actions.clear()
    del bpy.types.Action.sub_motion
    del bpy.types.Scene.sub_motion_list
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
