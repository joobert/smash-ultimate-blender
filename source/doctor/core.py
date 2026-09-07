"""Result model, check registry and runner for the Smash Export Doctor.

A *check* is a plain function that takes a :class:`DoctorScene` and yields
:class:`DoctorResult` objects. A *fix* is a plain function that takes the
Blender context plus one result and returns ``(ok, message)``.

Neither is a Blender class, so both stay unit-testable and both can be run
headlessly. The UI layer in ``ui.py`` mirrors results into a PropertyGroup
collection so a UIList can draw them.
"""

import traceback

from dataclasses import dataclass, field


# Severities, ordered worst first. The panel sorts and counts by this order.
ERROR = 'ERROR'
WARNING = 'WARNING'
INFO = 'INFO'

SEVERITY_ORDER = (ERROR, WARNING, INFO)
SEVERITY_ICON = {
    ERROR: 'ERROR',
    WARNING: 'ERROR',
    INFO: 'INFO',
}

# What a result points at, so clicking it can select the right thing.
TARGET_NONE = 'NONE'
TARGET_OBJECT = 'OBJECT'
TARGET_MATERIAL = 'MATERIAL'
TARGET_BONE = 'BONE'
TARGET_ACTION = 'ACTION'
TARGET_PATH = 'PATH'

# Which half of the add-on a check belongs to. Model export runs MODEL checks,
# animation export runs ANIM checks, and the panel runs both.
SCOPE_MODEL = 'MODEL'
SCOPE_ANIM = 'ANIM'


@dataclass
class DoctorResult:
    """One finding. Everything here survives the round trip into a PropertyGroup."""

    check_id: str
    severity: str
    message: str
    detail: str = ''
    target_type: str = TARGET_NONE
    # For BONE targets this is the armature object; sub_target is the bone.
    # For MATERIAL targets this is the mesh object that uses the material.
    target_name: str = ''
    sub_target: str = ''
    # True only when the exported file would be invalid or would fail to write,
    # not merely wrong-looking. Only these can block an export.
    blocking: bool = False
    fix_id: str = ''
    fix_label: str = ''
    # Safe fixes are the ones "Fix Safe Issues" is allowed to run unattended.
    fix_is_safe: bool = False
    payload: dict = field(default_factory=dict)

    @property
    def fixable(self) -> bool:
        return bool(self.fix_id) and self.fix_id in FIXES


@dataclass
class CheckSpec:
    check_id: str
    label: str
    description: str
    scopes: tuple
    function: object


CHECKS: list = []
FIXES: dict = {}


def check(check_id: str, label: str, description: str = '', scopes=(SCOPE_MODEL,)):
    """Register a check function. Decorated functions take a DoctorScene."""

    def decorator(function):
        CHECKS.append(CheckSpec(check_id, label, description, tuple(scopes), function))
        return function

    return decorator


def fixer(fix_id: str):
    """Register a fix. Decorated functions take (context, DoctorResult)."""

    def decorator(function):
        FIXES[fix_id] = function
        return function

    return decorator


def run_fix(context, result):
    """Run one result's fix. Returns (ok, message); never raises."""
    function = FIXES.get(result.fix_id)
    if function is None:
        return False, f'No fix is registered for "{result.check_id}".'
    try:
        return function(context, result)
    except Exception as error:  # a bad fix must not kill the whole pass
        print(f'[Smash Export Doctor] Fix "{result.fix_id}" failed:')
        print(traceback.format_exc())
        return False, f'{type(error).__name__}: {error}'


class DoctorScene:
    """The things every check wants, gathered once per run.

    Collecting the export set here keeps the checks honest: they see the same
    meshes the model exporter would see, not every mesh in the file.
    """

    def __init__(self, context, scopes=(SCOPE_MODEL, SCOPE_ANIM)):
        import bpy

        self.context = context
        self.scene = context.scene
        self.scopes = tuple(scopes)
        self.ssp = getattr(context.scene, 'sub_scene_properties', None)

        self.armature = self._find_armature(context)
        self.meshes = self._find_meshes()
        self.materials = self._find_materials()
        self.camera = self._find_camera(context)
        self.actions = self._find_actions(bpy)

    def _find_armature(self, context):
        ssp = self.ssp
        arma = getattr(ssp, 'model_export_arma', None) if ssp is not None else None
        if arma is not None and arma.name in context.view_layer.objects:
            return arma
        active = context.active_object
        if active is not None and active.type == 'ARMATURE':
            return active
        if active is not None and active.parent is not None and active.parent.type == 'ARMATURE':
            return active.parent
        return arma

    def _find_meshes(self):
        """The meshes the model exporter would pick up, in the order it uses."""
        if self.armature is None:
            return []
        meshes = [
            child for child in self.armature.children
            if child.type == 'MESH' and len(child.data.vertices) > 0
        ]
        # The exporter skips swing meshes entirely.
        meshes = [
            mesh for mesh in meshes
            if not getattr(getattr(mesh.data, 'sub_swing_data_linked_mesh', None), 'is_swing_mesh', False)
        ]
        meshes.sort(key=lambda mesh: mesh.get('numshb order', 10000))
        return meshes

    def _find_materials(self):
        """Every (mesh, slot index, material) triple in the export set."""
        triples = []
        for mesh in self.meshes:
            for index, slot in enumerate(mesh.material_slots):
                triples.append((mesh, index, slot.material))
        return triples

    def _find_camera(self, context):
        active = context.active_object
        if active is not None and active.type == 'CAMERA':
            return active
        return None

    def _find_actions(self, bpy):
        """Actions that plausibly belong to the armature or camera in play."""
        from ..anim.fcurve_compat import action_matches_armature

        actions = []
        seen = set()

        for owner in (self.armature, self.camera):
            if owner is None:
                continue
            anim = getattr(owner, 'animation_data', None)
            if anim is not None and anim.action is not None and anim.action.name not in seen:
                seen.add(anim.action.name)
                actions.append((owner, anim.action))

        if self.armature is not None:
            for action in bpy.data.actions:
                if action.name in seen:
                    continue
                if 'SAP Data' in action.name or '_old' in action.name:
                    continue
                try:
                    if action_matches_armature(action, self.armature):
                        seen.add(action.name)
                        actions.append((self.armature, action))
                except Exception:  # malformed actions must not stop the run
                    continue
        return actions


def severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


def run_checks(context, scopes=(SCOPE_MODEL, SCOPE_ANIM), only_check_ids=None):
    """Run every check in scope. Returns results sorted worst-first.

    A check that raises is reported as its own WARNING rather than aborting the
    run, so one broken check can never hide the other fourteen.
    """
    scene = DoctorScene(context, scopes)
    results = []

    for spec in CHECKS:
        if only_check_ids is not None and spec.check_id not in only_check_ids:
            continue
        if not set(spec.scopes) & set(scopes):
            continue
        try:
            produced = spec.function(scene)
            if produced:
                results.extend(produced)
        except Exception as error:
            print(f'[Smash Export Doctor] Check "{spec.check_id}" failed:')
            print(traceback.format_exc())
            results.append(DoctorResult(
                check_id=spec.check_id,
                severity=WARNING,
                message=f'The "{spec.label}" check could not run.',
                detail=f'{type(error).__name__}: {error}',
            ))

    results.sort(key=lambda result: (severity_rank(result.severity), result.check_id, result.message))
    return results


def counts(results) -> dict:
    tally = {ERROR: 0, WARNING: 0, INFO: 0}
    for result in results:
        if result.severity in tally:
            tally[result.severity] += 1
    return tally


def blocking_results(results):
    return [result for result in results if result.blocking and result.severity == ERROR]
