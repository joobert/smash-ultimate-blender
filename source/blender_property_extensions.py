import bpy
import re
from bpy.types import Scene, Object, Armature, PropertyGroup, Camera, Material, Bone, Mesh, Collection
from bpy.props import IntProperty, StringProperty, EnumProperty, BoolProperty, FloatProperty, CollectionProperty, PointerProperty
from bpy.props import FloatVectorProperty

from .exo import magic_exo_skel

from .model import export_model

from .anim import anim_data, import_anim, export_anim
from .swing import sub_swing_data
from .model.material import sub_matl_data
from .model.skel import helper_bone_data


def _update_idle_pose_folder(self, context):
    from .extras.idle_pose_library import refresh_idle_poses
    refresh_idle_poses(self)


def _update_smash_viewport(self, context):
    try:
        from .extras.smash_viewport import update_smash_viewport
        update_smash_viewport(self, context)
    except Exception:
        pass


def _update_smash_vp_background(self, context):
    try:
        from .extras.smash_viewport import update_smash_vp_background
        update_smash_vp_background(self, context)
    except Exception:
        pass


def _update_smash_vp_lighting(self, context):
    try:
        from .extras.smash_viewport import update_smash_vp_lighting
        update_smash_vp_lighting(self, context)
    except Exception:
        pass


def _update_sap_auto_sync_enabled(self, context):
    try:
        from .anim.anim_data import set_sap_auto_sync_enabled
        set_sap_auto_sync_enabled(bool(self.sap_auto_sync_enabled))
    except Exception:
        pass


def poll_armature_object(_self, obj):
    return obj is not None and getattr(obj, 'type', None) == 'ARMATURE'


def _update_eye_look_live_preview(self, context):
    try:
        from .extras.eye_rig import start_eye_preview, stop_eye_preview
        scene = getattr(context, 'scene', None)
        if self.eye_look_live_preview:
            start_eye_preview(scene)
        else:
            stop_eye_preview(scene)
    except Exception:
        pass


def register():
    from .extras.face_picker import SUB_PG_face_picker_data

    Armature.sub_anim_properties = PointerProperty(
        type=anim_data.SUB_PG_sub_anim_data
    )
    Armature.sub_face_picker = PointerProperty(
        type=SUB_PG_face_picker_data
    )
    Scene.sub_scene_properties = PointerProperty(
        type=SubSceneProperties
    )
    Armature.sub_helper_bone_data = PointerProperty(
        type=helper_bone_data.SubHelperBoneData
    )
    Material.sub_matl_data = PointerProperty(
        type=sub_matl_data.SUB_PG_sub_matl_data
    )
    Armature.sub_swing_data = PointerProperty(
        type=sub_swing_data.SUB_PG_sub_swing_data
    )
    Bone.sub_swing_blender_bone_data = PointerProperty(
        type=sub_swing_data.SUB_PG_blender_bone_data
    )
    Mesh.sub_swing_data_linked_mesh = PointerProperty(
        type=sub_swing_data.SUB_PG_sub_swing_data_linked_mesh
    )
    Collection.sub_swing_collection_props = PointerProperty(
        type=sub_swing_data.SUB_PG_sub_swing_master_collection_props
    )
    Object.sub_shpc = PointerProperty(
        type=SUB_PG_shpc_settings
    )

class ModelImportFile(PropertyGroup):
    name: StringProperty()

class ModelImportAlt(PropertyGroup):
    name: StringProperty()
    path: StringProperty()

class ModelImportItem(PropertyGroup):
    name: StringProperty()
    path: StringProperty()
    fallback_path: StringProperty()
    files: CollectionProperty(type=ModelImportFile)
    alts: CollectionProperty(type=ModelImportAlt)

class AnimationImportFolder(PropertyGroup):
    path: StringProperty(subtype="DIR_PATH")


class AnimationImportFile(PropertyGroup):
    name: StringProperty()
    path: StringProperty()
    selected: BoolProperty(
        name="Selected",
        description="Include this animation when importing selected animations",
        default=False,
    )

class IdlePoseItem(PropertyGroup):
    is_custom: BoolProperty(default=False)
    name: StringProperty(
        name="Pose Name",
        description="Name of the idle pose",
        default=""
    )
    data: StringProperty(
        name="Pose Data", 
        description="JSON string containing the pose data",
        default=""
    )
    
bpy.utils.register_class(ModelImportFile)
bpy.utils.register_class(ModelImportAlt)
bpy.utils.register_class(ModelImportItem)
bpy.utils.register_class(AnimationImportFolder)
bpy.utils.register_class(AnimationImportFile)

class MirrorCustomBoneItem(PropertyGroup):
    name: StringProperty(
        name="Bone Name",
        description="Custom bone that is not part of a normal Smash Ultimate armature",
        default=""
    )
    include: BoolProperty(
        name="Mirror",
        description="Mirror this custom bone",
        default=False
    )


def _update_shpc_preview(self, context):
    obj = getattr(self, "id_data", None)
    if obj is None or not obj.get("sub_shpc_root"):
        return
    from .extras.stage_tools.shpcanim import refresh_shpc_preview_object
    refresh_shpc_preview_object(obj, context.scene.frame_current)


def _update_stage_light_preview(self, context):
    from .extras.stage_tools.light_nuanmb import apply_stage_light_preview
    apply_stage_light_preview(context)


class SUB_PG_shpc_settings(PropertyGroup):
    intensity: FloatProperty(
        name="Intensity",
        description="Scale every SH coefficient on preview and export",
        default=1.0,
        min=0.0,
        soft_max=8.0,
        max=50.0,
        update=_update_shpc_preview,
    )
    tint: FloatVectorProperty(
        name="Tint",
        description="Tint the ambient grid RGB channels",
        subtype="COLOR",
        size=3,
        min=0.0,
        soft_max=2.0,
        max=8.0,
        default=(1.0, 1.0, 1.0),
        update=_update_shpc_preview,
    )
    use_vertex_colors: BoolProperty(
        name="Export Painted Ambient",
        description="Replace L0 ambient from the Col vertex colors on export",
        default=False,
    )
    sync_scene_frame: BoolProperty(
        name="Sync to Scene Frame",
        description="Switch SHPC keyframes when the scene frame changes",
        default=True,
        update=_update_shpc_preview,
    )


class UserPoseItem(PropertyGroup):
    name: StringProperty(
        name="Pose Name",
        description="Name of the user pose",
        default=""
    )
    data: StringProperty(
        name="Pose Data",
        description="JSON string containing the saved transforms for selected bones",
        default=""
    )


class SubSceneProperties(PropertyGroup):
    auto_import_default_eyelid: BoolProperty(
        name="Auto-import Default Eyelid",
        description="Import a00defaulteyelid.nuanmb from the model's motion folder when available",
        default=False,
    )
    model_import_folder_path: StringProperty(
        name="Model Import Folder Path",
        description="Path to the folder containing the model files",
        default=""
    )
    last_model_folder: StringProperty(
        name="Last Model Folder",
        description="Path to the last folder used for model import",
        default=""
    )
    model_import_numdlb_file_name: StringProperty(
        name="NUMDLB File Name",
        description="Name of the NUMDLB file",
        default=""
    )
    model_import_numshb_file_name: StringProperty(
        name="NUMSHB File Name",
        description="Name of the NUMSHB file",
        default=""
    )
    model_import_nusktb_file_name: StringProperty(
        name="NUSKTB File Name",
        description="Name of the NUSKTB file",
        default=""
    )
    model_import_numatb_file_name: StringProperty(
        name="NUMATB File Name",
        description="Name of the NUMATB file",
        default=""
    )
    model_import_nuhlpb_file_name: StringProperty(
        name="NUHLPB File Name",
        description="Name of the NUHLPB file",
        default=""
    )
    model_import_models: CollectionProperty(
        name="Model Import Models",
        description="List of found models",
        type=ModelImportItem
    )
    model_import_models_index: IntProperty(
        name="Model Import Models Index",
        default=0
    )
    model_export_arma: PointerProperty(
        name='Armature',
        description='Select the Armature',
        type=Object,
        poll=magic_exo_skel.poll_armatures,
        update=export_model.model_export_arma_update,
    )
    model_export_show_all_new_bones: BoolProperty(
        name='Show All',
        description='True if more than the first 5 bones should be displayed',
        default=False,
    )
    model_export_show_all_missing_bones: BoolProperty(
        name='Show All',
        description='True if more than the first 5 bones should be displayed',
        default=False,
    )
    last_swing_directory: StringProperty(
        name="Last Swing Directory",
        description="Path to the last directory used for importing or exporting swing files",
        default="",
        subtype='DIR_PATH'
    )
    vanilla_nusktb: StringProperty(
        name='Vanilla .NUSKTB file path',
        description='The path to the vanilla nusktb file',
        default='',
    )
    vanilla_update_prc: StringProperty(
        name='Vanilla update.prc file path',
        description='The path to the vanilla update.prc file',
        default='',
    )
    smash_armature: PointerProperty(
        name='Smash Armature',
        description='Select the Smash armature',
        type=Object,
        poll=magic_exo_skel.poll_armatures,
    )
    other_armature: PointerProperty(
        name='Other Armature',
        description='Select the Other armature',
        type=Object,
        poll=magic_exo_skel.poll_other_armatures,
    )
    bone_list: CollectionProperty(
        type=magic_exo_skel.BoneListItem
    )
    saved_bone_list: CollectionProperty(
        type=magic_exo_skel.BoneListItem
    )
    bone_list_index: IntProperty(
        name="Index for the exo bone list",
        default=0
    )
    pairable_bone_list: CollectionProperty(
        type=magic_exo_skel.PairableBoneListItem
    )
    armature_prefix: StringProperty(
        name="Prefix",
        description="The Prefix that will be added to the bones in the 'Other' armature. Must begin with H_ or else it wont work!",
        default="H_Exo_"
    )
    material_reimport_arma: PointerProperty(
        name='Armature',
        description='Select the Armature',
        type=Object,
        poll=magic_exo_skel.poll_armatures,  
    )
    material_reimport_folder: StringProperty(
        name='Material Reimport folder',
        description='The folder w/ .numatb & textures',
        default='',
    )
    material_reimport_numatb_path: StringProperty(
        name='Material Reimport .numatb',
        description='The selected .numatb',
        default='',
    )
    material_reimport_copy_source_arma: PointerProperty(
        name='Source Armature',
        description='Armature to copy materials from (meshes matched by name)',
        type=Object,
        poll=magic_exo_skel.poll_material_copy_source_armatures,
    )
    last_anim_import_dir: StringProperty(
        subtype="DIR_PATH",
        default=""
    )
    last_anim_export_dir: StringProperty(
        subtype="DIR_PATH",
        default=""
    )
    last_imported_model_path: StringProperty(
        name="Last Imported Model Path",
        description="Path to the last imported model",
        default=""
    )
    animation_import_folders: CollectionProperty(type=AnimationImportFolder)

    animation_import_folder_path: StringProperty(
        name="Animation Import Folder Path",
        description="Path to the folder containing animation files related to imported model",
        default="",
        update=_update_idle_pose_folder,
    )
    animation_import_files: CollectionProperty(
        name="Animation Import Files",
        description="List of found animations for imported model",
        type=AnimationImportFile
    )
    animation_import_selection_anchor: StringProperty(options={'HIDDEN'})
    action_export_selection_anchor: StringProperty(options={'HIDDEN'})
    eye_look_expanded: BoolProperty(name="Eye Look", default=False)
    smash_viewport_expanded: BoolProperty(name="Smash Viewport", default=False)
    animation_import_files_index: IntProperty(
        name="Animation Import Files Index",
        default=0
    )
    raw_animation_import_folder_path: StringProperty(
        name="Raw Animation Import Folder Path",
        description="Path to the folder containing .rawanim files",
        default=""
    )
    raw_animation_import_files: CollectionProperty(
        name="Raw Animation Import Files",
        description="List of found raw animation files",
        type=AnimationImportFile
    )
    raw_animation_import_files_index: IntProperty(
        name="Raw Animation Import Files Index",
        default=0
    )
    raw_animations_expanded: BoolProperty(
        name="Raw Animations Expanded",
        description="Whether the Raw Animations section is expanded",
        default=True
    )
    action_export_list: CollectionProperty(
        name="Action Export List",
        description="List of actions for batch export",
        type=export_anim.SUB_PG_anim_action_item
    )
    action_export_list_index: IntProperty(
        name="Action Export List Index",
        default=0
    )
    
    # Idle Pose Library Properties
    idle_pose_list: CollectionProperty(
        name="Idle Pose List",
        description="List of stored idle poses",
        type=IdlePoseItem
    )
    idle_pose_list_index: IntProperty(
        name="Idle Pose List Index",
        default=0
    )
    clean_keyframes_after_rig: BoolProperty(
        name="Clean keyframes after creation",
        description="After creating the animation rig, remove keys that sit on baked interpolation so only the keys that change the pose remain",
        default=True,
    )
    idle_pose_include_trans: BoolProperty(
        name="Include Trans Bone",
        description="Include the Trans bone when applying idle pose",
        default=True
    )
    idle_pose_mirrored: BoolProperty(
        name="Mirrored",
        description="Mirror the pose using Y axis",
        default=False
    )
    idle_pose_180_rotate: BoolProperty(
        name="180 Rotate",
        description="Rotate hip bone 180 degrees on Y-axis after mirroring",
        default=False
    )
    idle_pose_library_expanded: BoolProperty(
        name="Idle Pose Library Expanded",
        description="Whether the Idle Pose Library section is expanded",
        default=False
    )
    user_poses_expanded: BoolProperty(
        name="User Poses Expanded",
        description="Whether the User Poses section is expanded",
        default=False
    )
    ik_tools_expanded: BoolProperty(
        name="IK Tools Expanded",
        description="Whether the IK Tools section is expanded",
        default=False
    )
    anim_layers_panel_expanded: BoolProperty(
        name="Animation Layers Expanded",
        description="Whether the Animation Layers section in Ultimate Anim Tools is expanded",
        default=True
    )
    misc_anim_stuff_expanded: BoolProperty(
        name="Misc Anim Stuff Expanded",
        description="Whether the Misc Anim Stuff section is expanded",
        default=False
    )
    bulk_ik_expanded: BoolProperty(
        name="Bulk IK Expanded",
        description="Whether the Bulk IK section is expanded",
        default=False
    )
    bulk_ik_leg_l: StringProperty(name="Leg L", default="LegL")
    bulk_ik_knee_l: StringProperty(name="Knee L", default="KneeL")
    bulk_ik_foot_l: StringProperty(name="Foot L", default="FootL")
    bulk_ik_leg_r: StringProperty(name="Leg R", default="LegR")
    bulk_ik_knee_r: StringProperty(name="Knee R", default="KneeR")
    bulk_ik_foot_r: StringProperty(name="Foot R", default="FootR")
    related_animations_expanded: BoolProperty(
        name="Related Animations Expanded",
        description="Whether the Related Animations section is expanded",
        default=False
    )
    batch_export_actions_expanded: BoolProperty(
        name="Batch Export Actions Expanded",
        description="Whether the Batch Export Actions section is expanded",
        default=False
    )
    anim_include_raw_animation: BoolProperty(
        name="Include Raw Animation",
        description="Also export a sparse .rawanim file alongside the .nuanmb export",
        default=False,
    )
    sap_auto_sync_enabled: BoolProperty(
        name="SAP Auto-Sync",
        description=(
            "Automatically keep visibility/material SAP actions matched to the bone action. "
            "Turn off while using Rendered viewport shading if sampling restarts endlessly"
        ),
        default=True,
        update=_update_sap_auto_sync_enabled,
    )
    import_options_expanded: BoolProperty(
        name="Import Options Expanded",
        description="Whether the Import Options section is expanded",
        default=False
    )
    model_tools_expanded: BoolProperty(
        name="Model Tools Expanded",
        description="Whether the Model Tools section is expanded",
        default=False
    )
    roll_copy_source: PointerProperty(
        name="Source",
        description="Armature to copy roll values from",
        type=Object,
        poll=poll_armature_object,
    )
    roll_copy_target: PointerProperty(
        name="Target",
        description="Armature whose matching bone rolls will be changed",
        type=Object,
        poll=poll_armature_object,
    )
    roll_copy_selected_only: BoolProperty(
        name="Selected Target Bones Only",
        description="Only copy rolls for bones currently selected on the target armature",
        default=False,
    )
    mirror_animation_expanded: BoolProperty(
        name="Mirror Animation Expanded",
        description="Whether the Mirror Animation section is expanded",
        default=False
    )
    mirror_space: EnumProperty(
        name="Mirror Space",
        description="Coordinate space to use for mirroring",
        default='LOCAL',
        items = (
            ('LOCAL', 'Local', "Mirror using local/bone coordinate space"),
            ('GLOBAL', 'Global', "Mirror using world/global coordinate space"),
        )
    )
    mirror_custom_bones: CollectionProperty(
        name="Custom Mirror Bones",
        description="Extra bones that are not part of a normal Smash armature",
        type=MirrorCustomBoneItem
    )
    mirror_custom_bones_index: IntProperty(
        name="Custom Mirror Bones Index",
        default=0
    )
    mirror_custom_armature_name: StringProperty(
        name="Custom Mirror List Source Armature",
        description="Internal: armature the custom bone list was last scanned from",
        default=""
    )
    mirror_include_fingers: BoolProperty(
        name="Include Fingers",
        description="Include finger bones (FingerL11, FingerR23, etc.) in the mirroring process",
        default=True
    )
    mirror_smash_y_anim_flip: BoolProperty(
        name="Smash Y Anim Flip",
        description="Use Studio SB anim_flip for Y-axis mirroring (best for imported nuanmb animations on standard Smash rigs). Off uses fcurve mirroring like X/Z",
        default=False,
    )
    anim_include_transform: BoolProperty(
        name="Include Transform",
        description="Include Transform Track",
        default=True
    )
    anim_include_material: BoolProperty(
        name="Include Material",
        description="Include Material Track",
        default=True
    )
    anim_include_visibility: BoolProperty(
        name="Include Visibility",
        description="Include Visibility Track",
        default=True
    )
    shape_keys_prefix: StringProperty(
        name="Shape Keys Prefix",
        description="Prefix to use when converting shape keys to meshes",
        default=""
    )
    smash_viewport: BoolProperty(
        name="Smash Viewport",
        description=(
            "Set the scene render engine to Smash Viewport. Then use Rendered "
            "shading in the 3D View, the same way you would with Cycles. "
            "Overlays, selection, and object visibility stay under Blender"
        ),
        default=False,
        update=_update_smash_viewport,
    )
    smash_vp_bg_color: FloatVectorProperty(
        name="Background",
        description="3D View background while Smash Viewport is the render engine",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=(0.0, 0.0, 0.0),
        update=_update_smash_vp_background,
    )
    smash_vp_light_path: StringProperty(
        name="Stage Lights",
        description="Stage light.nuanmb used by Smash Viewport (same as SSBH Editor)",
        default="",
        subtype="FILE_PATH",
        update=_update_smash_vp_lighting,
    )
    eye_look_live_preview: BoolProperty(
        name="Live Preview",
        description=(
            "Show eye look in Solid Texture and Material Preview. Does not run in "
            "Rendered shading (that would restart EEVEE/Cycles sampling). Works with "
            "BL_EyeLook and imported EyeL/EyeR CustomVector31 anims. Bake still has "
            "to run to create the keyframes export reads"
        ),
        default=True,
        update=_update_eye_look_live_preview,
    )
    eye_look_gain_y: FloatProperty(
        name="Gain Y",
        description="Vertical travel per look angle in Look At mode",
        default=0.70, soft_min=0.0, soft_max=4.0,
    )
    eye_look_sensitivity_y: FloatProperty(
        name="Sensitivity Y",
        description="Vertical UV movement per Blender unit in Flat Offset mode",
        default=0.10, soft_min=0.0, soft_max=2.0,
    )
    eye_pupil_centre_auto: BoolProperty(
        name="Auto Pupil Centre",
        description="Measure the scale pivot from the eye meshes' average UV",
        default=True,
    )
    eye_pupil_centre: FloatVectorProperty(
        name="Pupil Centre UV",
        description="The UV the pupil scales about, when Auto Pupil Centre is off",
        size=2, default=(0.5, 0.5), soft_min=0.0, soft_max=1.0,
    )
    eye_look_mode: EnumProperty(
        name="Aim Mode",
        description="How the control bone's position becomes an eye direction",
        items=[
            ("LOOK_AT", "Look At (3D)",
             "Aim at wherever the control sits in 3D. Park it on the camera and the character looks at the camera"),
            ("OFFSET", "Flat Offset (2D)",
             "Map the control's sideways and vertical offset straight onto the eye UVs"),
        ],
        default="OFFSET",
    )
    eye_look_gain: FloatProperty(
        name="Gain",
        description="How far the eyes travel for a given look angle in Look At mode",
        default=0.35, soft_min=0.0, soft_max=2.0,
    )
    eye_look_scale_about_pupil: BoolProperty(
        name="Scale About Pupil",
        description="Compensate the UV translate so resizing the pupil keeps it centred and still aimed at the target",
        default=True,
    )
    eye_look_pupil_from_scale: BoolProperty(
        name="Pupil Size From Bone Scale",
        description="Drive CustomVector31 X/Y from the control bone's scale. Shrink the bone to shrink the pupil",
        default=True,
    )
    eye_look_invert_x: BoolProperty(
        name="Invert X",
        description="Flip which way the eyes look horizontally",
        default=False,
    )
    eye_look_invert_y: BoolProperty(
        name="Invert Y",
        description="Flip which way the eyes look vertically",
        default=False,
    )
    eye_look_sensitivity: FloatProperty(
        name="Sensitivity",
        description="UV units the eye moves per Blender unit the control moves",
        default=0.05, soft_min=0.0, soft_max=1.0,
    )
    eye_look_clamp: FloatProperty(
        name="Clamp",
        description="Largest UV offset allowed, so the pupil can't slide off the eye",
        default=0.5, min=0.0,
    )

    # Transform override bone filtering (shared by exporters)
    anim_override_bone_list: CollectionProperty(
        name="Transform Override Bone List",
        description="Bones listed here are used to filter which bones receive transform overrides",
        type=export_anim.SUB_PG_bone_override_item
    )
    anim_override_bone_list_index: IntProperty(
        name="Transform Override Bone List Index",
        default=0
    )
    anim_override_use_exclude_list: BoolProperty(
        name="Use Exclude List",
        description="If enabled, overrides apply to all bones except those in the list. If disabled, overrides apply only to bones in the list.",
        default=True
    )
    anim_override_armature_name: StringProperty(
        name="Override List Source Armature",
        description="Internal: name of the armature the override bone list was last populated from",
        default=""
    )

    # Transient flag: set by presets to force-enable translation overrides for the next export
    anim_preset_force_override_translation: BoolProperty(
        name="Preset: Force Override Translation",
        description="Internal flag toggled by presets to enable translation override for next export",
        default=False
    )

    # User Pose Library Properties
    user_pose_list: CollectionProperty(
        name="User Pose List",
        description="List of user-created poses (selected bones at a specific frame)",
        type=UserPoseItem
    )
    user_pose_list_index: IntProperty(
        name="User Pose List Index",
        default=0
    )
    user_pose_apply_only_selected: BoolProperty(
        name="Apply to only selected bones",
        description="If enabled, applying a user pose will affect only currently selected pose bones",
        default=False
    )
    stage_light_expanded: BoolProperty(
        name="Stage Lighting Expanded",
        description="Whether the Stage Lighting section is expanded",
        default=True,
    )
    stage_light_preview: EnumProperty(
        name="Viewport Preview",
        description="SSBH uses one directional light per mesh plus SH ambient, not every LightStg as a Blender sun",
        items=(
            ("AMBIENT", "Ambient Fill", "SH-like fill only. Closest to how SSBH lights a stage (baked maps + ambient)"),
            ("CHR", "Ambient + LightChr", "Fill plus LightChr. Smash fighter lighting"),
            ("STG0", "Ambient + LightStg0", "Fill plus LightStg0. Stage light set 0"),
            ("ALL", "All Lights (Debug)", "Every LightChr/LightStg sun contributes. Harsh and not how the game works"),
            ("NONE", "Gizmos Only", "Imported lights do not illuminate. Rotate them for export only"),
        ),
        default="CHR",
        update=_update_stage_light_preview,
    )
    stage_light_apply_ambient: BoolProperty(
        name="Apply Smash Ambient",
        description="Add an SH-like fill light and world so Smash EEVEE materials are not a black void",
        default=True,
        update=_update_stage_light_preview,
    )
    stage_light_drive_smash_viewport: BoolProperty(
        name="Drive Smash Viewport",
        description="Push Stage Tools light edits into Smash Viewport so you can tweak lighting there live",
        default=True,
    )
    stage_shpc_expanded: BoolProperty(
        name="Ambient SH Expanded",
        description="Whether the Ambient SH section is expanded",
        default=True,
    )
    stage_bf_ref_expanded: BoolProperty(
        name="Battlefield Reference Expanded",
        description="Whether the Battlefield Reference section is expanded",
        default=True,
    )
    last_stage_light_dir: StringProperty(
        name="Last Stage Light Directory",
        description="Last folder used for light.nuanmb import or export",
        default="",
        subtype="DIR_PATH",
    )
    last_stage_shpc_dir: StringProperty(
        name="Last Stage SHPC Directory",
        description="Last folder used for shpcanim import or export",
        default="",
        subtype="DIR_PATH",
    )




