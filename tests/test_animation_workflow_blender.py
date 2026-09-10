"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
from pathlib import Path
from types import SimpleNamespace
import importlib
import tempfile

# Reuse the isolated profile and actual add-on registration fixture.
fixture = Path(__file__).with_name('test_addon_registration_blender.py')
exec(compile(fixture.read_text().split('addon_utils.disable(MODULE')[0], str(fixture), 'exec'))
export = importlib.import_module(MODULE + '.source.anim.export_anim')
selection = importlib.import_module(MODULE + '.source.anim.selection')
model = importlib.import_module(MODULE + '.source.model.export_model')
doctor = importlib.import_module(MODULE + '.source.doctor')
doctor.preflight = lambda *args: True

items = [SimpleNamespace(name=n, selected=False) for n in ['run_a', 'idle', 'run_b', 'run_c']]
selection.select_range(items, 'selected', 3, 'run_a', shift=True, visible_indices=[0, 2, 3])
assert [i.selected for i in items] == [True, False, True, True]
selection.select_range(items, 'selected', 0, 'run_c', shift=True, visible_indices=[3, 2, 0])
assert [i.selected for i in items] == [True, False, True, True]
selection.select_range(items, 'selected', 2, 'idle', shift=True, visible_indices=[0, 2, 3])
assert [i.selected for i in items] == [False, False, True, False]
ui = SimpleNamespace(filter_name='run', bitflag_filter_item=1 << 30,
                     use_filter_invert=False, use_filter_sort_alpha=True,
                     use_filter_sort_reverse=True)
assert selection.visible_list_indices(ui, items) == [3, 2, 0]
ui.use_filter_invert = True
assert selection.visible_list_indices(ui, items) == [1]
for body in ('body/', ''):
    skel = f'C:/dump/fighter/mario/model/{body}c03/model.nusktb'
    prc = f'C:/dump/fighter/mario/motion/{body}c03/update.prc'
    assert Path(model.vanilla_reference_sibling(skel, 'prc')) == Path(prc)
    assert Path(model.vanilla_reference_sibling(prc, 'skel')) == Path(skel)

bpy.ops.object.camera_add()
obj = bpy.context.object
for frame in (1, 30):
    obj.location.x = frame / 10
    obj.keyframe_insert('location', frame=frame)
original_action = obj.animation_data.action
bpy.context.scene.frame_set(7, subframe=0.25)
bpy.context.scene.tool_settings.use_keyframe_insert_auto = True
with tempfile.TemporaryDirectory() as folder:
    output = str(Path(folder) / 'camera.nuanmb')
    result = bpy.ops.sub.anim_export(filepath=output, first_blender_frame=1, last_blender_frame=30)
    assert result == {'FINISHED'}, result
    assert Path(output).is_file()
    assert bpy.context.scene.frame_current == 7
    assert bpy.context.scene.frame_subframe == 0.25
    assert obj.animation_data.action == original_action
    assert bpy.context.scene.tool_settings.use_keyframe_insert_auto
    reporter = SimpleNamespace(report=lambda *args, **kwargs: None)
    iterator = export.export_camera_anim_steps(bpy.context, reporter, obj,
        str(Path(folder) / 'stepped.nuanmb'), 1, 30)
    assert sum(1 for _ in iterator) >= 30
    assert Path(output).read_bytes() == (Path(folder) / 'stepped.nuanmb').read_bytes()

    # Failure and cancellation must restore scene state and release the job guard.
    class Failure(export.AnimationExportJob):
        def report(self, *args): pass
        def export_steps(self, context):
            context.scene.frame_set(20)
            obj.animation_data.action = None
            yield
            raise RuntimeError('injected failure')
    bpy.context.scene.frame_set(7, subframe=0.25)
    try:
        Failure().execute(bpy.context)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Expected injected failure')
    assert obj.animation_data.action == original_action
    assert bpy.context.scene.frame_current == 7
    assert bpy.context.scene.tool_settings.use_keyframe_insert_auto
    assert not export.AnimationExportJob._running

# Real armature and batch exports, including SAP action restoration.
bpy.ops.object.armature_add()
obj = bpy.context.object
bone = obj.pose.bones[0]
for frame in (1, 10):
    bone.location.x = frame * 0.1
    bone.keyframe_insert('location', frame=frame)
original_action = obj.animation_data.action
obj.data.animation_data_create()
original_sap = bpy.data.actions.new('original SAP')
obj.data.animation_data.action = original_sap
ssp = bpy.context.scene.sub_scene_properties
ssp.action_export_list.clear()
for number in range(2):
    action = original_action.copy()
    action.name = f'clip_{number}'
    item = ssp.action_export_list.add()
    item.name, item.action, item.export = action.name, action, True
    bpy.data.actions.new(f'{obj.name} {action.name} SAP Data')
bpy.context.scene.frame_set(4, subframe=0.5)
with tempfile.TemporaryDirectory() as folder:
    result = bpy.ops.sub.batch_export_anim(directory=folder, first_blender_frame=1,
        include_material_track=False, include_visibility_track=False)
    assert result == {'FINISHED'}
    assert len(list(Path(folder).glob('*.nuanmb'))) == 2
    assert obj.animation_data.action == original_action
    assert obj.data.animation_data.action == original_sap
    assert bpy.context.scene.frame_current == 4
    assert bpy.context.scene.frame_subframe == 0.5

# Exercise the Escape branch with a suspended export and a modified scene.
job = export.AnimationExportJob()
job.report = lambda *args: None
job._scene = bpy.context.scene
job._frame = (4, 0.5)
job._actions = [(obj, True, original_action, obj.animation_data.action_slot)]
job._auto_key = True
job._job = (value for value in (None, None))
job._timer = None
export.AnimationExportJob._running = True
obj.animation_data.action = None
bpy.context.scene.frame_set(19)
assert job.modal(bpy.context, SimpleNamespace(type='ESC')) == {'CANCELLED'}
assert obj.animation_data.action == original_action
assert bpy.context.scene.frame_current == 4
assert not export.AnimationExportJob._running

addon_utils.disable(MODULE, default_set=False, handle_error=on_error)
assert not errors, errors
print('ANIMATION WORKFLOW TESTS PASSED')
profile.cleanup()
