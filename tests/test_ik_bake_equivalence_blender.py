"""The fast IK bake must produce the same curves as the evaluate-per-bone bake.

blender --background --factory-startup --python-exit-code 1 --python tests/test_ik_bake_equivalence_blender.py

Requires the IK benchmark baseline (.tests/benchmarks/ik_apply/out/baseline.blend);
skips with a clear message when it is absent, since there is no synthetic stand-in
for a real Smash limb chain.

Both branches live in ik_channels.bake and are selected by
_arithmetic_bake_ok, so forcing that predicate is enough to run each in turn
against identical scene state -- no production toggle required.
"""
from pathlib import Path
import importlib
import tempfile

fixture = Path(__file__).with_name('test_addon_registration_blender.py')
exec(compile(fixture.read_text().split('addon_utils.disable(MODULE')[0], str(fixture), 'exec'))

ik_channels = importlib.import_module(MODULE + '.source.extras.ik_channels')
fcurve_compat = importlib.import_module(MODULE + '.source.anim.fcurve_compat')

BASELINE = ROOT / '.tests' / 'benchmarks' / 'ik_apply' / 'out' / 'baseline.blend'

# Pose matrices are single precision, and the two paths reach the same basis by
# different arithmetic, so exact equality is not available. A wrong pole angle
# or a botched parent inversion moves a bone by whole units, not by 1e-5.
POSITION_TOLERANCE = 1e-4

if not BASELINE.exists():
    print(f'SKIP ik bake equivalence, no baseline at {BASELINE}')
    raise SystemExit(0)


def active_armature():
    obj = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
    bpy.context.view_layer.objects.active = obj
    for other in bpy.context.scene.objects:
        other.select_set(other is obj)
    return obj


def curve_values(obj):
    """Every keyed channel sampled at every key, as {(path, index): [(f, v)]}."""
    action = obj.animation_data.action
    out = {}
    for fcurve in fcurve_compat.get_all_action_fcurves(action, id_type='OBJECT'):
        points = [0.0] * (len(fcurve.keyframe_points) * 2)
        fcurve.keyframe_points.foreach_get('co', points)
        out[(fcurve.data_path, fcurve.array_index)] = [
            (points[i * 2], points[i * 2 + 1])
            for i in range(len(fcurve.keyframe_points))]
    return out


def bake_names(obj):
    apply_ik = importlib.import_module(MODULE + '.source.extras.apply_ik_animation')
    return apply_ik.collect_fk_bone_names(obj, limbs='BOTH')


def prepare(path):
    """Baseline plus IK controls and a full-clip match, saved for reuse."""
    bpy.ops.wm.open_mainfile(filepath=str(BASELINE))
    obj = active_armature()
    if bpy.context.object.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')
    ik_channels.create_controls(bpy.context, obj, 'BOTH')
    bpy.ops.sub.fk_to_ik_transfer(
        'EXEC_DEFAULT', cleanup_mode='BOTH', entire_animation=True,
        auto_keyframe=True, clean_animation=False)
    bpy.ops.wm.save_as_mainfile(filepath=str(path))


def run_bake(path, arithmetic):
    original = ik_channels._arithmetic_bake_ok
    ik_channels._arithmetic_bake_ok = lambda obj, names: arithmetic
    try:
        bpy.ops.wm.open_mainfile(filepath=str(path))
        obj = active_armature()
        if bpy.context.object.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        scene = bpy.context.scene
        names = bake_names(obj)
        assert names, 'no FK bones collected to bake'
        ik_channels.bake(bpy.context, obj, names, scene.frame_start, scene.frame_end)
        return curve_values(obj), names
    finally:
        ik_channels._arithmetic_bake_ok = original


with tempfile.TemporaryDirectory(prefix='sub_ik_bake_') as tmp:
    staged = Path(tmp) / 'matched.blend'
    prepare(staged)

    slow, names = run_bake(staged, arithmetic=False)
    fast, _ = run_bake(staged, arithmetic=True)

print(f'baked {len(names)} bones; {len(slow)} channels slow, {len(fast)} fast')

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
assert worst <= POSITION_TOLERANCE, (
    f'fast bake diverges by {worst:.3e} at {worst_where}')
print('ik bake equivalence OK')
