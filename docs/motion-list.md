# Motion list integration

Open the armature's **Object Data Properties → Ultimate Animation Data →
Ultimate Motion List**. The panel edits the motion entry that matches the
active action, and the export file browsers no longer carry these controls.

Leave the path detection alone to find `motion_list.yml`, `motion_list.yaml`,
or `motion_list.bin` in the export folder or its parents, through the `motion`
directory. The nearest directory holding any of them wins, and within it YAML
is preferred over the binary, so an edit lands in the readable file when both
are present. Exactly one file is read and written: a `.bin` sitting beside an
edited `.yml` is left alone and will drift out of step, so regenerate it
yourself when that matters. Tick **Override Path** to pick the file by hand -
the full path of the chosen list is shown under the toggle - in which case only
that file is used. The box lists the motion, cancel frame, blend frames, and
flags of the entry last read.

**Motion List Auto-Sync**, in the **Ultimate Animation Data** header box, is on
by default and loads the matching entry whenever the active action changes; a
missing motion list is ignored silently. **Manual Sync** below it re-runs both
the SAP action sync and the motion list load on demand.

Enable **Update Motion List on Export** to save changes with animation exports.
Matching uses the entry's `animations[].name` Hash40, not the move key: for
example, `attack_air_f` can reference `c05attackairf.nuanmb`. An animation
referenced by several motions cannot be disambiguated from this panel and
reports an error instead.

- **Cancel frame** comes from a pose marker named `Cancel Frame` on the action.
  **Set Cancel Marker** places it at the current frame, **Clear Cancel Marker**
  removes it. The marker's frame number is written as-is, so work with a scene
  start frame of 0 if you want it to match the in-game index. The panel always
  shows the value that will be written. Values outside 0–255 are rejected, not
  clamped. With no marker, the entry keeps its existing cancel frame, and
  entries without fighter extra data must have no marker.
- **Blend Frames** and the **Turn**, **Loop**, and **Move** flags live on the
  action, so each animation carries its own and batch export writes each one
  correctly. They are written only after **Sync From Motion List** loads them
  from the entry or you edit them by hand; until then the panel says so and
  export preserves whatever the entry already had. The eleven undocumented
  flags and the game, effect, and sound scripts are always preserved.
- Duplicating an action copies both its cancel marker and these values, so a
  retimed copy only needs its marker moved.
- Creating a new motion entry is not supported from this panel. Exporting an
  animation no entry references reports an error.

Motion keys and animation names are shown as readable strings, resolved through
the bundled `ParamLabels.csv` and through any sibling YAML that spells them out.
Unresolved hashes are shown as `0x…`. Saving a YAML list writes readable names
in place of hashes; the result re-encodes to identical bytes, and the binary
format is unaffected.

Binary reading and writing use a built-in codec. No external tool is required,
and there is no `yamlist` path to configure. YAML parsing is bundled and needs
no Python package installation in Blender. The schema and binary layout follow
[motion_lib](https://github.com/ultimate-research/motion_lib).

Motion edits are validated before writing the animation and committed only
after the animation is saved at the requested filename. If a file lock forces
an alternate animation filename, the motion list is left unchanged. A disk
change to the target file aborts the update before anything is written. A
first-write `.bak` preserves the original, and updates replace the file
atomically. If motion saving fails after animation saving, the animation
remains exported; the export reports the error so you can retry.

YAML values and unrelated keys are preserved, but serialization normalizes
formatting and removes comments. No motion list is created from scratch, and no
game scripts are generated.
