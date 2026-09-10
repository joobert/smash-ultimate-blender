bl_info = {
    'name': 'Smash Ultimate Blender Tools',
    'author': 'Carlos Aguilar, ScanMountGoat (SMG), CrusherD2, joobert',
    'category': 'Object',
    'location': 'View 3D > Tool Shelf > Ultimate',
    'description': 'A collection of tools for importing models and animations to smash ultimate.',
    'version': (4, 5, 0),
    'blender': (4, 4, 0),
    'warning': 'TO REMOVE: First "Disable" the plugin, then restart blender, then you can hit "Remove" to uninstall',
    'doc_url': 'https://github.com/ssbucarlos/smash-ultimate-blender/wiki',
    'tracker_url': 'https://github.com/ssbucarlos/smash-ultimate-blender/issues',
    'special thanks': 'SMG for making SSBH_DATA_PY, which none of this would be possible without. and also the rokoko plugin for being the reference used to make the exo_skel UI'
}

def check_unsupported_blender_versions():
    import bpy
    if bpy.app.version < (4, 4):
        raise ImportError('This addon requires Blender 4.4 or newer (Blender 4.4+ and 5.x are supported)')
    
def register():
    import bpy
    print('Loading Smash Ultimate Blender Tools...')

    check_unsupported_blender_versions()

    # Preferences are needed by ParamLabels and Timeline tools.
    from .source import addon_preferences
    addon_preferences.register()

    from .source import blender_property_extensions, new_classes_to_register
    from .source.extras import set_linear_vertex_color
    from .source.model.material import shader_nodes
    from .source.blender_property_extensions import SubSceneProperties
    from .source import swing

    # Register extras first to ensure the reset_animation operator is available
    from .source import extras
    extras.register()

    # Register swing module
    swing.register()

    new_classes_to_register.register()

    blender_property_extensions.register()
    
    bpy.types.VIEW3D_MT_paint_vertex.append(set_linear_vertex_color.menu_func)

    from .source.blender_compat import register_node_categories
    if getattr(shader_nodes.node_categories, 'HAS_NODEITEMS', True) and shader_nodes.node_categories.node_categories:
        register_node_categories('CUSTOM_ULTIMATE_NODES', shader_nodes.node_categories.node_categories)
    
    # Register texture conversion tools
    from .source.model.material import texture
    texture.register()

    # User-level ParamLabels.csv (%APPDATA%/Smash Ultimate Labels)
    try:
        from .source.param_labels import ensure_param_labels, load_param_labels
        labels_file = ensure_param_labels()
        load_param_labels()
        print(f'ParamLabels.csv: {labels_file}')
    except Exception as e:
        print(f'Could not set up ParamLabels.csv: {e}')

    # Register updater components
    from .source import updater
    updater.register()

    from .source.updater.version_check import check_for_newer_version
    check_for_newer_version()

    # Add sub_scene_properties to the Scene object
    if not hasattr(bpy.types.Scene, "sub_scene_properties"):
        bpy.types.Scene.sub_scene_properties = bpy.props.PointerProperty(type=SubSceneProperties)

    # Register anim_data module (includes handlers and timers)
    from .source.anim import anim_data
    anim_data.register()
    
    # Initialize SAP action auto-sync system
    from .source.anim.anim_data import init_sap_auto_sync
    init_sap_auto_sync()

    # Register retargeting module (expy_kit integration)
    from .source import retargeting
    retargeting.register()

    # Export Doctor owns its own Scene property, so it registers itself.
    from .source import doctor
    doctor.register()

    from .source.anim import motion_list_ui
    motion_list_ui.register()

    # Last, once every panel exists: Panel Presets owns both sidebar visibility
    # and sidebar order, and its registration applies the saved layout.
    from .source.extras import panel_presets
    panel_presets.register()

    print('Loaded Smash Ultimate Blender Tools!')

def unregister():
    import bpy
    print('Unloading Smash Ultimate Blender Tools...')

    from .source.extras import set_linear_vertex_color
    from .source import new_classes_to_register
    from .source.model.material import texture
    from .source import swing

    # Unregister panel presets first (restores original panel polls)
    from .source.extras import panel_presets
    panel_presets.unregister()

    from .source.anim import motion_list_ui
    motion_list_ui.unregister()

    from .source import doctor
    doctor.unregister()

    # Unregister retargeting module first (expy_kit integration)
    from .source import retargeting
    retargeting.unregister()

    from .source.blender_compat import unregister_node_categories
    unregister_node_categories('CUSTOM_ULTIMATE_NODES')
    
    # Unregister texture conversion tools
    texture.unregister()

    # Unregister updater components
    from .source import updater
    updater.unregister()

    bpy.types.VIEW3D_MT_paint_vertex.remove(set_linear_vertex_color.menu_func)

    # Cleanup SAP action auto-sync system
    from .source.anim.anim_data import cleanup_sap_auto_sync
    cleanup_sap_auto_sync()
    
    # Unregister anim_data module (includes handlers and timers)
    from .source.anim import anim_data
    anim_data.unregister()

    # Let feature modules remove their own panels, handlers, and keymaps before
    # the legacy central class list handles everything that remains.
    from .source import extras
    extras.unregister()

    new_classes_to_register.unregister()

    # Unregister swing module
    swing.unregister()

    from .source import addon_preferences
    addon_preferences.unregister()

    if hasattr(bpy.types.Scene, "sub_scene_properties"):
        del bpy.types.Scene.sub_scene_properties

    print('Unloaded Smash Ultimate Blender Tools!')
            
if __name__ == '__main__':
    register()
