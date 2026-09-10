"""Blender UI for the Smash Export Doctor: state, operators and the panel.

``core``/``checks`` know nothing about Blender's UI. This module mirrors their
plain results into a PropertyGroup collection so a UIList can draw them, turns
clicking a row into a selection, and wires the preflight run into both
exporters.
"""

import json

import bpy

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from . import checks  # noqa: F401 - importing registers every check and fix
from .core import (
    ERROR,
    INFO,
    SCOPE_ANIM,
    SCOPE_MODEL,
    TARGET_ACTION,
    TARGET_BONE,
    TARGET_MATERIAL,
    TARGET_OBJECT,
    TARGET_PATH,
    WARNING,
    DoctorResult,
    blocking_results,
    counts,
    run_checks,
    run_fix,
)


PANEL_ID = 'SUB_PT_smash_export_doctor'

# Guards the active_index update callback so selecting from code does not
# re-enter it while the UIList is still settling.
_suppress_select = False


class SUB_PG_doctor_result(PropertyGroup):
    """One finding, stored so the panel survives a redraw and a file save."""

    check_id: StringProperty()
    severity: StringProperty(default=INFO)
    message: StringProperty()
    detail: StringProperty()
    target_type: StringProperty(default='NONE')
    target_name: StringProperty()
    sub_target: StringProperty()
    blocking: BoolProperty(default=False)
    fix_id: StringProperty()
    fix_label: StringProperty()
    fix_is_safe: BoolProperty(default=False)
    payload_json: StringProperty(default='{}')

    def to_result(self) -> DoctorResult:
        try:
            payload = json.loads(self.payload_json or '{}')
        except ValueError:
            payload = {}
        return DoctorResult(
            check_id=self.check_id,
            severity=self.severity,
            message=self.message,
            detail=self.detail,
            target_type=self.target_type,
            target_name=self.target_name,
            sub_target=self.sub_target,
            blocking=self.blocking,
            fix_id=self.fix_id,
            fix_label=self.fix_label,
            fix_is_safe=self.fix_is_safe,
            payload=payload,
        )


def _on_active_index_change(self, context):
    """Clicking a row selects whatever the result blames."""
    if _suppress_select:
        return
    if not (0 <= self.active_index < len(self.results)):
        return
    try:
        select_result_target(context, self.results[self.active_index])
    except Exception as error:
        print(f'[Smash Export Doctor] Could not select the result target: {error}')


class SUB_PG_doctor_state(PropertyGroup):
    results: CollectionProperty(type=SUB_PG_doctor_result)
    active_index: IntProperty(
        name='Active Result',
        default=0,
        update=_on_active_index_change,
    )
    has_run: BoolProperty(default=False)
    last_scope: StringProperty(default='ALL')
    last_summary: StringProperty(default='')

    run_before_export: BoolProperty(
        name='Run Before Export',
        description='Run the matching checks automatically when a model or animation is exported',
        default=True,
    )
    block_on_errors: BoolProperty(
        name='Block Invalid Exports',
        description=(
            'Cancel the export when a check finds output that would be genuinely invalid. '
            'Wrong-looking but valid output is always reported and never blocked'
        ),
        default=True,
    )
    show_errors: BoolProperty(name='Errors', default=True)
    show_warnings: BoolProperty(name='Warnings', default=True)
    show_info: BoolProperty(name='Info', default=True)
    settings_expanded: BoolProperty(name='Settings', default=False)


def store_results(state: SUB_PG_doctor_state, results, scope='ALL'):
    """Replace the stored findings with a fresh run."""
    global _suppress_select
    _suppress_select = True
    try:
        state.results.clear()
        for result in results:
            entry = state.results.add()
            entry.check_id = result.check_id
            entry.severity = result.severity
            entry.message = result.message
            entry.detail = result.detail
            entry.target_type = result.target_type
            entry.target_name = result.target_name
            entry.sub_target = result.sub_target
            entry.blocking = result.blocking
            entry.fix_id = result.fix_id if result.fixable else ''
            entry.fix_label = result.fix_label
            entry.fix_is_safe = result.fix_is_safe
            entry.payload_json = json.dumps(result.payload)
        state.active_index = 0
        state.has_run = True
        state.last_scope = scope
        tally = counts(results)
        if not results:
            state.last_summary = 'No issues found.'
        else:
            state.last_summary = (
                f'{tally[ERROR]} errors, {tally[WARNING]} warnings, {tally[INFO]} informational'
            )
    finally:
        _suppress_select = False


# ---------------------------------------------------------------------------
# Selecting what a result blames
# ---------------------------------------------------------------------------


def _activate_object(context, obj):
    if obj is None or obj.name not in context.view_layer.objects:
        return False
    if context.mode not in {'OBJECT', 'POSE'}:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass
    for other in context.view_layer.objects:
        try:
            other.select_set(False)
        except RuntimeError:
            pass
    try:
        obj.select_set(True)
    except RuntimeError:
        pass
    context.view_layer.objects.active = obj
    return True


def _object_using_material(material_name):
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            if slot.material is not None and slot.material.name == material_name:
                return obj
    return None


def select_result_target(context, entry):
    """Select the object, material, bone or action a result points at."""
    target_type = entry.target_type

    if target_type == TARGET_MATERIAL:
        material_name = entry.sub_target
        obj = bpy.data.objects.get(entry.target_name) or _object_using_material(material_name)
        if not _activate_object(context, obj):
            return
        for index, slot in enumerate(obj.material_slots):
            if slot.material is not None and slot.material.name == material_name:
                obj.active_material_index = index
                break
        return

    if target_type == TARGET_BONE:
        from ..blender_compat import set_pose_bone_select

        armature = bpy.data.objects.get(entry.target_name)
        if not _activate_object(context, armature) or armature.type != 'ARMATURE':
            return
        bone = armature.data.bones.get(entry.sub_target)
        if bone is None:
            return
        # Bone.select is gone on Blender 5, so go through the compat helper.
        for pose_bone in armature.pose.bones:
            set_pose_bone_select(pose_bone, False)
        set_pose_bone_select(armature.pose.bones.get(entry.sub_target), True)
        armature.data.bones.active = bone
        return

    if target_type == TARGET_ACTION:
        owner = bpy.data.objects.get(entry.target_name)
        action = bpy.data.actions.get(entry.sub_target)
        if not _activate_object(context, owner) or action is None:
            return
        from ..blender_compat import assign_action

        if owner.animation_data is None:
            owner.animation_data_create()
        if owner.animation_data.action is not action:
            assign_action(owner.animation_data, action)
        return

    if target_type == TARGET_OBJECT:
        _activate_object(context, bpy.data.objects.get(entry.target_name))
        return

    # TARGET_PATH and TARGET_NONE have nothing in the scene to select.


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


SCOPE_ITEMS = (
    ('ALL', 'Everything', 'Run both the model and the animation checks'),
    ('MODEL', 'Model', 'Run the model export checks only'),
    ('ANIM', 'Animation', 'Run the animation export checks only'),
)


def _scopes_for(scope):
    if scope == 'MODEL':
        return (SCOPE_MODEL,)
    if scope == 'ANIM':
        return (SCOPE_ANIM,)
    return (SCOPE_MODEL, SCOPE_ANIM)


class SUB_OP_doctor_run(Operator):
    bl_idname = 'sub.doctor_run'
    bl_label = 'Run Checks'
    bl_description = 'Check the scene for problems that break a Smash model or animation export'
    bl_options = {'REGISTER'}

    scope: EnumProperty(
        name='Scope',
        items=SCOPE_ITEMS,
        default='ALL',
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        state = context.scene.sub_doctor
        results = run_checks(context, _scopes_for(self.scope))
        store_results(state, results, self.scope)
        tally = counts(results)
        if not results:
            self.report({'INFO'}, 'Smash Export Doctor: no issues found.')
        else:
            self.report(
                {'WARNING'} if tally[ERROR] else {'INFO'},
                f'Smash Export Doctor: {state.last_summary}.',
            )
        return {'FINISHED'}


class SUB_OP_doctor_clear(Operator):
    bl_idname = 'sub.doctor_clear'
    bl_label = 'Clear Results'
    bl_description = 'Discard the current results'
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.sub_doctor
        store_results(state, [])
        state.has_run = False
        state.last_summary = ''
        return {'FINISHED'}


class SUB_OP_doctor_select_result(Operator):
    bl_idname = 'sub.doctor_select_result'
    bl_label = 'Select Responsible Data'
    bl_description = 'Select the object, material, bone or action this result blames'
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        state = context.scene.sub_doctor
        index = self.index if self.index >= 0 else state.active_index
        if not (0 <= index < len(state.results)):
            return {'CANCELLED'}
        global _suppress_select
        _suppress_select = True
        try:
            state.active_index = index
        finally:
            _suppress_select = False
        select_result_target(context, state.results[index])
        return {'FINISHED'}


class SUB_OP_doctor_fix_result(Operator):
    bl_idname = 'sub.doctor_fix_result'
    bl_label = 'Fix This Issue'
    bl_description = 'Apply this result\'s fix, then re-run the checks'
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        state = context.scene.sub_doctor
        index = self.index if self.index >= 0 else state.active_index
        if not (0 <= index < len(state.results)):
            return {'CANCELLED'}

        entry = state.results[index]
        if not entry.fix_id:
            self.report({'WARNING'}, 'That result has no automatic fix.')
            return {'CANCELLED'}

        scope = state.last_scope
        ok, message = run_fix(context, entry.to_result())
        self.report({'INFO'} if ok else {'ERROR'}, message)

        store_results(context.scene.sub_doctor, run_checks(context, _scopes_for(scope)), scope)
        return {'FINISHED'} if ok else {'CANCELLED'}


class SUB_OP_doctor_fix_safe(Operator):
    bl_idname = 'sub.doctor_fix_safe'
    bl_label = 'Fix Safe Issues'
    bl_description = (
        'Apply every fix that cannot lose work: adding missing attributes, normalizing and '
        'limiting weights, applying transforms, repairing modifiers, slots and names'
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        state = context.scene.sub_doctor
        scope = state.last_scope
        pending = [
            entry.to_result() for entry in state.results
            if entry.fix_id and entry.fix_is_safe
        ]
        if not pending:
            self.report({'INFO'}, 'Nothing here can be fixed safely.')
            return {'CANCELLED'}

        fixed = 0
        failures = []
        for result in pending:
            ok, message = run_fix(context, result)
            if ok:
                fixed += 1
            else:
                failures.append(message)

        store_results(context.scene.sub_doctor, run_checks(context, _scopes_for(scope)), scope)

        if failures:
            self.report(
                {'WARNING'},
                f'Fixed {fixed} of {len(pending)} issues. First failure: {failures[0]}',
            )
        else:
            self.report({'INFO'}, f'Fixed {fixed} issues.')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class SUB_UL_doctor_results(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text='', icon=_severity_icon(item))
            return

        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.alert = item.severity == ERROR
        sub.label(text='', icon=_severity_icon(item))
        sub.label(text=item.message)

        if item.fix_id:
            fix = row.operator(
                SUB_OP_doctor_fix_result.bl_idname,
                text='',
                icon='CHECKMARK' if item.fix_is_safe else 'TOOL_SETTINGS',
                emboss=False,
            )
            fix.index = index

    def filter_items(self, context, data, propname):
        state = context.scene.sub_doctor
        items = getattr(data, propname)
        allowed = set()
        if state.show_errors:
            allowed.add(ERROR)
        if state.show_warnings:
            allowed.add(WARNING)
        if state.show_info:
            allowed.add(INFO)

        flags = [
            self.bitflag_filter_item if item.severity in allowed else 0
            for item in items
        ]
        return flags, []


def _severity_icon(item):
    if item.severity == ERROR:
        return 'CANCEL' if item.blocking else 'ERROR'
    if item.severity == WARNING:
        return 'ERROR'
    return 'INFO'


class SUB_PT_smash_export_doctor(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Export Doctor'
    bl_idname = PANEL_ID
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return getattr(context.scene, 'sub_doctor', None) is not None

    def draw(self, context):
        self.layout.use_property_decorate = False
        layout = self.layout
        layout.use_property_split = False
        state = context.scene.sub_doctor

        row = layout.row(align=True)
        row.scale_y = 1.0
        row.operator(SUB_OP_doctor_run.bl_idname, icon='VIEWZOOM', text='Run Checks').scope = 'ALL'
        run_row = layout.row(align=True)
        run_row.operator(SUB_OP_doctor_run.bl_idname, text='Model Only').scope = 'MODEL'
        run_row.operator(SUB_OP_doctor_run.bl_idname, text='Animation Only').scope = 'ANIM'

        if not state.has_run:
            layout.label(text='Run the checks before exporting.', icon='INFO')
            self._draw_settings(layout, state)
            return

        tally = {ERROR: 0, WARNING: 0, INFO: 0}
        safe_fixes = 0
        blocking = 0
        for entry in state.results:
            if entry.severity in tally:
                tally[entry.severity] += 1
            if entry.fix_id and entry.fix_is_safe:
                safe_fixes += 1
            if entry.blocking and entry.severity == ERROR:
                blocking += 1

        summary = layout.row(align=True)
        summary.prop(state, 'show_errors', text=f'{tally[ERROR]}', icon='CANCEL', toggle=True)
        summary.prop(state, 'show_warnings', text=f'{tally[WARNING]}', icon='ERROR', toggle=True)
        summary.prop(state, 'show_info', text=f'{tally[INFO]}', icon='INFO', toggle=True)
        summary.operator(SUB_OP_doctor_clear.bl_idname, text='', icon='X')

        if len(state.results) == 0:
            layout.label(text='No issues found.', icon='CHECKMARK')
            self._draw_settings(layout, state)
            return

        if blocking:
            box = layout.box()
            box.alert = True
            box.label(
                text=f'{blocking} issues would produce an invalid export.',
                icon='CANCEL',
            )

        layout.template_list(
            'SUB_UL_doctor_results', '',
            state, 'results',
            state, 'active_index',
            rows=8,
        )

        if safe_fixes:
            fix_row = layout.row()
            fix_row.scale_y = 1.0
            fix_row.operator(
                SUB_OP_doctor_fix_safe.bl_idname,
                icon='CHECKMARK',
                text=f'Fix Safe Issues ({safe_fixes})',
            )

        if 0 <= state.active_index < len(state.results):
            entry = state.results[state.active_index]
            box = layout.box()
            header = box.row()
            header.alert = entry.severity == ERROR
            header.label(text=entry.message, icon=_severity_icon(entry))
            for line in _wrap(entry.detail, 58):
                box.label(text=line)

            actions = box.row(align=True)
            if entry.target_type not in {'NONE', TARGET_PATH}:
                actions.operator(
                    SUB_OP_doctor_select_result.bl_idname,
                    icon='RESTRICT_SELECT_OFF',
                    text='Select',
                ).index = state.active_index
            if entry.fix_id:
                actions.operator(
                    SUB_OP_doctor_fix_result.bl_idname,
                    icon='CHECKMARK' if entry.fix_is_safe else 'TOOL_SETTINGS',
                    text=entry.fix_label or 'Fix',
                ).index = state.active_index

        self._draw_settings(layout, state)

    def _draw_settings(self, layout, state):
        box = layout.box()
        header = box.row()
        header.prop(
            state,
            'settings_expanded',
            icon='TRIA_DOWN' if state.settings_expanded else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        header.label(text='Preflight Settings')
        if not state.settings_expanded:
            return
        box.prop(state, 'run_before_export')
        row = box.row()
        row.enabled = state.run_before_export
        row.prop(state, 'block_on_errors')
        box.label(text='Only genuinely invalid output blocks an export.', icon='INFO')
        box.label(text='Wrong-looking but valid output is reported and lets you through.')

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


def _wrap(text, width):
    """Break detail text into panel-width lines without a textwrap dependency."""
    if not text:
        return []
    lines = []
    current = ''
    for word in text.split():
        candidate = f'{current} {word}'.strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Preflight, called by the exporters
# ---------------------------------------------------------------------------


def preflight(context, operator, scope='ALL') -> bool:
    """Run the checks before an export. Returns False when the export should stop.

    Blocking is deliberately narrow: only results a check marked as producing
    genuinely invalid output can cancel an export, and only when the user left
    "Block Invalid Exports" on.
    """
    state = getattr(context.scene, 'sub_doctor', None)
    if state is None or not state.run_before_export:
        return True

    results = run_checks(context, _scopes_for(scope))
    store_results(state, results, scope)

    if not results:
        return True

    tally = counts(results)
    blocking = blocking_results(results)

    if blocking and state.block_on_errors:
        operator.report(
            {'ERROR'},
            f'Export Doctor stopped the export: {len(blocking)} issues would produce invalid '
            f'output. See the Export Doctor panel.',
        )
        for result in blocking[:5]:
            operator.report({'ERROR'}, result.message)
        if len(blocking) > 5:
            operator.report({'ERROR'}, f'...and {len(blocking) - 5} more.')
        return False

    if tally[ERROR] or tally[WARNING]:
        operator.report(
            {'WARNING'},
            f'Export Doctor: {tally[ERROR]} errors, {tally[WARNING]} warnings. '
            'See the Export Doctor panel.',
        )
    return True


classes = (
    SUB_PG_doctor_result,
    SUB_PG_doctor_state,
    SUB_UL_doctor_results,
    SUB_OP_doctor_run,
    SUB_OP_doctor_clear,
    SUB_OP_doctor_select_result,
    SUB_OP_doctor_fix_result,
    SUB_OP_doctor_fix_safe,
    SUB_PT_smash_export_doctor,
)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    if not hasattr(bpy.types.Scene, 'sub_doctor'):
        bpy.types.Scene.sub_doctor = bpy.props.PointerProperty(type=SUB_PG_doctor_state)


def unregister():
    if hasattr(bpy.types.Scene, 'sub_doctor'):
        del bpy.types.Scene.sub_doctor
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
