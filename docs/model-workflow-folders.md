# Model workflow folders

In Blender Preferences, expand Smash Ultimate Blender Tools and find **Model Workflow Folders**.

- **Default Vanilla .nusktb Folder** sets the initial folder for the vanilla skeleton selector when no reference is selected. An existing selected reference takes priority.
- **Default Model Export Folder** sets the fallback destination for the model export browser.
- **Add Model Folder** adds a source-folder/export-folder pair. The source folder is filled from the selected export armature when available. Set it to the folder from which the model was imported (for example, its `model/body/c00` folder), then choose the corresponding export folder.

Model-specific destinations take priority over the global default. Matching uses the source folder recorded on the imported armature or its data, so renaming the armature does not break the mapping. Blank destinations fall back to the global default. The export browser opens at the resolved destination and still lets you choose another folder before exporting. Save Preferences if Blender's automatic preference saving is disabled.

The Idle Pose Library lists predefined poses available in the active animation folder. Changing that folder refreshes the entries immediately. Applying a predefined pose reads its first frame from that folder each time; it does not reuse data cached from a previous model or folder. Explicitly stored custom poses remain available across folder changes.

Validation: run `blender --background --factory-startup --python-exit-code 1 --python tests/workflow_settings.py` for Preferences registration, folder matching/fallback, idle folder changes, stepped stretch playback, and arms/legs/both IK creation. The IK creation fixture uses the real solver and matching code with isolated UI/state helpers. Run `tests/ik_stretch.py` with the same Blender flags for endpoint solver regression coverage.
