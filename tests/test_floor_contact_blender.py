"""blender --background --factory-startup --python tests/test_floor_contact_blender.py"""
import importlib.util
import importlib
import math
from pathlib import Path
import sys
import tempfile
import types

import bpy
try:
    from bpy_restrict_state import RestrictBlend
except ImportError:
    # Blender 5 dropped bpy_restrict_state. It was only used here to register
    # under the same restricted context an add-on sees at load time; without
    # the module there is nothing to restrict, so a plain no-op stands in.
    from contextlib import nullcontext as RestrictBlend
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parents[1]
# Import real IK/preset modules without registering unrelated import/export tools.
for name, directory in [('floor_test', ROOT), ('floor_test.source', ROOT / 'source'),
                        ('floor_test.source.extras', ROOT / 'source/extras'),
                        ('floor_test.source.anim', ROOT / 'source/anim')]:
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
floor = importlib.import_module('floor_test.source.extras.ik_floor_contact')
channels = importlib.import_module('floor_test.source.extras.ik_channels')
bpy.types.Armature.sub_ik_stretch_legs = bpy.props.BoolProperty(default=False)
bpy.types.Armature.sub_use_ik_legs = bpy.props.FloatProperty(default=1.0)
with RestrictBlend():
    floor.register()
assert bpy.app.timers.is_registered(floor._restore_contacts)


def update():
    bpy.context.view_layer.update()
    return bpy.context.evaluated_depsgraph_get()


def matrix(obj):
    return obj.evaluated_get(update()).matrix_world.copy()


def close(a, b, message='', tol=1e-4):
    assert abs(a-b) < tol, (message, a, b)


data = bpy.data.armatures.new('Contact test')
arm = bpy.data.objects.new('Contact test', data)
bpy.context.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
arm.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
trans = data.edit_bones.new('Trans')
trans.head, trans.tail = (0, 0, 0), (0, 1, 0)
for side, x in [('L', 1), ('R', -1)]:
    parent = trans
    original_parent = trans
    for name, head, tail in [
        ('Leg', (x, 0, 4), (x, -.2, 2.5)),
        ('Knee', (x, -.2, 2.5), (x, 0, 1)),
        ('Foot', (x, 0, 1), (x, 1, 1)),
    ]:
        bone = data.edit_bones.new(channels.PREFIX + name + side)
        bone.head, bone.tail, bone.parent = head, tail, parent
        parent = bone
        original = data.edit_bones.new(name + side)
        original.head, original.tail, original.parent = head, tail, original_parent
        original_parent = original
    target = data.edit_bones.new('FootIK' + side)
    target.head, target.tail = (x, 0, 1), (x, 1, 1)
    pole = data.edit_bones.new('KneeIK' + side)
    pole.head, pole.tail = (x, -3, 2), (x, -3, 2.5)
bpy.ops.object.mode_set(mode='OBJECT')
for side in 'LR':
    mid = arm.pose.bones[channels.PREFIX + 'Knee' + side]
    con = mid.constraints.new('IK')
    con.name = 'SUB IK Solve'
    con.target, con.subtarget = arm, 'FootIK' + side
    con.chain_count = 2
    con.use_stretch = False
    end = arm.pose.bones[channels.PREFIX + 'Foot' + side]
    con = end.constraints.new('COPY_ROTATION')
    con.name = 'SUB IK End Rotation'
    con.target, con.subtarget = arm, 'FootIK' + side
arm.data['sub_independent_ik'] = 2
update()
left = floor.setup_limb(bpy.context, arm, 'FootIKL', 'LEGS', [(0, -.2, -.1), (0, .7, -.1)])
right = floor.setup_limb(bpy.context, arm, 'FootIKR', 'LEGS', [(0, -.2, -.1), (0, .7, -.1)])
channels.wire(arm)
control = arm.pose.bones['FootIKL']
control.location.z = -2
left.softness = 0
close(matrix(left.solved).translation.z, .1, 'floor clamp')
close(matrix(left.raw).translation.z, -1, 'unmodified control')
arm.data.sub_ik_stretch_legs = True
arm.data.update_tag()
end = arm.evaluated_get(update()).pose.bones[channels.PREFIX + 'FootL']
close(end.matrix.translation.z, .1, 'stretch stops at floor')
arm.data.sub_ik_stretch_legs = False
arm.data.update_tag()
arm.sub_floor_contact.enabled = False
close(matrix(left.solved).translation.z, -1, 'disable restores source')
arm.sub_floor_contact.enabled = True
bpy.context.scene.sub_floor_height = 2
close(matrix(left.solved).translation.z, 2.1, 'scene floor')
bpy.context.scene.sub_floor_height = 0
left.softness = .2
control.location.z = -.8
close(matrix(left.solved).translation.z, .175, 'soft approach')
control.rotation_mode = 'XYZ'
control.rotation_euler.x = .5
control.location.z = -2
solved = matrix(left.solved)
for marker in (left.heel, left.toe):
    assert (solved @ marker.location).z >= -1e-4, 'rotated contact penetrates'
floor.capture(bpy.context, arm, left)
left.planted = True
anchor = matrix(left.anchor_heel).translation
control.location.x += 1
solved = matrix(left.solved)
lowest = min((solved @ p.location for p in (left.heel, left.toe)), key=lambda p: p.z)
close(lowest.x, anchor.x, 'plant x')
close(lowest.y, anchor.y, 'plant y')
left.planted = False
left.auto_plant = True
left.release_distance = .5
close(matrix(left.solved).translation.x, matrix(left.raw).translation.x, 'auto release distance')
control.location.x -= 1
close((matrix(left.solved) @ left.heel.location).y, matrix(left.anchor_heel).translation.y, 'auto attachment')
control.location.z = 2
close(matrix(left.solved).translation.z, matrix(left.raw).translation.z, 'auto release height')
control.location.z = -2
left.auto_plant = False
left.align = True
close(matrix(left.oriented).to_euler().x, 0, 'calibrated alignment')
left.align = False
floor.capture(bpy.context, arm, left)
left.planted = left.lock_rotation = True
control.rotation_euler.x = 1
close(matrix(left.oriented).to_euler().x, .5, 'locked rotation')
left.planted = left.lock_rotation = False
control.rotation_euler.x = .5
floor.mirror(bpy.context, arm, left)
for p, q in zip((left.heel, left.toe), (right.heel, right.toe)):
    close(p.location.z, q.location.z, 'symmetric calibration')
saved = floor.serialize(arm)
assert len(saved['limbs']) == 2
for obj in (left.raw, left.oriented, left.solved, right.solved):
    if obj.animation_data:
        for fc in obj.animation_data.drivers:
            assert fc.driver.is_valid, (obj.name, fc.data_path, fc.driver.expression)

# Full object transforms, not just a character at the world origin.
arm.location = (3, -4, 2)
arm.rotation_euler = (.2, -.3, .5)
arm.scale = (2, 2, 2)
control.location.z = -5
solved = matrix(left.solved)
for marker in (left.heel, left.toe):
    assert (solved @ marker.location).z >= -1e-4, 'transformed rig penetration'

# A keyed control produces the same contact on forward/reverse/random seeks.
for frame, z in [(1, -5), (5, 0), (10, -3)]:
    control.location.z = z
    control.keyframe_insert('location', frame=frame)
expected = {}
for frame in [1, 5, 10, 3, 7]:
    bpy.context.scene.frame_set(frame)
    expected[frame] = matrix(left.solved)
for frame in [10, 1, 7, 5, 3]:
    bpy.context.scene.frame_set(frame)
    actual = matrix(left.solved)
    assert max(abs(actual[i][j] - expected[frame][i][j]) for i in range(4) for j in range(4)) < 1e-4

floor.remove(bpy.context, arm)
assert not arm.sub_floor_contact.limbs
assert not any(obj.get('sub_floor_owner') == arm for obj in bpy.data.objects)
con = arm.pose.bones[channels.PREFIX + 'KneeL'].constraints['SUB IK Solve']
assert con.target == arm and con.subtarget == 'FootIKL'
floor.load_calibration(bpy.context, arm, saved)
assert len(arm.sub_floor_contact.limbs) == 2
left, right = arm.sub_floor_contact.limbs
floor.unregister()
with RestrictBlend():
    floor.register()
floor._restore_contacts()  # Simulate the first post-registration timer tick.
left, right = arm.sub_floor_contact.limbs
assert arm.pose.bones[channels.PREFIX + 'KneeL'].constraints['SUB IK Solve'].target == left.solved

# The existing collection-preset format carries calibration and excludes helpers.
presets = importlib.import_module('floor_test.source.extras.collection_presets')
for cls in (presets.SUB_PG_collection_preset_item, presets.SUB_PG_collection_preset_settings):
    bpy.utils.register_class(cls)
bpy.types.Scene.sub_collection_presets = bpy.props.PointerProperty(type=presets.SUB_PG_collection_preset_settings)
preset = presets.build_preset('Contact fixture', arm, bpy.context)
assert preset['sections']['floor_contact']['limbs']
assert not any(' • ' in name for name in preset['armature_fingerprint']['objects'])
floor.remove(bpy.context, arm)
arm.data['sub_independent_ik'] = 0
presets.apply_preset(preset, arm, bpy.context)
assert not arm.sub_floor_contact.limbs, 'calibration waits for IK'
arm.data['sub_independent_ik'] = 2
channels.ensure(arm, bpy.context)
assert len(arm.sub_floor_contact.limbs) == 2
left, right = arm.sub_floor_contact.limbs
del bpy.types.Scene.sub_collection_presets
for cls in (presets.SUB_PG_collection_preset_settings, presets.SUB_PG_collection_preset_item):
    bpy.utils.unregister_class(cls)
arm.animation_data_clear()
arm.matrix_world = Matrix.Identity(4)
control.matrix_basis = Matrix.Identity(4)
control.location.z = -2
floor.setup_body(bpy.context, arm)
arm.sub_floor_contact.adjust_body = True
graph = update()
root_z = (arm.matrix_world @ arm.evaluated_get(graph).pose.bones['Trans'].matrix).translation.z
assert root_z < -.8 and root_z > -1, ('body height', root_z)
end = arm.evaluated_get(graph).pose.bones[channels.PREFIX + 'FootL']
assert (end.matrix.translation - matrix(left.solved).translation).length < .005, 'body reaches target'
arm.data.sub_ik_stretch_legs = True
arm.data.update_tag()
close(arm.evaluated_get(update()).pose.bones['Trans'].matrix.translation.z, 0, 'stretch bypasses body correction')
arm.data.sub_ik_stretch_legs = False
arm.data.update_tag()
arm.sub_floor_contact.adjust_body = False
close((arm.matrix_world @ arm.evaluated_get(update()).pose.bones['Trans'].matrix).translation.z, 0, 'body off')

# Deleting one contact leaves the other limb's named driver paths valid.
floor.remove(bpy.context, arm, controls={'FootIKL'})
right = arm.sub_floor_contact.limbs[0]
arm.pose.bones['FootIKR'].location.z = -2
close(matrix(right.solved).translation.z, .1, 'partial removal')
with tempfile.TemporaryDirectory(prefix='sub_floor_test_') as temp:
    blend = str(Path(temp) / 'contact.blend')
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    arm = bpy.data.objects['Contact test']
    right = arm.sub_floor_contact.limbs[0]
    close(matrix(right.solved).translation.z, .1, 'blend reload')

# Optional existing bake/remove workflow must retain opt-in body correction.
floor.load_calibration(bpy.context, arm, saved)
names = [prefix + side for side in 'LR' for prefix in ('Leg', 'Knee', 'Foot')]
for name in names:
    con = arm.pose.bones[name].constraints.new('COPY_TRANSFORMS')
    con.name = channels.OUTPUT
    con.target, con.subtarget = arm, channels.PREFIX + name
    con.owner_space = con.target_space = 'POSE'
channels.wire(arm)
floor.setup_body(bpy.context, arm)
arm.sub_floor_contact.adjust_body = True
bpy.context.scene.frame_set(3)
update()
expected = {n: arm.pose.bones[n].matrix.copy() for n in names + ['Trans']}
channels.bake(bpy.context, arm, names, 3, 3)
floor.remove(bpy.context, arm)
update()
for name, expected_matrix in expected.items():
    actual = arm.pose.bones[name].matrix
    assert max(abs(actual[i][j]-expected_matrix[i][j]) for i in range(4) for j in range(4)) < .005, ('baked contact', name)
floor.unregister()
print('FLOOR CONTACT TESTS PASSED')
