"""Pose-bone basis math without a depsgraph round-trip.

Setting ``pose_bone.matrix`` makes Blender convert an armature-space matrix
into the bone's ``matrix_basis``, but the conversion reads the *evaluated*
parent, so a chain has to be written parent-first with a
``view_layer.update()`` between every bone. Bake and match loops already know
every bone's target armature-space matrix up front, so the round-trips buy
nothing -- they just re-derive an arithmetic result.

``basis_from_world`` does that arithmetic directly. It models only the default
inheritance case, which is what ``BKE_armature_mat_pose_to_bone`` reduces to
when a bone inherits rotation, inherits scale in FULL mode, and uses local
location:

    pose = parent_pose @ offs_bone @ basis          (parented)
    pose = matrix_local @ basis                     (root)

where ``offs_bone`` is the bone's rest transform relative to its parent,
including the parent-length offset along Y.

Any other flag combination makes Blender compose separate rotation/scale and
location matrices, optionally orthogonalizing or rescaling them. Rather than
reimplement those branches on trust, ``basis_from_world`` returns ``None`` for
them so callers fall back to the ``pose_bone.matrix`` setter. See
``tests/test_pose_math_blender.py`` for the equivalence check against real
rig bones.
"""
from mathutils import Matrix


def supports(pose_bone) -> bool:
    """True when this bone's inheritance flags match the modeled default case."""
    bone = pose_bone.bone
    return (bone.use_inherit_rotation
            and bone.inherit_scale == 'FULL'
            and bone.use_local_location)


def offset_matrix(bone) -> Matrix:
    """Rest transform of ``bone`` relative to its parent, parent length included.

    Blender builds this from ``bone_mat`` plus ``head`` plus the parent's
    length; the same value falls out of the rest matrices, both of which the
    Python API exposes directly.
    """
    parent = bone.parent
    if parent is None:
        return bone.matrix_local.copy()
    return parent.matrix_local.inverted_safe() @ bone.matrix_local


def parent_to_world(pose_bone, parent_world: Matrix | None) -> Matrix | None:
    """Armature-space matrix that ``matrix_basis`` is applied on top of.

    ``parent_world`` is the parent's armature-space pose matrix. Pass ``None``
    for a root bone, or for a bone whose parent's pose is unknown.
    """
    if not supports(pose_bone):
        return None
    bone = pose_bone.bone
    if bone.parent is None:
        return bone.matrix_local.copy()
    if parent_world is None:
        return None
    return parent_world @ offset_matrix(bone)


def basis_from_world(pose_bone, target_world: Matrix,
                     parent_world: Matrix | None = None) -> Matrix | None:
    """``matrix_basis`` that puts ``pose_bone`` at ``target_world``.

    Equivalent to ``pose_bone.matrix = target_world`` followed by a depsgraph
    update, for bones this module models. Returns ``None`` when the bone's
    inheritance flags fall outside that set, or when a parented bone's parent
    pose was not supplied -- callers must then use the setter and evaluate.

    ``target_world`` and ``parent_world`` are armature-space (the space
    ``pose_bone.matrix`` reads and writes), not scene-world.
    """
    base = parent_to_world(pose_bone, parent_world)
    if base is None:
        return None
    return base.inverted_safe() @ target_world


def world_from_basis(pose_bone, basis: Matrix,
                     parent_world: Matrix | None = None) -> Matrix | None:
    """Forward direction, for verifying ``basis_from_world`` round-trips."""
    base = parent_to_world(pose_bone, parent_world)
    if base is None:
        return None
    return base @ basis


def can_replay(pose_bones) -> bool:
    """True when these bones can be re-placed at sampled matrices arithmetically.

    Two conditions. Their inheritance flags must be ones this module models.
    And nothing may still constrain them: replaying a sample assumes each bone
    ends up exactly where it was sampled, which is what lets a child use its
    parent's sampled matrix as a reference instead of evaluating for it. A
    live constraint would move the bone after its basis was applied and break
    that chain. Call after muting or removing whatever drove the sampled pose.
    """
    return all(supports(pose_bone)
               and not any(not con.mute for con in pose_bone.constraints)
               for pose_bone in pose_bones)


def reference_names(pose_bones) -> list:
    """Parents outside the set, whose sampled matrices anchor it.

    These are not re-placed, so their sampled matrices stay valid and serve as
    the fixed frames the outermost bones hang from. Sample them too.
    """
    inside = {pose_bone.name for pose_bone in pose_bones}
    return list(dict.fromkeys(
        pose_bone.parent.name for pose_bone in pose_bones
        if pose_bone.parent is not None and pose_bone.parent.name not in inside))


def apply_world(pose_bone, target_world: Matrix,
                parent_world: Matrix | None = None) -> bool:
    """Place ``pose_bone`` arithmetically. False when the caller must evaluate.

    On False the bone is left untouched, so the caller can fall back to
    ``pose_bone.matrix = target_world`` and a ``view_layer.update()`` without
    having to undo anything.
    """
    basis = basis_from_world(pose_bone, target_world, parent_world)
    if basis is None:
        return False
    pose_bone.matrix_basis = basis
    return True
