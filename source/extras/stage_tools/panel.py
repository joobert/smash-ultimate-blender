import bpy
from bpy.types import Panel

from .battlefield_ref import COLLECTION_NAME as BF_COLLECTION_NAME
from .light_nuanmb import find_stage_light_objects
from .shpcanim import find_shpc_mesh, find_shpc_root


class SUB_PT_stage_tools(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Ultimate"
    bl_label = "Stage Tools"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 90

    @classmethod
    def poll(cls, context):
        if context.mode not in {"OBJECT", "POSE", "PAINT_VERTEX", "EDIT_MESH"}:
            return False
        return getattr(context.scene, "sub_scene_properties", None) is not None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        ssp = getattr(context.scene, "sub_scene_properties", None)
        if ssp is None:
            return

        box = layout.box()
        header = box.row()
        header.prop(
            ssp,
            "stage_light_expanded",
            icon="TRIA_DOWN" if ssp.stage_light_expanded else "TRIA_RIGHT",
            icon_only=True,
            emboss=False,
        )
        header.label(text="Stage Lighting (.nuanmb)")
        if ssp.stage_light_expanded:
            box.operator("sub.import_stage_light", icon="IMPORT")
            box.operator("sub.export_stage_light", icon="EXPORT")
            lights = find_stage_light_objects(context)
            if lights:
                box.label(text=f"{len(lights)} lighting nodes in the scene")
                box.prop(ssp, "stage_light_preview", text="Viewport Preview")
                box.prop(ssp, "stage_light_apply_ambient")
                box.prop(ssp, "stage_light_drive_smash_viewport")
                if ssp.stage_light_drive_smash_viewport:
                    box.label(text="Rotate / tint lights to update Smash Viewport.")
                active = context.view_layer.objects.active
                if active is not None and active.get("sub_stage_light_node"):
                    box.label(text=f"Selected: {active.get('sub_stage_light_node')}")
                    if active.type == "LIGHT":
                        col = box.column(align=True)
                        col.prop(active.data, "energy", text="Intensity (CustomFloat0)")
                        col.prop(active.data, "color", text="Color (CustomVector0)")
                    if active.get("sub_stage_light_kind") == "SceneAttributes":
                        col = box.column(align=True)
                        for key in sorted(active.keys()):
                            if str(key).startswith("Custom"):
                                col.prop(active, f'["{key}"]', text=str(key))
            else:
                box.label(text="Import a stage light.nuanmb to edit lights.")
            box.label(text="SSBH uses one light per mesh, not every LightStg.")
            box.label(text="Stages are mostly baked maps + SH ambient.")
            box.label(text="Rotate SUN lights to change direction.")
            box.label(text="Most lights sit at the origin; only rotation matters.")
            box.label(text="LightStg0 rotation also drives fighter shadows.")

        box = layout.box()
        header = box.row()
        header.prop(
            ssp,
            "stage_shpc_expanded",
            icon="TRIA_DOWN" if ssp.stage_shpc_expanded else "TRIA_RIGHT",
            icon_only=True,
            emboss=False,
        )
        header.label(text="Ambient SH (.shpcanim)")
        if ssp.stage_shpc_expanded:
            box.operator("sub.import_shpcanim", icon="IMPORT")
            box.operator("sub.export_shpcanim", icon="EXPORT")
            root = find_shpc_root(context)
            if root is None:
                box.label(text="Import a .shpcanim to edit ambient lighting.")
            else:
                box.label(text=f"Active: {root.name}")
                if hasattr(root, "sub_shpc"):
                    col = box.column(align=True)
                    col.prop(root.sub_shpc, "intensity")
                    col.prop(root.sub_shpc, "tint")
                    col.prop(root.sub_shpc, "use_vertex_colors")
                    col.prop(root.sub_shpc, "sync_scene_frame")
                row = box.row(align=True)
                row.operator("sub.refresh_shpc_preview")
                row.operator("sub.bake_shpc_multipliers")
                mesh = find_shpc_mesh(root)
                if mesh is not None:
                    box.label(text="Vertex-paint Col to edit local ambient.")
                    box.label(text="Enable Export Painted Ambient before export.")
            box.label(text="Intensity and tint affect the whole grid.")

        box = layout.box()
        header = box.row()
        header.prop(
            ssp,
            "stage_bf_ref_expanded",
            icon="TRIA_DOWN" if ssp.stage_bf_ref_expanded else "TRIA_RIGHT",
            icon_only=True,
            emboss=False,
        )
        header.label(text="Battlefield Reference")
        if ssp.stage_bf_ref_expanded:
            box.operator("sub.import_battlefield_reference", icon="IMPORT")
            existing = [
                collection
                for collection in bpy.data.collections
                if collection.name.startswith(BF_COLLECTION_NAME)
            ]
            if existing:
                box.label(text=f"{len(existing)} reference stage(s) in the scene")
            box.label(text="Enter the scale your fighter is set to in code.")
            box.label(text="The stage is scaled by the inverse of it.")
            box.label(text="Reference geometry only, do not export it.")
