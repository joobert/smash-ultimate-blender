# Smash Ultimate Blender Tools (Animation Workflow)

[![wiki](https://img.shields.io/badge/wiki-guide-success)](https://github.com/ssbucarlos/smash-ultimate-blender/wiki)

Blender addon for Super Smash Bros. Ultimate models and animations. This is the **animation-workflow** fork of [ssbucarlos/smash-ultimate-blender](https://github.com/ssbucarlos/smash-ultimate-blender). It keeps the original import/export pipeline and adds the animator-facing tools below.

If you only need vanilla model and animation I/O, the [upstream addon](https://github.com/ssbucarlos/smash-ultimate-blender) is enough. Use this fork if you want Smash Viewport, the animation rig, folder/raw anim workflows, retargeting, facial tools, and the rest of the Ultimate tab extras.

![Mario in Smash Viewport with the Ultimate tab](docs/readme/blender-overview.png)

## Smash Viewport

A custom render engine (`Smash Viewport`) that draws Smash shaders with the same `ssbh_wgpu` renderer SSBH Editor uses.

![Mario idle in Smash Viewport](docs/readme/smash-viewport.png)

1. Properties → Render → **Render Engine → Smash Viewport**
2. In the 3D View, set shading to **Rendered** (not Solid / Material Preview)
3. Floor grid and other overlays stay under Viewport Overlays; hide them there if you want a clean black backdrop

![Smash Viewport render settings](docs/readme/smash-viewport-settings.png)

- **Background** — clear color for the Smash draw (default black)
- **Stage Lights** — load a stage `light.nuanmb` here, or import it in Stage Tools and edit the suns live
- **Training Lights** — the default training-stage lighting (pauses Stage Tools live sync until you edit a light again)
- Smash shaders are perspective. Numpad ortho views are matched with a pulled-back perspective so lighting still works
- Non-Smash meshes (retarget sources, Jump Force, etc.) get a simple lit preview so they are not black silhouettes. Smash still comes from `ssbh_wgpu`
- **Relink Smash Model Folder** — older `.blend` files never stored the `.numshb` path. Point it at the original model folder so Smash Viewport can load that fighter again
- Importing an animation while in Solid shading re-enables armature deform on meshes a previous Smash Viewport session left disabled, so expression and face meshes follow the rig instead of floating at bind pose

Shipped native plugins (the addon updater installs these with the rest of the zip):

| OS | File | GPU API |
| --- | --- | --- |
| Windows | `native/bin/ssbh_blender_preview.dll` | DX12 |
| Linux | `native/bin/libssbh_blender_preview.so` | Vulkan |
| macOS | `native/bin/libssbh_blender_preview.dylib` | Metal (Intel + Apple Silicon) |

After a native plugin update, **fully quit Blender** (not F3 reload) so the OS can replace the loaded library. Build notes: [`native/README.md`](native/README.md).

## What this fork adds

These are the pieces that are **not** in the main plugin, or are substantially different here.

### Animation importer and exporter

![Ultimate tab: model and animation importer](docs/readme/ultimate-tab.png)

**Importer** (select the Smash armature or camera first):

- Imported action-name extensions are configurable in the add-on preferences: `.nuanmb` is hidden by default and `.rawanim` is shown by default

- Browse one or more `.nuanmb` files, or **Browse Animation Folder** and pick from a list
- Click, Ctrl-click, or Shift-click to select animations, with **Select All** and **Deselect All** controls
- **Import Selected Animations** / **Import All Animations**
- Import options: transform, material, visibility
- **Raw Animations** — browse a folder of `.rawanim` files, import selected or all (fighter `motion/body` folders auto-point at `rawanims` when present)
- In Armature Data → **Ultimate Visibility Track Entries**, use **Purge Current** or **Purge All Anims** to remove tracks with no corresponding model mesh and compact the shared list safely
- Visibility names are matched case-insensitively; case-only duplicates are merged using logical OR so an enabled state is not lost

**Exporter**:

- Export the current action, or batch export from a matching multi-select checkbox list. The batch button shows the selected count
- **Export Raw Animation** and **Include Raw with .NUANMB Export** live in the **Raw Animations** panel, not here
- Bone override list, populate-from-armature, **Thrown** preset
- Bones named `BL_*` are skipped on anim (and model) export so helper controls never ship in-game

### Animation Tools

![Animation rig controls on Mario](docs/readme/animation-rig.png)

- **Create Animation Rig** — control shapes on a `smush_blender_import` armature
  - Optional IK (hands, feet, poles), including extra arms such as `HandL2`
  - **Finger sliders** (thumb pad + curl/spread; extra hands get the same controls). Toggle sliders vs Smash finger circles after creation
  - **Eye look** (`BL_EyeLook` + `CustomVector31`)
  - Hide helper / swing bones, match IK to the loaded anim, clean leftover keyframes
- **Rig Extras** — once a rig exists, IK, Eyes, and Fingers each show as Added or Missing with a one-click **Add** or **Bake & Remove**, so you can build a rig without one of them and add it later
- Remove rig, or **Bake and Remove Rig** (fingers / eyes / IK)
- **Idle Pose Library** — store/apply idles, include Trans, mirrored, 180 rotate
- **User Poses** — add/remove poses, apply to selected bones or the current frame
- **IK Tools** — create arm/leg IK, bake & remove IK/FK, toggle influence, **Bulk IK** on every loaded animation
- **Mirror Animation** — space, Smash Y Anim Flip, custom extra bones, **Mirror All Loaded Animations**
- **Misc Anim Stuff** (collapsible) — Transfer Hip Animation to Trans, Reset Bone Locations, Ground Character, Invert Positive and Negative, Remove Animation from Swing Bones, **GIF or Photo** (also on the Action Editor header)
- **Animation Layers** — when the Animation Layers addon is installed, its UI is embedded in a collapsible section here, and layer handlers are paused around rig edits that would otherwise crash Blender

#### Independent IK and FK

IK and FK are separate channels rather than one influence slider. Full notes: [`docs/ik-fk-workflow.md`](docs/ik-fk-workflow.md).

- **Switch to IK** / **Switch to FK** per Arms, Legs, or Both. Each switch keys only the destination mode at the current frame, so opposite-mode keys on different frames blend between them. The same rows appear in Animation Rig and in IK Tools
- Creating limbs offers to copy the current FK animation into IK. Creation and matching add no switch keys and leave FK transform keys intact; new controls start in FK
- **Match IK to Current Animation** appears when an action is newly loaded, controls are newly created, or a limb is unmatched. Matching samples the scene frame range and does not overwrite edited controls
- **Bake & Remove IK** takes a limb selection (all present IK, legs only, arms only, arms and legs), samples the evaluated motion before removing controls, and leaves unrelated limb channels alone
- Solver animation lives on hidden, nondeforming `BL_SUB_IK_*` bones; original bones blend to them through driven constraints
- Regression suite: `blender --background --factory-startup --python-exit-code 1 --python tests/ik_regression.py`

### GIF or Photo and Animation Navigation

On the Action Editor / Dope Sheet header (and in Animation Tools):

- **Start Animation Scroll** — mouse-wheel through every loaded action; the timeline jumps to that clip's range; unanimated bones go to rest (`_RET` bones are left alone)
- **Start Sequence** — plays every compatible action once, beginning at the selected action and updating the frame range for each clip
- **Ctrl+Down / Ctrl+Up** — play the next / previous compatible action; navigation wraps at both ends
- **GIF or Photo** — copies a transparent Smash Viewport (or GPU viewport) capture to the clipboard. Photo is a still. GIF records the scene range at 30 FPS (Esc cancels). Temp files are deleted when Blender closes
- **Easy Facial Animation** appears on that header when a face library is set up

The Timeline header includes FPS preset buttons. Their four values (and button visibility) are configurable in the add-on preferences.

### Model Tools

![Model Tools and the rest of the Ultimate tab](docs/readme/model-tools.png)

- Limit Weights to 4
- Mirror Vertex Groups / Mirror Mesh as Separate Object
- Unstack UV Islands
- Convert Shape Keys to Meshes (prefix)
- Remove Selected Bones, Connect Bone Chain, Delete Unweighted Bones
- **Refresh Bone Drawing** in Armature Data > Viewport Display works around invisible bones on imported rigs without retaining any rig edits
- **Roll Value Copier** — copy bone roll from a source armature to a target (name-matched, optional selected-only)

### Armature Collection Presets

The **Armature Collection Presets** panel in the Ultimate tab saves and restores an armature's organization and viewport setup. Presets can include scene collections and object placement, materials, bone collections, bone colors/custom shapes, and armature display settings. Each section can be enabled independently.

- Choose a **Blend File**, **Global**, or **Custom** preset library. Configure the custom directory in the add-on preferences.
- Select an armature and use **Save New** to capture it. Related descendants are included by default; custom-shape objects are optional.
- Use **Preview** to inspect matches without changing the file, then **Apply** to review the same mapping and apply it with Undo support.
- Matching is exact/case-insensitive and understands Blender suffixes such as `.001`. Optional fuzzy matching defaults to an 85% threshold and requires explicit approval of the displayed mappings.
- Unmatched objects stay where they are unless **Move to Unmatched** is selected.
- Presets preserve multiple object collection memberships and nested bone collections. Applying never deletes objects or collections and does not disturb collection links belonging exclusively to other scenes.
- The list supports search, refresh, best-match selection, update, duplicate, rename, delete, multi-file import, and export.

### Easy Facial Animation

Opens from the Action Editor header, Ultimate Animation Data, or its own window.

- Vis-mesh and bone-based expression tabs
- Capture / apply expressions with thumbnails
- Save/load a JSON expression library
- Face camera create / view / remove
- Preview assigned vis tracks

### Misc.

- **Eye Look (CustomVector31)** — set up EyeL/EyeR tracks, add `BL_EyeLook`, match from material, bake, live preview (Solid Texture / Material). Look At vs offset mode, gain/sensitivity, clamp, invert, pupil size from control-bone scale. Live preview does not export; bake writes the keys export reads
- **Convert All to Principled BSDF** / **Revert to Smash Material** on the imported armature
- Smash Viewport toggle/status (same engine as Render Properties)

### Retargeting

Expy Kit lives in the Ultimate tab (always listed; most operators want Pose Mode).

- Smash auto-preset for `smush_blender_import`
- Save/load bone-map presets
- Map by proximity from a reference armature
- Bind, conversion, bake constrained actions

### Stage Tools

- Import/export stage `light.nuanmb`, viewport preview, edit intensity/color on selected lights
- **Drive Smash Viewport** (on by default) — Import Light Nuanmb also loads that file in Smash Viewport. Rotate `LightStg0` / change intensity or color and Smash Viewport updates after a short delay. **Training Lights** or **Load Stage Lights** in Render Properties pause live sync until you edit a Stage Tools light again
- Ambient SH `.shpcanim` import/export, intensity/tint, vertex-paint local ambient, bake multipliers

### Animation data and Blender 4 / 5

- Ultimate Animation Data still drives vis and material tracks
- SAP action auto-sync so vis/mat stay attached to the current action
- Action slots / fcurve helpers for Blender 4 and 5
- `ParamLabels.csv` lives outside the addon so updates do not wipe custom hashes (`%APPDATA%/Smash Ultimate Labels` on Windows)
- **Append New Hashes** adds lowercase bone names, `bonecol` collision names, and child mesh names in a responsive batch; additional CSV destinations are configurable in the add-on preferences

### Panel Presets

**Panel Presets** at the bottom of the Ultimate tab controls the whole layout of the tab: which panels are visible *and* what order they appear in.

- **All Panels**, **Animate**, and **Modeling** ship built in; add, duplicate, rename, and delete your own
- The panel list is a single checklist: tick a panel to show it, and use the up/down arrows beside the list to move it. The reset arrow restores the add-on's default order for that preset
- Each preset carries its own order, so switching presets relays out the tab
- **Save Presets** writes them to your Blender config so other `.blend` files pick them up; presets also travel inside a saved `.blend`
- Changes apply immediately. Panel Presets itself is never hidden or moved — it stays pinned at the bottom so a bad preset is always undoable

### Helper bones (`BL_*`)

Any extra control you add (IK widgets, finger sliders, eye look, custom helpers) **must** be named with the `BL_` prefix. The rig already does this. Export skips `BL_*` bones so they never end up in a `.nusktb` or `.nuanmb`.

## Shared with the main plugin

Still here, documented on the [upstream wiki](https://github.com/ssbucarlos/smash-ultimate-blender/wiki):

- Import/export of `.numdlb`, `.numshb`, `.nusktb`, `.nuhlpb`, `update.prc`, `.numeshexb`, `.adjb`, `.numatb`, `.nutexb`, `.nuanmb`, `swing.prc`
- Batch `.nuanmb` import from the file browser, plus Ctrl-click/Shift-click multi-selection in the loaded animation-folder list
- Camera `.nuanmb`
- Magic Exo Skel Maker (build, preview modifiers, un-exo, bone align, weight transfer, cleanup)
- Material Re-Importer, Attribute Renamer, swing collision editing
- Vis/mat drivers and the original eye modal
- Smash material conversion (Principled BSDF and Fortnite FPv3 `_D` / `_M` / `_S` maps)

## Installation

Tutorials for the shared import/export flow are on the [wiki](https://github.com/ssbucarlos/smash-ultimate-blender/wiki).

1. Download this fork, not the upstream zip
   - Latest: GitHub → [CrusherD2/smash-ultimate-blender](https://github.com/CrusherD2/smash-ultimate-blender) → branch **`animation-workflow`** → Code → **Download ZIP**
   - Or a specific commit from that branch
2. Blender → Edit → Preferences → Add-ons → Install From Disk → pick the zip
3. Enable **Smash Ultimate Blender Tools**
4. In the 3D Viewport, open the Sidebar (`N`) and open the **Ultimate** tab. **If the tab is missing, switch to Object or Pose mode** (Edit Mesh hides most of these panels). Model Importer/Exporter, Swing, Material Re-Importer, Attribute Renamer, and Magic Exo Skel Maker are available in Pose mode too, not just Object mode
5. If a panel you expect is missing while the tab is visible, check the active preset in **Panel Presets** at the bottom of the tab

## System requirements

64-bit **Blender 4.4+** and **Blender 5.x** on Windows, Linux, and macOS (including Apple Silicon). Smash Viewport needs a working DX12 / Vulkan / Metal GPU stack on those platforms.

If Blender runs but the addon will not enable, open an issue on [this fork](https://github.com/CrusherD2/smash-ultimate-blender/issues).

## Updating

### Auto-updater

This fork watches the **`animation-workflow`** branch (not GitHub Releases). When a new commit lands, **Update Available!** appears in the Ultimate tab with the changelog and **Download & Install Update**. It backs up the current install and restarts Blender.

If your installed version is newer than the one published on that branch — a local build with unreleased work, for instance — the panel stays hidden rather than offering an update that would replace newer code with older. Equal versions still update on new commits, and if the branch cannot be reached the usual commit check applies.

Smash Viewport binaries ship in that zip. After an update that changes them, quit Blender completely once so the new `.dll` / `.so` / `.dylib` can load.

### Manual update

Disable the addon, restart Blender, Remove, then install the new zip.

## Uninstalling

Disable the addon, restart Blender, then Remove.

## In case of problems

1. Export issues: [wiki: export issues](https://github.com/ssbucarlos/smash-ultimate-blender/wiki/Read-this-if-you-have-export-issues.-Or-want-to-avoid-Export-Issues)
2. [Known issues](https://github.com/ssbucarlos/smash-ultimate-blender/wiki/Known-Blender-Issues)
3. Fork-specific bugs: [CrusherD2/smash-ultimate-blender issues](https://github.com/CrusherD2/smash-ultimate-blender/issues)

## Useful tools

- SSBH Editor — https://github.com/ScanMountGoat/ssbh_editor
- Ultimate Tex — https://github.com/ScanMountGoat/ultimate_tex
- Switch Toolbox — https://github.com/KillzXGaming/Switch-Toolbox

## Special thanks

- SMG for [ssbh_data_py](https://github.com/ScanMountGoat/ssbh_data_py), SSBH Editor / `ssbh_wgpu`, and CrossMod shader reference
- [ssbucarlos/smash-ultimate-blender](https://github.com/ssbucarlos/smash-ultimate-blender) for the original addon
- The Rokoko plugin for UI patterns used in the exo panels
- Expy Kit, which the Retargeting tab embeds
