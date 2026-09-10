"""Write many keyframes per channel in one pass.

``pose_bone.keyframe_insert()`` resolves an RNA path, finds or creates the
F-curve, inserts one point, and re-sorts the keyframe array -- per channel, per
bone, per frame. A 300-frame bake of a dozen bones is tens of thousands of
those calls, and the sorting makes it worse than linear.

Blender's bulk path (``keyframe_points.add()`` plus ``foreach_set()``) writes a
whole channel in one call. ``source/anim/import_anim.py`` already relies on it
for imported bone transforms; this module generalizes that into something the
bake and match loops can use, including merging against keys the channel
already holds.

Typical use::

    writer = PoseKeyWriter(armature_object)
    for frame in frames:
        ...
        writer.stash_pose_bone(pose_bone, frame)
    writer.flush()

Quaternion sign continuity is handled at flush time across the whole stashed
run, which is more reliable than the running ``previous`` dict the per-frame
inserts used -- it cannot be thrown off by bones stashed out of order.
"""
import bpy

from .fcurve_compat import ensure_fcurve_for_datablock, find_fcurve

# Frames are compared as floats; keys land on integers, so this is generous.
FRAME_EPSILON = 1e-4


def _enum_value(rna_type, property_name, identifier):
    """Int backing an enum identifier, for foreach_set on enum properties."""
    prop = rna_type.bl_rna.properties[property_name]
    return prop.enum_items[identifier].value


def _interpolation_value(identifier):
    return _enum_value(bpy.types.Keyframe, 'interpolation', identifier)


def _rotation_channel(rotation_mode):
    """(property name, channel count) for a bone's rotation mode."""
    if rotation_mode == 'QUATERNION':
        return 'rotation_quaternion', 4
    if rotation_mode == 'AXIS_ANGLE':
        return 'rotation_axis_angle', 4
    return 'rotation_euler', 3


def align_quaternions(values):
    """Negate quaternions that flip hemisphere, so interpolation takes the short arc.

    Mirrors what the per-frame ``_key`` helper did with its ``previous`` dict,
    but over the finished sequence.
    """
    aligned = []
    previous = None
    for quaternion in values:
        current = list(quaternion)
        if previous is not None:
            dot = sum(a * b for a, b in zip(previous, current))
            if dot < 0.0:
                current = [-component for component in current]
        aligned.append(current)
        previous = current
    return aligned


class PoseKeyWriter:
    """Accumulate keyframes, then write each channel in a single pass.

    ``id_data`` is the datablock the keys belong to -- the armature object for
    pose bones. The action is resolved at flush time, so this cooperates with
    the bake-time action binding the callers set up.
    """

    def __init__(self, id_data, interpolation='LINEAR'):
        self.id_data = id_data
        self.interpolation = interpolation
        # (data_path, index) -> {'group': str, 'frames': [], 'values': []}
        self._channels = {}
        self._quaternion_paths = set()

    # -- stashing -------------------------------------------------------------

    def stash_channel(self, data_path, index, frame, value, group=''):
        key = (data_path, index)
        channel = self._channels.get(key)
        if channel is None:
            channel = self._channels[key] = {'group': group, 'frames': [], 'values': []}
        channel['frames'].append(float(frame))
        channel['values'].append(float(value))

    def stash_vector(self, data_path, frame, values, group=''):
        for index, value in enumerate(values):
            self.stash_channel(data_path, index, frame, value, group)

    def stash_pose_bone(self, pose_bone, frame):
        """Stash the bone's current basis transform: location, rotation, scale.

        Matches the channel set the per-frame ``_key_pose_bone`` wrote, so
        callers can swap one for the other without changing what lands in the
        action.
        """
        base = pose_bone.path_from_id()
        group = pose_bone.name
        self.stash_vector(f'{base}.location', frame, pose_bone.location, group)
        self.stash_vector(f'{base}.scale', frame, pose_bone.scale, group)

        property_name, _count = _rotation_channel(pose_bone.rotation_mode)
        rotation_path = f'{base}.{property_name}'
        self.stash_vector(rotation_path, frame, getattr(pose_bone, property_name), group)
        if pose_bone.rotation_mode == 'QUATERNION':
            self._quaternion_paths.add(rotation_path)

    def stash_matrix_basis(self, pose_bone, frame, basis):
        """Stash a basis matrix without assigning it to the bone first."""
        translation, rotation, scale = basis.decompose()
        base = pose_bone.path_from_id()
        group = pose_bone.name
        self.stash_vector(f'{base}.location', frame, translation, group)
        self.stash_vector(f'{base}.scale', frame, scale, group)

        if pose_bone.rotation_mode == 'QUATERNION':
            path = f'{base}.rotation_quaternion'
            self.stash_vector(path, frame, rotation, group)
            self._quaternion_paths.add(path)
        elif pose_bone.rotation_mode == 'AXIS_ANGLE':
            axis, angle = rotation.to_axis_angle()
            self.stash_vector(f'{base}.rotation_axis_angle', frame,
                              (angle, axis[0], axis[1], axis[2]), group)
        else:
            euler = rotation.to_euler(pose_bone.rotation_mode)
            self.stash_vector(f'{base}.rotation_euler', frame, euler, group)

    def __bool__(self):
        return bool(self._channels)

    # -- flushing -------------------------------------------------------------

    def _align_quaternion_channels(self):
        """Apply hemisphere alignment across each quaternion channel group."""
        for path in self._quaternion_paths:
            components = [self._channels.get((path, index)) for index in range(4)]
            if any(channel is None for channel in components):
                continue
            length = len(components[0]['values'])
            if any(len(channel['values']) != length for channel in components):
                continue
            aligned = align_quaternions(
                [[channel['values'][i] for channel in components] for i in range(length)])
            for index, channel in enumerate(components):
                channel['values'] = [row[index] for row in aligned]

    def _ensure_action(self):
        """The action to key into, created if the ID has none.

        keyframe_insert() creates one implicitly, so callers that used to key
        per frame relied on it -- baking onto a rig with no action yet has to
        keep working.
        """
        from ..blender_compat import assign_action

        if self.id_data.animation_data is None:
            self.id_data.animation_data_create()
        anim = self.id_data.animation_data
        if anim.action is None:
            assign_action(anim, bpy.data.actions.new(name=f'{self.id_data.name}Action'))
        return anim.action

    def _resolve_fcurve(self, action, data_path, index, group):
        fcurve = find_fcurve(action, data_path, index=index)
        if fcurve is not None:
            return fcurve
        return ensure_fcurve_for_datablock(
            action, self.id_data, data_path, index=index, action_group=group)

    def flush(self, action=None):
        """Write every stashed channel. Returns the number of keys written."""
        if not self._channels:
            return 0

        if action is None:
            action = self._ensure_action()

        self._align_quaternion_channels()
        interpolation = _interpolation_value(self.interpolation)
        written = 0

        for (data_path, index), channel in self._channels.items():
            fcurve = self._resolve_fcurve(action, data_path, index, channel['group'])
            if fcurve is None:
                continue
            merged = self._merge(fcurve, channel['frames'], channel['values'])
            self._write(fcurve, merged, interpolation)
            written += len(channel['frames'])

        self._channels.clear()
        self._quaternion_paths.clear()
        return written

    @staticmethod
    def _merge(fcurve, frames, values):
        """Existing keys outside the stashed frames, plus the stashed ones.

        Stashed frames win, which is what per-frame ``keyframe_insert`` did.
        """
        count = len(fcurve.keyframe_points)
        if count:
            existing = [0.0] * (count * 2)
            fcurve.keyframe_points.foreach_get('co', existing)
            low = min(frames) - FRAME_EPSILON
            high = max(frames) + FRAME_EPSILON
            stashed = {round(frame, 4) for frame in frames}
            kept = [(existing[i * 2], existing[i * 2 + 1]) for i in range(count)
                    if not (low <= existing[i * 2] <= high
                            and round(existing[i * 2], 4) in stashed)]
        else:
            kept = []

        merged = kept + list(zip(frames, values))
        merged.sort(key=lambda point: point[0])
        return merged

    @staticmethod
    def _write(fcurve, points, interpolation):
        flat = []
        for frame, value in points:
            flat.append(frame)
            flat.append(value)

        fcurve.keyframe_points.clear()
        if not points:
            fcurve.update()
            return
        fcurve.keyframe_points.add(count=len(points))
        fcurve.keyframe_points.foreach_set('co', flat)
        fcurve.keyframe_points.foreach_set('interpolation', [interpolation] * len(points))
        fcurve.update()


def set_interpolation(fcurve, identifier='LINEAR'):
    """Retype every key on a curve in one call."""
    count = len(fcurve.keyframe_points)
    if not count:
        return
    fcurve.keyframe_points.foreach_set(
        'interpolation', [_interpolation_value(identifier)] * count)
    fcurve.update()
