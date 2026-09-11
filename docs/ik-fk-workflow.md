# Independent IK and FK

Create legs, arms, or both from IK Tools or Animation Rig. Creation matches the current FK pose and inserts an IK mode key at the current frame for the created limbs. The controls are ready to use immediately. Accept the optional match dialog to copy the full FK animation to IK. FK transform keys are preserved.

Switch to FK or Switch to IK keys only that destination at the current frame, for the selected limbs. Place opposite mode keys on different frames to blend between them. Switching does not rematch or overwrite edited controls.

IK Stretch Arms / IK Stretch Legs in the IK controls is off by default. Hands and feet retain their solved position when a target is out of reach. Enable it to let the endpoint follow the target's position. Clicking a stretch button inserts or updates a stepped state key in the same SAP action as the FK/IK switches, even with Auto Key disabled. Scrubbing restores that state. Newly created target and pole controls have no parent; internal solver bones retain their chain hierarchy. Existing independent rigs have their endpoint constraints repaired when the addon loads, without rematching animation.

Enable **Stretch Chain** beside an IK Stretch control to distribute the target's positional offset across the full parent chain without adding scale. The root remains anchored and the displacement increases toward the endpoint. With arm chain stretch enabled, each ArmIK pole has its own keyframable **Pull** influence: zero retains normal pole-only behavior and one pulls that chain's bend bone fully toward its ArmIK control. Left, right, extra, and custom arm chains are independent.

Under **Create IK Controls**, expand **Custom IK Bones**. Choose a setup name, root, bend bone, endpoint, and Arms/Legs switch group. The bones must lie on one parent path and cannot overlap another IK setup. Custom chains participate in matching, switching, and Bake & Remove IK. Presets are stored as JSON in Blender's version-specific user scripts directory under `presets/smash_custom_ik`.

Matching samples the scene frame range. A newly loaded action, newly created controls, or an unmatched limb makes the match button appear. Matching only one frame does not mark the entire animation matched. Rematch after changing the source FK animation if you want those changes copied into IK.

Position IK Controls evaluates independent limbs together to reduce scene updates, including when Entire Animation is enabled. Solver iterations, pole fitting precision, and keyframe sampling are unchanged. Rigs with external or cross-limb dependencies, unsupported drivers, or animated constraint settings use sequential matching. Interactive pose-tool refreshes are deferred until matching finishes.

Bake & Remove IK allows selecting legs, arms, or all present IK. It samples the evaluated motion before removing those controls and preserves unrelated limb channels.

## Existing scenes

Restart Blender after updating the addon. Use Match IK to Current Animation for existing controls to migrate them to independent solver chains. Keep a saved copy of the scene before migrating. The original FK keys remain intact; migration replaces the old constraints on the selected limb chains.

## Implementation and checks

Hidden, nondeforming `BL_SUB_IK_` bones own the independent solver animation. Position-only `BL_SUB_IK_PULL_` outputs provide chain stretch and per-chain ArmIK pull without scaling the solver. Original bones blend to these generated outputs through driven constraints. Matching stores solver seed transforms, end targets, poles, and pole angles without changing the FK source curves. Pole fitting considers both bone orientations and stabilizes near-straight chains.

Run `blender --background --factory-startup --python-exit-code 1 --python tests/ik_regression.py`. An optional `-- path/to/character.blend` loads only an armature datablock for the stress test; it never saves the fixture.

Tests cover each creation path and limb selection, integer-frame pose accuracy, independence in both directions, explicit switch keys, interpolation, action match tracking, and scoped baking/removal. Stress tests cover straight chains and large twists. Subframe matching and arbitrary third-party constraints are not covered by these tests.
