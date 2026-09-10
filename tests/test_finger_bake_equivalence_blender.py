"""The fast finger-slider bake must match the evaluate-per-depth bake.

blender --background --factory-startup --python-exit-code 1 --python tests/test_finger_bake_equivalence_blender.py

Requires the IK benchmark baseline (.tests/benchmarks/ik_apply/out/baseline.blend).
Both branches live in _bake_finger_visual_matrices and are selected by
pose_math.can_replay, so forcing that predicate runs each in turn against
identical scene state.
"""
from pathlib import Path
import importlib
import tempfile

fixture = Path(__file__).with_name('test_addon_registration_blender.py')
exec(compile(fixture.read_text().split('addon_utils.disable(MODULE')[0], str(fixture), 'exec'))

finger_sliders = importlib.import_module(MODULE + '.source.extras.finger_sliders')
pose_math = importlib.import_module(MODULE + '.source.extras.pose_math')
fcurve_compat = importlib.import_module(MODULE + '.source.anim.fcurve_compat')

BASELINE = ROOT / '.tests' / 'benchmarks' / 'ik_apply' / 'out' / 'baseline.blend'
TOLERANCE = 1e-4

if not BASELINE.exists():
    print(f'SKIP finger bake equivalence, no baseline at {BASELINE}')
    raise SystemExit(0)


def active_armature():
    obj = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
    bpy.context.view_layer.objects.active = obj
    for other in bpy.context.scene.objects:
        other.select_set(other is obj)
    return obj


def curve_values(obj):
    action = obj.animation_data.action
    out = {}
    for fcurve in fcurve_compat.get_all_action_fcurves(action, id_type='OBJECT'):
        points = [0.0] * (len(fcurve.keyframe_points) * 2)
        fcurve.keyframe_points.foreach_get('co', points)
        out[(fcurve.data_path, fcurve.array_index)] = [
            (points[i * 2], points[i * 2 + 1])
            for i in range(len(fcurve.keyframe_points))]
    return out


def prepare(path):
    bpy.ops.wm.open_mainfile(filepath=str(BASELINE))
    obj = active_armature()
    if bpy.context.object.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')
    finger_sliders.build_finger_sliders(bpy.context, obj)
    bpy.context.view_layer.update()
    assert finger_sliders.has_finger_slider_constraints(obj), 'no slider constraints built'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))


def run_bake(path, replay):
    original = pose_math.can_replay
    pose_math.can_replay = lambda pose_bones: replay
    try:
        bpy.ops.wm.open_mainfile(filepath=str(path))
        obj = active_armature()
        if bpy.context.object.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        keyed = finger_sliders.bake_finger_slider_keys(bpy.context, obj)
        return curve_values(obj), keyed
    finally:
        pose_math.can_replay = original


with tempfile.TemporaryDirectory(prefix='sub_finger_bake_') as tmp:
    staged = Path(tmp) / 'sliders.blend'
    prepare(staged)
    slow, slow_keyed = run_bake(staged, replay=False)
    fast, fast_keyed = run_bake(staged, replay=True)

print(f'finger bake keyed {slow_keyed} slow, {fast_keyed} fast; '
      f'{len(slow)} vs {len(fast)} channels')
assert slow_keyed == fast_keyed, 'different key counts'

missing = set(slow) - set(fast)
extra = set(fast) - set(slow)
assert not missing, f'fast bake dropped channels: {sorted(missing)[:10]}'
assert not extra, f'fast bake invented channels: {sorted(extra)[:10]}'

worst = 0.0
worst_where = None
for key, slow_points in slow.items():
    fast_points = fast[key]
    assert len(slow_points) == len(fast_points), (
        f'{key}: {len(slow_points)} keys slow vs {len(fast_points)} fast')
    for (slow_frame, slow_value), (fast_frame, fast_value) in zip(slow_points, fast_points):
        assert abs(slow_frame - fast_frame) < 1e-4, f'{key}: frame {slow_frame} vs {fast_frame}'
        difference = abs(slow_value - fast_value)
        if difference > worst:
            worst, worst_where = difference, (key, slow_frame)

print(f'worst channel difference {worst:.3e} at {worst_where}')
assert worst <= TOLERANCE, f'fast finger bake diverges by {worst:.3e} at {worst_where}'
print('finger bake equivalence OK')
