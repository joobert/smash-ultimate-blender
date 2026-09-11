# Attribute Renamer

Open **3D Viewport → Sidebar (N) → Ultimate → Attribute Renamer** in Object
or Pose Mode. These three operations have different scopes. Save your file
before bulk renaming; each operation supports Blender Undo.

## Rename Mesh Attributes

Select the mesh objects to repair, then click **Rename Mesh Attributes**.
For each selected mesh, an unrecognized first UV layer becomes `map1` and an
unrecognized first color attribute becomes `colorSet1`. Already recognized
Smash names are retained. UV layers beginning with `.` or `_sub_eye` are skipped.

The tool recognizes UV names `map1`, `bake1`, `uvSet`, `uvSet1`, and `uvSet2`.
Recognized color names are `colorSet1`, `colorSet2`, `colorSet2_1`,
`colorSet2_2`, `colorSet2_3`, and `colorSet3` through `colorSet7`.

It cannot infer the purpose of additional layers. A warning about attributes
after the first means you must inspect the material's shader requirements and
rename those layers manually in Object Data Properties. This operation changes
names only: it does not create missing UVs/colors, change their values, or
guarantee that the chosen shader has all its required attributes. Run Export
Doctor afterward. Objects sharing a mesh datablock share these name changes.

## Rename Materials to Mesh

This processes **all mesh objects in the blend file**, regardless of selection.
A single-slot material receives the object's name; multiple slots use
`<object name>_mat1`, `_mat2`, and so on. The substrings `Pikachu_` and
`_VIS_O_OBJShape` are removed from the resulting name.

The operation renames existing material datablocks; it does not duplicate them.
If several objects share a material, later objects can rename the same material
again. Blender may add numeric suffixes to keep datablock names unique. Check
the result before relying on material labels for re-import or export.

## Rename Textures to Material

This processes **all materials in the blend file** with node trees and renames
images referenced by their top-level Image Texture nodes to the material name.
Shared images can therefore be renamed more than once. Blender can add numeric
suffixes when names collide.

Only the image datablock name changes. The command does not rename image files
on disk, change image paths, or write exported textures. Inspect shared images
and export names before saving the result.
