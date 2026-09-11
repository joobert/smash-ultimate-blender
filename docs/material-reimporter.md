# Material Re-Importer

Open **3D Viewport → Sidebar (N) → Ultimate → Material Re-Importer**.
Choose the destination in **Select an Armature**. This panel can reload
material definitions from disk or reuse materials from another armature.
Save a copy before replacing edited material assignments.

## Reload from a model folder

1. Choose **Select folder w/ .NUMATB & textures** and select the folder holding
   the material definitions and their textures.
2. Check the displayed `.numatb` filename. If there is one candidate it is used;
   with several candidates, `model.numatb` is preferred. Otherwise the selected
   candidate is not guaranteed, so use **Re-select .numatb** to choose explicitly.
3. Click **Re-Import materials**. The importer builds materials from that file
   and replaces matching material slots on mesh objects directly parented to
   the chosen armature.
4. Inspect the meshes and material values before exporting the model.

Matching uses existing material names after the model exporter's name trimming,
against labels in the `.numatb`. Unmatched slots keep their assignments. If
existing names become duplicates after trimming, the operation warns and stops;
resolve those ambiguous names before retrying. A folder with no `.numatb`
shows an error: choose a valid file before re-importing. Ensure target slots
contain materials before running the operation.

This replaces matching assignments with imported materials; it does not merge
your node edits into the definitions on disk. It does not import mesh geometry
or write a model export.

## Copy from another armature

1. Keep the destination in **Select an Armature** and choose the source under
   **Copy materials from different armature**.
2. Click **Copy Materials**. Mesh names are matched using the model exporter's
   name trimming. Ambiguous source names and meshes with no usable source
   materials are skipped with a warning.
3. Check multi-material meshes. Valid destination face material indices are
   preserved; indices beyond the new slot count become zero.

The copied slots reference the source's material datablocks, so subsequent
material edits affect both users. A destination mesh datablock shared with
other objects is copied before its slots change, protecting other objects'
assignments. Copying supports Undo. No disk folder is needed for this workflow.
