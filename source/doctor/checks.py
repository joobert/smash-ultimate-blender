"""Every Smash Export Doctor check, and the fixes that go with them.

The checks mirror what the model and animation exporters actually do. Where an
exporter raises, the result is an ``ERROR`` marked ``blocking``; where an
exporter silently produces something wrong, the result is an ``ERROR`` that
does not block; where it produces something merely surprising, a ``WARNING``
or ``INFO``.
"""

import math
import os
import re

import bpy

from mathutils import Matrix

from .core import (
    ERROR,
    INFO,
    SCOPE_ANIM,
    SCOPE_MODEL,
    TARGET_ACTION,
    TARGET_BONE,
    TARGET_MATERIAL,
    TARGET_OBJECT,
    TARGET_PATH,
    WARNING,
    DoctorResult,
    check,
    fixer,
)


# Attributes the mesh exporter always generates itself, so a mesh never has to
# carry them.
GENERATED_ATTRIBUTES = {'Position0', 'Normal0', 'Tangent0', 'Binormal0'}

SMASH_UV_NAMES = ('map1', 'bake1', 'uvSet', 'uvSet1', 'uvSet2')
SMASH_COLOR_NAMES = (
    'colorSet1', 'colorSet2', 'colorSet2_1', 'colorSet2_2', 'colorSet2_3',
    'colorSet3', 'colorSet4', 'colorSet5', 'colorSet6', 'colorSet7',
)

# Neutral values, taken from the master shader's own defaults for the color
# sets it can neutralize. Anything not listed here has no known neutral, so the
# doctor reports it but will not invent one.
NEUTRAL_COLOR_SETS = {
    'colorSet1': (0.5, 0.5, 0.5, 0.5),
    'colorSet5': (0.0, 0.0, 0.0, 1.0 / 3.0),
}

# Modifiers that change topology or drop attributes badly enough that the
# exported mesh rarely matches what the user sees.
DESTRUCTIVE_MODIFIERS = {
    'BOOLEAN', 'BUILD', 'DECIMATE', 'EXPLODE', 'MASK', 'MULTIRES', 'NODES',
    'REMESH', 'SKIN', 'SUBSURF', 'WIREFRAME',
}

# The node names the camera exporter writes into every .nuanmb.
CAMERA_NODE_NAME = 'gya_camera'
CAMERA_SHAPE_NODE_NAME = 'gya_cameraShape'

BL_CONTROL_PREFIX = 'BL_'

_DUP_SUFFIX = re.compile(r'\.\d\d\d$')
_POSE_BONE_PATH = re.compile(r'^pose\.bones\[["\']([^"\']+)["\']')

TRANSFORM_EPSILON = 1e-5


def _trim(name: str) -> str:
    return _DUP_SUFFIX.split(name)[0]


def _is_finite(values) -> bool:
    for value in values:
        if not math.isfinite(value):
            return False
    return True


def _matrix_is_finite(matrix) -> bool:
    return _is_finite([component for row in matrix for component in row])


def _mesh_uv_names(mesh_object) -> set:
    """UV maps the exporter would look at, skipping its own scratch layers."""
    return {
        layer.name for layer in mesh_object.data.uv_layers
        if not layer.name.startswith('.') and not layer.name.startswith('_sub_eye')
    }


def _mesh_color_names(mesh_object) -> set:
    return {
        attribute.name for attribute in mesh_object.data.color_attributes
        if attribute.name != '_smush_blender_custom_normals'
    }


def _required_attributes(shader_label: str):
    from ..model.material.create_blender_materials_from_matl import get_vertex_attributes

    return set(get_vertex_attributes(shader_label))


def _deform_group_indices(mesh_object, armature):
    """Vertex group indices the exporter counts as bone influences."""
    if armature is None:
        return set()
    bones = armature.data.bones
    return {group.index for group in mesh_object.vertex_groups if group.name in bones}


def _object_local_matrix(obj):
    """The transform the model exporter throws away.

    Mesh vertices are read in object space and written straight into the
    .numshb, so anything in the mesh's matrix relative to its armature simply
    does not reach the game.
    """
    if obj.parent is not None:
        return obj.matrix_local.copy()
    return obj.matrix_world.copy()


def _decompose_problems(matrix):
    """Return (has_transform, negative_scale, has_shear) for a local matrix."""
    translation, rotation, scale = matrix.decompose()

    has_transform = (
        translation.length > TRANSFORM_EPSILON
        or abs(rotation.angle) > TRANSFORM_EPSILON
        or any(abs(axis - 1.0) > TRANSFORM_EPSILON for axis in scale)
    )
    negative_scale = matrix.to_3x3().determinant() < 0.0

    # decompose() cannot represent shear, so a sheared matrix is exactly the
    # one that does not rebuild from its own loc/rot/scale.
    rebuilt = (
        Matrix.Translation(translation)
        @ rotation.to_matrix().to_4x4()
        @ Matrix.Diagonal(scale).to_4x4()
    )
    has_shear = any(
        abs(matrix[row][column] - rebuilt[row][column]) > 1e-4
        for row in range(4)
        for column in range(4)
    )
    return has_transform, negative_scale, has_shear


def _action_key_frames(action):
    from ..anim.fcurve_compat import get_all_action_fcurves

    frames = []
    for fcurve in get_all_action_fcurves(action):
        for keyframe in fcurve.keyframe_points:
            frames.append(float(keyframe.co[0]))
    return frames


def _no_armature_result(check_id):
    return DoctorResult(
        check_id=check_id,
        severity=INFO,
        message='No export armature selected.',
        detail='Pick one in the Model Exporter, or select an armature in the viewport.',
    )


# ---------------------------------------------------------------------------
# Model checks
# ---------------------------------------------------------------------------


@check(
    'missing_materials',
    'Missing material assignments',
    'Every exported mesh needs a material with a Smash shader label.',
    scopes=(SCOPE_MODEL,),
)
def check_missing_materials(scene):
    results = []
    if scene.armature is None:
        return [_no_armature_result('missing_materials')]

    for mesh in scene.meshes:
        if len(mesh.material_slots) == 0:
            results.append(DoctorResult(
                check_id='missing_materials',
                severity=ERROR,
                message=f'"{mesh.name}" has no material slots.',
                detail='The .numdlb cannot be written without a material. '
                       'Assign a material, or disable .NUMDLB export.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                blocking=True,
            ))
            continue

        for index, slot in enumerate(mesh.material_slots):
            material = slot.material
            if material is None:
                results.append(DoctorResult(
                    check_id='missing_materials',
                    severity=ERROR,
                    message=f'"{mesh.name}" slot {index + 1} is empty.',
                    detail='Create or assign a material for this slot.',
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                    blocking=index == 0,
                ))
                continue

            sub_matl_data = getattr(material, 'sub_matl_data', None)
            shader_label = getattr(sub_matl_data, 'shader_label', '') if sub_matl_data else ''
            if not shader_label:
                results.append(DoctorResult(
                    check_id='missing_materials',
                    severity=ERROR,
                    message=f'"{material.name}" has no Smash shader label.',
                    detail=f'Used by "{mesh.name}". Convert it with Material Tools, or set a '
                           'shader label so a .numatb entry can be written.',
                    target_type=TARGET_MATERIAL,
                    target_name=mesh.name,
                    sub_target=material.name,
                    blocking=True,
                ))
    return results


@check(
    'shader_attributes',
    'Mesh attributes required by the shader',
    'The chosen Smash shader reads UV maps and color sets that must exist on the mesh.',
    scopes=(SCOPE_MODEL,),
)
def check_shader_attributes(scene):
    results = []
    if scene.armature is None:
        return results

    # Attribute *names* are a property of the mesh, not of any one material, so
    # check them once per mesh. A mesh with three slots must not report the same
    # bad UV name three times.
    for mesh in scene.meshes:
        uv_names = _mesh_uv_names(mesh)
        color_names = _mesh_color_names(mesh)

        if not uv_names:
            results.append(DoctorResult(
                check_id='shader_attributes',
                severity=ERROR,
                message=f'"{mesh.name}" has no UV maps.',
                detail='Tangent generation fails without UVs, so the model export stops here. '
                       'Add a "map1" UV map and unwrap the mesh.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                blocking=True,
                fix_id='add_uv_map',
                fix_label='Add "map1" UV map',
                fix_is_safe=True,
                payload={'object': mesh.name, 'attribute': 'map1'},
            ))

        for name in sorted(uv_names - set(SMASH_UV_NAMES)):
            results.append(DoctorResult(
                check_id='shader_attributes',
                severity=ERROR,
                message=f'"{mesh.name}" has the invalid UV map name "{name}".',
                detail='The exporter stops on UV names it does not recognise. Valid names are '
                       + ', '.join(SMASH_UV_NAMES) + '. Use the Attribute Renamer.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                blocking=True,
            ))

        for name in sorted(color_names - set(SMASH_COLOR_NAMES)):
            results.append(DoctorResult(
                check_id='shader_attributes',
                severity=ERROR,
                message=f'"{mesh.name}" has the invalid color attribute name "{name}".',
                detail='The exporter stops on color names it does not recognise. Valid names are '
                       + ', '.join(SMASH_COLOR_NAMES) + '. Use the Attribute Renamer.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                blocking=True,
            ))

    # What the shader needs on top of that is per material.
    reported = set()
    for mesh, _slot_index, material in scene.materials:
        if material is None:
            continue
        sub_matl_data = getattr(material, 'sub_matl_data', None)
        shader_label = getattr(sub_matl_data, 'shader_label', '') if sub_matl_data else ''
        if not shader_label:
            # Already reported by check_missing_materials.
            continue

        try:
            required = _required_attributes(shader_label)
        except Exception as error:
            results.append(DoctorResult(
                check_id='shader_attributes',
                severity=WARNING,
                message=f'Could not read the shader database for "{material.name}".',
                detail=f'{type(error).__name__}: {error}',
                target_type=TARGET_MATERIAL,
                target_name=mesh.name,
                sub_target=material.name,
            ))
            continue

        uv_names = _mesh_uv_names(mesh)
        color_names = _mesh_color_names(mesh)

        for attribute in sorted(required - GENERATED_ATTRIBUTES):
            if (mesh.name, attribute) in reported:
                continue
            reported.add((mesh.name, attribute))
            if attribute in SMASH_UV_NAMES:
                if attribute in uv_names:
                    continue
                results.append(DoctorResult(
                    check_id='shader_attributes',
                    severity=ERROR,
                    message=f'"{mesh.name}" is missing the UV map "{attribute}".',
                    detail=f'Shader {shader_label} on "{material.name}" reads it. '
                           'Without it the mesh samples the wrong coordinates in game.',
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                    fix_id='add_uv_map',
                    fix_label=f'Add "{attribute}" UV map',
                    fix_is_safe=True,
                    payload={'object': mesh.name, 'attribute': attribute},
                ))
            elif attribute.startswith('colorSet'):
                # colorSet2 ships split across colorSet2_1..3 on some meshes.
                if any(name == attribute or name.startswith(attribute + '_') for name in color_names):
                    continue
                fixable = attribute in NEUTRAL_COLOR_SETS
                detail = f'Shader {shader_label} on "{material.name}" reads it.'
                if not fixable:
                    detail += ' There is no documented neutral value for this set, so add it by hand.'
                results.append(DoctorResult(
                    check_id='shader_attributes',
                    severity=WARNING,
                    message=f'"{mesh.name}" is missing the color set "{attribute}".',
                    detail=detail,
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                    fix_id='add_color_set' if fixable else '',
                    fix_label=f'Add neutral "{attribute}"' if fixable else '',
                    fix_is_safe=fixable,
                    payload={'object': mesh.name, 'attribute': attribute},
                ))
    return results


@check(
    'vertex_weight_count',
    'More than four vertex weights',
    'Smash supports at most four bone influences per vertex.',
    scopes=(SCOPE_MODEL,),
)
def check_vertex_weight_count(scene):
    results = []
    if scene.armature is None:
        return results

    for mesh in scene.meshes:
        deform_indices = _deform_group_indices(mesh, scene.armature)
        if not deform_indices:
            continue
        over_count = 0
        worst = 0
        for vertex in mesh.data.vertices:
            influences = sum(1 for group in vertex.groups if group.group in deform_indices)
            if influences > 4:
                over_count += 1
                worst = max(worst, influences)
        if over_count:
            results.append(DoctorResult(
                check_id='vertex_weight_count',
                severity=ERROR,
                message=f'"{mesh.name}" has {over_count} vertices over the 4-weight limit.',
                detail=f'The worst vertex has {worst} bone influences. The model exporter stops '
                       'on the first one it finds. Limiting weights can change how the mesh '
                       'deforms, so check the result.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                blocking=True,
                fix_id='limit_weights',
                fix_label='Limit to 4 weights',
                fix_is_safe=True,
                payload={'object': mesh.name, 'armature': scene.armature.name},
            ))
    return results


@check(
    'weight_normalization',
    'Non-normalized weights',
    'Weights that do not sum to one, and vertices with no weight at all.',
    scopes=(SCOPE_MODEL,),
)
def check_weight_normalization(scene):
    results = []
    if scene.armature is None:
        return results

    for mesh in scene.meshes:
        deform_indices = _deform_group_indices(mesh, scene.armature)
        if not deform_indices:
            continue

        unweighted = 0
        unnormalized = 0
        for vertex in mesh.data.vertices:
            weights = [
                group.weight for group in vertex.groups
                if group.group in deform_indices
            ]
            total = sum(weights)
            if not weights or total <= 0.0:
                unweighted += 1
            elif abs(total - 1.0) > 1e-4:
                unnormalized += 1

        if unweighted:
            results.append(DoctorResult(
                check_id='weight_normalization',
                severity=WARNING,
                message=f'"{mesh.name}" has {unweighted} unweighted vertices.',
                detail='Vertices with no bone influence collapse toward the model origin in '
                       'game. Weight them, or parent the whole mesh to a single bone.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
            ))

        if unnormalized:
            results.append(DoctorResult(
                check_id='weight_normalization',
                severity=INFO,
                message=f'"{mesh.name}" has {unnormalized} vertices whose weights do not sum to 1.',
                detail='The exporter normalizes these on the way out, so the exported file is '
                       'correct. Normalizing now makes Blender match what ships.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                fix_id='normalize_weights',
                fix_label='Normalize weights',
                fix_is_safe=True,
                payload={'object': mesh.name, 'armature': scene.armature.name},
            ))
    return results


@check(
    'modifiers',
    'Unsupported modifiers',
    'Modifier setups that do not survive the trip into a .numshb.',
    scopes=(SCOPE_MODEL,),
)
def check_modifiers(scene):
    results = []
    if scene.armature is None:
        return results

    for mesh in scene.meshes:
        armature_modifiers = [
            modifier for modifier in mesh.modifiers if modifier.type == 'ARMATURE'
        ]

        if len(armature_modifiers) > 1:
            results.append(DoctorResult(
                check_id='modifiers',
                severity=ERROR,
                message=f'"{mesh.name}" has {len(armature_modifiers)} Armature modifiers.',
                detail='Stacked Armature modifiers deform the mesh twice when modifiers are '
                       'applied on export. Keep only the one pointing at the export armature.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                fix_id='remove_extra_armature_modifiers',
                fix_label='Keep one Armature modifier',
                fix_is_safe=True,
                payload={'object': mesh.name, 'armature': scene.armature.name},
            ))

        for modifier in armature_modifiers:
            if modifier.object is not scene.armature:
                target = modifier.object.name if modifier.object else 'nothing'
                results.append(DoctorResult(
                    check_id='modifiers',
                    severity=ERROR,
                    message=f'"{mesh.name}" is deformed by {target}, not the export armature.',
                    detail=f'The Armature modifier "{modifier.name}" does not point at '
                           f'"{scene.armature.name}", so applying modifiers bakes the wrong pose.',
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                    fix_id='retarget_armature_modifier',
                    fix_label='Point at the export armature',
                    fix_is_safe=True,
                    payload={
                        'object': mesh.name,
                        'modifier': modifier.name,
                        'armature': scene.armature.name,
                    },
                ))

        if not armature_modifiers and _deform_group_indices(mesh, scene.armature):
            results.append(DoctorResult(
                check_id='modifiers',
                severity=WARNING,
                message=f'"{mesh.name}" is weighted but has no Armature modifier.',
                detail='The .numshb still exports its weights, but the mesh will not follow the '
                       'armature in Blender, so posing it lies to you.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                fix_id='add_armature_modifier',
                fix_label='Add Armature modifier',
                fix_is_safe=True,
                payload={'object': mesh.name, 'armature': scene.armature.name},
            ))

        for modifier in mesh.modifiers:
            if modifier.type == 'ARMATURE':
                continue
            if not modifier.show_viewport:
                results.append(DoctorResult(
                    check_id='modifiers',
                    severity=WARNING,
                    message=f'"{mesh.name}" modifier "{modifier.name}" is disabled in the viewport.',
                    detail='Export evaluates the viewport depsgraph, so a modifier hidden there '
                           'is silently dropped even with "Apply Modifiers" on.',
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                ))
            elif modifier.type in DESTRUCTIVE_MODIFIERS:
                results.append(DoctorResult(
                    check_id='modifiers',
                    severity=WARNING,
                    message=f'"{mesh.name}" uses the {modifier.type} modifier "{modifier.name}".',
                    detail='This modifier changes topology, so vertex weights, UV maps and color '
                           'sets may not survive into the exported mesh. Apply it and check the '
                           'result before exporting.',
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                ))
            else:
                results.append(DoctorResult(
                    check_id='modifiers',
                    severity=INFO,
                    message=f'"{mesh.name}" modifier "{modifier.name}" will be baked in.',
                    detail=f'{modifier.type} is applied when "Apply Modifiers" is on and ignored '
                           'otherwise, so the exported mesh differs from the source mesh.',
                    target_type=TARGET_OBJECT,
                    target_name=mesh.name,
                ))
    return results


@check(
    'object_transforms',
    'Negative scale, shear, or unapplied transforms',
    'The model exporter reads object-space vertices, so object transforms are lost.',
    scopes=(SCOPE_MODEL,),
)
def check_object_transforms(scene):
    results = []
    if scene.armature is None:
        return results

    candidates = [(scene.armature, True)] + [(mesh, False) for mesh in scene.meshes]

    for obj, is_armature in candidates:
        matrix = _object_local_matrix(obj)
        if not _matrix_is_finite(matrix):
            # Reported by check_nan_transforms; decompose() would be garbage.
            continue

        has_transform, negative_scale, has_shear = _decompose_problems(matrix)
        if not has_transform and not negative_scale and not has_shear:
            continue

        can_apply = (
            obj.type == 'MESH'
            and obj.data.users == 1
            and obj.data.shape_keys is None
            and obj.library is None
        )

        if negative_scale:
            results.append(DoctorResult(
                check_id='object_transforms',
                severity=ERROR,
                message=f'"{obj.name}" has negative scale.',
                detail='Mirrored objects export with inside-out normals and flipped tangents. '
                       'Apply the scale so the mesh data itself is mirrored.',
                target_type=TARGET_OBJECT,
                target_name=obj.name,
                fix_id='apply_transform' if can_apply else '',
                fix_label='Apply transform' if can_apply else '',
                fix_is_safe=can_apply,
                payload={'object': obj.name},
            ))
        if has_shear:
            results.append(DoctorResult(
                check_id='object_transforms',
                severity=ERROR,
                message=f'"{obj.name}" has a sheared transform.',
                detail='Shear cannot be expressed as loc/rot/scale, so nothing downstream can '
                       'reproduce it. Apply the transform.',
                target_type=TARGET_OBJECT,
                target_name=obj.name,
                fix_id='apply_transform' if can_apply else '',
                fix_label='Apply transform' if can_apply else '',
                fix_is_safe=can_apply,
                payload={'object': obj.name},
            ))
        if has_transform and not negative_scale and not has_shear:
            if is_armature:
                detail = ('Bone transforms are exported in armature space, so this object-level '
                          'transform never reaches the .nusktb.')
            else:
                detail = ('Vertices are exported in object space, so this transform is thrown '
                          'away and the mesh lands somewhere else in game.')
            results.append(DoctorResult(
                check_id='object_transforms',
                severity=ERROR,
                message=f'"{obj.name}" has an unapplied transform.',
                detail=detail,
                target_type=TARGET_OBJECT,
                target_name=obj.name,
                fix_id='apply_transform' if can_apply else '',
                fix_label='Apply transform' if can_apply else '',
                fix_is_safe=can_apply,
                payload={'object': obj.name},
            ))
    return results


@check(
    'duplicate_names',
    'Duplicate mesh and material names',
    'Blender .001 suffixes are trimmed on export, which can collide.',
    scopes=(SCOPE_MODEL,),
)
def check_duplicate_names(scene):
    results = []
    if scene.armature is None:
        return results

    # Meshes sharing a trimmed name are grouped into one Smash mesh on purpose,
    # so that is worth saying out loud but is not a problem.
    mesh_groups = {}
    for mesh in scene.meshes:
        mesh_groups.setdefault(_trim(mesh.name), []).append(mesh.name)
    for group_name, names in sorted(mesh_groups.items()):
        if len(names) > 1:
            results.append(DoctorResult(
                check_id='duplicate_names',
                severity=INFO,
                message=f'{len(names)} meshes export as the single group "{group_name}".',
                detail='Smash groups meshes by name, so this is how multi-material meshes are '
                       'meant to look. Members: ' + ', '.join(sorted(names)),
                target_type=TARGET_OBJECT,
                target_name=sorted(names)[0],
            ))

    material_groups = {}
    for _mesh, _index, material in scene.materials:
        if material is None:
            continue
        material_groups.setdefault(_trim(material.name), set()).add(material.name)
    for trimmed, names in sorted(material_groups.items()):
        if len(names) > 1:
            results.append(DoctorResult(
                check_id='duplicate_names',
                severity=WARNING,
                message=f'{len(names)} materials collide on the trimmed name "{trimmed}".',
                detail='Because the names are not unique after trimming, the exporter gives up '
                       'on trimming and ships the .001 suffixes in the .numatb. Rename them: '
                       + ', '.join(sorted(names)),
                target_type=TARGET_MATERIAL,
                target_name='',
                sub_target=sorted(names)[0],
            ))

    texture_names = {}
    for _mesh, _index, material in scene.materials:
        if material is None:
            continue
        sub_matl_data = getattr(material, 'sub_matl_data', None)
        for texture in getattr(sub_matl_data, 'textures', []) or []:
            image = getattr(texture, 'image', None)
            if image is not None:
                texture_names.setdefault(_trim(image.name), set()).add(image.name)
    for trimmed, names in sorted(texture_names.items()):
        if len(names) > 1:
            results.append(DoctorResult(
                check_id='duplicate_names',
                severity=WARNING,
                message=f'{len(names)} textures collide on the trimmed name "{trimmed}".',
                detail='The exporter ships the untrimmed names instead, which will not match the '
                       'nutexb files the game expects. Rename them: ' + ', '.join(sorted(names)),
            ))
    return results


@check(
    'skeleton_bones',
    'Missing or extra skeleton bones',
    'Standard bones must match the vanilla .nusktb the model is replacing.',
    scopes=(SCOPE_MODEL,),
)
def check_skeleton_bones(scene):
    from ..model.export_model import get_standard_bone_changes

    results = []
    if scene.armature is None:
        return results

    ssp = scene.ssp
    vanilla_nusktb = getattr(ssp, 'vanilla_nusktb', '') if ssp is not None else ''
    if not vanilla_nusktb:
        return [DoctorResult(
            check_id='skeleton_bones',
            severity=INFO,
            message='No vanilla .nusktb selected, so the skeleton was not compared.',
            detail='Character mods should pick one in the Model Exporter so missing and added '
                   'standard bones can be caught before export.',
        )]

    if not os.path.isfile(vanilla_nusktb):
        return [DoctorResult(
            check_id='skeleton_bones',
            severity=ERROR,
            message='The selected vanilla .nusktb is not on disk.',
            detail=vanilla_nusktb,
            target_type=TARGET_PATH,
            target_name=vanilla_nusktb,
            blocking=True,
        )]

    new_bones, missing_bones = get_standard_bone_changes(scene.armature, vanilla_nusktb)

    for bone in sorted(missing_bones):
        results.append(DoctorResult(
            check_id='skeleton_bones',
            severity=ERROR,
            message=f'Standard bone "{bone}" is missing from the armature.',
            detail='The vanilla skeleton has it. Animations and in-game code that reference it '
                   'will break. Re-add the bone or pick a different vanilla .nusktb.',
            target_type=TARGET_OBJECT,
            target_name=scene.armature.name,
            blocking=True,
        ))

    if new_bones:
        update_prc = getattr(ssp, 'vanilla_update_prc', '') if ssp is not None else ''
        detail = 'New standard bones need a regenerated update.prc in the motion folder, for '
        detail += 'example fighter/demon/motion/body/c00/update.prc.'
        if not update_prc:
            detail += ' No vanilla update.prc is selected yet.'
        results.append(DoctorResult(
            check_id='skeleton_bones',
            severity=WARNING,
            message=f'{len(new_bones)} standard bones are not in the vanilla skeleton.',
            detail=detail + ' Bones: ' + ', '.join(sorted(new_bones)),
            target_type=TARGET_OBJECT,
            target_name=scene.armature.name,
        ))
    return results


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


@check(
    'nan_transforms',
    'NaN or infinite transforms',
    'Non-finite numbers anywhere in the export set.',
    scopes=(SCOPE_MODEL, SCOPE_ANIM),
)
def check_nan_transforms(scene):
    results = []

    objects = list(scene.meshes)
    if scene.armature is not None:
        objects.append(scene.armature)
    if scene.camera is not None:
        objects.append(scene.camera)

    for obj in objects:
        if not _matrix_is_finite(obj.matrix_world):
            results.append(DoctorResult(
                check_id='nan_transforms',
                severity=ERROR,
                message=f'"{obj.name}" has a non-finite object transform.',
                detail='NaN or infinity in a matrix propagates into every value derived from it. '
                       'Reset the transform before exporting.',
                target_type=TARGET_OBJECT,
                target_name=obj.name,
                blocking=True,
            ))

    for mesh in scene.meshes:
        bad = 0
        for vertex in mesh.data.vertices:
            if not _is_finite(vertex.co):
                bad += 1
        if bad:
            results.append(DoctorResult(
                check_id='nan_transforms',
                severity=ERROR,
                message=f'"{mesh.name}" has {bad} vertices at non-finite positions.',
                detail='These write NaN into the .numshb, which crashes or corrupts the model in '
                       'game. Delete or move them.',
                target_type=TARGET_OBJECT,
                target_name=mesh.name,
                blocking=True,
            ))

    if scene.armature is not None:
        for bone in scene.armature.data.bones:
            if not _matrix_is_finite(bone.matrix_local):
                results.append(DoctorResult(
                    check_id='nan_transforms',
                    severity=ERROR,
                    message=f'Bone "{bone.name}" has a non-finite rest transform.',
                    detail='The .nusktb cannot represent this. Fix the bone in Edit Mode.',
                    target_type=TARGET_BONE,
                    target_name=scene.armature.name,
                    sub_target=bone.name,
                    blocking=True,
                ))
        for pose_bone in scene.armature.pose.bones:
            if not _matrix_is_finite(pose_bone.matrix_basis):
                results.append(DoctorResult(
                    check_id='nan_transforms',
                    severity=ERROR,
                    message=f'Bone "{pose_bone.name}" has a non-finite pose transform.',
                    detail='Usually a zero-length constraint target or a divide by zero in a '
                           'driver. Clear the pose transform and re-key it.',
                    target_type=TARGET_BONE,
                    target_name=scene.armature.name,
                    sub_target=pose_bone.name,
                    blocking=True,
                ))

    from ..anim.fcurve_compat import get_all_action_fcurves

    for owner, action in scene.actions:
        bad_curves = []
        for fcurve in get_all_action_fcurves(action):
            for keyframe in fcurve.keyframe_points:
                if not _is_finite(keyframe.co):
                    bad_curves.append(f'{fcurve.data_path}[{fcurve.array_index}]')
                    break
        if bad_curves:
            results.append(DoctorResult(
                check_id='nan_transforms',
                severity=ERROR,
                message=f'"{action.name}" has non-finite keyframes.',
                detail=f'{len(bad_curves)} curves affected, first: {bad_curves[0]}.',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
                blocking=True,
            ))
    return results


@check(
    'bone_scale_compensation',
    'Bone scale compensation hazards',
    'Blender inheritance settings that Smash has no way to represent.',
    scopes=(SCOPE_MODEL, SCOPE_ANIM),
)
def check_bone_scale_compensation(scene):
    results = []
    if scene.armature is None:
        return results

    for bone in scene.armature.data.bones:
        inherit_scale = getattr(bone, 'inherit_scale', 'FULL')
        if inherit_scale != 'FULL':
            results.append(DoctorResult(
                check_id='bone_scale_compensation',
                severity=WARNING,
                message=f'Bone "{bone.name}" uses inherit scale "{inherit_scale}".',
                detail='Smash has one per-track compensate-scale flag, not per-bone inheritance '
                       'modes, so this bone poses differently in game than in Blender. Set it '
                       'back to Full and use the exporter Compensate Scale flag instead.',
                target_type=TARGET_BONE,
                target_name=scene.armature.name,
                sub_target=bone.name,
                fix_id='reset_inherit_scale',
                fix_label='Set inherit scale to Full',
                fix_is_safe=True,
                payload={'armature': scene.armature.name, 'bone': bone.name},
            ))
        if not getattr(bone, 'use_inherit_rotation', True):
            results.append(DoctorResult(
                check_id='bone_scale_compensation',
                severity=WARNING,
                message=f'Bone "{bone.name}" does not inherit rotation.',
                detail='Smash skeletons always inherit rotation, so the exported pose will not '
                       'match what Blender shows.',
                target_type=TARGET_BONE,
                target_name=scene.armature.name,
                sub_target=bone.name,
                fix_id='reset_inherit_rotation',
                fix_label='Re-enable inherit rotation',
                fix_is_safe=True,
                payload={'armature': scene.armature.name, 'bone': bone.name},
            ))

    # A scaled parent under an animated child is the case compensate scale exists
    # for, so say so once rather than per bone.
    scaled_parents = set()
    for pose_bone in scene.armature.pose.bones:
        if not pose_bone.children:
            continue
        if any(abs(axis - 1.0) > 1e-4 for axis in pose_bone.scale):
            scaled_parents.add(pose_bone.name)
    if scaled_parents:
        results.append(DoctorResult(
            check_id='bone_scale_compensation',
            severity=INFO,
            message=f'{len(scaled_parents)} posed bones scale their children.',
            detail='If the children should keep their size, turn on Compensate Scale in the '
                   'Animation Exporter. Bones: ' + ', '.join(sorted(scaled_parents)),
            target_type=TARGET_OBJECT,
            target_name=scene.armature.name,
        ))
    return results


@check(
    'export_paths',
    'Export paths that do not exist',
    'Remembered files and folders that have since moved or been deleted.',
    scopes=(SCOPE_MODEL, SCOPE_ANIM),
)
def check_export_paths(scene):
    results = []
    ssp = scene.ssp
    if ssp is None:
        return results

    # (property, kind, label, blocking)
    watched = (
        ('vanilla_nusktb', 'FILE', 'Vanilla .nusktb', True),
        ('vanilla_update_prc', 'FILE', 'Vanilla update.prc', True),
        ('model_import_folder_path', 'DIR', 'Model import folder', False),
        ('last_model_folder', 'DIR', 'Last model folder', False),
        ('animation_import_folder_path', 'DIR', 'Animation import folder', False),
        ('raw_animation_import_folder_path', 'DIR', 'Raw animation folder', False),
        ('last_anim_import_dir', 'DIR', 'Last animation import folder', False),
        ('last_anim_export_dir', 'DIR', 'Last animation export folder', False),
        ('last_swing_directory', 'DIR', 'Last swing folder', False),
        ('last_stage_light_dir', 'DIR', 'Last stage lighting folder', False),
        ('last_stage_shpc_dir', 'DIR', 'Last ambient SH folder', False),
    )

    for prop_name, kind, label, blocking in watched:
        raw = getattr(ssp, prop_name, '')
        if not raw:
            continue
        path = bpy.path.abspath(raw)
        exists = os.path.isfile(path) if kind == 'FILE' else os.path.isdir(path)
        if exists:
            continue
        noun = 'file' if kind == 'FILE' else 'folder'
        results.append(DoctorResult(
            check_id='export_paths',
            severity=ERROR if blocking else WARNING,
            message=f'{label} {noun} does not exist.',
            detail=path,
            target_type=TARGET_PATH,
            target_name=path,
            blocking=blocking,
            fix_id='clear_path',
            fix_label=f'Clear "{label}"',
            fix_is_safe=not blocking,
            payload={'property': prop_name},
        ))
    return results


# ---------------------------------------------------------------------------
# Animation checks
# ---------------------------------------------------------------------------


@check(
    'unbaked_bl_controls',
    'Animated BL_* controls that are not baked',
    'Rig control bones are skipped on export, so their animation has to be baked first.',
    scopes=(SCOPE_ANIM,),
)
def check_unbaked_bl_controls(scene):
    from ..anim.fcurve_compat import get_all_action_fcurves

    results = []
    if scene.armature is None:
        return results

    pose_bone_names = {bone.name for bone in scene.armature.pose.bones}

    for owner, action in scene.actions:
        if owner.type != 'ARMATURE':
            continue
        animated_controls = set()
        animated_exported = set()
        for fcurve in get_all_action_fcurves(action):
            match = _POSE_BONE_PATH.match(fcurve.data_path or '')
            if match is None:
                continue
            bone_name = match.group(1)
            if bone_name not in pose_bone_names:
                continue
            if bone_name.startswith(BL_CONTROL_PREFIX):
                animated_controls.add(bone_name)
            else:
                animated_exported.add(bone_name)

        if not animated_controls:
            continue

        listed = ', '.join(sorted(animated_controls)[:6])
        if len(animated_controls) > 6:
            listed += f', and {len(animated_controls) - 6} more'

        if not animated_exported:
            results.append(DoctorResult(
                check_id='unbaked_bl_controls',
                severity=ERROR,
                message=f'"{action.name}" only animates rig controls.',
                detail='Every keyed bone starts with BL_, and the exporter skips those, so this '
                       f'animation exports as a static pose. Bake the rig first. Controls: {listed}',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
                fix_id='bake_animation_rig',
                fix_label='Bake and remove rig...',
                fix_is_safe=False,
                payload={'armature': owner.name, 'action': action.name},
            ))
        else:
            results.append(DoctorResult(
                check_id='unbaked_bl_controls',
                severity=WARNING,
                message=f'"{action.name}" animates {len(animated_controls)} rig controls.',
                detail='BL_ bones are skipped on export, so anything they drive that has not been '
                       f'baked onto Smash bones is lost. Controls: {listed}',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
                fix_id='bake_animation_rig',
                fix_label='Bake and remove rig...',
                fix_is_safe=False,
                payload={'armature': owner.name, 'action': action.name},
            ))
    return results


@check(
    'action_slots',
    'Invalid action slots',
    'Layered actions must carry a slot matching the ID they animate.',
    scopes=(SCOPE_ANIM,),
)
def check_action_slots(scene):
    from ..blender_compat import (
        id_type_for_id_data,
        slot_display_name,
        slot_id_type,
        uses_legacy_action_fcurves,
    )
    from ..anim.fcurve_compat import get_all_action_fcurves

    results = []

    for owner, action in scene.actions:
        if uses_legacy_action_fcurves(action):
            continue

        expected_type = id_type_for_id_data(owner)
        slots = list(getattr(action, 'slots', []) or [])

        if not slots:
            results.append(DoctorResult(
                check_id='action_slots',
                severity=ERROR,
                message=f'"{action.name}" has no action slot.',
                detail='A layered action with no slot animates nothing, so it exports as a static '
                       'pose. Re-assigning the action rebuilds the slot.',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
                blocking=True,
                fix_id='repair_action_slot',
                fix_label='Rebuild action slot',
                fix_is_safe=True,
                payload={'owner': owner.name, 'action': action.name},
            ))
            continue

        if not any(slot_id_type(slot) == expected_type for slot in slots):
            found = ', '.join(sorted({str(slot_id_type(slot)) for slot in slots}))
            results.append(DoctorResult(
                check_id='action_slots',
                severity=ERROR,
                message=f'"{action.name}" has no {expected_type} slot.',
                detail=f'It carries {found} slots, but "{owner.name}" needs {expected_type}. '
                       'Blender will not bind the action, so nothing exports.',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
                blocking=True,
                fix_id='repair_action_slot',
                fix_label='Rebuild action slot',
                fix_is_safe=True,
                payload={'owner': owner.name, 'action': action.name},
            ))
            continue

        anim = getattr(owner, 'animation_data', None)
        if anim is not None and anim.action == action:
            assigned = getattr(anim, 'action_slot', None)
            if assigned is None and get_all_action_fcurves(action):
                results.append(DoctorResult(
                    check_id='action_slots',
                    severity=ERROR,
                    message=f'"{owner.name}" has "{action.name}" assigned with no slot bound.',
                    detail='The curves exist but nothing evaluates them, so the export samples '
                           'the rest pose.',
                    target_type=TARGET_ACTION,
                    target_name=owner.name,
                    sub_target=action.name,
                    blocking=True,
                    fix_id='repair_action_slot',
                    fix_label='Bind the action slot',
                    fix_is_safe=True,
                    payload={'owner': owner.name, 'action': action.name},
                ))
            elif assigned is not None and slot_display_name(assigned) != owner.name:
                results.append(DoctorResult(
                    check_id='action_slots',
                    severity=WARNING,
                    message=f'"{action.name}" is bound to the slot "{slot_display_name(assigned)}".',
                    detail=f'The add-on names slots after the ID they drive, so "{owner.name}" is '
                           'expected here. Switching actions may bind the wrong curves.',
                    target_type=TARGET_ACTION,
                    target_name=owner.name,
                    sub_target=action.name,
                    fix_id='repair_action_slot',
                    fix_label='Rebind to this object',
                    fix_is_safe=True,
                    payload={'owner': owner.name, 'action': action.name},
                ))
    return results


@check(
    'camera_node_names',
    'Camera node names',
    'Camera anims are written with fixed node names that the scene should match.',
    scopes=(SCOPE_ANIM,),
)
def check_camera_node_names(scene):
    results = []
    camera = scene.camera
    if camera is None:
        return results

    if _trim(camera.name) != CAMERA_NODE_NAME:
        results.append(DoctorResult(
            check_id='camera_node_names',
            severity=WARNING,
            message=f'Camera "{camera.name}" is not named "{CAMERA_NODE_NAME}".',
            detail='The exporter always writes the Transform node as "gya_camera", so a camera '
                   'named anything else will not round-trip and is easy to mix up with a stage '
                   'camera that needs a different node.',
            target_type=TARGET_OBJECT,
            target_name=camera.name,
            fix_id='rename_camera_object',
            fix_label=f'Rename to "{CAMERA_NODE_NAME}"',
            fix_is_safe=True,
            payload={'object': camera.name},
        ))

    if _trim(camera.data.name) != CAMERA_SHAPE_NODE_NAME:
        results.append(DoctorResult(
            check_id='camera_node_names',
            severity=WARNING,
            message=f'Camera data "{camera.data.name}" is not named "{CAMERA_SHAPE_NODE_NAME}".',
            detail='FieldOfView, NearClip and FarClip are written under the "gya_cameraShape" '
                   'node. Matching the data name keeps Blender and the .nuanmb readable together.',
            target_type=TARGET_OBJECT,
            target_name=camera.name,
            fix_id='rename_camera_data',
            fix_label=f'Rename to "{CAMERA_SHAPE_NODE_NAME}"',
            fix_is_safe=True,
            payload={'object': camera.name},
        ))
    return results


@check(
    'action_frame_range',
    'Action frame ranges that omit keys',
    'Export samples the scene frame range, so keys outside it never ship.',
    scopes=(SCOPE_ANIM,),
)
def check_action_frame_range(scene):
    results = []
    frame_start = scene.scene.frame_start
    frame_end = scene.scene.frame_end

    for owner, action in scene.actions:
        frames = _action_key_frames(action)
        if not frames:
            results.append(DoctorResult(
                check_id='action_frame_range',
                severity=WARNING,
                message=f'"{action.name}" has no keyframes.',
                detail='Exporting it produces a single static pose.',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
            ))
            continue

        first = min(frames)
        last = max(frames)
        if first < frame_start or last > frame_end:
            results.append(DoctorResult(
                check_id='action_frame_range',
                severity=WARNING,
                message=f'"{action.name}" has keys outside the scene range.',
                detail=f'Keys run {first:g} to {last:g} but the scene range is '
                       f'{frame_start} to {frame_end}, so the export samples only part of it.',
                target_type=TARGET_ACTION,
                target_name=owner.name,
                sub_target=action.name,
                fix_id='fit_scene_frame_range',
                fix_label='Fit scene range to this action',
                fix_is_safe=True,
                payload={
                    'action': action.name,
                    'first': int(math.floor(first)),
                    'last': int(math.ceil(last)),
                },
            ))
    return results


# ---------------------------------------------------------------------------
# Fixes
# ---------------------------------------------------------------------------


def _object_from(result, key='object'):
    name = result.payload.get(key, result.target_name)
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise LookupError(f'"{name}" is no longer in the file.')
    return obj


@fixer('add_uv_map')
def fix_add_uv_map(context, result):
    mesh_object = _object_from(result)
    name = result.payload['attribute']
    if name in mesh_object.data.uv_layers:
        return True, f'"{name}" already exists on "{mesh_object.name}".'
    mesh_object.data.uv_layers.new(name=name, do_init=True)
    return True, f'Added the UV map "{name}" to "{mesh_object.name}".'


@fixer('add_color_set')
def fix_add_color_set(context, result):
    mesh_object = _object_from(result)
    name = result.payload['attribute']
    neutral = NEUTRAL_COLOR_SETS.get(name)
    if neutral is None:
        return False, f'No neutral value is documented for "{name}".'
    mesh = mesh_object.data
    if name in mesh.color_attributes:
        return True, f'"{name}" already exists on "{mesh_object.name}".'
    attribute = mesh.color_attributes.new(name=name, type='FLOAT_COLOR', domain='CORNER')
    for datum in attribute.data:
        datum.color = neutral
    return True, f'Added a neutral "{name}" to "{mesh_object.name}".'


def _deform_indices_for_fix(result, mesh_object):
    """Resolve the export armature the same way the check did.

    The mesh may not have an Armature modifier yet (that is its own fix), so
    the payload's armature name comes first and find_armature() is a fallback.
    """
    armature = bpy.data.objects.get(result.payload.get('armature', ''))
    if armature is None:
        armature = mesh_object.find_armature()
    if armature is None and mesh_object.parent is not None and mesh_object.parent.type == 'ARMATURE':
        armature = mesh_object.parent
    bones = armature.data.bones if armature is not None else {}
    return {group.index for group in mesh_object.vertex_groups if group.name in bones}


@fixer('limit_weights')
def fix_limit_weights(context, result):
    """Keep the four heaviest deform influences per vertex and renormalize."""
    mesh_object = _object_from(result)
    deform_indices = _deform_indices_for_fix(result, mesh_object)
    if not deform_indices:
        return False, f'"{mesh_object.name}" has no vertex groups matching armature bones.'

    trimmed = 0
    to_remove = {}
    for vertex in mesh_object.data.vertices:
        influences = [group for group in vertex.groups if group.group in deform_indices]
        if len(influences) <= 4:
            continue
        influences.sort(key=lambda group: group.weight, reverse=True)
        keep = influences[:4]
        total = sum(group.weight for group in keep)
        if total > 0.0:
            for group in keep:
                group.weight = group.weight / total
        for group in influences[4:]:
            to_remove.setdefault(group.group, []).append(vertex.index)
        trimmed += 1

    for group_index, vertex_indices in to_remove.items():
        mesh_object.vertex_groups[group_index].remove(vertex_indices)

    return True, f'Limited {trimmed} vertices on "{mesh_object.name}" to 4 weights.'


@fixer('normalize_weights')
def fix_normalize_weights(context, result):
    mesh_object = _object_from(result)
    deform_indices = _deform_indices_for_fix(result, mesh_object)
    if not deform_indices:
        return False, f'"{mesh_object.name}" has no vertex groups matching armature bones.'

    normalized = 0
    for vertex in mesh_object.data.vertices:
        influences = [group for group in vertex.groups if group.group in deform_indices]
        total = sum(group.weight for group in influences)
        if not influences or total <= 0.0 or abs(total - 1.0) <= 1e-4:
            continue
        for group in influences:
            group.weight = group.weight / total
        normalized += 1
    return True, f'Normalized {normalized} vertices on "{mesh_object.name}".'


@fixer('apply_transform')
def fix_apply_transform(context, result):
    mesh_object = _object_from(result)
    if mesh_object.data.users != 1:
        return False, f'"{mesh_object.name}" shares its mesh data, so applying would move others.'
    if mesh_object.data.shape_keys is not None:
        return False, f'"{mesh_object.name}" has shape keys, which Blender cannot transform-apply.'

    previous_active = context.view_layer.objects.active
    previous_selection = [obj for obj in context.view_layer.objects if obj.select_get()]
    previous_mode = context.mode

    try:
        if previous_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.view_layer.objects:
            obj.select_set(False)
        mesh_object.select_set(True)
        context.view_layer.objects.active = mesh_object
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    finally:
        for obj in context.view_layer.objects:
            obj.select_set(False)
        for obj in previous_selection:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        if previous_active is not None:
            context.view_layer.objects.active = previous_active

    return True, f'Applied the transform on "{mesh_object.name}".'


@fixer('remove_extra_armature_modifiers')
def fix_remove_extra_armature_modifiers(context, result):
    mesh_object = _object_from(result)
    armature = bpy.data.objects.get(result.payload.get('armature', ''))
    modifiers = [modifier for modifier in mesh_object.modifiers if modifier.type == 'ARMATURE']
    if len(modifiers) <= 1:
        return True, f'"{mesh_object.name}" already has at most one Armature modifier.'

    keep = next((m for m in modifiers if m.object is armature), modifiers[0])
    removed = 0
    for modifier in modifiers:
        if modifier is keep:
            continue
        mesh_object.modifiers.remove(modifier)
        removed += 1
    return True, f'Removed {removed} extra Armature modifiers from "{mesh_object.name}".'


@fixer('retarget_armature_modifier')
def fix_retarget_armature_modifier(context, result):
    mesh_object = _object_from(result)
    armature = bpy.data.objects.get(result.payload.get('armature', ''))
    if armature is None:
        return False, 'The export armature is no longer in the file.'
    modifier = mesh_object.modifiers.get(result.payload.get('modifier', ''))
    if modifier is None:
        return False, 'That modifier no longer exists.'
    modifier.object = armature
    return True, f'"{modifier.name}" on "{mesh_object.name}" now uses "{armature.name}".'


@fixer('add_armature_modifier')
def fix_add_armature_modifier(context, result):
    mesh_object = _object_from(result)
    armature = bpy.data.objects.get(result.payload.get('armature', ''))
    if armature is None:
        return False, 'The export armature is no longer in the file.'
    if any(modifier.type == 'ARMATURE' for modifier in mesh_object.modifiers):
        return True, f'"{mesh_object.name}" already has an Armature modifier.'
    modifier = mesh_object.modifiers.new(name='Armature', type='ARMATURE')
    modifier.object = armature
    return True, f'Added an Armature modifier to "{mesh_object.name}".'


def _bone_from(result):
    armature = bpy.data.objects.get(result.payload.get('armature', result.target_name))
    if armature is None:
        raise LookupError('That armature is no longer in the file.')
    bone = armature.data.bones.get(result.payload.get('bone', result.sub_target))
    if bone is None:
        raise LookupError('That bone is no longer in the armature.')
    return armature, bone


@fixer('reset_inherit_scale')
def fix_reset_inherit_scale(context, result):
    _armature, bone = _bone_from(result)
    bone.inherit_scale = 'FULL'
    return True, f'"{bone.name}" now inherits scale fully.'


@fixer('reset_inherit_rotation')
def fix_reset_inherit_rotation(context, result):
    _armature, bone = _bone_from(result)
    bone.use_inherit_rotation = True
    return True, f'"{bone.name}" now inherits rotation.'


@fixer('repair_action_slot')
def fix_repair_action_slot(context, result):
    from ..blender_compat import assign_action, ensure_action_slot

    owner = bpy.data.objects.get(result.payload.get('owner', result.target_name))
    action = bpy.data.actions.get(result.payload.get('action', result.sub_target))
    if owner is None or action is None:
        return False, 'That object or action is no longer in the file.'

    ensure_action_slot(action, owner)
    if owner.animation_data is None:
        owner.animation_data_create()
    previous = owner.animation_data.action
    assign_action(owner.animation_data, action)
    if previous is not None and previous is not action:
        assign_action(owner.animation_data, previous)
    return True, f'Rebuilt the action slot for "{action.name}".'


@fixer('rename_camera_object')
def fix_rename_camera_object(context, result):
    camera = _object_from(result)
    camera.name = CAMERA_NODE_NAME
    return True, f'Renamed the camera to "{camera.name}".'


@fixer('rename_camera_data')
def fix_rename_camera_data(context, result):
    camera = _object_from(result)
    camera.data.name = CAMERA_SHAPE_NODE_NAME
    return True, f'Renamed the camera data to "{camera.data.name}".'


@fixer('fit_scene_frame_range')
def fix_fit_scene_frame_range(context, result):
    first = int(result.payload['first'])
    last = int(result.payload['last'])
    context.scene.frame_start = min(first, last)
    context.scene.frame_end = max(first, last)
    return True, f'Scene range set to {context.scene.frame_start}-{context.scene.frame_end}.'


@fixer('clear_path')
def fix_clear_path(context, result):
    ssp = getattr(context.scene, 'sub_scene_properties', None)
    prop_name = result.payload.get('property', '')
    if ssp is None or not hasattr(ssp, prop_name):
        return False, 'That setting no longer exists.'
    setattr(ssp, prop_name, '')
    return True, f'Cleared the remembered path for "{prop_name}".'


@fixer('bake_animation_rig')
def fix_bake_animation_rig(context, result):
    """Hand off to the existing bake operator, which asks what to bake."""
    armature = bpy.data.objects.get(result.payload.get('armature', result.target_name))
    if armature is None:
        return False, 'That armature is no longer in the file.'
    context.view_layer.objects.active = armature
    if not bpy.ops.sub.bake_and_remove_rig.poll():
        return False, 'The animation rig operator is not available for this armature.'
    bpy.ops.sub.bake_and_remove_rig('INVOKE_DEFAULT')
    return True, 'Opened Bake and Remove Rig. Re-run the doctor once it finishes.'
