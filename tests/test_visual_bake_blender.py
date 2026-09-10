"""Run with blender --background --factory-startup --python this_file.py."""
import importlib.util
from pathlib import Path
import sys

import bpy
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bake_worker', ROOT / 'source/retargeting/bake_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
baker = worker.load_baker()


def rig(name):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for name, parent, x in [('root', None, 0), ('child', 'root', 0), ('rest', None, 2), ('helper_RET', None, 3)]:
        bone = data.edit_bones.new(name)
        bone.head = (x, 1 if parent else 0, 0)
        bone.tail = (x, 2 if parent else 1, 0)
        if parent:
            bone.parent = data.edit_bones[parent]
            bone.inherit_scale = 'NONE'
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.animation_data_create()
    return obj


def clip(source, name, sparse=False):
    baker.assign_action(source.animation_data, None)
    baker._reset_pose(source)
    action = bpy.data.actions.new(name)
    baker.assign_action(source.animation_data, action)
    for frame in [1, 3]:
        root = source.pose.bones['root']
        root.rotation_mode = 'XYZ'
        root.rotation_euler.z = frame * 0.2
        root.keyframe_insert('rotation_euler', frame=frame)
        if not sparse:
            child = source.pose.bones['child']
            child.location.x = frame * 0.4
            child.keyframe_insert('location', frame=frame)
            root.scale = (1 + frame * .1, 1, 1)
            root.keyframe_insert('scale', frame=frame)
    return action


def expected(source, dest, action):
    baker.assign_action(source.animation_data, None)
    baker.assign_action(dest.animation_data, None)
    baker._reset_pose(source)
    baker._reset_pose(dest)
    baker.assign_action(source.animation_data, action)
    result = []
    for frame in range(1, 4):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        evaluated = dest.evaluated_get(bpy.context.evaluated_depsgraph_get())
        result.append([evaluated.matrix_world @ bone.matrix for bone in evaluated.pose.bones])
    return result


def check(parallel, bone_parent=False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source, dest = rig('Source'), rig('Destination')
    for name in ('root', 'child'):
        c = dest.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        c.target, c.subtarget = source, name
    external = bpy.data.objects.new('Custom target', None)
    bpy.context.collection.objects.link(external)
    external.location = (2, 3, 1)
    c = dest.pose.bones['rest'].constraints.new('COPY_LOCATION')
    c.target = external  # No source action channels and a user-added COPY constraint.
    c.influence = .5
    c = dest.pose.bones['helper_RET'].constraints.new('COPY_ROTATION')
    c.target, c.subtarget = source, 'root'
    parent = rig('Parent') if bone_parent else bpy.data.objects.new('Parent', None)
    if not bone_parent:
        bpy.context.collection.objects.link(parent)
    parent.location = (1, 0, 2)
    dest.parent = parent
    if bone_parent:
        dest.parent_type = 'BONE'
        dest.parent_bone = 'child'
    dest.matrix_parent_inverse = Matrix.Translation((0, 0, -2))
    dest.delta_location = (.2, 0, 0)
    c = dest.constraints.new('COPY_LOCATION')
    c.target = external
    c.influence = .3
    actions = [clip(source, 'Attack'), clip(source, 'Idle', True)]
    reference = [expected(source, dest, action) for action in actions]
    if parallel == 'VISIBLE':
        count = baker.bake_visible_actions(bpy.context, source, dest, actions)
        assert count == 2
        pairs = list(zip(actions, [bpy.data.actions['Attack'], bpy.data.actions['Idle']]))
    elif parallel == 'CONSTRAINED':
        pairs = baker.bake_constrained_actions(bpy.context, source, dest, actions)
    else:
        pairs = baker.bake_visual_actions(bpy.context, source, dest, actions, parallel=parallel)
    assert [a.name for a, _ in pairs] == ['Attack_old', 'Idle_old']
    assert [a.name for _, a in pairs] == ['Attack', 'Idle']
    # Verify in both switching directions; rest keys must overwrite prior poses.
    for index in (1, 0, 1):
        baker.load_baked_action(dest, pairs[index][1])
        for frame in range(1, 4):
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            evaluated = dest.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for bone, matrix in zip(evaluated.pose.bones, reference[index][frame - 1]):
                actual = evaluated.matrix_world @ bone.matrix
                error = max(abs(a - b) for ar, br in zip(actual, matrix) for a, b in zip(ar, br))
                assert error < 2e-5, (parallel, index, frame, bone.name, error)
    print('PASS visual matrices, sparse clips, user constraints, helpers, object parenting, names; parallel=', parallel)


check(False)
check('VISIBLE')
check('CONSTRAINED')
check(False, bone_parent=True)


def check_same_armature_and_rollback():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source = rig('Own Actions')
    actions = [clip(source, 'Original.001'), clip(source, 'Sparse', True)]
    # A user IK constraint changes the parent chain, not just the constrained bone.
    target = bpy.data.objects.new('IK target', None)
    bpy.context.collection.objects.link(target)
    target.location = (.5, 1.7, .3)
    constraint = source.pose.bones['child'].constraints.new('IK')
    constraint.target = target
    constraint.chain_count = 2
    reference = [expected(source, source, action) for action in actions]
    pairs = baker.bake_visual_actions(bpy.context, source, source, actions, parallel=True)
    assert pairs[0][1].name == 'Original.001'
    for index, (_, action) in enumerate(pairs):
        baker.load_baked_action(source, action)
        for frame in range(1, 4):
            bpy.context.scene.frame_set(frame)
            evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for bone, matrix in zip(evaluated.pose.bones, reference[index][frame - 1]):
                actual = evaluated.matrix_world @ bone.matrix
                error = max(abs(a - b) for ar, br in zip(actual, matrix) for a, b in zip(ar, br))
                assert error < 2e-5, ('same armature IK', index, frame, bone.name, error)
    before = set(bpy.data.actions.keys())
    write_channel = baker._write_channel
    def fail(*args, **kwargs):
        raise RuntimeError('injected curve writing failure')
    baker._write_channel = fail
    try:
        try:
            baker.bake_visual_actions(bpy.context, source, source, [pairs[0][1]], parallel=False)
        except RuntimeError as error:
            assert 'injected' in str(error)
        else:
            raise AssertionError('Expected failure')
    finally:
        baker._write_channel = write_channel
    assert set(bpy.data.actions.keys()) == before
    assert source.animation_data.action == pairs[-1][1]
    print('PASS same-armature IK, exact original names, failure rollback')


check_same_armature_and_rollback()
print('ALL VISUAL BAKE TESTS PASSED')
