"""pose_math.basis_from_world must equal the pose_bone.matrix setter.

blender --background --factory-startup --python-exit-code 1 --python tests/test_pose_math_blender.py

Runs against the real Smash rig when the IK benchmark baseline is present
(.tests/benchmarks/ik_apply/out/baseline.blend), and always against a
synthetic rig covering the inheritance flags the module deliberately refuses.
"""
from pathlib import Path
import importlib
import random

from mathutils import Euler, Matrix, Vector

fixture = Path(__file__).with_name('test_addon_registration_blender.py')
exec(compile(fixture.read_text().split('addon_utils.disable(MODULE')[0], str(fixture), 'exec'))

pose_math = importlib.import_module(MODULE + '.source.extras.pose_math')

TOLERANCE = 1e-5
BASELINE = ROOT / '.tests' / 'benchmarks' / 'ik_apply' / 'out' / 'baseline.blend'


def max_difference(a: Matrix, b: Matrix) -> float:
    return max(abs(a[r][c] - b[r][c]) for r in range(4) for c in range(4))


def ground_truth_basis(context, pose_bone, target):
    """What Blender itself produces, via the setter plus a depsgraph update."""
    pose_bone.matrix = target
    context.view_layer.update()
    return pose_bone.matrix_basis.copy()


def check_armature(context, obj, label):
    """Compare both paths for every bone, at a few pseudo-random poses."""
    rng = random.Random(4242)
    checked = 0
    unsupported = []
    worst = 0.0
    worst_bone = None

    for pose_bone in obj.pose.bones:
        parent = pose_bone.parent

        # Give the parent a non-trivial pose so the test would catch a formula
        # that only happens to work at rest.
        if parent is not None:
            parent.rotation_mode = 'XYZ'
            parent.rotation_euler = Euler([rng.uniform(-1.0, 1.0) for _ in range(3)])
            parent.location = Vector([rng.uniform(-0.3, 0.3) for _ in range(3)])
            context.view_layer.update()

        parent_world = parent.matrix.copy() if parent is not None else None

        target = (Matrix.Translation([rng.uniform(-2.0, 2.0) for _ in range(3)])
                  @ Euler([rng.uniform(-2.0, 2.0) for _ in range(3)]).to_matrix().to_4x4())

        computed = pose_math.basis_from_world(pose_bone, target, parent_world)
        expected = ground_truth_basis(context, pose_bone, target)

        if computed is None:
            unsupported.append(pose_bone.name)
            continue

        difference = max_difference(computed, expected)
        if difference > worst:
            worst, worst_bone = difference, pose_bone.name
        assert difference <= TOLERANCE, (
            f'{label}: {pose_bone.name} basis differs by {difference:.3e}\n'
            f'computed:\n{computed}\nexpected:\n{expected}')
        checked += 1

        # The forward direction must agree too, or bake would drift.
        round_tripped = pose_math.world_from_basis(pose_bone, computed, parent_world)
        assert max_difference(round_tripped, target) <= TOLERANCE, (
            f'{label}: {pose_bone.name} does not round-trip')

    print(f'{label}: {checked} bones matched (worst {worst:.3e} on {worst_bone}), '
          f'{len(unsupported)} unsupported {unsupported[:8]}')
    return checked, unsupported


def build_synthetic():
    """A chain per inheritance mode, so the refusal path is exercised."""
    armature = bpy.data.armatures.new('synthetic')
    obj = bpy.data.objects.new('synthetic', armature)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    previous = None
    for index in range(8):
        bone = armature.edit_bones.new(f'b{index}')
        bone.head = Vector((index * 0.1, index * 0.5, 0.0))
        bone.tail = Vector((index * 0.1, index * 0.5 + 0.5, 0.0))
        bone.roll = index * 0.3
        bone.parent = previous
        previous = bone
    bpy.ops.object.mode_set(mode='POSE')

    # b0..b2 keep every default, so the arithmetic path is exercised on a
    # multi-level chain. b3..b7 each break exactly one assumption.
    modes = ['FULL', 'FULL', 'FULL', 'FULL', 'FULL', 'NONE', 'AVERAGE', 'ALIGNED']
    for bone, mode in zip(armature.bones, modes):
        bone.inherit_scale = mode
    armature.bones['b3'].use_inherit_rotation = False
    armature.bones['b4'].use_local_location = False
    return obj


# --- Synthetic rig: correctness plus the deliberate refusals ------------------

synthetic = build_synthetic()
checked, unsupported = check_armature(bpy.context, synthetic, 'synthetic')
assert checked >= 3, 'default-case bones should have been verified'
for name in ('b0', 'b1', 'b2'):
    assert name not in unsupported, f'{name} is a default-case bone'
for name in ('b3', 'b4', 'b5', 'b6', 'b7'):
    assert name in unsupported, f'{name} has non-default flags and must be refused'

# --- Real Smash rig, when the benchmark baseline has been built ---------------

if BASELINE.exists():
    bpy.ops.wm.open_mainfile(filepath=str(BASELINE))
    armatures = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    assert armatures, 'baseline.blend has no armature'
    for obj in armatures:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        if bpy.context.object.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        checked, unsupported = check_armature(bpy.context, obj, f'smash:{obj.name}')
        assert checked > 0, f'{obj.name}: no bones took the arithmetic path'
        assert not unsupported, (
            f'{obj.name}: real rig bones fell back, bake would still evaluate: '
            f'{unsupported}')
else:
    print(f'SKIP real-rig check, no baseline at {BASELINE}')

print('pose_math equivalence OK')
