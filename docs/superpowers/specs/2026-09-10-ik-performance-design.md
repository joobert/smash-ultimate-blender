# IK apply/bake performance

Speed up IK matching, IK baking, and animation import while IK is active.
Remove the "Bake and Exit" checkbox from the retargeting bake dialog.

## Problem

Three per-frame loops dominate every IK operation. Each multiplies the
frame count by a large constant.

**Depsgraph evaluations in `match()`.** Every `yield` in
`_match_chain_steps` (`source/extras/ik_channels.py`) becomes a
`context.view_layer.update()`, a full dependency-graph re-evaluation.
Per frame per chain: about four seed yields, three candidate probes,
eighteen golden-section iterations at one or two probes each, and two
final yields. Roughly 28 evaluations. A 300-frame clip on a four-limb
rig costs about 8,400 full scene evaluations.

**Depsgraph evaluations in `bake()`.** The write pass calls
`view_layer.update()` inside the per-bone loop, because `pb.matrix = m`
requires the parent evaluated before the child is set. Twelve bones
across 300 frames is 3,600 evaluations, spent on arithmetic over world
matrices the sample pass already captured.

**`keyframe_insert()` per bone per frame.** `_key` and
`_key_pose_bone_visual` each resolve an RNA path, look up an F-curve,
insert, and re-sort the keyframe array. 300 frames times twelve bones
times about ten channels is roughly 36,000 calls. Blender's bulk path
is 20 to 50 times faster for this shape and appears nowhere in the
IK code, though `import_anim.py` already proves the pattern.

Two structural costs compound these. `limb_path()` re-walks the bone
tree on every frame of every loop. `_refresh_imported_ik` runs a
separate full frame sweep per limb kind, so an arms-and-legs rig pays
two complete sample-and-solve passes on every import.

`_refresh_imported_ik` also calls `match()` without `_batch=True`.
Since `_batch` defaults to `False`, `_can_batch_match` never runs on
the import path, and every import takes the fully sequential
one-update-per-yield branch.

## Non-goals

Subprocess worker pools. Blender's depsgraph and `bpy` data are
single-threaded, so parallelism requires separate processes, as
`source/retargeting/fast_bake.py` does. That carries a save plus N
process launches, a floor of several seconds. Once the costs above are
removed, a typical clip finishes below that floor, and workers would
make it slower. Revisit only if benchmarks show long clips still
hurting.

## Architecture

Three units, each independently testable.

`source/anim/fcurve_bulk.py` (new) accumulates pose-bone location,
rotation, and scale samples, then writes them to F-curves in one
`keyframe_points.add()` plus `foreach_set()` pass. This generalizes
`BoneTranslationFCurves` in `source/anim/import_anim.py` into something
the bake paths can use.

`source/extras/pose_math.py` (new) provides
`basis_from_world(pose_bone, target_world, parent_world)`, replicating
`BKE_armature_mat_pose_to_bone` in `mathutils` while honoring
`use_inherit_rotation`, `inherit_scale`, and `use_local_location`. It
returns `None` for any flag combination it does not model, so callers
fall back to setting `pb.matrix` and evaluating.

`source/extras/ik_channels.py` (changed) rewrites `match()` and
`bake()` against the two modules above, plus the cached pole angle
below.

`pose_math` is load-bearing: it is what lets both `bake()` and
`match()` set bones without a depsgraph round-trip. It gets its
verification test before anything depends on it.

## The pole angle

`_match_chain_steps` places the pole control directly in the desired
bend plane. The correct `pole_angle` is therefore not a per-frame
unknown. It is a near-constant per-chain offset arising from bone roll
convention. The current code rediscovers that constant from scratch on
every frame.

Per chain:

1. On the first frame, determine the angle fully. Evaluate at zero,
   compute `delta`, test both signs, and refine with the existing
   golden-section code when the residual exceeds tolerance. Cache the
   winning angle and sign convention on the chain.
2. On every later frame, set the cached angle and take one evaluation,
   measuring the residual the current code already measures. A
   residual at or below `1e-9`, today's own convergence threshold, is
   accepted. Anything above it falls through to the full first-frame
   path for that frame, which then re-caches.

The golden-section search is not deleted. It is demoted to the
fallback it should always have been. A chain whose geometry defeats
the closed form degrades to today's behavior and today's output, frame
by frame. The worst case is today's speed.

The seed-setting yields lose their evaluations entirely via
`pose_math.basis_from_world`, since the seed chain's target world
matrices are known up front.

Per frame per chain: about 28 evaluations become one, with four to six
on the first frame and on any frame that trips the fallback.

## bake()

The sample pass stays. It must evaluate the constrained IK pose, which
is irreducible at N evaluations. The write pass loses its depsgraph
work: instead of `frame_set`, then `pb.matrix = m`, then
`view_layer.update()`, then `keyframe_insert` per bone, it derives each
bone's basis arithmetically parent-first from the captured world
matrices and stashes it.

The sample pass also captures world matrices of parents outside
`names`. Those are unaffected by the bake and serve as fixed reference
frames. Parents inside `names` use their sampled world matrix, which
is what the bake makes true.

`N + N*bones` evaluations become `N`. No `keyframe_insert` calls.

## Import path

`_refresh_imported_ik` fuses its per-kind `match()` calls into one call
covering every eligible kind, so the clip is swept once rather than
once per limb kind. The `preserve_controls` eligibility check still
runs per kind, before the fused call.

It passes `_batch=True`, letting `_can_batch_match` decide as intended.
This is a standalone fix and lands as its own commit so its
contribution is visible separately in the benchmarks.

## Verification

Correctness gates come first in implementation order, not last.

**`pose_math` equivalence.** For every bone in the real imported rig,
at several frames, compare `basis_from_world` against ground truth
from `pb.matrix = m; view_layer.update()`. Report any bone diverging
beyond `1e-6`, and any bone where the function returns `None`. Runs
against the actual model, not a synthetic rig.

**Output equivalence.** Run old and new `match()` and `bake()` on
identical scene state, then diff every resulting F-curve keyframe
value frame by frame. A wrong pole angle produces a large error, not a
small one, so the tolerance can be tight. This gate decides whether
the analytic path ships.

**Existing suite.** `tests/test_animation_workflow_blender.py`,
`test_export_import_ik_features_blender.py`,
`test_visual_bake_blender.py`, and `test_floor_contact_blender.py`
pass, run headless with `--background --factory-startup
--python-exit-code 1 --python <file>`.

## Benchmarks

`.tests/benchmarks/ik_apply/run.py`, following the before/after
source-swap pattern of `.tests/benchmarks/ik_creation/run.py`, against
the same model and motion assets. Three scenarios: import with IK
active, IK creation, and Bake and Remove IK. Three runs each, median
reported, with the setup metadata block the existing harness prints.

## Bake and Exit

Remove the `do_bake` property, its `draw()` row, and the
`if not self.do_bake` guard from `source/retargeting/__init__.py` and
`expy_kit/operators.py`. OK alone then means bake. Independent of the
performance work; lands as its own commit.

## Other hot spots

Addressed after the core is proven and the bulk writer exists, in this
order, each profiled before being touched:

1. Material and visibility import in `source/anim/import_anim.py`,
   per-frame `keyframe_insert` on `arma.data` per property. Thousands
   of calls per clip on a rig with many material tracks.
2. `source/extras/fk_to_ik.py`, seventeen `frame_set` and
   `view_layer.update()` sites in the same per-frame-per-bone shape.
3. `finger_sliders.bake_finger_slider_keys` and
   `eye_rig.bake_eye_look_keys`, both per-frame `keyframe_insert`
   loops, both reached from the same Bake and Remove Rig confirmation.
4. `clean_animation` in `ik_channels.py`, unprofiled. Measure first.

Anything measured and found not worth changing is reported as such.
