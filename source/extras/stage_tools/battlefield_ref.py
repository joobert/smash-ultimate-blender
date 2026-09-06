"""Import a Battlefield stage mesh as a size reference for fighter work.

The bundled mesh is in the same coordinate space as an imported fighter: Z up,
stage floor at Z 0, roughly 120 units across the main platform. It is turned to
face -X so it lines up with the direction Smash's camera looks from. Dropping it
into a scene at scale 1.0 therefore shows a fighter at its true in-game size
relative to the stage - as long as the fighter model in the scene is authored at
the same scale it is drawn at in game.

Fighters that are scaled in fighter source code break that assumption. A fighter
whose code multiplies its model scale by 0.5 is authored at twice the size it
appears in game, so the reference has to grow by the same factor to compensate.
The importer takes that fighter scale and applies its reciprocal to the stage.
"""

import math
import os

import bpy
from bpy.props import FloatProperty
from bpy.types import Operator


COLLECTION_NAME = "Battlefield Reference"
ROOT_NAME = "Battlefield Reference"

# The stage exports facing +X. Smash's camera looks down -X, so the reference is
# turned -90 degrees about the global Z axis to present its front to the camera.
ROOT_ROTATION_Z = math.radians(-90.0)

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
_GLB_PATH = os.path.join(_ASSET_DIR, "BF.glb")
_DAE_PATH = os.path.join(_ASSET_DIR, "BF.dae")


def stage_scale_for(fighter_scale: float) -> float:
    """Scale to apply to the stage so a fighter drawn at `fighter_scale` matches."""
    if fighter_scale <= 0.0:
        raise ValueError("Fighter scale must be greater than 0")
    return 1.0 / fighter_scale


def _import_reference_mesh() -> list[bpy.types.Object]:
    """Import the bundled stage and return the objects it created.

    Prefers the .glb because Collada import is legacy and absent from newer
    Blender builds. The .dae is the original export and stays in the repo as the
    source of truth, so it is also usable as a fallback where Collada exists.
    """
    before = set(bpy.data.objects)

    if os.path.exists(_GLB_PATH):
        bpy.ops.import_scene.gltf(filepath=_GLB_PATH)
    elif os.path.exists(_DAE_PATH) and hasattr(bpy.ops.wm, "collada_import"):
        bpy.ops.wm.collada_import(filepath=_DAE_PATH)
    elif os.path.exists(_DAE_PATH):
        raise RuntimeError(
            "Only BF.dae is bundled and this Blender build has no Collada "
            "importer. Re-add BF.glb to the addon's stage_tools/assets folder."
        )
    else:
        raise RuntimeError(f"No Battlefield reference mesh found in {_ASSET_DIR}")

    return [obj for obj in bpy.data.objects if obj not in before]


def import_battlefield_reference(context: bpy.types.Context, fighter_scale: float):
    """Import the stage into its own collection, inversely scaled to the fighter.

    Returns (root empty, imported objects).
    """
    scale = stage_scale_for(fighter_scale)
    imported = _import_reference_mesh()

    collection = bpy.data.collections.new(COLLECTION_NAME)
    context.scene.collection.children.link(collection)

    root = bpy.data.objects.new(ROOT_NAME, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 10.0
    root.rotation_euler = (0.0, 0.0, ROOT_ROTATION_Z)
    root.scale = (scale, scale, scale)
    root["sub_battlefield_fighter_scale"] = fighter_scale
    collection.objects.link(root)

    for obj in imported:
        for existing in list(obj.users_collection):
            existing.objects.unlink(obj)
        collection.objects.link(obj)
        if obj.parent is None:
            obj.parent = root
            obj.matrix_parent_inverse.identity()

    return root, imported


class SUB_OP_import_battlefield_reference(Operator):
    bl_idname = "sub.import_battlefield_reference"
    bl_label = "Import Battlefield Reference"
    bl_description = (
        "Import a Battlefield stage mesh for size reference, scaled so the "
        "fighter in this scene matches its in-game size relative to the stage"
    )
    bl_options = {"REGISTER", "UNDO"}

    fighter_scale: FloatProperty(
        name="Fighter Scale",
        description=(
            "Scale your fighter is given in its source code. The stage is "
            "imported at the reciprocal of this so the fighter reads at its "
            "in-game size"
        ),
        default=1.00,
        min=0.01,
        max=5.00,
        soft_min=0.01,
        soft_max=5.00,
        precision=2,
        step=1,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "fighter_scale")
        layout.label(text=f"Stage imported at x{stage_scale_for(self.fighter_scale):.3f}")
        layout.label(text="Use 1.00 if your fighter is not scaled via code.")

    def execute(self, context):
        try:
            root, imported = import_battlefield_reference(context, self.fighter_scale)
        except Exception as error:  # noqa: BLE001 - surfaced to the user
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Imported {len(imported)} Battlefield objects at "
            f"x{stage_scale_for(self.fighter_scale):.3f} into '{root.users_collection[0].name}'",
        )
        return {"FINISHED"}


classes = (SUB_OP_import_battlefield_reference,)
