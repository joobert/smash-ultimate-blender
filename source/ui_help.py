"""Shared panel documentation links; keep destinations in step with README."""
DOCS_ROOT = 'https://github.com/joobert/smash-ultimate-blender/blob/animation-workflow/'

# Retired panel types can survive an add-on reload or third-party tab reordering.
# Their controls now live inside the parents' draw functions.
RETIRED_PANEL_IDS = (
    'ULTIMATE_PT_retarget_custom', 'ULTIMATE_PT_retarget_spine',
    'ULTIMATE_PT_retarget_arms', 'ULTIMATE_PT_retarget_arms_IK',
    'ULTIMATE_PT_retarget_legs', 'ULTIMATE_PT_retarget_legs_IK',
    'ULTIMATE_PT_retarget_fingers', 'ULTIMATE_PT_retarget_face',
    'ULTIMATE_PT_retarget_root', 'ULTIMATE_PT_expy_retarget',
    'ULTIMATE_PT_BindPanel', 'ULTIMATE_PT_BindSettings',
    'SUB_PT_ultimate_exo_skel',
)


def merged_panel_id(identifier):
    if identifier == 'SUB_PT_ultimate_exo_skel':
        return 'SUB_PT_model_tools'
    if identifier in RETIRED_PANEL_IDS:
        return 'SUB_PT_retargeting_main'
    return identifier


def unregister_retired_panels():
    import bpy
    for identifier in RETIRED_PANEL_IDS:
        cls = bpy.types.Panel.bl_rna_get_subclass_py(identifier)
        if cls is not None:
            bpy.utils.unregister_class(cls)


def panel_doc_path(panel):
    module = type(panel).__module__
    name = type(panel).__name__
    if 'attribute_renamer' in module:
        return 'docs/attribute-renamer.md'
    if 'reimport_materials' in module:
        return 'docs/material-reimporter.md'
    if name == 'SUB_PT_raw_animations':
        return 'README.md#raw-animations'
    if 'motion_list' in module:
        return 'docs/motion-list.md'
    if '.retargeting' in module:
        anchor = 'retargeting'
    elif '.doctor' in module:
        anchor = 'export-doctor'
    elif '.swing' in module:
        anchor = 'swing-physics'
    elif '.updater' in module:
        anchor = 'auto-updater'
    elif 'panel_preset' in module:
        anchor = 'panel-presets'
    elif 'collection_preset' in module:
        anchor = 'armature-collection-presets'
    elif 'face_picker' in module or 'finger_sliders' in module:
        anchor = 'easy-facial-animation'
    elif 'stage_tools' in module:
        anchor = 'stage-tools'
    elif '.material.texture' in module:
        anchor = 'texture-optimization'
    elif '.material' in module:
        anchor = 'materials'
    elif '.skel' in module:
        anchor = 'helper-bones-bl_'
    elif '.exo' in module or 'model_tools' in name or 'attribute_renamer' in module:
        anchor = 'model-tools'
    elif '.model' in module:
        anchor = 'model-folders-and-export-settings'
    elif '.anim.anim_data' in module:
        anchor = 'animation-data-and-blender-4--5'
    elif '.anim' in module:
        anchor = 'animation-importer-and-exporter'
    elif 'floor_contact' in module:
        return 'docs/ik-floor-contact.md'
    elif 'ik_' in module:
        return 'docs/ik-fk-workflow.md'
    elif 'smash_viewport' in module:
        anchor = 'misc'
    elif 'misc_utilities' in name:
        anchor = 'misc'
    else:
        anchor = 'animation-tools'
    return 'README.md#' + anchor


def draw_panel_help(layout, panel):
    # Also guard inherited callbacks and panels moved to another editor/tab.
    if (getattr(panel, 'bl_space_type', None) != 'VIEW_3D'
            or getattr(panel, 'bl_region_type', None) != 'UI'
            or getattr(panel, 'bl_category', None) != 'Ultimate'):
        return
    row = layout.row(align=True)
    row.scale_x = 1.0
    row.operator('wm.url_open', text='?', emboss=False).url = DOCS_ROOT + panel_doc_path(panel)
