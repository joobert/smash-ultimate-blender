# Live IK floor contact

Find **Live Floor Contact** inside **Ultimate → Animation Tools → IK Tools**.
Create the usual IK controls first. Contact is evaluated by Blender constraints
and drivers while posing, playing, and scrubbing. It does not bake poses, add
keys to the current action, or change the IK stretch switches.

## Calibrate a model

1. Choose **Set Up Floor Contact**. Correction pauses during calibration.
2. Pose the feet flat. Expand a foot's settings and choose **Edit Contact
   Markers**. Move its Heel and Toe markers onto the bottom of the mesh. These
   are contact samples, not an automatic mesh collision calculation.
3. Choose **Mirror Calibration** to copy those offsets across the armature's
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

Toe discovery is case-insensitive. A connected descendant whose name contains
`BaseToe` or `ToeBase` (for example `BaseToeR`) is preferred over the `ToeR`
root as the reverse-foot pivot and initial Toe marker. Other connected bones
containing `Toe` remain part of the leg's IK/FK animation handling.

## Pose and plant

- **Floor Height** is a world Z value belonging to the scene, initially zero.
- **Floor** keeps calibrated points above the plane and allows horizontal
  movement. The original IK control can still be dragged below the plane; the
  separate contact target stops at the floor.
- **Contact Softness** sets the height of the gentle approach zone. Zero gives
  a hard boundary. Above the zone, the target is unchanged. Softness never
  permits the calibrated points to penetrate the floor.
- **Plant Now** captures this limb's current location and holds its contact
  against the floor. **Release** restores sliding and lifting. The **Planted**
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
  limb's rotation. **Heel / Toe Roll** uses the lower contact point as the
  pivot; otherwise, horizontal planting uses the midpoint. Your rotation drives
  the roll; the assistant does not generate foot animation.

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
