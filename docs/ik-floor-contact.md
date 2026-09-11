# Live IK floor contact

Find **Live Floor Contact** inside **Ultimate → Animation Tools → IK Tools**.
The compact view shows enable, floor height, and Plant/Release per limb.
Expand **Advanced** for tuning, body assistance, markers, and removal.
Calibration exposes marker editing and mirroring directly.

Create the usual IK controls first. Contact is evaluated by Blender constraints
and drivers while posing, playing, and scrubbing. It does not bake poses, add
keys to the current action, or change the IK stretch switches.

## Calibrate a model

1. Choose **Set Up Floor Contact**. Correction pauses during calibration.
2. Pose the feet flat. Choose **Edit Markers** for that foot. Move its Heel and Toe markers onto the bottom of the mesh. These
   are contact samples, not an automatic mesh collision calculation.
3. Choose **Mirror** to copy those offsets across the armature's
   local X axis. Mirroring uses rest geometry, so an asymmetric current pose
   does not change the saved opposite-side offsets.
4. Choose **Finish Calibration**. This records the flat orientation, captures
   the initial plant references, hides the markers, and returns to Pose Mode.
5. Save or update an **Armature Collection Preset**, with **IK Floor
   Calibration** included. Use that panel's **Link to Model Folder** for the
   existing automatic import matching. Calibration loaded before IK creation
   is retained and installed when the IK controls are created.

Hands use Palm and Fingers markers. Their calibration loads with contact off;
feet load with contact on. Calibrate hands flat against the floor before using
their optional orientation alignment. Body height, plant locations, scene
floor height, and animation-specific planting choices are not model calibration.

Toe discovery is case-insensitive and follows descendants of `ToeL`/`ToeR`
regardless of Blender's **Connected** flag. The deepest descendant containing
`Toe` in its name becomes the grounded pivot. Thus `ToeL → BaseToeL` grounds
`BaseToeL`; a longer chain grounds its last toe bone. Earlier segments move
with the foot around that final joint. For equal-depth branches, a stable name
order chooses one pivot; this is not a solver that pins multiple separate toes.

## Pose and plant

- **Floor Height** is a world Z value belonging to the scene, initially zero.
- **Floor** keeps calibrated points above the plane and allows horizontal
  movement. The original IK control can still be dragged below the plane; the
  separate contact target stops at the floor.
- **Contact Softness** sets the height of the gentle approach zone. Zero gives
  a hard boundary. Above the zone, the target is unchanged. Softness never
  permits the calibrated points to penetrate the floor.
- **Plant** captures this limb's current location and holds its contact
  against the floor. **Release** clears manual planting, toe pinning, and automatic planting to
  restore sliding and lifting. The **Planted**
  property can also be keyframed. Plant markers are editable/keyframeable
  objects; use **Show Contact Markers** to expose them.
- **Auto Plant at Marker** attaches near that limb's saved plant marker and
  releases on lift or distance. Contact Softness also eases the release. Each
  limb operates independently. This is spatial attachment to editable markers,
  not first-touch detection of footsteps throughout a walking action. It needs
  no history cache or preprocessing and gives the same result on arbitrary seeks.
- **Horizontal Resistance** adds optional attraction toward that plant marker
  near the floor. It defaults to zero and does not accumulate simulated friction.
- **Align to Floor** uses the orientation recorded during flat calibration,
  preserving heading. **Lock Planted Rotation** holds a manually planted
  limb's rotation. **Follow Heel / Toe Contact** uses the lower contact point as the
  pivot; otherwise, horizontal planting uses the midpoint. Your rotation drives
  the roll; the assistant does not generate foot animation.

## Foot roll and toe curl

To keep the toe still while lifting the heel, leave `FootIK` and `ToeIK` in
place and rotate `FootRollIK`. It moves the ankle around the toe joint while
the terminal toe keeps its position and orientation.
Use `ToeIK` when you want to rotate the toe itself instead.

Multi-joint feet also get `ToeBendIKL/R` at the first toe joint. Rotate it to
bend the foot/ankle relative to that joint while keeping the toe chain still.
For `FootL -> ToeL -> BaseToeL`, `FootRollIKL` lifts the heel around `BaseToeL`;
`ToeBendIKL` adds articulation between `FootL` and `ToeL`. The controls combine,
and zero toe bend keeps the existing heel lift. `ToeIKL` still rotates the
terminal toe itself. A foot with just one toe bone gets no extra bend control.

The bend control is rotation-only and belongs to the IK Bones collection.
Fresh IK and matching install it; matching keys its neutral pose along with an
internal toe-orientation seed. Bake & Remove preserves the resulting deform
bone poses and removes both generated bones.

This motion does not require floor contact or **Pin Toe**. Floor correction
can translate the whole foot; disable it when testing an exact stationary toe.
The ankle target must be reachable, or leg stretch must be enabled. With
stretch off, an unreachable ankle target can still cause toe drift.

Reapply fresh IK to existing rigs to use the updated heel lift. Creation and
matching correct the pivot space automatically, including when `FootIK` and
the FK foot have different rest orientations. Existing nonzero roll poses can
change when their pivot is corrected or moved to a terminal toe.

**Pin Toe** holds the calibrated sample horizontally. If a backward rock puts
the heel below the toe, floor correction rests the heel on the floor and lets
the toe lift instead of forcing the heel underground. Contact markers affect
floor correction, not the anatomical hinge location.

The current rig has one anatomical roll hinge. A full heel/ball/toe-tip roll
system would add distinct pivots, as described in Blender's
[Rigify leg documentation](https://docs.blender.org/manual/en/latest/addons/rigging/rigify/rig_types/limbs.html).

## Stretch and body height

The existing stretch switch remains authoritative. With stretch on, the
endpoint follows the protected target without a new reach cap. With stretch
off, the original IK solver preserves limb length, so an unreachable target can
leave a gap. Floor protection describes the contact target: it cannot guarantee
an unreachable limb's mesh will meet that target.

**Set Up Body Height Adjustment** offers optional downward world-Z correction
of `Trans` to help feet reach. **Adjust Body Height** toggles it without editing
root keys. It is disabled while leg stretch is enabled. It requires an
unparented `Trans`, legs descending from it with standard transform inheritance,
and independent foot controls. Custom constrained ancestor hierarchies are
rejected rather than creating a dependency cycle. It is reach assistance, not a
balance solver: impossible horizontal reach or conflicting leg requirements
can still need manual posing. Body movement is preserved by default.

## Reversibility and scope

Switch off **Floor Contact** to restore the original IK targets' transforms.
**Remove Floor Contact** restores the solver's original target references and
removes this armature's contact helpers. The existing IK removal workflow also
cleans up contact for the removed limbs. Saved external calibration presets
remain available.

Helpers live in the **IK Floor Contact** collection and start hidden. Use
**Show Contact Markers** to reveal their editable markers; excluding the helper collection from
evaluation disables the mechanism. Foot protection uses the two calibrated
points, not every mesh vertex, so extreme sideways rolls may need additional
manual clearance. There is no wall, ledge, or moving-weapon contact mode here.
An armature linked into multiple scenes retains the floor-height scene chosen
when its contact helpers were created.

Regression checks run with:

```powershell
& 'C:\Program Files\Blender Foundation\Blender 4.5\blender.exe' --background --factory-startup --python-exit-code 1 --python tests/test_floor_contact_blender.py
```
