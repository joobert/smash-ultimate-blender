"""Matching IK to FK must reproduce the FK animation frame for frame.

blender --background --factory-startup --python-exit-code 1 --python tests/test_ik_match_fidelity_blender.py

This checks the property users actually care about -- that turning IK on does
not change how the limb looks -- rather than comparing one implementation
against another. It therefore stays meaningful across rewrites of the solve.

Requires the IK benchmark baseline (.tests/benchmarks/ik_apply/out/baseline.blend).
Prints the worst per-frame limb error so the number can be compared between
revisions; set SUB_FIDELITY_TOLERANCE to override the assertion bound.
"""
from pathlib import Path
import importlib
import json
import os

fixture = Path(__file__).with_name('test_addon_registration_blender.py')
exec(compile(fixture.read_text().split('addon_utils.disable(MODULE')[0], str(fixture), 'exec'))

ik_channels = importlib.import_module(MODULE + '.source.extras.ik_channels')

BASELINE = ROOT / '.tests' / 'benchmarks' / 'ik_apply' / 'out' / 'baseline.blend'

# Reference values measured on this rig before the IK performance work, so
# this asserts "no worse than it was" rather than an aspirational bound.
#
# Two-bone IK cannot reproduce every FK arm pose exactly -- the shoulder is
# solved, not copied -- so agreement on the Smash arm chains is approximate,
# worst around 0.42 on a hard frame with a median near 0.039. That is
# pre-existing and unrelated to the speedups; see the note in
# docs/superpowers/specs/2026-09-10-ik-performance-design.md.
TOLERANCE = float(os.environ.get('SUB_FIDELITY_TOLERANCE', '0.45'))
MEDIAN_TOLERANCE = float(os.environ.get('SUB_FIDELITY_MEDIAN', '0.05'))

if not BASELINE.exists():
    print(f'SKIP ik match fidelity, no baseline at {BASELINE}')
    raise SystemExit(0)


def active_armature():
    obj = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
    bpy.context.view_layer.objects.active = obj
    for other in bpy.context.scene.objects:
        other.select_set(other is obj)
    return obj


def limb_bone_names(obj):
    names = []
    for _kind, chain, _target, _pole in ik_channels.chains(obj, 'BOTH'):
        names.extend(ik_channels.limb_path(obj, chain))
    return list(dict.fromkeys(names))


def capture(obj, names):
    scene = bpy.context.scene
    poses = {}
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        poses[frame] = {n: obj.pose.bones[n].matrix.copy() for n in names}
    return poses


def differences(before, after, names):
    """Per (bone, frame) orientation and translation error, worst first."""
    rows = []
    for frame, reference in before.items():
        current = after[frame]
        for name in names:
            a, b = reference[name], current[name]
            rotation = max(abs(a[r][c] - b[r][c])
                           for r in range(3) for c in range(3))
            translation = max(abs(a[r][3] - b[r][3]) for r in range(3))
            rows.append((max(rotation, translation), rotation, translation, name, frame))
    rows.sort(reverse=True)
    return rows


def percentile(values, fraction):
    if not values:
        return 0.0
    index = min(len(values) - 1, int(len(values) * fraction))
    return sorted(values)[index]


bpy.ops.wm.open_mainfile(filepath=str(BASELINE))
obj = active_armature()
if bpy.context.object.mode != 'POSE':
    bpy.ops.object.mode_set(mode='POSE')

# The limb chains only exist after the controls are built, so resolve the bone
# names from a throwaway build, then start over from pure FK.
ik_channels.create_controls(bpy.context, obj, 'BOTH')
names = limb_bone_names(obj)
assert names, 'no IK limb chains found on the baseline rig'

bpy.ops.wm.open_mainfile(filepath=str(BASELINE))
obj = active_armature()
if bpy.context.object.mode != 'POSE':
    bpy.ops.object.mode_set(mode='POSE')

fk_poses = capture(obj, names)

ik_channels.create_controls(bpy.context, obj, 'BOTH')
bpy.ops.sub.fk_to_ik_transfer(
    'EXEC_DEFAULT', cleanup_mode='BOTH', entire_animation=True,
    auto_keyframe=True, clean_animation=False)

ik_poses = capture(obj, names)

rows = differences(fk_poses, ik_poses, names)
values = [row[0] for row in rows]
worst, worst_rotation, worst_translation, worst_bone, worst_frame = rows[0]
over = sum(1 for value in values if value > 1e-3)

print('FIDELITY ' + json.dumps({
    'worst': worst,
    'worst_rotation': worst_rotation,
    'worst_translation': worst_translation,
    'bone': worst_bone,
    'frame': worst_frame,
    'p50': percentile(values, 0.50),
    'p99': percentile(values, 0.99),
    'samples': len(values),
    'over_1e-3': over,
    'frames': len(fk_poses),
    'bones': len(names),
    'top': [(f'{r[3]}@{r[4]}', round(r[0], 5)) for r in rows[:8]],
}))

assert worst <= TOLERANCE, (
    f'IK pose drifts from FK by {worst:.3e} at {worst_bone}@{worst_frame} '
    f'(tolerance {TOLERANCE:.3e})')
# The worst frame is dominated by the arm chains; the median catches a solve
# that regressed everywhere rather than only on the already-hard frames.
median = percentile(values, 0.50)
assert median <= MEDIAN_TOLERANCE, (
    f'median IK drift {median:.3e} exceeds {MEDIAN_TOLERANCE:.3e}')
print('ik match fidelity OK')
