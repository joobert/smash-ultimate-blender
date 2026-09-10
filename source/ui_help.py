"""Shared panel documentation links; keep destinations in step with README."""
DOCS_ROOT = 'https://github.com/joobert/smash-ultimate-blender/blob/animation-workflow/'


def panel_doc_path(panel):
    module = type(panel).__module__
    name = type(panel).__name__
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
    row = layout.row(align=True)
    row.scale_x = 1.0
    row.operator('wm.url_open', text='?', emboss=False).url = DOCS_ROOT + panel_doc_path(panel)
