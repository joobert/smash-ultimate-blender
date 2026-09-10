"""Guided retargeting wizard and bind/bake helpers."""
import bpy


_REBIND_BUSY = False
_REBIND_TOKEN = 0
_SYNCING_BIND_PROPS = False
_SELECT_TOKEN = 0

PRESET_NO_MESSAGE = (
    "Make a preset first in the Expy Mapping section of the Retargeting tab.\n\n"
    "A preset tells the addon which bone on each armature is which body part. "
    "If ArmR is the Right Arm on rig A and R_Arm is the Right Arm on rig B, "
    "mapping both to the same Right Arm slot will bind those bones together."
)

ANIM_NO_MESSAGE = (
    "Load every animation you want to retarget onto the source armature first, "
    "then start Guided Mode again."
)

CHECK_ANIM_MESSAGE = (
    "Check if the animations look okay on the target armature, then click "
    "Continue Guided Mode."
)


def _invoke_later(op_callable, **kwargs):
    """Invoke another operator after the current dialog closes."""
    def _do():
        try:
            op_callable('INVOKE_DEFAULT', **kwargs)
        except Exception as exc:
            print(f"Guided mode: could not open next prompt: {exc}")
        return None

    bpy.app.timers.register(_do, first_interval=0.05)


def expand_last_operator():
    """Bind settings live on the Retargeting tab. Do not force the HUD.

    Setting SpaceView3D.show_region_hud from a timer crashes Blender 5.2
    (ED_area_init / rna_Space_show_region_hud_update).
    """
    tag_retargeting_redraw()


def apply_root_bind_defaults(scene, source, target, *, force_root_bone=False):
    """Default Root Animation to Bone mode; motion bone from Bind To Root mapping."""
    del source
    if getattr(scene, 'expykit_constrain_root', 'None') in {'None', ''}:
        scene.expykit_constrain_root = 'Bone'
        scene.expykit_root_cp_loc_x = True
        scene.expykit_root_cp_loc_y = True
        scene.expykit_root_cp_loc_z = True
        scene.expykit_root_cp_rot_x = True
        scene.expykit_root_cp_rot_y = True
        scene.expykit_root_cp_rot_z = True
        if hasattr(scene, 'expykit_root_use_loc_min_z'):
            scene.expykit_root_use_loc_min_z = True

    # Root motion bone lives on Bind To (constraints reference this armature).
    # Never invent "Trans" — leave empty when that Root slot is unset.
    root_name = ""
    if target is not None and getattr(target, 'type', None) == 'ARMATURE':
        try:
            root_name = (target.data.expykit_retarget.root or "").strip()
        except Exception:
            root_name = ""

    current = (getattr(scene, 'expykit_root_motion_bone', '') or "").strip()
    if force_root_bone or not current:
        scene.expykit_root_motion_bone = root_name


def view3d_override(context):
    """3D View override so bind operators work when clicked from the N-panel."""
    wm = getattr(context, "window_manager", None)
    if wm is None:
        return None
    scene = getattr(context, "scene", None)
    view_layer = getattr(context, "view_layer", None)
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            return {
                'window': window,
                'screen': screen,
                'area': area,
                'region': region,
                'scene': scene or window.scene,
                'view_layer': view_layer or window.view_layer,
            }
    return None


def bind_keep_target(scene, target=None):
    """The Bind To armature (Smash / target). Never the source animation rig."""
    keep = getattr(scene, 'expykit_bind_to', None)
    if keep is not None and getattr(keep, 'type', None) == 'ARMATURE':
        return keep
    bound_src, bound_trg = bound_pair(scene)
    if bound_trg is not None and bound_trg != bound_src:
        return bound_trg
    if target is not None and getattr(target, 'type', None) == 'ARMATURE':
        return target
    return bound_trg


def bind_keep_drop(scene, source=None, target=None):
    """Keep Bind To. Drop the other armature of the pair."""
    keep = bind_keep_target(scene, target)
    bound_src, bound_trg = bound_pair(scene)
    drop = None
    # Prefer the pair from the bind operation that just completed. The stored
    # pair can belong to an earlier bind and used to make the old source win.
    for ob in (source, target, bound_src, bound_trg):
        if ob is not None and ob != keep:
            drop = ob
            break
    return drop, keep


def select_only_target(context, source, target):
    """Leave only the Bind To / target armature selected in pose mode."""
    scene = getattr(context, 'scene', None) or getattr(bpy.context, 'scene', None)
    keep = bind_keep_target(scene, target) if scene is not None else target
    if not keep:
        return
    drop, _keep = bind_keep_drop(scene, source, keep) if scene is not None else (source, keep)

    _select_only_armature(context, keep, drop)


def select_only_constrained(context, constrained, driver):
    """Leave selected the armature that received the retarget constraints."""
    if constrained:
        _select_only_armature(context, constrained, driver)


def _select_only_armature(context, keep, drop=None):
    """Select one armature reliably across Blender's pose-mode restoration."""

    override = view3d_override(context)
    ctx = context

    def _run():
        c = bpy.context
        view_layer = c.view_layer
        if c.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        keep_name = keep.name
        # Deselect through the view layer, not selected_objects captured while
        # in multi-object pose mode. Blender can retain the previous active
        # source in that collection until the mode switch has fully settled.
        for ob in list(getattr(view_layer, 'objects', []) or []):
            if ob is None:
                continue
            try:
                ob.select_set(False)
            except Exception:
                pass
        if drop is not None and drop.name != keep_name:
            try:
                drop.select_set(False)
            except Exception:
                pass

        try:
            keep.hide_set(False)
            keep.hide_viewport = False
        except Exception:
            pass
        view_layer.objects.active = keep
        try:
            keep.select_set(True)
        except Exception:
            pass
        try:
            bpy.ops.object.mode_set(mode='POSE')
        except Exception:
            pass
        # Some Blender versions restore the former active pose object while
        # entering pose mode. Assert the intended final state once more.
        view_layer.objects.active = keep
        try:
            keep.select_set(True)
        except Exception:
            pass
        if drop is not None and drop.name != keep_name:
            try:
                drop.select_set(False)
            except Exception:
                pass

    if override:
        try:
            with ctx.temp_override(**override):
                _run()
                return
        except Exception:
            pass
    _run()


def schedule_select_only_target(source, target):
    """Dialog close restores the source as active; re-apply after that."""
    global _SELECT_TOKEN
    _SELECT_TOKEN += 1
    token = _SELECT_TOKEN

    def _make():
        def _do():
            if token != _SELECT_TOKEN:
                return None
            try:
                select_only_target(bpy.context, source, target)
            except Exception:
                pass
            return None
        return _do

    for delay in (0.0, 0.08, 0.25):
        try:
            bpy.app.timers.register(_make(), first_interval=delay)
        except Exception:
            pass


def schedule_select_only_constrained(constrained, driver):
    """Reassert constrained-armature selection after a bind dialog closes."""
    global _SELECT_TOKEN
    _SELECT_TOKEN += 1
    token = _SELECT_TOKEN

    def _make():
        def _do():
            if token != _SELECT_TOKEN:
                return None
            try:
                select_only_constrained(bpy.context, constrained, driver)
            except Exception:
                pass
            return None
        return _do

    for delay in (0.0, 0.08, 0.25):
        try:
            bpy.app.timers.register(_make(), first_interval=delay)
        except Exception:
            pass


def clear_bound_constraints(source, target):
    """Remove only constraints created by the previous bind for this pair.

    The original operator's ``remove`` policy only considers constraint types
    that are enabled in the *new* settings. Turning Copy Location/Rotation off
    therefore left the old constraint behind, making Apply Bind Settings look
    like it did nothing.
    """
    if not source or not target:
        return 0
    copy_types = {'COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE'}
    limit_types = {'LIMIT_LOCATION', 'LIMIT_ROTATION', 'LIMIT_SCALE'}
    removed = 0
    settings = getattr(getattr(source, 'data', None), 'expykit_retarget', None)
    root_name = getattr(settings, 'root', '') if settings is not None else ''

    for pbone in getattr(getattr(source, 'pose', None), 'bones', []) or []:
        constraints = getattr(pbone, 'constraints', None)
        if constraints is None:
            continue
        bound_here = any(
            getattr(con, 'type', '') in copy_types
            and getattr(con, 'target', None) == target
            for con in constraints
        )
        for con in reversed(constraints):
            is_pair_copy = (
                getattr(con, 'type', '') in copy_types
                and getattr(con, 'target', None) == target
            )
            # Expy adds root limit constraints alongside the target copy
            # constraint. Remove those as part of replacing that root bind.
            is_root_limit = (
                bound_here
                and bool(root_name)
                and getattr(pbone, 'name', '') == root_name
                and getattr(con, 'type', '') in limit_types
            )
            if not (is_pair_copy or is_root_limit):
                continue
            constraints.remove(con)
            removed += 1

    constraints = getattr(source, 'constraints', None)
    if constraints is not None:
        bound_object = any(
            getattr(con, 'type', '') in copy_types
            and getattr(con, 'target', None) == target
            for con in constraints
        )
        for con in reversed(constraints):
            is_pair_copy = (
                getattr(con, 'type', '') in copy_types
                and getattr(con, 'target', None) == target
            )
            is_root_limit = bound_object and getattr(con, 'type', '') in limit_types
            if not (is_pair_copy or is_root_limit):
                continue
            constraints.remove(con)
            removed += 1
    return removed


def tag_retargeting_redraw():
    wm = getattr(bpy.context, 'window_manager', None)
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, 'screen', None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _invalidate_smash_viewport():
    """Make native model visibility/pose changes effective on the next draw."""
    try:
        from ..extras import smash_viewport
        smash_viewport.on_retarget_bind_changed()
    except Exception:
        try:
            from ..extras import smash_viewport
            smash_viewport.invalidate_animation_state(redraw=True)
        except Exception:
            pass


def iter_source_meshes(source):
    """Meshes driven by or parented to the source armature."""
    if not source:
        return
    for ob in bpy.data.objects:
        if ob == source:
            continue
        if ob.parent == source:
            yield ob
            continue
        for mod in getattr(ob, 'modifiers', []):
            if mod.type == 'ARMATURE' and mod.object == source:
                yield ob
                break


def hide_source_keep_target(context, source, target):
    """Hide the bake driver and its meshes; keep the baked armature selected."""
    if source:
        source.hide_set(True)
        for ob in iter_source_meshes(source):
            ob.hide_set(True)
    _invalidate_smash_viewport()

    # Do not use select_only_target here. That helper intentionally resolves
    # Scene.expykit_bind_to, which is the driver in Expy Kit terminology. The
    # armature receiving the baked actions is ``target`` here.
    select_only_constrained(context, target, source)
    schedule_select_only_constrained(target, source)


def remember_bind_pair(scene, source, target):
    scene.expykit_bound_source = source
    scene.expykit_bound_target = target
    scene.expykit_bind_is_active = True
    _invalidate_smash_viewport()
    tag_retargeting_redraw()


def bound_pair(scene):
    source = getattr(scene, 'expykit_bound_source', None)
    target = getattr(scene, 'expykit_bound_target', None)
    if source and target and source.type == 'ARMATURE' and target.type == 'ARMATURE':
        return source, target
    return None, None


def prepare_bind_selection(context, source, target):
    """Match Bind Armatures selection: both selected, target active, pose mode."""
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

    for ob in list(context.selected_objects):
        ob.select_set(False)

    source.select_set(True)
    target.select_set(True)
    context.view_layer.objects.active = target
    try:
        bpy.ops.object.mode_set(mode='POSE')
    except Exception:
        pass


def reapply_bind_from_scene(context):
    """Re-run bind using the last pair so sidebar edits update the pose."""
    global _REBIND_BUSY, _SYNCING_BIND_PROPS
    if _REBIND_BUSY:
        return False

    scene = context.scene
    source, target = bound_pair(scene)
    if not source or not target:
        return False
    scene.expykit_bind_is_active = True

    _REBIND_BUSY = True
    try:
        clear_bound_constraints(source, target)

        def _run():
            prepare_bind_selection(context, source, target)
            result = bpy.ops.armature.expykit_constrain_to_armature(
                'EXEC_DEFAULT',
                force_dialog=False,
                from_scene=True,
            )
            return result

        override = view3d_override(context)
        if override:
            with context.temp_override(**override):
                result = _run()
        else:
            result = _run()
        if result is None or 'CANCELLED' in result:
            return False
        select_only_constrained(context, source, target)
        # Blender can restore the pre-operator multi-object pose selection after
        # this function returns. Reassert the constrained model's selection once
        # the operator and its UI event have both finished.
        schedule_select_only_constrained(source, target)
        return True
    except Exception as exc:
        print(f"Reapply bind failed: {exc}")
        return False
    finally:
        _REBIND_BUSY = False


def schedule_reapply_bind():
    """Debounce live bind-setting changes."""
    global _REBIND_TOKEN
    _REBIND_TOKEN += 1
    token = _REBIND_TOKEN

    def _do():
        if token != _REBIND_TOKEN:
            return None
        try:
            reapply_bind_from_scene(bpy.context)
        except Exception:
            pass
        return None

    bpy.app.timers.register(_do, first_interval=0.2)


def on_bind_setting_update(self, context):
    """Store sidebar edits without changing the current object selection.

    Rebinding requires Expy Kit to select both armatures and enter pose mode.
    Doing that from every RNA update made even a checkbox click in the Bind To
    panel activate the source rig. The panel has an explicit Apply Bind
    Settings button, so edits remain passive until that operator is used.
    """
    if not _SYNCING_BIND_PROPS:
        tag_retargeting_redraw()


def migrate_throw_from_custom(settings):
    """Copy legacy custom.throw into the main throw slot."""
    if not settings:
        return
    if getattr(settings, 'throw', ''):
        return
    custom = getattr(settings, 'custom', None)
    if custom is None:
        return
    throw_bone = getattr(custom, 'throw', '') or ''
    if throw_bone:
        settings.throw = throw_bone


def _guided_yes_no_row(layout, step):
    row = layout.row(align=True)
    row.scale_y = 1.4
    yes = row.operator("object.ultimate_guided_answer", text="Yes")
    yes.step = step
    yes.answer = 'YES'
    no = row.operator("object.ultimate_guided_answer", text="No")
    no.step = step
    no.answer = 'NO'


def apply_guided_answer(context, step, answer, report=None):
    """Advance Guided Mode from a Yes/No choice or a confirm dialog."""
    scene = context.scene

    if step == 'PRESET':
        if answer == 'NO':
            scene.expykit_guided_phase = 'PRESET'
            _invoke_later(bpy.ops.object.ultimate_guided_info, message=PRESET_NO_MESSAGE)
            return {'FINISHED'}
        scene.expykit_guided_phase = 'ANIM'
        _invoke_later(bpy.ops.object.ultimate_guided_mode, step='ANIM')
        return {'FINISHED'}

    if step == 'ANIM':
        if answer == 'NO':
            scene.expykit_guided_phase = 'PRESET'
            _invoke_later(bpy.ops.object.ultimate_guided_info, message=ANIM_NO_MESSAGE)
            return {'FINISHED'}
        scene.expykit_guided_phase = 'SELECT'
        _invoke_later(bpy.ops.object.ultimate_guided_mode, step='SELECT')
        return {'FINISHED'}

    if step == 'SELECT':
        target = scene.expykit_bind_to
        source = context.object
        if (not source or source.type != 'ARMATURE' or source == target) and target:
            source = next(
                (ob for ob in context.selected_objects if ob.type == 'ARMATURE' and ob != target),
                None,
            )
        if not source or source.type != 'ARMATURE':
            if report:
                report({'ERROR'}, "Select the source armature first")
            return {'CANCELLED'}
        if not target or target.type != 'ARMATURE':
            if report:
                report({'ERROR'}, "Select an armature to bind to")
            return {'CANCELLED'}
        if source == target:
            if report:
                report({'ERROR'}, "Source and target armatures must be different")
            return {'CANCELLED'}

        if context.view_layer.objects.active != source:
            context.view_layer.objects.active = source
        source.select_set(True)

        scene.expykit_guided_phase = 'BIND'
        scene.expykit_guided_explain = True
        _invoke_later(bpy.ops.object.ultimate_bind_armatures)
        return {'FINISHED'}

    if step == 'CHECK':
        scene.expykit_guided_phase = 'BAKE'
        tag_retargeting_redraw()
        return {'FINISHED'}

    if step == 'BAKE':
        if answer == 'NO':
            scene.expykit_guided_phase = 'BAKE'
            return {'FINISHED'}
        _invoke_later(bpy.ops.armature.ultimate_bake_actions)
        return {'FINISHED'}

    return {'FINISHED'}


class ULTIMATE_OT_guided_info(bpy.types.Operator):
    """Show a Guided Mode explanation dialog."""
    bl_description = 'Show a Guided Mode explanation dialog.'
    bl_idname = "object.ultimate_guided_info"
    bl_label = "Guided Mode"
    bl_options = {'INTERNAL'}

    message: bpy.props.StringProperty(default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        for line in self.message.split('\n'):
            col.label(text=line if line else " ")

    def execute(self, context):
        return {'FINISHED'}


class ULTIMATE_OT_guided_answer(bpy.types.Operator):
    """Yes or No answer for a Guided Mode prompt."""
    bl_description = 'Yes or No answer for a Guided Mode prompt.'
    bl_idname = "object.ultimate_guided_answer"
    bl_label = "Guided Mode"
    bl_options = {'INTERNAL'}

    step: bpy.props.StringProperty(options={'SKIP_SAVE', 'HIDDEN'})
    answer: bpy.props.StringProperty(options={'SKIP_SAVE', 'HIDDEN'})

    def execute(self, context):
        return apply_guided_answer(context, self.step, self.answer, report=self.report)


class ULTIMATE_OT_guided_mode(bpy.types.Operator):
    """Walk through retargeting with yes/no prompts."""
    bl_idname = "object.ultimate_guided_mode"
    bl_label = "Guided Mode"
    bl_description = "Step-by-step prompts for presets, animations, binding, and baking"
    bl_options = {'INTERNAL'}

    step: bpy.props.EnumProperty(
        items=[
            ('AUTO', "Auto", ""),
            ('PRESET', "Preset", ""),
            ('ANIM', "Animations", ""),
            ('SELECT', "Select Armature", ""),
            ('CHECK', "Check Animations", ""),
            ('BAKE', "Bake", ""),
        ],
        default='AUTO',
        options={'SKIP_SAVE', 'HIDDEN'},
    )

    def invoke(self, context, event):
        scene = context.scene
        step = self.step
        if step == 'AUTO':
            step = scene.expykit_guided_phase or 'PRESET'
            if step in {'IDLE', 'DONE', ''}:
                step = 'PRESET'
            self.step = step

        if step in {'PRESET', 'ANIM', 'BAKE'}:
            return context.window_manager.invoke_popup(self, width=380)

        if step == 'CHECK':
            return context.window_manager.invoke_props_dialog(self, width=420)

        if step == 'SELECT':
            return context.window_manager.invoke_props_dialog(self, width=400)

        return context.window_manager.invoke_popup(self, width=380)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        scene = context.scene

        if self.step == 'PRESET':
            col.label(text="Have you made a Preset?", icon='QUESTION')
            col.separator()
            _guided_yes_no_row(col, 'PRESET')
        elif self.step == 'ANIM':
            col.label(text="Have you loaded all your animations on the source armature?", icon='QUESTION')
            col.separator()
            _guided_yes_no_row(col, 'ANIM')
        elif self.step == 'SELECT':
            col.label(text="Select Armature to bind", icon='ARMATURE_DATA')
            col.separator()
            col.label(text="Pick the target armature (the one to retarget onto).")
            col.prop(scene, 'expykit_bind_to', text="Bind To")
        elif self.step == 'CHECK':
            col.label(text="Check the result", icon='INFO')
            col.separator()
            for line in CHECK_ANIM_MESSAGE.split('\n'):
                col.label(text=line)
            col.separator()
            col.label(text="Click OK, then use Continue Guided Mode when ready.")
        elif self.step == 'BAKE':
            col.label(text="Ready to bake?", icon='QUESTION')
            col.separator()
            _guided_yes_no_row(col, 'BAKE')

    def execute(self, context):
        if self.step in {'PRESET', 'ANIM', 'BAKE'}:
            return {'FINISHED'}
        return apply_guided_answer(context, self.step, 'YES', report=self.report)


def guided_button_label(scene):
    phase = getattr(scene, 'expykit_guided_phase', 'IDLE') or 'IDLE'
    if phase in {'BIND', 'CHECK', 'BAKE'}:
        return "Continue Guided Mode"
    return "Guided Mode"


class ULTIMATE_OT_apply_bind_settings(bpy.types.Operator):
    """Re-apply the Bind to Active Armature settings to the last bound pair."""
    bl_description = 'Re-apply the Bind to Active Armature settings to the last bound pair.'
    bl_idname = "object.ultimate_apply_bind_settings"
    bl_label = "Apply Bind Settings"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        source, target = bound_pair(context.scene)
        return bool(source and target)

    def execute(self, context):
        if reapply_bind_from_scene(context):
            self.report({'INFO'}, "Bind settings applied")
            return {'FINISHED'}
        self.report({'WARNING'}, "Could not apply bind settings")
        return {'CANCELLED'}


class ULTIMATE_OT_bind_wait_select(bpy.types.Operator):
    """After the bind popup, leave only the constrained armature selected."""
    bl_description = 'After the bind popup, leave only the constrained armature selected.'
    bl_idname = "object.ultimate_bind_wait_select"
    bl_label = "Bind Finish Select"
    bl_options = {'INTERNAL'}

    def modal(self, context, event):
        region = getattr(context, 'region', None)
        rtype = getattr(region, 'type', '') if region else ''
        if rtype in {'HUD', 'UI', 'TOOLS', 'TOOL_HEADER', 'HEADER', 'NAVIGATION_BAR'}:
            return {'PASS_THROUGH'}
        if event.value == 'PRESS' and event.type in {
            'LEFTMOUSE', 'RIGHTMOUSE', 'RET', 'NUMPAD_ENTER', 'ESC',
        }:
            constrained, driver = bound_pair(context.scene)
            select_only_constrained(context, constrained, driver)
            tag_retargeting_redraw()
            return {'FINISHED', 'PASS_THROUGH'}
        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}


classes = (
    ULTIMATE_OT_guided_info,
    ULTIMATE_OT_guided_answer,
    ULTIMATE_OT_guided_mode,
    ULTIMATE_OT_apply_bind_settings,
    ULTIMATE_OT_bind_wait_select,
)
