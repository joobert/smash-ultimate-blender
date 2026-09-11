from . import attribute_renamer
from . import create_animation_rig
from . import create_meshes
from . import eye_rig
from . import smash_viewport
from . import finger_sliders
from . import face_picker
from . import misc_panel
from . import set_linear_vertex_color
from . import create_ik_arms
from . import create_ik_armsandlegs
from . import create_ik_legs
from . import bone_removal
from . import bone_chain
from . import apply_ik_animation
from . import hip_animation_transfer
from . import idle_pose_library
from . import ik_influence_toggle
from . import ik_fk_switch
from . import ik_pole_alignment
from . import limit_weights
from . import unstack_uvs
from . import rename_utils
from . import fk_to_ik
from . import bulk_ik
from . import anim_layers_compat
from . import anim_rig_extras
from . import user_poses

# Import reset_animation module and ensure it's properly registered
from . import reset_animation
from . import mirror_animation
from . import roll_copy
from . import stage_tools

# Import animation_scroll module
from . import animation_scroll
from . import viewport_capture
from . import timeline_fps
from . import refresh_bone_drawing
from . import collection_presets
from . import ik_floor_contact
from . import custom_ik

# Explicit registration function for the package
def register():
    # Register reset_animation first to ensure it's available for the panel
    reset_animation.register()
    
    # Register mirror_animation
    mirror_animation.register()

    roll_copy.register()
    stage_tools.register()
    
    # Register rename_utils
    rename_utils.register()
    
    # Register animation_scroll
    animation_scroll.register()
    timeline_fps.register()
    refresh_bone_drawing.register()
    collection_presets.register()
    ik_floor_contact.register()
    custom_ik.register()
    viewport_capture.register()
    face_picker.register()
    eye_rig.register()
    smash_viewport.register()
    create_animation_rig.register()
    
    # Register misc_panel first since IK panel depends on it
    misc_panel.register()
    # Ensure user_poses module is imported so class references are available
    # (classes are registered via new_classes_to_register)
    
    # Register FK/IK modules
    fk_to_ik.register()
    bulk_ik.register()
    create_ik_arms.register()
    create_ik_legs.register()
    create_ik_armsandlegs.register()
    ik_fk_switch.register()
    ik_pole_alignment.register()
    
def unregister():
    custom_ik.unregister()
    face_picker.unregister()
    smash_viewport.unregister()
    eye_rig.unregister()
    create_animation_rig.unregister()
    viewport_capture.unregister()
    ik_floor_contact.unregister()
    collection_presets.unregister()
    refresh_bone_drawing.unregister()
    timeline_fps.unregister()
    # Unregister animation_scroll
    animation_scroll.unregister()
    
    # Unregister rename_utils
    rename_utils.unregister()
    
    # Unregister reset_animation
    reset_animation.unregister()
    
    roll_copy.unregister()
    stage_tools.unregister()

    # Unregister mirror_animation
    mirror_animation.unregister()
    
    # Unregister FK/IK modules (reverse order)
    ik_pole_alignment.unregister()
    create_ik_armsandlegs.unregister()
    create_ik_legs.unregister()
    create_ik_arms.unregister()
    ik_fk_switch.unregister()
    fk_to_ik.unregister()
    bulk_ik.unregister()
    
    # Unregister misc_panel last
    misc_panel.unregister()
