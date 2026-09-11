"""Non-destructive, dependency-graph evaluated IK contact targets.

Controls -> orientation -> calibrated points -> contact target -> IK solver.
There is no frame handler writing the pose and no dependency on playback order.
Only the independent solver's owned constraints are redirected. Original keys,
pole targets, stretch drivers and FK/IK blending remain authoritative.
"""
import math
import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from mathutils import Matrix, Vector

COLLECTION = 'IK Floor Contact'
PENDING = 'sub_floor_calibration'
BODY_CONSTRAINT = 'SUB Floor Body Height'


def _changed(self, context):
    self.id_data.update_tag()


def _driver(owner, prop, expression, variables, index=None):
    driver = (owner.driver_add(prop, index) if index is not None else owner.driver_add(prop)).driver
    driver.type = 'SCRIPTED'
    for variable in list(driver.variables):
        driver.variables.remove(variable)
    for name, spec in variables.items():
        var = driver.variables.new()
        var.name = name
        target = var.targets[0]
        if len(spec) == 2:
            var.type = 'SINGLE_PROP'
            target.id_type = spec[0].id_type
            target.id, target.data_path = spec
        else:
            var.type = 'TRANSFORMS'
            target.id, target.transform_type, target.bone_target = spec
            target.transform_space = 'WORLD_SPACE'
    driver.expression = expression


def _copy(owner, type_, name, target, bone=''):
    con = owner.constraints.get(name) or owner.constraints.new(type_)
    con.name = name
    con.target = target
    con.subtarget = bone
    con.owner_space = con.target_space = 'WORLD'
    return con


def _empty(context, arm, name, size, visible=False):
    collection = bpy.data.collections.get(COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION)
    if collection.name not in context.scene.collection.children:
        context.scene.collection.children.link(collection)
    obj = bpy.data.objects.new(arm.name + ' • ' + name, None)
    collection.objects.link(obj)
    obj['sub_floor_owner'] = arm
    obj.empty_display_type = 'SPHERE' if visible else 'PLAIN_AXES'
    obj.empty_display_size = size if visible else 0.0
    obj.hide_render = True
    obj.hide_select = not visible
    obj.show_in_front = True
    return obj


def _visibility(self, context):
    for limb in self.limbs:
        for marker in (limb.heel, limb.toe, limb.anchor):
            if marker:
                marker.hide_set(not self.show_markers)


class SUB_PG_floor_limb(PropertyGroup):
    control: StringProperty()
    kind: StringProperty()
    mirror_source: StringProperty()
    expanded: BoolProperty(name='Contact Settings', default=False, options=set())
    enabled: BoolProperty(name='Floor', default=True, update=_changed)
    planted: BoolProperty(name='Planted', default=False, update=_changed,
                          description='Hold the contact marker; can be keyframed')
    auto_plant: BoolProperty(name='Auto Plant at Marker', default=False, update=_changed,
                            description='Plant near this contact marker; release on lift or distance. Deterministic when scrubbing')
    softness: FloatProperty(name='Contact Softness', default=0.05, min=0.0, subtype='DISTANCE', update=_changed,
                            description='Height of the smooth contact transition; zero gives a hard floor')
    release_height: FloatProperty(name='Release Height', default=0.15, min=0.0001, subtype='DISTANCE', update=_changed)
    release_distance: FloatProperty(name='Release Distance', default=0.5, min=0.0001, subtype='DISTANCE', update=_changed)
    resistance: FloatProperty(name='Horizontal Resistance', default=0.0, min=0.0, max=1.0, update=_changed,
                              description='At contact, resist movement away from the plant marker', subtype='FACTOR')
    align: BoolProperty(name='Align to Floor', default=False, update=_changed)
    lock_rotation: BoolProperty(name='Lock Planted Rotation', default=False, update=_changed)
    rolling: BoolProperty(name='Heel / Toe Roll', default=True, update=_changed,
                         description='Keep the lowest calibrated point at the plant marker while rotating')
    pin_toe: BoolProperty(name='Pin Toe', default=False, update=_changed,
                         description='Use the calibrated toe marker as the fixed floor-contact pivot')
    raw: PointerProperty(type=bpy.types.Object)
    oriented: PointerProperty(type=bpy.types.Object)
    heel: PointerProperty(type=bpy.types.Object)
    toe: PointerProperty(type=bpy.types.Object)
    anchor: PointerProperty(type=bpy.types.Object)
    anchor_heel: PointerProperty(type=bpy.types.Object)
    anchor_toe: PointerProperty(type=bpy.types.Object)
    alignment: PointerProperty(type=bpy.types.Object)
    solved: PointerProperty(type=bpy.types.Object)


class SUB_PG_floor_contact(PropertyGroup):
    enabled: BoolProperty(name='Floor Contact', default=True, update=_changed)
    calibrating: BoolProperty(name='Calibrating', default=False, update=_changed)
    adjust_body: BoolProperty(name='Adjust Body Height', default=False, update=_changed,
                             description='Allow vertical Trans correction to reach planted feet with stretch disabled')
    body_target: PointerProperty(type=bpy.types.Object)
    show_markers: BoolProperty(name='Show Contact Markers', default=True, update=_visibility)
    limbs: CollectionProperty(type=SUB_PG_floor_limb)


def _point_defaults(arm, control, floor):
    """An editable starting guess, not a mesh-contact claim."""
    matrix = arm.matrix_world @ arm.pose.bones[control].matrix
    length = max(arm.data.bones[control].length, 0.1)
    inverse = matrix.inverted_safe()
    points = []
    for distance in (-0.15, 0.45):
        point = matrix @ Vector((0, length * distance, 0))
        point.z = floor
        points.append(tuple(inverse @ point))
    return points


def setup_limb(context, arm, control, kind, points=None):
    props = arm.sub_floor_contact
    limb = next((item for item in props.limbs if item.control == control), None)
    if limb is not None and all((limb.raw, limb.oriented, limb.heel, limb.toe, limb.anchor,
                                 limb.anchor_heel, limb.anchor_toe, limb.alignment, limb.solved)):
        return limb
    if limb is not None:
        remove(context, arm, controls={control})
    limb = props.limbs.add()
    limb.name = limb.control = control
    limb.kind = kind
    limb.expanded = kind == 'LEGS' and 'IKL' in control
    limb.enabled = kind == 'LEGS'
    size = max(arm.data.bones[control].length * 0.07, 0.025)
    for attr, label, visible in (
        ('raw', 'Input', False), ('oriented', 'Orientation', False),
        ('heel', 'Heel' if kind == 'LEGS' else 'Palm', True),
        ('toe', 'Toe' if kind == 'LEGS' else 'Fingers', True),
        ('anchor', 'Plant', True), ('solved', 'Contact', False),
        ('anchor_heel', 'Planted Heel', False), ('anchor_toe', 'Planted Toe', False),
        ('alignment', 'Calibrated Orientation', False),
    ):
        setattr(limb, attr, _empty(context, arm, control + ' ' + label, size, visible))
    from . import ik_channels
    job = next((job for job in ik_channels.chains(arm) if job[2] == control), None)
    source = ik_channels.endpoint_target(arm, job[1], control) if job else control
    _copy(limb.raw, 'COPY_TRANSFORMS', 'IK Input', arm, source)
    _copy(limb.oriented, 'COPY_TRANSFORMS', 'IK Orientation', limb.raw)
    points = points or _point_defaults(arm, control, context.scene.sub_floor_height)
    for marker, point in zip((limb.heel, limb.toe), points):
        marker.parent = limb.oriented
        marker.matrix_parent_inverse = Matrix.Identity(4)
        marker.location = point
    for marker, source in ((limb.anchor_heel, limb.heel), (limb.anchor_toe, limb.toe)):
        marker.parent = limb.anchor
        for index in range(3):
            fc = marker.driver_add('location', index)
            driver = fc.driver
            driver.type = 'AVERAGE'
            var = driver.variables.new()
            var.type = 'SINGLE_PROP'
            var.targets[0].id = source
            var.targets[0].data_path = 'location[' + str(index) + ']'
    limb.alignment.matrix_world = arm.matrix_world @ arm.pose.bones[control].matrix
    limb.anchor.empty_display_type = 'CIRCLE'
    limb.anchor.empty_display_size = size * 2
    _copy(limb.solved, 'COPY_TRANSFORMS', 'IK Source', limb.oriented)
    configure(context.scene, arm, limb)
    arm.update_tag()
    context.view_layer.update()
    capture(context, arm, limb)
    rewire(arm)
    _visibility(props, context)
    return limb


def configure(scene, arm, limb):
    """All live math uses native drivers with explicit dependency variables."""
    path = 'sub_floor_contact.limbs[' + json.dumps(limb.name) + ']'
    common = {
        'on': (arm, 'sub_floor_contact.enabled'),
        'cal': (arm, 'sub_floor_contact.calibrating'),
        'en': (arm, path + '.enabled'),
        'pin': (arm, path + '.planted'),
        'auto': (arm, path + '.auto_plant'),
        's': (arm, path + '.softness'),
        'rh': (arm, path + '.release_height'),
        'rd': (arm, path + '.release_distance'),
        'friction': (arm, path + '.resistance'),
        'toe_pin': (arm, path + '.pin_toe'),
        'floor': (scene, 'sub_floor_height'),
        'hz': (limb.heel, 'LOC_Z', ''), 'tz': (limb.toe, 'LOC_Z', ''),
        'hx': (limb.heel, 'LOC_X', ''), 'tx': (limb.toe, 'LOC_X', ''),
        'hy': (limb.heel, 'LOC_Y', ''), 'ty': (limb.toe, 'LOC_Y', ''),
        'x': (limb.oriented, 'LOC_X', ''), 'y': (limb.oriented, 'LOC_Y', ''),
        'z': (limb.oriented, 'LOC_Z', ''),
        'hax': (limb.anchor_heel, 'LOC_X', ''), 'hay': (limb.anchor_heel, 'LOC_Y', ''),
        'tax': (limb.anchor_toe, 'LOC_X', ''), 'tay': (limb.anchor_toe, 'LOC_Y', ''),
        'roll': (arm, path + '.rolling'),
    }

    def drive(owner, prop, expression, extra=None):
        # Avoid false dependencies (notably orientation -> contact points -> orientation).
        import re
        variables = dict(common, **(extra or {}))
        used = set(re.findall(r'\b[A-Za-z_]\w*\b', expression))
        _driver(owner, prop, expression, {k: v for k, v in variables.items() if k in used})

    # Alignment is a correction relative to the calibrated sole, not bone Euler zero.
    align = _copy(limb.oriented, 'COPY_ROTATION', 'Floor Alignment', limb.alignment)
    align.use_z = False
    drive(align, 'influence', 'on*en*(1-cal)*align', {'align': (arm, path + '.align')})
    lock = _copy(limb.oriented, 'COPY_ROTATION', 'Plant Rotation', limb.anchor)
    drive(lock, 'influence', 'on*en*(1-cal)*pin*lock', {'lock': (arm, path + '.lock_rotation')})


    # A stable local reference for horizontal contact: lowest point for rolling,
    # midpoint for a rigid plant. No stateful "previous frame" accumulator.
    for name, expression in (
        ('px', 'tx if toe_pin else ((hx if hz<=tz else tx) if roll else (hx+tx)/2)'),
        ('py', 'ty if toe_pin else ((hy if hz<=tz else ty) if roll else (hy+ty)/2)'),
        ('ax', 'tax if toe_pin else ((hax if hz<=tz else tax) if roll else (hax+tax)/2)'),
        ('ay', 'tay if toe_pin else ((hay if hz<=tz else tay) if roll else (hay+tay)/2)'),
        ('d', '(tz if toe_pin else min(hz,tz))-floor'),
        ('attachment', 'max(0,min(1,(rh-max(0,d))/max(s,0.000001),(rd-sqrt((px-ax)**2+(py-ay)**2))/max(s,0.000001))) if auto else 0'),
        ('ease', 'attachment*attachment*(3-2*attachment)'),
        ('weight', '1 if pin or toe_pin else max(ease,friction*max(0,1-max(0,d)/max(rh,0.000001)))'),
        ('height', 'z-(tz if toe_pin else min(hz,tz))+floor+(0 if pin or toe_pin or d<=0 else (1-ease)*(d*d/s*(2-d/s) if s>0 and d<s else d))'),
    ):
        limb.solved[name] = 0.0
        drive(limb.solved, '["' + name + '"]', expression)
        common[name] = (limb.solved, '["' + name + '"]')
    for axis, raw, point, anchor in (('x', 'x', 'px', 'ax'), ('y', 'y', 'py', 'ay')):
        con = limb.solved.constraints.new('LIMIT_LOCATION')
        con.name = 'Floor Plant ' + axis.upper()
        con.owner_space = 'WORLD'
        setattr(con, 'use_min_' + axis, True)
        setattr(con, 'use_max_' + axis, True)
        for bound in ('min_', 'max_'):
            drive(con, bound + axis, f'{anchor}-({point}-{raw})')
        drive(con, 'influence', 'on*en*(1-cal)*weight')

    con = limb.solved.constraints.new('LIMIT_LOCATION')
    con.name = 'Floor Height'
    con.owner_space = 'WORLD'
    con.use_min_z = con.use_max_z = True
    # d -> s*(2*t^2-t^3) is C1 at both 0 and s, never penetrates,
    # and does not alter a target outside the softness zone.
    for prop in ('min_z', 'max_z'):
        drive(con, prop, 'height')
    drive(con, 'influence', 'on*en*(1-cal)')


def capture(context, arm, limb):
    context.view_layer.update()
    graph = context.evaluated_depsgraph_get()
    points = [p.evaluated_get(graph).matrix_world.translation.copy() for p in (limb.heel, limb.toe)]
    matrix = limb.oriented.evaluated_get(graph).matrix_world.copy()
    matrix.translation.z += context.scene.sub_floor_height - min(p.z for p in points)
    limb.anchor.matrix_world = matrix


def rewire(arm):
    """Reconnect after existing IK repair/recreation without touching user constraints."""
    from . import ik_channels
    if not hasattr(arm, 'sub_floor_contact'):
        return
    by_control = {limb.control: limb for limb in arm.sub_floor_contact.limbs if limb.solved}
    for _, names, control, _ in ik_channels.chains(arm):
        limb = by_control.get(control)
        if not limb:
            continue
        path = ik_channels.limb_path(arm, names)
        mid = arm.pose.bones.get(ik_channels.PREFIX + path[-2]) if len(path) >= 2 else None
        end = arm.pose.bones.get(ik_channels.PREFIX + names[2])
        constraints = ([mid.constraints.get('SUB IK Solve')] if mid else [])
        constraints += ik_channels.end_constraints(end) if end else []
        for con in constraints:
            if con:
                if con.target != limb.solved or con.subtarget:
                    con.target = limb.solved
                    con.subtarget = ''
                if con.type != 'IK':
                    con.target_space = con.owner_space = 'WORLD'


def setup_body(context, arm):
    """Optional vertical correction using unconstrained channel references.

    References read local animation channels, never the corrected Trans matrix.
    Unsupported parent inheritance is rejected rather than creating feedback.
    """
    from . import ik_channels
    root = arm.pose.bones.get('Trans')
    if root is None or root.parent:
        raise ValueError('Body adjustment requires an unparented Trans bone')
    jobs = [job for job in ik_channels.chains(arm) if job[0] == 'LEGS'
            and any(l.control == job[2] for l in arm.sub_floor_contact.limbs)]
    needed = {}
    if not jobs:
        raise ValueError('Set up foot contact before body adjustment')
    for _, names, control, _ in jobs:
        if arm.pose.bones[control].parent or arm.pose.bones[control].constraints:
            raise ValueError('Body adjustment requires independent, unconstrained foot controls')
        upper = arm.pose.bones.get(names[0])
        if upper is None or root not in upper.parent_recursive:
            raise ValueError('Legs must descend from Trans for body adjustment')
        for bone in [upper] + list(upper.parent_recursive):
            if bone.bone.inherit_scale != 'FULL' or not bone.bone.use_inherit_rotation or not bone.bone.use_local_location:
                raise ValueError('Body adjustment requires standard transform inheritance above the legs')
            if bone.constraints and bone != upper:
                if any(c.name != BODY_CONSTRAINT for c in bone.constraints):
                    raise ValueError('Body adjustment cannot bypass custom constraints above the legs')
            needed[bone.name] = bone
    remove_body(arm)
    ghosts = {}
    for bone in sorted(needed.values(), key=lambda p: len(p.parent_recursive)):
        ghost = _empty(context, arm, bone.name + ' Body Reference', 0)
        ghost['sub_floor_body'] = True
        ghosts[bone.name] = ghost
        ghost.parent = ghosts.get(bone.parent.name) if bone.parent else arm
        ghost.matrix_parent_inverse = (bone.parent.bone.matrix_local.inverted_safe() @ bone.bone.matrix_local
                                       if bone.parent else bone.bone.matrix_local.copy())
        ghost.rotation_mode = bone.rotation_mode
        rotation = ('rotation_quaternion', 4) if bone.rotation_mode == 'QUATERNION' else (
            ('rotation_axis_angle', 4) if bone.rotation_mode == 'AXIS_ANGLE' else ('rotation_euler', 3))
        for prop, size in (('location', 3), ('scale', 3), rotation):
            for index in range(size):
                driver = ghost.driver_add(prop, index).driver
                driver.type = 'AVERAGE'
                var = driver.variables.new()
                var.type = 'SINGLE_PROP'
                var.targets[0].id = arm
                var.targets[0].data_path = bone.path_from_id() + '.' + prop + '[' + str(index) + ']'
    target = _empty(context, arm, 'Body Height', 0)
    target['sub_floor_body'] = True
    arm.sub_floor_contact.body_target = target
    limits = []
    for index, (_, names, control, _) in enumerate(jobs):
        limb = next(l for l in arm.sub_floor_contact.limbs if l.control == control)
        hip = ghosts[names[0]]
        # Scale-aware total reach measured from unmodified rest lengths. The
        # strict no-stretch solver remains responsible for the final knee pose.
        length = arm.data.bones[names[0]].length + arm.data.bones[names[1]].length
        path = 'sub_floor_contact.limbs[' + json.dumps(limb.name) + ']'
        variables = {'en': (arm, path + '.enabled'), 'reach': (target, '["reach' + str(index) + '"]')}
        for prefix, obj in (('h', hip), ('t', limb.solved)):
            for axis in 'xyz':
                variables[prefix + axis] = (obj, 'LOC_' + axis.upper(), '')
        # Use the smallest object scale for conservative reach on scaled rigs.
        target['reach' + str(index)] = length
        _driver(target, '["reach' + str(index) + '"]', str(length) + '*min(abs(sx),abs(sy),abs(sz))',
                {s: (arm, 'SCALE_' + a, '') for s, a in zip(('sx', 'sy', 'sz'), 'XYZ')})
        name = 'drop' + str(index)
        target[name] = 0.0
        _driver(target, '["' + name + '"]',
                'min(0,tz+sqrt(max(0,reach*reach-(tx-hx)**2-(ty-hy)**2))-hz) if en else 0', variables)
        limits.append(name)
    driver = target.driver_add('location', 2).driver
    driver.type = 'SCRIPTED'
    for name in limits:
        var = driver.variables.new()
        var.name = name
        var.type = 'SINGLE_PROP'
        var.targets[0].id = target
        var.targets[0].data_path = '["' + name + '"]'
    driver.expression = 'min(0,' + ','.join(limits) + ')' if limits else '0'
    con = _copy(root, 'COPY_LOCATION', BODY_CONSTRAINT, target)
    con.use_x = con.use_y = False
    con.use_offset = True
    variables = {'on': (arm, 'sub_floor_contact.enabled'), 'cal': (arm, 'sub_floor_contact.calibrating'),
                 'body': (arm, 'sub_floor_contact.adjust_body')}
    # Stretch is still controlled by the existing rig setting.
    if hasattr(arm.data, 'sub_ik_stretch_legs'):
        variables['stretch'] = (arm.data, 'sub_ik_stretch_legs')
    _driver(con, 'influence', 'on*(1-cal)*body' + ('*(1-stretch)' if 'stretch' in variables else ''), variables)
    arm.update_tag()


def remove_body(arm):
    root = arm.pose.bones.get('Trans')
    if root:
        con = root.constraints.get(BODY_CONSTRAINT)
        if con:
            con.driver_remove('influence')
            root.constraints.remove(con)
    for obj in list(bpy.data.objects):
        if obj.get('sub_floor_owner') == arm and obj.get('sub_floor_body'):
            bpy.data.objects.remove(obj, do_unlink=True)
    arm.sub_floor_contact.body_target = None


def remove(context, arm, controls=None):
    props = arm.sub_floor_contact
    if controls is None or any(l.kind == 'LEGS' and l.control in controls for l in props.limbs):
        remove_body(arm)
    for index in reversed(range(len(props.limbs))):
        limb = props.limbs[index]
        if controls is not None and limb.control not in controls:
            continue
        for bone in arm.pose.bones:
            for con in bone.constraints:
                if getattr(con, 'target', None) == limb.solved and limb.solved:
                    con.target = arm
                    con.subtarget = limb.control
                    if con.type != 'IK':
                        con.target_space = con.owner_space = 'POSE'
        for obj in (limb.solved, limb.heel, limb.toe, limb.oriented, limb.raw,
                    limb.anchor_heel, limb.anchor_toe, limb.anchor, limb.alignment):
            if obj and obj.get('sub_floor_owner') == arm:
                bpy.data.objects.remove(obj, do_unlink=True)
        props.limbs.remove(index)
    if PENDING in arm:
        data = json.loads(arm[PENDING])
        data['limbs'] = [item for item in data.get('limbs', [])
                         if controls is not None and item['control'] not in controls]
        arm[PENDING] = json.dumps(data)
    arm.update_tag()


def serialize(arm):
    if not hasattr(arm, 'sub_floor_contact'):
        return {}
    limbs = []
    for limb in arm.sub_floor_contact.limbs:
        if not limb.heel or not limb.toe:
            continue
        limbs.append({'control': limb.control, 'kind': limb.kind,
                      'points': [list(p.location) for p in (limb.heel, limb.toe)],
                      'alignment': list(limb.alignment.rotation_euler),
                      'softness': limb.softness, 'rolling': limb.rolling,
                      'pin_toe': limb.pin_toe})
    if not limbs:
        return json.loads(arm.get(PENDING, '{}'))
    return {'version': 1, 'limbs': limbs}


def load_calibration(context, arm, data):
    """Presets may load before IK exists; retain calibration until controls exist."""
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Unsupported floor calibration version')
    for item in data.get('limbs', []):
        points = item.get('points', [])
        if (item.get('kind') not in {'LEGS', 'ARMS'} or not isinstance(item.get('control'), str)
                or len(points) != 2 or any(len(p) != 3 or any(not math.isfinite(float(v)) for v in p) for p in points)):
            raise ValueError('Invalid floor contact calibration')
        if not math.isfinite(float(item.get('softness', .05))) or float(item.get('softness', .05)) < 0:
            raise ValueError('Contact softness must be a finite nonnegative distance')
        alignment = item.get('alignment', (0, 0, 0))
        if len(alignment) != 3 or not all(math.isfinite(float(v)) for v in alignment):
            raise ValueError('Invalid calibrated orientation')
    remove(context, arm, controls={item['control'] for item in data.get('limbs', [])})
    arm[PENDING] = json.dumps(data)
    arm.sub_floor_contact.enabled = True
    arm.sub_floor_contact.calibrating = False
    restore_pending(context, arm)
    arm.sub_floor_contact.show_markers = False


def restore_pending(context, arm):
    if PENDING not in arm or not hasattr(arm, 'sub_floor_contact') or not arm.data.get('sub_independent_ik'):
        return
    data = json.loads(arm[PENDING])
    for item in data.get('limbs', []):
        control = item['control']
        if control not in arm.pose.bones:
            continue
        if any(l.control == control for l in arm.sub_floor_contact.limbs):
            continue
        limb = setup_limb(context, arm, control, item['kind'], item['points'])
        limb.softness = max(0.0, float(item.get('softness', .05)))
        limb.rolling = bool(item.get('rolling', True))
        limb.pin_toe = bool(item.get('pin_toe', False))
        limb.alignment.rotation_euler = item.get('alignment', (0, 0, 0))


def mirror(context, arm, limb):
    import re
    match = re.fullmatch(r'(FootIK|HandIK)([LR])(.*)', limb.control)
    if not match:
        raise ValueError('This control has no recognized left/right counterpart')
    other_name = match[1] + ('R' if match[2] == 'L' else 'L') + match[3]
    if other_name not in arm.pose.bones:
        raise ValueError('Create the opposite IK control first')
    # Mirror in armature REST space: current asymmetric poses must not contaminate calibration.
    source = arm.data.bones[limb.control].matrix_local
    inverse = arm.data.bones[other_name].matrix_local.inverted_safe()
    points = []
    for marker in (limb.heel, limb.toe):
        point = source @ marker.location
        point.x = -point.x
        points.append(tuple(inverse @ point))
    other = setup_limb(context, arm, other_name, limb.kind, points)
    for marker, point in zip((other.heel, other.toe), points):
        marker.location = point
    other.softness = limb.softness
    other.rolling = limb.rolling
    other.pin_toe = limb.pin_toe
    limb.mirror_source = ''
    other.mirror_source = limb.control
    capture(context, arm, other)


def finish_calibration(context, arm):
    graph = context.evaluated_depsgraph_get()
    limbs = {limb.control: limb for limb in arm.sub_floor_contact.limbs}
    for limb in limbs.values():
        if limb.mirror_source not in limbs:
            limb.alignment.matrix_world = limb.raw.evaluated_get(graph).matrix_world.copy()
    reflection = Matrix.Diagonal(Vector((-1, 1, 1, 1)))
    for limb in limbs.values():
        source = limbs.get(limb.mirror_source)
        if source:
            delta = (arm.matrix_world.inverted_safe() @ source.alignment.matrix_world
                     @ arm.data.bones[source.control].matrix_local.inverted_safe())
            limb.alignment.matrix_world = (arm.matrix_world @ reflection @ delta @ reflection
                                           @ arm.data.bones[limb.control].matrix_local)
        capture(context, arm, limb)
    arm.sub_floor_contact.calibrating = False


class SUB_OP_floor_contact(Operator):
    bl_description = 'Place selected foot IK controls on the configured floor and manage their contact locks'
    bl_idname = 'sub.floor_contact'
    bl_label = 'IK Floor Contact'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[(x, label, '') for x, label in (
        ('SETUP', 'Set Up Floor Contact'), ('MIRROR', 'Mirror Calibration'),
        ('PLANT', 'Plant Now'), ('RELEASE', 'Release'), ('CAPTURE', 'Move Plant Here'),
        ('FINISH', 'Finish Calibration'),
        ('BODY', 'Set Up Body Height Adjustment'),
        ('SELECT', 'Select Calibration Markers'), ('REMOVE', 'Remove Floor Contact'))])
    control: StringProperty()

    def execute(self, context):
        from .create_animation_rig import find_target_armature
        from . import ik_channels
        arm = find_target_armature(context)
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, 'Select the IK armature')
            return {'CANCELLED'}
        if not arm.data.get('sub_independent_ik'):
            self.report({'ERROR'}, 'Create IK controls with the existing IK tools first')
            return {'CANCELLED'}
        try:
            if self.action == 'SETUP':
                # Upgrade existing independent leg IK to the reverse-foot
                # controls before contact helpers choose their input target.
                ik_channels.ensure(arm, context)
                jobs = list(ik_channels.chains(arm))
                if not jobs:
                    raise ValueError('No supported IK chains found')
                for kind, _, control, _ in jobs:
                    setup_limb(context, arm, control, kind)
                arm.sub_floor_contact.calibrating = True
                self.report({'INFO'}, 'Position one side’s contact markers on the mesh, then Mirror Calibration')
            elif self.action == 'REMOVE':
                remove(context, arm)
            elif self.action == 'BODY':
                setup_body(context, arm)
                arm.sub_floor_contact.adjust_body = True
            elif self.action == 'FINISH':
                finish_calibration(context, arm)
                arm.sub_floor_contact.show_markers = False
                if context.object and context.object.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                for obj in context.selected_objects:
                    obj.select_set(False)
                arm.select_set(True)
                context.view_layer.objects.active = arm
                bpy.ops.object.mode_set(mode='POSE')
            else:
                limb = next((l for l in arm.sub_floor_contact.limbs if l.control == self.control), None)
                if limb is None:
                    raise ValueError('Contact limb no longer exists; set up contact again')
                if self.action == 'MIRROR':
                    mirror(context, arm, limb)
                elif self.action in {'PLANT', 'CAPTURE'}:
                    capture(context, arm, limb)
                    if self.action == 'PLANT':
                        limb.planted = True
                elif self.action == 'RELEASE':
                    limb.planted = False
                    limb.auto_plant = False
                elif self.action == 'SELECT':
                    arm.sub_floor_contact.calibrating = True
                    if context.object and context.object.mode != 'OBJECT':
                        bpy.ops.object.mode_set(mode='OBJECT')
                    for obj in context.selected_objects:
                        obj.select_set(False)
                    arm.sub_floor_contact.show_markers = True
                    for marker in (limb.heel, limb.toe):
                        marker.hide_set(False)
                        marker.select_set(True)
                    context.view_layer.objects.active = limb.heel
            return {'FINISHED'}
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def draw(layout, context, arm):
    if arm is None or not hasattr(arm, 'sub_floor_contact'):
        return
    box = layout.box()
    box.label(text='Live Floor Contact', icon='CON_FLOOR')
    props = arm.sub_floor_contact
    if not props.limbs:
        box.operator('sub.floor_contact', text='Set Up Floor Contact').action = 'SETUP'
        box.label(text='Calibrate the sole / palm once per model.')
        return
    box.prop(props, 'enabled')
    box.prop(context.scene, 'sub_floor_height')
    box.prop(props, 'show_markers')
    if props.body_target:
        box.prop(props, 'adjust_body')
    else:
        box.operator('sub.floor_contact', text='Set Up Body Height Adjustment').action = 'BODY'
    if props.calibrating:
        box.label(text='Calibration: floor correction is temporarily paused.')
        box.label(text='Pose soles flat; move markers to the mesh bottom.')
        box.operator('sub.floor_contact', text='Finish Calibration').action = 'FINISH'
    for limb in props.limbs:
        col = box.box()
        row = col.row(align=True)
        row.prop(limb, 'expanded', text='', icon='TRIA_DOWN' if limb.expanded else 'TRIA_RIGHT', emboss=False)
        row.label(text=limb.control)
        row.prop(limb, 'enabled')
        if not limb.expanded:
            continue
        if props.enabled and limb.enabled and not props.calibrating and limb.solved:
            from . import ik_channels
            for kind, names, control, _ in ik_channels.chains(arm):
                if control != limb.control:
                    continue
                graph = context.evaluated_depsgraph_get()
                evaluated = arm.evaluated_get(graph)
                end = evaluated.pose.bones.get(ik_channels.PREFIX + names[2])
                if end and getattr(arm.data, 'sub_use_ik_' + kind.lower(), 1) > .001:
                    actual = (evaluated.matrix_world @ end.matrix).translation
                    target = limb.solved.evaluated_get(graph).matrix_world.translation
                    if (actual - target).length > max(.001, end.length * .01):
                        col.label(text='Contact target is outside the solved limb’s reach.', icon='INFO')
                break
        col.prop(limb, 'softness')
        row = col.row(align=True)
        for action, label in (('PLANT', 'Plant Now'), ('RELEASE', 'Release')):
            op = row.operator('sub.floor_contact', text=label)
            op.action, op.control = action, limb.control
        col.prop(limb, 'planted')
        col.prop(limb, 'auto_plant')
        if limb.auto_plant:
            col.label(text='Uses this limb’s editable plant marker.')
            col.prop(limb, 'release_height')
            col.prop(limb, 'release_distance')
            op = col.operator('sub.floor_contact', text='Move Plant Marker Here')
            op.action, op.control = 'CAPTURE', limb.control
        col.prop(limb, 'resistance')
        row = col.row(align=True)
        row.prop(limb, 'align')
        row.prop(limb, 'lock_rotation')
        if limb.kind == 'LEGS':
            col.prop(limb, 'rolling')
            col.prop(limb, 'pin_toe')
            if limb.pin_toe:
                suffix = limb.control[6:] if limb.control.startswith('FootIK') else ''
                col.label(text='Rotate FootRollIK' + suffix + ' to roll from the toe.')
        row = col.row(align=True)
        for action, label in (('SELECT', 'Edit Contact Markers'), ('MIRROR', 'Mirror Calibration')):
            op = row.operator('sub.floor_contact', text=label)
            op.action, op.control = action, limb.control
    box.label(text='Save calibration with an Armature Collection Preset.')
    if not props.adjust_body or not props.body_target:
        box.label(text='Body motion is preserved; unreachable targets may leave a gap.')
    else:
        box.label(text='Body height assists reach; horizontal reach is still limited.')
    box.operator('sub.floor_contact', text='Remove Floor Contact').action = 'REMOVE'


CLASSES = (SUB_PG_floor_limb, SUB_PG_floor_contact, SUB_OP_floor_contact)


def _restore_contacts():
    """Repair saved helpers after Blender releases its registration restrictions."""
    for obj in list(bpy.data.objects):
        if obj.get('sub_floor_owner') and obj.animation_data:
            for fc in obj.animation_data.drivers:
                fc.mute = False
        if obj.type == 'ARMATURE' and obj.sub_floor_contact.limbs:
            for limb in obj.sub_floor_contact.limbs:
                # Remove the short-lived generated roll helper from files made
                # by the previous implementation and restore the direct path.
                old = bpy.data.objects.get(obj.name + ' • ' + limb.control + ' Foot Roll')
                if old and old.get('sub_floor_owner') == obj:
                    for marker in (limb.heel, limb.toe):
                        if marker and marker.parent == old:
                            location = marker.location.copy()
                            marker.parent = limb.oriented
                            marker.matrix_parent_inverse = Matrix.Identity(4)
                            marker.location = location
                    source = limb.solved.constraints.get('IK Source') if limb.solved else None
                    if source:
                        source.target = limb.oriented
                    bpy.data.objects.remove(old, do_unlink=True)
                suffix = limb.control[6:] if limb.control.startswith('FootIK') else ''
                toe = obj.pose.bones.get('Toe' + suffix) if suffix else None
                legacy = toe.constraints.get('SUB Pin Toe') if toe else None
                if legacy:
                    legacy.driver_remove('influence')
                    toe.constraints.remove(legacy)
            rewire(obj)
            root = obj.pose.bones.get('Trans')
            if root and root.constraints.get(BODY_CONSTRAINT):
                root.constraints[BODY_CONSTRAINT].mute = False
    return None


def _schedule_restore():
    if not bpy.app.timers.is_registered(_restore_contacts):
        bpy.app.timers.register(_restore_contacts, first_interval=0.0)


@persistent
def _floor_load_post(_unused):
    _schedule_restore()


def register():
    # addon_utils.enable() runs inside RestrictBlend: bpy.data.objects and
    # scene context are unavailable until registration has returned.
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.sub_floor_contact = PointerProperty(type=SUB_PG_floor_contact)
    bpy.types.Scene.sub_floor_height = FloatProperty(name='Floor Height', default=0.0, subtype='DISTANCE', update=_changed)
    if _floor_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_floor_load_post)
    _schedule_restore()


def unregister():
    if bpy.app.timers.is_registered(_restore_contacts):
        bpy.app.timers.unregister(_restore_contacts)
    if _floor_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_floor_load_post)
    # Helpers/drivers persist in the blend for re-enabling the add-on. Restore
    # normal IK while RNA properties (and hence their drivers) are unavailable.
    for arm in getattr(bpy.data, 'objects', ()):
        if arm.type == 'ARMATURE':
            for limb in arm.sub_floor_contact.limbs:
                for bone in arm.pose.bones:
                    for con in bone.constraints:
                        if limb.solved and getattr(con, 'target', None) == limb.solved:
                            con.target, con.subtarget = arm, limb.control
                            if con.type != 'IK':
                                con.target_space = con.owner_space = 'POSE'
            root = arm.pose.bones.get('Trans')
            if root and root.constraints.get(BODY_CONSTRAINT):
                root.constraints[BODY_CONSTRAINT].mute = True
        elif arm.get('sub_floor_owner') and arm.animation_data:
            for fc in arm.animation_data.drivers:
                fc.mute = True
    del bpy.types.Scene.sub_floor_height
    del bpy.types.Object.sub_floor_contact
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
