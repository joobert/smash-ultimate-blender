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

## Outcome

Measured on Blender 4.5.7, the c40 Smash rig (89 bones), a 156-frame clip
for IK creation and a 371-frame clip for import and bake. Median of
three runs.

| Phase | Before | After | Change |
| --- | --- | --- | --- |
| Create IK (dialog OK) | 4.52s | 3.04s | 1.5x |
| Import animation, IK active | 40.87s | 6.99s | 5.8x |
| Bake and Remove IK | 20.46s | 0.71s | 28.8x |
| Whole cycle | 66.06s | 10.70s | 6.2x |
| Finger slider bake (157 frames) | 1.42s | 0.27s | 5.2x |

### The pole angle was not replaced

The design called for replacing the golden-section pole angle search with
a closed form. That was implemented, measured, and reverted.

The objective looked analytically solvable. Changing the pole angle
should rotate the solved chain rigidly about the root-to-target axis,
which makes the squared distance to the sampled pose exactly
C + A*cos(t) + B*sin(t), and three samples pin that down. The closed
form does find angles that score *lower* on that objective than the
search does. It also drifts the end effector further from the FK pose:
worst-case limb error went from 0.415 to 0.622 and the median from 0.039
to 0.049. Clamping the closed form to the same bracket the search used
did not recover it.

The likely cause is that Blender's IK solver warm-starts from the
previous evaluation, so the residual depends on the path taken through
angles rather than only on the angle. That makes a small-step local
search meaningful and a three-probe fit not.

Cutting the iteration count was measured separately: 10 or 12 steps are
indistinguishable from 18 on the benchmark clip, and even 3 steps stays
within noise. That is single-clip evidence about output quality for a
saving well under a second, so it was not taken either.

The search therefore still costs about 25 evaluations per chain per
frame, and it is now the great majority of what a match costs. Every
speedup above comes from removing work around it.

### Fidelity is approximate, and was already

Matching IK to FK does not reproduce the FK animation exactly on this
rig: median limb error 0.039, worst 0.415, on the arm chains in
particular. Two-bone IK solves the shoulder rather than copying it, so
some poses are not reachable. This is pre-existing and unchanged --
before and after agree to five decimal places across the whole
distribution -- but it is worth knowing about independently of
performance. tests/test_ik_match_fidelity_blender.py asserts against
these measured values.

### Bugs found on the way

Both predate this work and both break on Blender 4.4/4.5, the add-on's
stated minimum.

`Action.fcurve_ensure_for_datablock` exists on Blender 4.x but takes no
`group_name`, so passing one raised TypeError. That broke raw animation
import and the Bake Visible retarget path.

`set_finger_slider_mode` assigned `PoseBone.select`, which only exists on
Blender 5. Finger sliders could not be built or toggled at all.

### Hot spots measured and left alone

Material and visibility import: the whole of `import_model_anim` is 0.31s
of a 7.0s import, so its per-frame `keyframe_insert` calls are not worth
restructuring.

`eye_rig.bake_eye_look_keys`: dominated by the per-frame `frame_set` it
needs to read the posed control, with roughly 0.1s of keyframe overhead
to win.

`fk_to_ik._execute_transfer_body`: unreachable. `_execute_transfer`
routes to `ik_channels.match` unconditionally.

`clean_animation`: only runs behind the "Clean animation" checkbox, and
is pure Python over F-curves with no depsgraph work.

### Subprocess workers

Not built, as the design said. The remaining per-phase times are below
the several-second floor of saving the file and launching worker
Blenders, so workers would make these operations slower.
