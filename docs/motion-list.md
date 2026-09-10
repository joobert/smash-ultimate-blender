# Motion list integration

Open **Animation Exporter → Motion List**, or use the same controls in the
single and batch export file browsers. Select an active animation, then press
**Read Matching Entry** to review its current cancel frame, blend frames, and
flags. Reading does not modify the file or enable updates.

Leave **Motion List** empty to detect `motion_list.bin`, `motion_list.yml`, or
`motion_list.yaml` in the export folder or its parents, through the `motion`
directory. The nearest file wins. If multiple formats exist in that directory,
choose the intended file explicitly. A list can therefore live directly in
`fighter/<fighter>/motion`, or in `motion/body/cXX` beside the animations.
Reading from the sidebar uses the selected animation's import/export folder;
export-time detection always uses the actual output path.

Enable **Update Motion List on Export** to save changes with animation exports.
Matching uses the entry's `animations[].name` Hash40, not the move key: for
example, `attack_air_f` can reference `c05attackairf.nuanmb`. Enter **Motion Key**
only to resolve an animation shared by several moves or to create a new entry.
For batch export, leave this field empty so each output matches independently.

- **Cancel Frame → Keep Existing** preserves the current value, including zero.
  **Exported Length** uses the `.nuanmb` final frame index: end minus start.
  Exporting frames 1–40 sets 39. **Custom** uses your entered value. The game
  format stores a byte, so values outside 0–255 are rejected, not clamped.
  Entries without fighter extra data require **Keep Existing**.
- **Override Blend Frames** replaces the entry's byte-sized blend frame count.
  When unchecked, each entry keeps its existing value.
- **Override Flags** writes the fourteen exposed flags. When unchecked, each
  entry keeps its own flags. Unknown flag meanings are explicitly labeled in
  tooltips; reading an entry lets you preserve them while changing known flags.
- To create a new entry, specify a new **Motion Key** and an existing
  **New Entry Template** key. The template supplies scripts, flags, and fighter
  extra data; its animation reference is replaced with the exported filename.
  Template scripts are copied unchanged, so review them before using a new move.
  Existing keys pointing at other animations are never silently overwritten.

The plugin detects `yamlist` on PATH or at `~/.cargo/bin/yamlist.exe`. Set the
**yamlist** path to use another installation. It invokes `disasm` and `asm` in
temporary directories and checks the resulting binary. Clear the path to use
the bundled binary codec. YAML parsing is bundled and does not require a Python
package installation in Blender. The schema and binary layout follow
[motion_lib](https://github.com/ultimate-research/motion_lib).

Motion edits are validated before writing the animation and committed only
after the animation is saved at the requested filename. If a file lock forces
an alternate animation filename, the motion list is left unchanged. A disk
change during export aborts the motion update. A first-write `.bak` preserves
the original list, and updates replace the file atomically. If motion saving
fails after animation saving, the animation remains exported; the export
reports the error so you can retry the motion update.

Only the chosen list is updated; sibling BIN/YAML representations are not
synchronized. YAML values and unrelated keys are preserved, but serialization
normalizes formatting and removes comments. No motion list is created from
scratch, and no game scripts are generated.
