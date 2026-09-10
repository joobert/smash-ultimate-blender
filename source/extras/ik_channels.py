"""Independent IK solve bones. Original bones retain their editable FK action.

Only the output Copy Transforms constraints blend; the IK solver always runs at
full influence on a separate chain. Matching samples FK before writing IK keys.
"""
import math
import json
import uuid
import re
import bpy
from mathutils import Matrix, Vector

PREFIX = 'BL_SUB_IK_'
OUTPUT = 'SUB IK Blend'
VERSION = 'sub_independent_ik'
MATCH_KEY = 'sub_ik_channel_matches'
END_ROTATION = 'SUB IK End Rotation'
END_SCALE = 'SUB IK End Scale'
END_LOCATION = 'SUB IK Stretch'


def end_constraints(end):
    return [c for c in end.constraints
            if c.name in {END_ROTATION, END_SCALE, END_LOCATION}]


def wire_end_controls(obj):
    """Repair old endpoint transforms without rebuilding or rematching the rig."""
    from .create_animation_rig import _ensure_constraint_influence_driver
    for kind, names, target, _ in chains(obj):
        end = obj.pose.bones.get(PREFIX + names[2])
        if end is None:
            continue
        for con in list(end.constraints):
            if con.type == 'COPY_TRANSFORMS' and con.target == obj and con.subtarget == target:
                con.driver_remove('influence')
                end.constraints.remove(con)
        for name, type_ in ((END_ROTATION, 'COPY_ROTATION'),
                            (END_SCALE, 'COPY_SCALE'),
                            (END_LOCATION, 'COPY_LOCATION')):
            con = end.constraints.get(name)
            if con is None:
                con = end.constraints.new(type_)
                con.name = name
                con.target = obj
                con.subtarget = target
                con.target_space = con.owner_space = 'POSE'
            if name == END_LOCATION:
                _ensure_constraint_influence_driver(
                    obj, end, con, 'sub_ik_stretch_' + kind.lower())


def limb_path(obj, names):
    """Actual parent path, including twist/offset bones between named joints."""
    path = []
    bone = obj.data.bones.get(names[-1])
    while bone is not None:
        path.append(bone.name)
        if bone.name == names[0]:
            path.reverse()
            return tuple(path) if names[1] in path else ()
        bone = bone.parent
    return ()


def solve_bone(obj, names):
    return obj.pose.bones[PREFIX + limb_path(obj, names)[-2]]


def create_controls(context, obj, limbs='BOTH'):
    """One idempotent creation path for IK Tools and the Animation Rig."""
    from . import create_animation_rig as rig, anim_layers_compat
    rig._activate_armature(context, obj)
    jobs = []
    for b in obj.data.bones:
        bone_match = re.fullmatch(r'(Leg|Shoulder)([LR]\d*(?:\.\d{3})*)', b.name)
        if not bone_match:
            continue
        part, suffix = bone_match.groups()
        kind = 'LEGS' if part == 'Leg' else 'ARMS'
        if limbs not in (kind, 'BOTH'):
            continue
        names = tuple(p + suffix for p in (('Leg', 'Knee', 'Foot') if kind == 'LEGS' else ('Shoulder', 'Arm', 'Hand')))
        if all(n in obj.data.bones for n in names) and limb_path(obj, names):
            jobs.append((kind, names, ('FootIK' if kind == 'LEGS' else 'HandIK') + suffix,
                         ('KneeIK' if kind == 'LEGS' else 'ArmIK') + suffix))
    # Existing controls may have user-authored dependencies or animation. Only
    # freshly generated controls are eligible for the independent fast path.
    fresh_controls = all(
        name not in obj.data.bones
        for _, names, target, pole in jobs
        for name in (*(PREFIX + n for n in names), target, pole)
    )
    with rig.defer_pose_tool_updates(), rig._disable_autokey(context), anim_layers_compat.anim_layers_paused():
        bpy.ops.object.mode_set(mode='EDIT')
        bones = obj.data.edit_bones
        for _, names, target, pole in jobs:
            root, mid, end = [bones[n] for n in names]
            if target not in bones:
                control = bones.new(target)
                control.matrix = end.matrix.copy()
                control.length = max(end.length * 1.5, .1)
                control.parent = None
                control.use_deform = False
            if pole not in bones:
                axis = (end.head-root.head).normalized()
                bend = mid.head-root.head-axis*(mid.head-root.head).dot(axis)
                if bend.length < 1e-6:
                    bend = axis.orthogonal()
                control = bones.new(pole)
                control.head = mid.head + bend.normalized() * (root.length + mid.length)
                control.tail = control.head + Vector((0, max(mid.length*.25, .1), 0))
                control.parent = None
                control.use_deform = False
        bpy.ops.object.mode_set(mode='POSE')
        collection = obj.data.collections.get('IK Bones') or obj.data.collections.new('IK Bones')
        for _, _, target, pole in jobs:
            for n in (target, pole):
                collection.assign(obj.data.bones[n])
                obj.data.bones[n].color.palette = 'THEME01'
        if jobs:
            # Seed the new controls from the current FK pose before enabling IK.
            match(context, obj, limbs, entire=False, key=True, _batch=fresh_controls)
            rig._key_use_ik(obj, context.scene.frame_current, limbs=limbs, enabled=True)
            rig._set_ik_enabled(context, obj, True, limbs=limbs)
            context.view_layer.update()
    return len(jobs)


def _signature(obj, kind):
    return [obj.data.bones[PREFIX + names[0]].get('sub_ik_generation', '')
            for _, names, _, _ in chains(obj, kind) if PREFIX + names[0] in obj.data.bones]


def mark_matched(obj, action, limbs):
    if action is None:
        return
    records = json.loads(action.get(MATCH_KEY, '{}'))
    for kind in ('ARMS', 'LEGS'):
        if limbs in (kind, 'BOTH'):
            signature = _signature(obj, kind)
            if signature:
                records[kind] = signature
    action[MATCH_KEY] = json.dumps(records)


def unmatched(obj, action):
    """A different action or recreated solver needs its own match."""
    try:
        records = json.loads(action.get(MATCH_KEY, '{}')) if action else {}
    except (ValueError, TypeError):
        records = {}
    return {kind for kind in ('ARMS', 'LEGS') if list(chains(obj, kind))
            and (not _signature(obj, kind) or records.get(kind) != _signature(obj, kind))}


def chains(obj, limbs='BOTH'):
    from .fk_to_ik import iter_leg_fk_chains, iter_arm_fk_chains
    if limbs in {'LEGS', 'BOTH'}:
        for c in iter_leg_fk_chains(obj):
            names = (c['leg'].name, c['knee'].name, c['foot'].name)
            if limb_path(obj, names):
                yield 'LEGS', names, c['foot_ik'].name, c['knee_ik'].name
    if limbs in {'ARMS', 'BOTH'}:
        for c in iter_arm_fk_chains(obj):
            if all(c[k] is not None for k in ('shoulder', 'arm', 'hand', 'hand_ik', 'arm_ik')):
                names = (c['shoulder'].name, c['arm'].name, c['hand'].name)
                if limb_path(obj, names):
                    yield 'ARMS', names, c['hand_ik'].name, c['arm_ik'].name


def outputs(obj, limbs='BOTH'):
    for kind, names, target, pole in chains(obj, limbs):
        for name in limb_path(obj, names):
            pb = obj.pose.bones[name]
            con = pb.constraints.get(OUTPUT)
            if con is not None:
                yield pb, con, kind


def ensure(obj, context, limbs='BOTH'):
    from . import create_animation_rig as rig
    jobs = list(chains(obj, limbs))
    if not jobs:
        return
    fresh = [j for j in jobs if any(PREFIX + n not in obj.data.bones for n in limb_path(obj, j[1]))]
    if fresh:
        # Heal the old rest-hold workaround once, before installing independent chains.
        rig._clear_fk_rest_hold(obj)
        rig.unmute_all_ik_fk_fcurves(obj)
        bpy.ops.object.mode_set(mode='EDIT')
        bones = obj.data.edit_bones
        for kind, names, target, pole in fresh:
            path = limb_path(obj, names)
            for name in path:
                src = bones[name]
                dst = bones.get(PREFIX + name) or bones.new(PREFIX + name)
                dst.matrix = src.matrix.copy()
                dst.length = src.length
                dst.use_deform = False
                dst['sub_ik_generation'] = uuid.uuid4().hex
                dst.inherit_scale = src.inherit_scale
                dst.use_local_location = src.use_local_location
                dst.use_inherit_rotation = src.use_inherit_rotation
                parent = src.parent
                dst.parent = bones.get(PREFIX + parent.name) if parent and parent.name in path else parent
                dst.use_connect = src.use_connect
            # Controls must not inherit any FK limb rotation.
            for name in (target, pole):
                control = bones[name]
                matrix = control.matrix.copy()
                control.parent = None
                control.use_connect = False
                control.matrix = matrix
        bpy.ops.object.mode_set(mode='POSE')
        collection = obj.data.collections.get('IK Internal') or obj.data.collections.new('IK Internal')
        collection.is_visible = False
        for kind, names, target, pole in fresh:
            path = limb_path(obj, names)
            for name in path:
                source = obj.pose.bones[name]
                solver = obj.pose.bones[PREFIX + name]
                for old in list(solver.constraints):
                    if old.name == 'SUB IK Solve':
                        solver.constraints.remove(old)
                old_output = source.constraints.get(OUTPUT)
                if old_output is not None:
                    old_output.driver_remove('influence')
                    source.constraints.remove(old_output)
                solver.rotation_mode = 'QUATERNION'
                solver.matrix_basis = source.matrix_basis.copy()
                collection.assign(solver.bone)
                # Remove only the old constraints owned by this limb's controls.
                for con in list(source.constraints):
                    if con.type in {'IK', 'COPY_ROTATION'} and con.target == obj and con.subtarget in (target, pole):
                        con.driver_remove('influence')
                        source.constraints.remove(con)
                for axis in 'xyz':
                    setattr(solver, 'lock_ik_' + axis, False)
                    setattr(solver, 'use_ik_limit_' + axis, False)
                solver.ik_stretch = 0.0
                con = source.constraints.new('COPY_TRANSFORMS')
                con.name = OUTPUT
                con.target = obj
                con.subtarget = solver.name
                con.target_space = con.owner_space = 'POSE'
            mid = solve_bone(obj, names)
            con = mid.constraints.new('IK')
            con.name = 'SUB IK Solve'
            con.target = con.pole_target = obj
            con.subtarget = target
            con.pole_subtarget = pole
            con.chain_count = len(path) - 1
            con.use_stretch = False
            con.iterations = 200
    obj.data[VERSION] = 2
    wire(obj)
    from . import ik_floor_contact
    ik_floor_contact.restore_pending(context, obj)
    context.view_layer.update()


def wire(obj):
    from .create_animation_rig import _ensure_constraint_influence_driver, _limb_switch_prop
    wire_end_controls(obj)
    for pb, con, kind in outputs(obj):
        _ensure_constraint_influence_driver(obj, pb, con, _limb_switch_prop(kind))
        con.mute = False
    from . import ik_floor_contact
    ik_floor_contact.rewire(obj)


def _key(pb, frame, previous):
    from .fk_to_ik import _key_pose_bone
    if pb.rotation_mode == 'QUATERNION':
        q = pb.rotation_quaternion.copy()
        if pb.name in previous and q.dot(previous[pb.name]) < 0:
            q.negate()
            pb.rotation_quaternion = q
        previous[pb.name] = q
    _key_pose_bone(pb, frame)


def _angle(a, b, axis):
    a = a - axis * a.dot(axis)
    b = b - axis * b.dot(axis)
    if min(a.length, b.length) < 1e-8:
        return 0.0
    return math.atan2(axis.dot(a.cross(b)), a.dot(b))


def clean_animation(obj, limbs='BOTH', tolerance=1e-4):
    """Reduce baked transform curves, validating against the original at quarter frames.

    Keep endpoints, leave modifiers/discrete curves alone, and never touch switch
    curves or other limbs. Curves whose curvature needs Bezier handles are kept.
    """
    from ..anim.fcurve_compat import get_all_action_fcurves
    from .anim_layers_compat import viewport_driving_action
    from bisect import bisect_right
    action, _ = viewport_driving_action(obj)
    if action is None:
        return 0
    names = set()
    for _, group, target, pole in chains(obj, limbs):
        names.update(limb_path(obj, group))
        names.update(PREFIX + n for n in limb_path(obj, group))
        names.update((target, pole))
    paths = {obj.pose.bones[n].path_from_id() for n in names if n in obj.pose.bones}
    allowed = {path + '.' + channel for path in paths for channel in
               ('location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale')}
    allowed.update(solve_bone(obj, group).constraints['SUB IK Solve'].path_from_id() + '.pole_angle'
                   for _, group, _, _ in chains(obj, limbs))
    removed = 0
    for fc in get_all_action_fcurves(action, id_type='OBJECT'):
        points = fc.keyframe_points
        if fc.data_path not in allowed or len(points) < 3 or fc.modifiers or fc.mute or fc.lock:
            continue
        if any(p.interpolation not in {'LINEAR', 'BEZIER'} for p in points):
            continue
        coords = [(float(p.co.x), float(p.co.y)) for p in points]
        if any(b[0] <= a[0] for a, b in zip(coords, coords[1:])):
            continue
        keep = {0, len(coords)-1}
        pending = [(0, len(coords)-1)]
        while pending:
            first, last = pending.pop()
            if last-first < 2:
                continue
            x0, y0 = coords[first]
            x1, y1 = coords[last]
            error, index = max((abs(coords[i][1] - (y0 + (y1-y0)*(coords[i][0]-x0)/(x1-x0))), i)
                               for i in range(first+1, last))
            if error > tolerance:
                keep.add(index)
                pending.extend(((first, index), (index, last)))
        if len(keep) == len(coords):
            continue
        reduced = [coords[i] for i in sorted(keep)]
        xs = [c[0] for c in reduced]
        # Include original key times as well as <= quarter-frame intervals.
        # Validate before mutating so rejected reductions leave keys/handles exact.
        valid = True
        for left, right in zip(coords, coords[1:]):
            count = max(4, math.ceil((right[0]-left[0])*4))
            for j in range(count+1):
                x = left[0] + (right[0]-left[0])*j/count
                i = min(max(bisect_right(xs, x)-1, 0), len(reduced)-2)
                x0, y0 = reduced[i]
                x1, y1 = reduced[i+1]
                y = y0 + (y1-y0)*(x-x0)/(x1-x0)
                if abs(fc.evaluate(x)-y) > tolerance:
                    valid = False
                    break
            if not valid:
                break
        if not valid:
            continue
        for i in reversed(range(len(coords))):
            if i not in keep:
                points.remove(points[i], fast=True)
                removed += 1
        for p in points:
            p.interpolation = 'LINEAR'
        fc.update()
    return removed


def _can_batch_match(obj, jobs):
    """Only combine evaluations when each limb reads independent pose inputs.

    Custom drivers, external constraints, and cross-limb dependencies retain the
    sequential path. Output constraints have already been muted by match().
    """
    if len(jobs) < 2 or obj.parent or obj.constraints:
        return False
    owners = {}
    for index, (_, names, target, pole) in enumerate(jobs):
        for name in (*(PREFIX + n for n in names), target, pole):
            if name in owners:
                return False
            owners[name] = index
    props = {'sub_use_ik_arms', 'sub_use_ik_legs',
             'sub_ik_stretch_arms', 'sub_ik_stretch_legs'}
    if obj.data.animation_data and obj.data.animation_data.drivers:
        return False
    if obj.animation_data and obj.animation_data.action:
        from ..anim.fcurve_compat import get_all_action_fcurves
        for curve in get_all_action_fcurves(obj.animation_data.action, id_type='OBJECT'):
            # Animated constraint settings can change dependencies after the
            # initial check. Our own pole-angle keys only affect their own limb.
            if '.constraints[' in curve.data_path:
                if not any(
                    curve.data_path == solve_bone(obj, names).constraints['SUB IK Solve'].path_from_id() + '.pole_angle'
                    for _, names, _, _ in jobs
                ):
                    return False
    for curve in obj.animation_data.drivers if obj.animation_data else ():
        driver = curve.driver
        if not curve.data_path.endswith('.influence') or len(driver.variables) != 1:
            return False
        var = driver.variables[0]
        if (driver.type != 'SCRIPTED' or driver.expression != var.name
                or var.type != 'SINGLE_PROP' or var.targets[0].id != obj.data
                or var.targets[0].data_path not in props):
            return False
    for bone in obj.pose.bones:
        owner = owners.get(bone.name)
        if bone.parent and bone.parent.name in owners:
            if owner != owners[bone.parent.name]:
                return False
        for con in bone.constraints:
            # Only the output blends stay muted throughout matching. Check all
            # other constraints, including ones animated from muted to active.
            if (con.mute and con.name == OUTPUT and con.type == 'COPY_TRANSFORMS'
                    and con.target == obj and con.subtarget == PREFIX + bone.name):
                continue
            if con.type not in {'COPY_TRANSFORMS', 'COPY_LOCATION', 'COPY_ROTATION',
                                'COPY_SCALE', 'DAMPED_TRACK', 'IK'}:
                return False
            if getattr(con, 'use_bbone_shape', False):
                return False
            if con.type == 'IK' and (owner is None or con.chain_count != 2):
                return False
            targets = [(con.target, con.subtarget)]
            if con.type == 'IK':
                targets.append((con.pole_target, con.pole_subtarget))
            for target, name in targets:
                if target is None:
                    continue
                if target != obj or not name:
                    return False
                if name in owners and owners[name] != owner:
                    return False
    return True


def _evaluate_match_steps(context, steps, batch):
    if not batch:
        for step in steps:
            for _ in step:
                context.view_layer.update()
        return
    pending = steps
    while pending:
        waiting = []
        for step in pending:
            try:
                next(step)
            except StopIteration:
                continue
            waiting.append(step)
        if waiting:
            context.view_layer.update()
        pending = waiting


def _match_chain_steps(obj, job, matrices, frame, key, previous_q, previous_pole, previous_angle):
    """Yield at evaluation barriers; preserve each chain's original solve order."""
    kind, names, target, pole = job
    solver = [obj.pose.bones[PREFIX + name] for name in names]
    con = solve_bone(obj, names).constraints['SUB IK Solve']
    con.mute = True
    for endpoint in end_constraints(solver[2]):
        endpoint.mute = True
    yield
    seed = [obj.pose.bones[PREFIX + name] for name in limb_path(obj, names)]
    for pb, name in zip(seed, limb_path(obj, names)):
        pb.matrix = matrices[name]
        yield
    # Independent seed channels preserve animated bone length,
    # translation and axial twist without reading FK during playback.
    if key:
        for pb in seed:
            _key(pb, frame, previous_q)
    root, mid, end = [matrices[n].translation for n in names]
    axis = end - root
    if axis.length < 1e-8:
        axis = matrices[names[0]].to_3x3().col[1].normalized()
    else:
        axis.normalize()
    bend = mid - root - axis * (mid - root).dot(axis)
    if bend.length < max((mid-root).length, 1.0) * 1e-5:
        bend = previous_pole.get(target, matrices[names[0]].to_3x3().col[0]).copy()
        bend -= axis * bend.dot(axis)
        if bend.length < 1e-8:
            bend = axis.orthogonal()
    bend.normalize()
    previous_pole[target] = bend.copy()
    control = obj.pose.bones[target]
    control.rotation_mode = 'QUATERNION'
    control.matrix = matrices[names[2]]
    pole_pb = obj.pose.bones[pole]
    m = pole_pb.matrix.copy()
    m.translation = mid + bend * max((mid-root).length + (end-mid).length, 0.5)
    pole_pb.matrix = m
    con.mute = False
    for endpoint in end_constraints(solver[2]):
        endpoint.mute = False
    con.pole_angle = 0.0
    yield
    # Angle from the zero-angle solve to the desired bend plane.
    delta = _angle(solver[1].matrix.translation-root, mid-root, axis)
    if (mid-root-axis*(mid-root).dot(axis)).length < 1e-5:
        delta = _angle(solver[0].matrix.to_3x3().col[0], matrices[names[0]].to_3x3().col[0], axis)
    # Repeated candidates do not need another scene evaluation.
    errors = {}
    def error(angle):
        if angle in errors:
            return errors[angle]
        con.pole_angle = math.atan2(math.sin(angle), math.cos(angle))
        yield
        score = sum(sum((solver[j].matrix.col[i]-matrices[names[j]].col[i]).length_squared for i in range(4)) for j in (0, 1))
        errors[angle] = score
        return score
    def best(candidates):
        scores = []
        for candidate in candidates:
            scores.append((candidate, (yield from error(candidate))))
        return min(scores, key=lambda pair: pair[1])[0]
    candidates = [delta, -delta, previous_angle.get(target, 0.0)]
    angle = yield from best(candidates)
    # Refine both bone orientations, not just the knee position.
    # This handles axial twist and near-straight chains where a
    # position-only pole test has almost no useful signal.
    if (yield from error(angle)) > 1e-9:
        lo, hi = angle - .2, angle + .2
        ratio = (math.sqrt(5.0)-1.0)*.5
        a, b = hi-ratio*(hi-lo), lo+ratio*(hi-lo)
        fa = yield from error(a)
        fb = yield from error(b)
        for _ in range(18):
            if fa < fb:
                hi, b, fb = b, a, fa
                a = hi-ratio*(hi-lo)
                fa = yield from error(a)
            else:
                lo, a, fa = a, b, fb
                b = lo+ratio*(hi-lo)
                fb = yield from error(b)
        angle = yield from best((angle, a, b))
    con.pole_angle = math.atan2(math.sin(angle), math.cos(angle))
    yield
    previous_angle[target] = angle
    if key:
        _key(control, frame, previous_q)
        _key(pole_pb, frame, previous_q)
        con.keyframe_insert('pole_angle', frame=frame, group=solver[1].name)


def match(context, obj, limbs='BOTH', entire=True, key=True, clean=False, _batch=False):
    from . import create_animation_rig as rig, anim_layers_compat
    from ..anim.fcurve_compat import get_all_action_fcurves
    ensure(obj, context, limbs)
    jobs = list(chains(obj, limbs))
    if not jobs:
        raise RuntimeError('No complete IK chains for the requested limbs')
    scene = context.scene
    original = scene.frame_current
    frames = range(scene.frame_start, scene.frame_end + 1) if entire else [original]
    states = [(con, con.mute) for _, con, _ in outputs(obj, limbs)]
    paused = rig._IK_FK_MUTE_SYNC_PAUSED
    rig.pause_ik_fk_mute_sync(True)
    samples = {}
    previous_q, previous_pole, previous_angle = {}, {}, {}
    try:
        with rig.defer_pose_tool_updates(), rig._disable_autokey(context), anim_layers_compat.bind_driving_action_for_bake(obj, context):
            for con, _ in states:
                con.mute = True
            batch = _batch and _can_batch_match(obj, jobs)
            # Capture the entire source before writing any destination channels.
            for frame in frames:
                scene.frame_set(frame)
                context.view_layer.update()
                samples[frame] = {name: obj.pose.bones[name].matrix.copy() for _, names, _, _ in jobs for name in limb_path(obj, names)}
            for frame, matrices in samples.items():
                scene.frame_set(frame)
                steps = [
                    _match_chain_steps(obj, job, matrices, frame, key,
                                       previous_q, previous_pole, previous_angle)
                    for job in jobs
                ]
                _evaluate_match_steps(context, steps, batch)
            if key and obj.animation_data and obj.animation_data.action:
                owned = {PREFIX+n for _, names, _, _ in jobs for n in limb_path(obj, names)} | {n for _, _, target, pole in jobs for n in (target, pole)}
                paths = tuple(obj.pose.bones[n].path_from_id() + '.' for n in owned)
                for fc in get_all_action_fcurves(obj.animation_data.action, id_type='OBJECT'):
                    if fc.data_path.startswith(paths):
                        for point in fc.keyframe_points:
                            point.interpolation = 'LINEAR'
            if entire and key and clean:
                clean_animation(obj, limbs)
            if entire and key:
                from .anim_rig_extras import mark_ik_matched
                mark_ik_matched(obj, limbs)
    finally:
        for con, mute in states:
            con.mute = mute
        rig.pause_ik_fk_mute_sync(paused)
        scene.frame_set(original)
        context.view_layer.update()
    return len(samples) * len(jobs)


def bake(context, obj, names, start, end, clear_constraints=True):
    """Sample first, then write local FK keys parent-first with blends disabled."""
    from . import create_animation_rig as rig, anim_layers_compat
    from . import ik_floor_contact
    names = [n for n in names if n in obj.pose.bones and not n.startswith(PREFIX)]
    root = obj.pose.bones.get('Trans')
    body = root.constraints.get(ik_floor_contact.BODY_CONSTRAINT) if root else None
    if body and any(kind == 'LEGS' and any(n in names for n in group)
                    for kind, group, _, _ in chains(obj)):
        if 'Trans' not in names:
            names.append('Trans')
    else:
        body = None
    names.sort(key=lambda n: len(obj.pose.bones[n].parent_recursive))
    original = context.scene.frame_current
    paused = rig._IK_FK_MUTE_SYNC_PAUSED
    rig.pause_ik_fk_mute_sync(True)
    constraints = [(pb, con, con.mute) for pb, con, _ in outputs(obj) if pb.name in names]
    if body:
        constraints.append((root, body, body.mute))
    samples = {}
    previous = {}
    try:
        with rig._disable_autokey(context), anim_layers_compat.bind_driving_action_for_bake(obj, context):
            for frame in range(int(start), int(end) + 1):
                context.scene.frame_set(frame)
                context.view_layer.update()
                samples[frame] = {n: obj.pose.bones[n].matrix.copy() for n in names}
            for _, con, _ in constraints:
                con.mute = True
            for frame, matrices in samples.items():
                context.scene.frame_set(frame)
                for n in names:
                    pb = obj.pose.bones[n]
                    pb.matrix = matrices[n]
                    context.view_layer.update()
                    _key(pb, frame, previous)
            if clear_constraints:
                for pb, con, _ in constraints:
                    con.driver_remove('influence')
                    pb.constraints.remove(con)
                if body:
                    ik_floor_contact.remove_body(obj)
            else:
                # Caller is baking away IK; leave the output disabled.
                for _, con, _ in constraints:
                    con.mute = True
    except Exception:
        for _, con, was_muted in constraints:
            con.mute = was_muted
        raise
    finally:
        rig.pause_ik_fk_mute_sync(paused)
        context.scene.frame_set(original)
        context.view_layer.update()
    return len(samples) * len(names)


def remove(context, obj, limbs='BOTH'):
    from . import create_animation_rig as rig
    from ..anim.fcurve_compat import get_all_action_fcurves, remove_fcurve
    jobs = list(chains(obj, limbs))
    from . import ik_floor_contact
    ik_floor_contact.remove(context, obj, controls={job[2] for job in jobs})
    names = {PREFIX+n for _, group, _, _ in jobs for n in group}
    names.update(n for _, _, target, pole in jobs for n in (target, pole))
    for pb, con, _ in list(outputs(obj, limbs)):
        con.driver_remove('influence')
        pb.constraints.remove(con)
    paths = tuple(obj.pose.bones[n].path_from_id() + '.' for n in names if n in obj.pose.bones)
    for action in rig._iter_armature_actions(obj):
        for fc in list(get_all_action_fcurves(action, id_type='OBJECT')):
            if paths and fc.data_path.startswith(paths):
                remove_fcurve(action, fc, id_type='OBJECT')
    rig._remove_ik_fk_switch_keys(obj, limbs)
    bpy.ops.object.mode_set(mode='EDIT')
    for n in names:
        if n in obj.data.edit_bones:
            obj.data.edit_bones.remove(obj.data.edit_bones[n])
    bpy.ops.object.mode_set(mode='POSE')
    rig._IK_FK_APPLYING = True
    try:
        for kind in ('ARMS', 'LEGS'):
            if limbs in (kind, 'BOTH'):
                setattr(obj.data, rig._limb_switch_prop(kind), 0.0)
    finally:
        rig._IK_FK_APPLYING = False
    return list(names)
