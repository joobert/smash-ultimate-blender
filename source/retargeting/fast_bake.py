"""Bake evaluated visual transforms, with isolated Blender processes per batch.

No Blender RNA or dependency graph is accessed by Python worker threads.
Every clip starts from rest and writes complete transforms, including rest keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import bpy
import numpy as np
from mathutils import Matrix

from ..anim.fcurve_compat import action_frame_range_safe, ensure_fcurve_for_datablock
from ..blender_compat import assign_action, ensure_action_slot, slot_display_name, slot_id_type


def load_baked_action(armature, action):
    if armature is None or action is None or armature.type != 'ARMATURE':
        return False
    armature.animation_data_create()
    assign_action(armature.animation_data, action)
    sap = bpy.data.actions.get(f"{armature.name} {action.name} SAP Data")
    if sap is not None:
        armature.data.animation_data_create()
        assign_action(armature.data.animation_data, sap)
    try:
        from . import guided
        guided._invalidate_smash_viewport()
    except (ImportError, AttributeError):
        pass
    bpy.context.view_layer.update()
    return True


def _assign_driving_action(source, action, slot_handle=None):
    source.animation_data_create()
    source.animation_data.action = action
    if action is None:
        return
    slots = getattr(action, 'slots', ())
    slot = next((s for s in slots if s.handle == slot_handle), None)
    if slot is None:
        slot = next((s for s in slots if slot_id_type(s) == 'OBJECT'
                     and slot_display_name(s) == source.name), None)
    if slot is not None:
        source.animation_data.action_slot = slot
    else:
        assign_action(source.animation_data, action)


def _reset_pose(armature):
    # Reset all rotation representations: actions may use a different one.
    for bone in armature.pose.bones:
        bone.location = (0, 0, 0)
        bone.rotation_euler = (0, 0, 0)
        bone.rotation_quaternion = (1, 0, 0, 0)
        bone.rotation_axis_angle = (0, 0, 1, 0)
        bone.scale = (1, 1, 1)


def _decompose_sequence(matrices, mode):
    locations, rotations, scales = [], [], []
    previous = None
    for matrix in matrices:
        loc, quat, scale = matrix.decompose()
        if mode == 'QUATERNION':
            if previous is not None and quat.dot(previous) < 0:
                quat.negate()
            rot = quat
        elif mode == 'AXIS_ANGLE':
            axis, angle = quat.to_axis_angle()
            rot = (angle, *axis)
        else:
            rot = quat.to_euler(mode, previous) if previous is not None else quat.to_euler(mode)
        locations.append(tuple(loc))
        rotations.append(tuple(rot))
        scales.append(tuple(scale))
        previous = rot.copy() if hasattr(rot, 'copy') else rot
    return np.asarray(locations), np.asarray(rotations), np.asarray(scales)


def _sample_action(context, source, dest, action, names, source_basis, dest_basis, slot_handle=None):
    """Sample LOCAL basis from the evaluated hierarchy, without mutating it."""
    assign_action(source.animation_data, None)
    if source != dest:
        assign_action(dest.animation_data, None)
    _reset_pose(source)
    if source != dest:
        _reset_pose(dest)
    source.matrix_basis = Matrix(source_basis)
    dest.matrix_basis = Matrix(dest_basis)
    _assign_driving_action(source, action, slot_handle)
    start, end = action_frame_range_safe(action)
    start, end = int(np.floor(start)), int(np.ceil(end))
    frames = np.arange(start, max(start, end) + 1, dtype=float)
    matrices = [[] for _ in names]
    object_matrices = []
    for frame in frames:
        context.scene.frame_set(int(frame))
        context.view_layer.update()
        evaluated = dest.evaluated_get(context.evaluated_depsgraph_get())
        for index, name in enumerate(names):
            bone = evaluated.pose.bones[name]
            kwargs = {}
            if bone.parent is not None:
                kwargs = dict(parent_matrix=bone.parent.matrix,
                              parent_matrix_local=bone.parent.bone.matrix_local)
            matrices[index].append(bone.bone.convert_local_to_pose(
                bone.matrix, bone.bone.matrix_local, invert=True, **kwargs))
        # matrix_local includes the parent inverse. Remove it to obtain the
        # writable basis. Blender also handles bone parenting in matrix_local.
        object_matrices.append(evaluated.matrix_parent_inverse.inverted_safe()
                               @ evaluated.matrix_local if evaluated.parent
                               else evaluated.matrix_world.copy())
    modes = [dest.pose.bones[name].rotation_mode for name in names]
    channels = [_decompose_sequence(values, mode) for values, mode in zip(matrices, modes)]
    channels.append(_decompose_sequence(object_matrices, dest.rotation_mode))
    return frames, modes + [dest.rotation_mode], channels


def _write_channel(action, dest, path, axis, frames, values, group, interpolation):
    curve = ensure_fcurve_for_datablock(action, dest, path, index=axis,
                                      action_group=group, id_type='OBJECT')
    curve.keyframe_points.add(len(frames))
    coords = np.column_stack((frames, values)).ravel()
    curve.keyframe_points.foreach_set('co', coords)
    for point in curve.keyframe_points:
        point.interpolation = interpolation
    curve.update()


def _write_action(dest, name, names, result, fake_user, interpolation):
    frames, modes, channels = result
    action = bpy.data.actions.new(name)
    try:
        ensure_action_slot(action, dest)
        action.use_fake_user = fake_user
        for bone_name, mode, (loc, rot, scale) in zip([*names, None], modes, channels):
            prefix = dest.pose.bones[bone_name].path_from_id() + '.' if bone_name else ''
            rotation = ('rotation_quaternion' if mode == 'QUATERNION' else
                        'rotation_axis_angle' if mode == 'AXIS_ANGLE' else 'rotation_euler')
            for prop, values in [('location', loc), (rotation, rot), ('scale', scale)]:
                for axis in range(values.shape[1]):
                    _write_channel(action, dest, prefix + prop, axis, frames, values[:, axis],
                                   bone_name or 'Object Transforms', interpolation)
    except BaseException:
        bpy.data.actions.remove(action, do_unlink=True)
        raise
    return action


def _parallel_samples(context, source, dest, actions, names, bases, slots, progress):
    """Yield sampled arrays; subprocesses each own an independent dependency graph."""
    worker = Path(__file__).with_name('bake_worker.py')
    workers = min(len(actions), max(1, (os.cpu_count() or 2) // 2), 4)
    with tempfile.TemporaryDirectory(prefix='ultimate-bake-') as directory:
        directory = Path(directory)
        snapshot = directory / 'preview.blend'
        # This leaves the user's current filename and saved project untouched.
        bpy.data.libraries.write(str(snapshot), set(bpy.data.scenes) | set(actions),
                                 path_remap='ABSOLUTE', fake_user=True)
        jobs = []
        try:
            for index in range(workers):
                indices = list(range(index, len(actions), workers))
                manifest = dict(source=source.name, dest=dest.name, bones=names,
                                bases=bases, scene=context.scene.name,
                                view_layer=context.view_layer.name,
                                actions=[dict(index=i, name=actions[i].name, slot=slots[i]) for i in indices])
                config = directory / f'job-{index}.json'
                config.write_text(json.dumps(manifest), encoding='utf-8')
                log = open(directory / f'worker-{index}.log', 'w', encoding='utf-8')
                command = [bpy.app.binary_path, '--background', '--threads',
                           str(max(1, (os.cpu_count() or 2) // workers)),
                           str(snapshot), '--python-exit-code', '1', '--python', str(worker),
                           '--', str(config)]
                try:
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except BaseException:
                    log.close()
                    raise
                jobs.append((process, log))
            pending = set(range(len(actions)))
            while pending:
                for process, log in jobs:
                    code = process.poll()
                    if code not in (None, 0):
                        log.flush()
                        detail = Path(log.name).read_text(encoding='utf-8', errors='replace')[-3000:]
                        raise RuntimeError(f'Blender bake worker failed ({code}): {detail}')
                for index in sorted(pending):
                    output = directory / f'result-{index}.npz'
                    if not output.exists():
                        continue
                    with np.load(output, allow_pickle=False) as data:
                        modes = data['modes'].tolist()
                        channels = [(data[f'loc{i}'], data[f'rot{i}'], data[f'scale{i}'])
                                    for i in range(len(modes))]
                        yield index, (data['frames'], modes, channels)
                    pending.remove(index)
                    progress((len(actions) - len(pending)) / len(actions))
                if pending and all(p.poll() is not None for p, _ in jobs):
                    raise RuntimeError('Bake workers exited without producing all clips')
                if pending:
                    time.sleep(0.1)
            for process, _ in jobs:
                if process.wait() != 0:
                    raise RuntimeError('Blender bake worker failed during shutdown')
        finally:
            for process, log in jobs:
                if process.poll() is None:
                    process.terminate()
                process.wait()
                log.close()


def bake_visual_actions(context, source, dest, actions, fake_user_new=True,
                        clear_users_old=True, bone_names=None, interp_name='LINEAR',
                        parallel=True, log_label='Bake Visual', use_retarget_clean=True):
    """Both UI bake modes share this transactional, complete visual bake."""
    del use_retarget_clean, bone_names
    actions = list(dict.fromkeys(actions))
    if not actions:
        return []
    # Full hierarchy, including helpers: any bone may influence a user constraint.
    names = [bone.name for bone in dest.pose.bones]
    if not names:
        raise RuntimeError('Destination armature has no pose bones')
    for obj in {source, dest}:
        obj.animation_data_create()
    objects = list(dict.fromkeys((source, dest)))
    states = [(obj, obj.animation_data.action, getattr(obj.animation_data, 'action_slot', None),
               obj.matrix_basis.copy(), [pb.matrix_basis.copy() for pb in obj.pose.bones]) for obj in objects]
    frame, subframe = context.scene.frame_current, context.scene.frame_subframe
    bases = [list(map(list, obj.matrix_basis)) for obj in (source, dest)]
    original_names = [action.name for action in actions]
    original_fake = [action.use_fake_user for action in actions]
    slots = [getattr(source.animation_data, 'action_slot_handle', None)
             if source.animation_data.action == action else None for action in actions]
    baked = {}
    success = False
    context.window_manager.progress_begin(0, 100)
    try:
        # Free every original name before evaluation starts. Never normalize real
        # names (e.g. a legitimate .001); only our temporary suffix is removed.
        for action, name in zip(actions, original_names):
            action.name = name + '_old'
            action.use_fake_user = True
        progress = lambda fraction: context.window_manager.progress_update(fraction * 100)
        if parallel:
            samples = _parallel_samples(context, source, dest, actions, names, bases, slots, progress)
        else:
            samples = ((i, _sample_action(context, source, dest, action, names, *bases, slots[i]))
                       for i, action in enumerate(actions))
        try:
            for index, result in samples:
                baked[index] = _write_action(dest, original_names[index], names, result,
                                             fake_user_new, interp_name)
                progress(len(baked) / len(actions))
        finally:
            close = getattr(samples, 'close', None)
            if close:
                close()
        success = True
    finally:
        if not success:
            for action in baked.values():
                bpy.data.actions.remove(action, do_unlink=True)
            for action, name, fake in zip(actions, original_names, original_fake):
                action.name = name
                action.use_fake_user = fake
        for obj, action, slot, basis, pose in states:
            assign_action(obj.animation_data, action)
            if slot is not None and action is not None:
                obj.animation_data.action_slot = slot
            obj.matrix_basis = basis
            for bone, matrix in zip(obj.pose.bones, pose):
                bone.matrix_basis = matrix
        if success:
            if clear_users_old and source != dest:
                assign_action(source.animation_data, None)
                _reset_pose(source)
            assign_action(dest.animation_data, baked[0])
            # The evaluated result already includes these contributions. Keep
            # constraints available for inspection when the UI doesn't delete
            # them, but never apply them a second time over the baked keys.
            for owner in [dest, *dest.pose.bones]:
                for constraint in owner.constraints:
                    constraint.mute = True
            dest.animation_data.use_nla = False
            dest.delta_location = (0, 0, 0)
            dest.delta_rotation_euler = (0, 0, 0)
            dest.delta_rotation_quaternion = (1, 0, 0, 0)
            dest.delta_scale = (1, 1, 1)
            for driver in dest.animation_data.drivers:
                if driver.data_path.rsplit('.', 1)[-1] in {
                    'location', 'rotation_euler', 'rotation_quaternion',
                    'rotation_axis_angle', 'scale', 'delta_location',
                    'delta_rotation_euler', 'delta_rotation_quaternion', 'delta_scale',
                }:
                    driver.mute = True
        context.scene.frame_set(frame, subframe=subframe)
        context.view_layer.update()
        context.window_manager.progress_end()
    print(f'{log_label}: baked {len(baked)} actions using ' + ('Blender worker processes' if parallel else 'live evaluation'))
    return [(action, baked[i]) for i, action in enumerate(actions)]


def bake_visible_actions(context, source_armature, dest_armature, actions_to_bake,
                         fake_user_new=True, clear_users_old=True, keep_ik_bones=True):
    pairs = bake_visual_actions(context, source_armature, dest_armature, actions_to_bake,
                                fake_user_new=fake_user_new, clear_users_old=clear_users_old,
                                log_label='Bake Visible')
    return len(pairs)


def bake_constrained_actions(context, source, dest, actions, bone_names=None,
                             fake_user_new=True, clear_users_old=True):
    return bake_visual_actions(context, source, dest, actions, fake_user_new=fake_user_new,
                               clear_users_old=clear_users_old, log_label='Bake Constrained')
