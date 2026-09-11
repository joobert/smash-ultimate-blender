"""Smash Viewport: ssbh_wgpu preview over the 3D View.

A normal Blender render engine. Set the scene engine to Smash Viewport and use
Rendered shading. Overlays, selection, and object visibility stay under Blender.
"""

from __future__ import annotations

import ctypes
import math
import os
import re
import sys
import time
from ctypes import POINTER, c_char_p, c_float, c_int, c_uint, c_ubyte, c_void_p, c_size_t

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector

ENGINE_ID = "SMASH_VIEWPORT"
_DRAW_HANDLER_ATTR = "_sub_smash_vp_draw_handler"
_ANIM_RIG_FLAG = "sub_animation_rig"
_EXTRA_BONE_PREFIX = "BL_"
_MODEL_FOLDER_KEYS = ("sub_smash_model_folder", "smash_model_folder")
_NUMSHB_ORDER = "numshb order"
_NUMSHB_NAME = "numshb name"
_NUMSHB_SUB = "numshb subindex"
_TIMER_INTERVAL = 1.0 / 60.0
_IK_BONE = re.compile(r"^(Foot|Hand|Knee|Arm)IK([LR])(\d*)$")

_Y_UP_TO_Z_UP = Matrix.Rotation(math.radians(90.0), 4, "X")
_Z_UP_TO_Y_UP = _Y_UP_TO_Z_UP.inverted()
_X_MAJOR_TO_Y_MAJOR = Matrix.Rotation(math.radians(-90.0), 4, "Z")
_Y_MAJOR_TO_X_MAJOR = _X_MAJOR_TO_Y_MAJOR.inverted()
# Blender window_matrix is OpenGL clip Z [-1, 1]. wgpu/DX12 is [0, 1].
_GL_TO_WGPU_CLIP = Matrix((
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.5, 0.5),
    (0.0, 0.0, 0.0, 1.0),
))
# Smash shaders are perspective-only. Locked ortho views use a long-lens
# stand-in at this distance so lighting works; larger = closer to true ortho.
_ORTHO_EYE_MIN = 400.0

_lib = None
_lib_error = ""
_preview = None
_loaded_folder = ""
_loaded_arm_fp = None
_reload_after_undo = False
_pixels = bytearray()
_float_pixels = None
_pixel_size = (0, 0)
_last_status = ""
_last_error = ""
_name_keep = []
_name_arr_keep = None
_vis_name_keep = []
_cv_name_keep = []
_timer_running = False
_ignore_update = False
_in_tick = False
_saved_engines = {}
_saved_color = {}
_saved_grids = {}
_last_tick_key = None
_last_pose_fp = None
_last_pose_arm_fp = None
_last_synced_arm_fp = None
_mesh_sync_timer = False
_last_vis_state = None
_last_model_vis_state = None
_last_mesh_transform_state = None
_last_cv31_state = None
_last_cam_key = None
_last_frame = None
_last_draw_mono = 0.0
_pixel_version = 0
_blit_tex = None
_blit_tex_key = None
_gpu_failed = False
_blit_ubyte_ok = True
_saved_mesh_filter = {}
_applied_bg = None
_applied_light = None
_applied_light_frame = None
_bg_picker_touched = False
_MSGBUS_OWNER = object()
_resume_attempts = 0
_shade_shader = None
_shade_cache = {}
_shade_image_cache = {}
_shade_look_cache = {}
_shade_arm_cache = {}
_shade_mod_cache = {}
_shade_merged = None
_shade_white_tex = None
_extra_fp = None
_extra_mesh_map = {}
_extra_ok = False
_extra_smash_arms = set()
_extra_blender_smash = set()
_extra_gpu_order = []
_smash_arm_cache = {}
_smash_arm_count = None
_bone_name_tuple = None
_mod_show_saved = {}
_in_mod_sync = False
_saved_in_front = {}
_saved_mesh_hide = {}
_saved_mesh_select = {}
_saved_mesh_display = {}
_numshb_folder_cache = {}
_numshb_object_cache = {}
_primary_arm_obj = None
_fill_batch = None
_fill_batch_size = None


def _clear_shade_caches():
    global _shade_merged, _shade_shader, _smash_arm_count
    global _primary_arm_obj, _fill_batch, _fill_batch_size
    _shade_cache.clear()
    _shade_image_cache.clear()
    _shade_look_cache.clear()
    _shade_arm_cache.clear()
    _shade_mod_cache.clear()
    _smash_arm_cache.clear()
    _numshb_folder_cache.clear()
    _numshb_object_cache.clear()
    _smash_arm_count = None
    _primary_arm_obj = None
    _fill_batch = None
    _fill_batch_size = None
    _shade_merged = None
    _shade_shader = None


def _clear_undo_object_caches():
    """Drop Blender object lookups after undo without evicting GPU resources.

    Blender's undo system can replace RNA wrappers even when the rendered model
    is unchanged. Pointer-indexed lookup caches must be refreshed, but textures,
    shaders, batches, and the native wgpu preview remain valid and are expensive
    to recreate for every Ctrl+Z.
    """
    global _smash_arm_count, _primary_arm_obj
    _shade_arm_cache.clear()
    _shade_mod_cache.clear()
    _smash_arm_cache.clear()
    _numshb_folder_cache.clear()
    _numshb_object_cache.clear()
    _smash_arm_count = None
    _primary_arm_obj = None


def _drop_viewport_object_maps():
    """Forget pointer-keyed viewport overrides. Undo replaces RNA pointers."""
    global _mod_show_saved
    _mod_show_saved = {}
    _saved_mesh_hide.clear()
    _saved_mesh_select.clear()
    _saved_mesh_display.clear()
    _saved_in_front.clear()


def last_shader_error():
    return _last_error


def last_draw_status():
    return _last_status


def invalidate_animation_state(redraw=True):
    """Force the next draw to upload Blender's current pose and SAP values.

    Assigning or importing an Action can change the evaluated pose without
    changing ``frame_current``. The viewport's frame-based cache must therefore
    be invalidated explicitly or the native model stays on the previous pose
    until playback advances by one frame.
    """
    global _last_pose_fp, _last_pose_arm_fp, _last_synced_arm_fp
    global _last_vis_state, _last_model_vis_state, _last_cv31_state
    global _last_frame, _primary_arm_obj, _smash_arm_count, _extra_fp
    global _last_mesh_transform_state
    _last_mesh_transform_state = None
    _last_pose_fp = None
    _last_pose_arm_fp = None
    _last_synced_arm_fp = None
    _last_vis_state = None
    _last_model_vis_state = None
    _last_cv31_state = None
    _last_frame = None
    _primary_arm_obj = None
    _smash_arm_count = None
    _extra_fp = None
    if redraw and _viewport_enabled():
        _schedule_mesh_draw_sync()
        _tag_preview_redraw()


def on_retarget_bind_changed():
    """Bind/unbind: refresh pose and extras without hiding either character."""
    _heal_smash_file_viewport_state()
    _clear_extra_models()
    invalidate_animation_state(redraw=True)


def _first_scene():
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        return scene
    try:
        return bpy.data.scenes[0]
    except Exception:
        return None


def _scene_wants_smash(scene):
    if scene is None:
        return False
    ssp = getattr(scene, "sub_scene_properties", None)
    # Only the checkbox — a leftover SMASH_VIEWPORT engine must not auto-reenable.
    return ssp is not None and bool(getattr(ssp, "smash_viewport", False))


def _has_rendered_3d_view():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return False
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            shading = getattr(space, "shading", None) if space is not None else None
            if shading is not None and getattr(shading, "type", "") == "RENDERED":
                return True
    return False


def _id_key(ob):
    try:
        return int(ob.as_pointer())
    except Exception:
        return id(ob)


def native_plugin_path():
    addon_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    native = os.path.join(addon_dir, "native")
    names = []
    if sys.platform == "win32":
        names = ["ssbh_blender_preview.dll", "ssbh_blender_preview.reload.dll"]
    elif sys.platform == "darwin":
        names = ["libssbh_blender_preview.dylib", "ssbh_blender_preview.dylib"]
    else:
        names = ["libssbh_blender_preview.so"]
    search = [
        os.path.join(native, "bin"),
        os.path.join(native, "ssbh_blender_preview", "target", "release"),
        os.path.join(native, "ssbh_blender_preview", "target", "debug"),
    ]
    found = []
    for folder in search:
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                found.append(path)
    if not found:
        return ""
    found.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return found[0]


def _bind_lib(lib):
    lib.ssbh_preview_create.restype = c_void_p
    lib.ssbh_preview_create.argtypes = []
    lib.ssbh_preview_destroy.restype = None
    lib.ssbh_preview_destroy.argtypes = [c_void_p]
    lib.ssbh_preview_load_folder.restype = c_int
    lib.ssbh_preview_load_folder.argtypes = [c_void_p, c_char_p]
    lib.ssbh_preview_resize.restype = c_int
    lib.ssbh_preview_resize.argtypes = [c_void_p, c_uint, c_uint, c_float]
    lib.ssbh_preview_set_camera.restype = c_int
    lib.ssbh_preview_set_camera.argtypes = [
        c_void_p,
        POINTER(c_float),
        POINTER(c_float),
        POINTER(c_float),
        c_float,
        c_float,
        c_float,
    ]
    lib.ssbh_preview_set_world_transforms.restype = c_int
    lib.ssbh_preview_set_world_transforms.argtypes = [
        c_void_p,
        POINTER(c_char_p),
        POINTER(c_float),
        c_uint,
    ]
    lib.ssbh_preview_set_mesh_visibility.restype = c_int
    lib.ssbh_preview_set_mesh_visibility.argtypes = [
        c_void_p,
        POINTER(c_char_p),
        POINTER(c_uint),
        POINTER(c_ubyte),
        c_uint,
    ]
    if hasattr(lib, "ssbh_preview_set_custom_vector"):
        lib.ssbh_preview_set_custom_vector.restype = c_int
        lib.ssbh_preview_set_custom_vector.argtypes = [
            c_void_p,
            POINTER(c_char_p),
            c_char_p,
            POINTER(c_float),
            c_uint,
        ]
    if hasattr(lib, "ssbh_preview_apply_material_anim"):
        lib.ssbh_preview_apply_material_anim.restype = c_int
        lib.ssbh_preview_apply_material_anim.argtypes = [
            c_void_p,
            c_char_p,
            c_float,
        ]
    lib.ssbh_preview_render.restype = c_int
    lib.ssbh_preview_render.argtypes = [
        c_void_p,
        ctypes.c_void_p,
        c_size_t,
        POINTER(c_uint),
        POINTER(c_uint),
    ]
    lib.ssbh_preview_poll.restype = c_int
    lib.ssbh_preview_poll.argtypes = [
        c_void_p,
        ctypes.c_void_p,
        c_size_t,
        POINTER(c_uint),
        POINTER(c_uint),
    ]
    lib.ssbh_preview_using_gpu.restype = c_int
    lib.ssbh_preview_using_gpu.argtypes = [c_void_p]
    lib.ssbh_preview_present_gl.restype = c_int
    lib.ssbh_preview_present_gl.argtypes = [c_void_p, c_uint, c_uint, c_int]
    lib.ssbh_preview_last_error.restype = c_char_p
    lib.ssbh_preview_last_error.argtypes = []
    optional = (
        ("ssbh_preview_set_mesh_transforms", c_int, [c_void_p, POINTER(c_uint), POINTER(c_char_p), POINTER(c_uint), POINTER(c_float), c_uint]),
        ("ssbh_preview_set_clear_color", c_int, [c_void_p, c_float, c_float, c_float, c_float]),
        ("ssbh_preview_load_lighting", c_int, [c_void_p, c_char_p]),
        ("ssbh_preview_clear_lighting", c_int, [c_void_p]),
        ("ssbh_preview_set_lighting_frame", c_int, [c_void_p, c_float]),
        ("ssbh_preview_render_wait", c_int, [
            c_void_p,
            ctypes.c_void_p,
            c_size_t,
            POINTER(c_uint),
            POINTER(c_uint),
        ]),
        ("ssbh_preview_load_extra_folder", c_int, [c_void_p, c_char_p, c_char_p]),
        ("ssbh_preview_clear_extra_models", c_int, [c_void_p]),
        ("ssbh_preview_set_model_visible", c_int, [c_void_p, c_uint, c_int]),
        ("ssbh_preview_gif_begin", c_int, [c_char_p, c_uint, c_int]),
        ("ssbh_preview_gif_add_frame", c_int, [ctypes.c_void_p, c_size_t, c_uint, c_uint]),
        ("ssbh_preview_gif_finish", c_int, []),
        ("ssbh_preview_gif_cancel", c_int, []),
    )
    for name, restype, argtypes in optional:
        if hasattr(lib, name):
            fn = getattr(lib, name)
            fn.restype = restype
            fn.argtypes = argtypes
    return lib


def _native_error(lib=None):
    handle = lib or _lib
    if handle is None:
        return _lib_error or "Native plugin not loaded"
    try:
        raw = handle.ssbh_preview_last_error()
    except Exception:
        return "Native plugin error"
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def load_native_library():
    global _lib, _lib_error
    if _lib is not None:
        return _lib
    path = native_plugin_path()
    if not path:
        _lib_error = (
            "Smash Viewport plugin not found. Windows ships "
            "native/bin/ssbh_blender_preview.dll; Linux and macOS plugins are "
            "native/bin/libssbh_blender_preview.so and "
            "native/bin/libssbh_blender_preview.dylib. See native/README.md."
        )
        return None
    if sys.platform == "win32":
        os.environ.setdefault("WGPU_BACKEND", "dx12")
    elif sys.platform == "darwin":
        os.environ.setdefault("WGPU_BACKEND", "metal")
    else:
        os.environ.setdefault("WGPU_BACKEND", "vulkan")
    try:
        _lib = _bind_lib(ctypes.CDLL(path))
        _lib_error = ""
        return _lib
    except OSError as exc:
        _lib_error = f"Failed to load {path}: {exc}"
        _lib = None
        return None


def _ensure_preview():
    global _preview, _last_error
    lib = load_native_library()
    if lib is None:
        _last_error = _lib_error
        return None
    if _preview:
        return _preview
    handle = lib.ssbh_preview_create()
    if not handle:
        _last_error = _native_error(lib) or "ssbh_preview_create failed"
        return None
    _preview = handle
    return _preview


def shutdown_preview():
    global _preview, _loaded_folder, _loaded_arm_fp, _pixels, _float_pixels, _pixel_size
    global _last_tick_key, _last_pose_fp, _last_vis_state, _last_model_vis_state, _last_cv31_state
    global _last_cam_key, _last_frame, _last_status, _last_error
    global _blit_tex, _blit_tex_key, _gpu_failed
    global _applied_bg, _applied_light, _applied_light_frame, _bone_name_tuple, _name_arr_keep, _reload_after_undo
    if _lib is not None and _preview:
        try:
            _lib.ssbh_preview_destroy(_preview)
        except Exception:
            pass
    global _last_mesh_transform_state
    _last_mesh_transform_state = None
    _preview = None
    _loaded_folder = ""
    _loaded_arm_fp = None
    _reload_after_undo = False
    _pixels = bytearray()
    _float_pixels = None
    _pixel_size = (0, 0)
    _last_tick_key = None
    _last_pose_fp = None
    _last_vis_state = None
    _last_model_vis_state = None
    _last_cv31_state = None
    _last_cam_key = None
    _last_frame = None
    _last_status = ""
    _blit_tex = None
    _blit_tex_key = None
    _gpu_failed = False
    _applied_bg = None
    _applied_light = None
    _applied_light_frame = None
    _bone_name_tuple = None
    _name_arr_keep = None
    _reset_extra_state()
    _clear_shade_caches()


def _folder_has_numshb(folder):
    folder = (folder or "").strip()
    if not folder:
        return False
    cached = _numshb_folder_cache.get(folder)
    if cached is not None:
        return cached
    ok = False
    if os.path.isdir(folder):
        try:
            names = os.listdir(folder)
        except Exception:
            names = ()
        ok = any(name.lower().endswith(".numshb") for name in names)
    _numshb_folder_cache[folder] = ok
    return ok


def _folder_is_smash_model(folder):
    """True when the folder has the files ssbh_wgpu needs to shade a fighter."""
    folder = (folder or "").strip()
    if not folder or not os.path.isdir(folder):
        return False
    try:
        names = [name.lower() for name in os.listdir(folder)]
    except Exception:
        return False
    has_mesh = any(name.endswith(".numshb") for name in names)
    has_skel = any(name.endswith(".nusktb") for name in names)
    has_matl = any(name.endswith(".numatb") for name in names)
    return has_mesh and (has_skel or has_matl)


def _scan_for_smash_folder(root, depth=0):
    if _folder_is_smash_model(root):
        return root
    if not root or not os.path.isdir(root) or depth >= 3:
        return ""
    try:
        names = os.listdir(root)
    except Exception:
        return ""
    preferred = []
    other = []
    for name in names:
        child = os.path.join(root, name)
        if not os.path.isdir(child):
            continue
        lower = name.lower()
        if lower in ("body", "c00", "c01", "model", "fighter"):
            preferred.append(child)
        else:
            other.append(child)
    for child in preferred + other:
        found = _scan_for_smash_folder(child, depth + 1)
        if found:
            return found
    return ""


def _walk_up_for_smash_folder(path):
    path = (path or "").strip()
    if path and os.path.isfile(path):
        path = os.path.dirname(path)
    current = path
    for _ in range(6):
        if not current:
            break
        found = _scan_for_smash_folder(current, 0)
        if found:
            return found
        parent = os.path.dirname(current.rstrip("\\/"))
        if parent == current:
            break
        current = parent
    return ""


def _mesh_texture_dirs(obj):
    dirs = []
    seen = set()
    data = getattr(obj, "data", None) if obj is not None else None
    materials = getattr(data, "materials", None) if data is not None else None
    if not materials:
        return dirs
    for mat in materials:
        if mat is None:
            continue
        tree = getattr(mat, "node_tree", None) if getattr(mat, "use_nodes", False) else None
        nodes = getattr(tree, "nodes", None) if tree is not None else None
        if not nodes:
            continue
        for node in nodes:
            image = getattr(node, "image", None)
            filepath = getattr(image, "filepath", "") if image is not None else ""
            if not filepath:
                continue
            try:
                abs_path = bpy.path.abspath(filepath)
            except Exception:
                abs_path = filepath
            folder = os.path.dirname(abs_path)
            key = os.path.normcase(os.path.normpath(folder)) if folder else ""
            if key and key not in seen and os.path.isdir(folder):
                seen.add(key)
                dirs.append(folder)
    return dirs


def _reload_armature(context):
    obj = getattr(context, "object", None)
    if obj is not None and getattr(obj, "type", "") == "ARMATURE":
        return obj
    if obj is not None:
        try:
            arm = obj.find_armature()
        except Exception:
            arm = None
        if arm is not None:
            return arm
        parent = getattr(obj, "parent", None)
        if parent is not None and getattr(parent, "type", "") == "ARMATURE":
            return parent
    try:
        from .create_animation_rig import find_target_armature
        arm = find_target_armature(context)
        if arm is not None:
            return arm
    except Exception:
        pass
    scene = getattr(context, "scene", None)
    objects = getattr(scene, "objects", None) if scene is not None else None
    if objects:
        for arm in objects:
            if getattr(arm, "type", "") == "ARMATURE" and _is_smash_armature(arm):
                return arm
    return None


def _find_smash_model_folder(context, arm=None):
    """Locate a fighter folder with .numshb + .nusktb/.numatb for Smash Viewport."""
    scene = getattr(context, "scene", None)
    roots = []
    seen = set()

    def add_root(path):
        path = (path or "").strip()
        if path and os.path.isfile(path):
            path = os.path.dirname(path)
        if not path:
            return
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            return
        seen.add(key)
        roots.append(path)

    add_root(_folder_from_armature(arm))
    add_root(_model_folder(scene))
    add_root(_guess_model_folder(scene))
    ssp = getattr(scene, "sub_scene_properties", None) if scene is not None else None
    if ssp is not None:
        for attr in (
            "model_import_folder_path",
            "last_imported_model_path",
            "last_model_folder",
            "vanilla_nusktb",
        ):
            add_root(getattr(ssp, attr, "") or "")
    if arm is not None:
        children = getattr(arm, "children_recursive", None) or getattr(arm, "children", None) or []
        for child in children:
            if getattr(child, "type", "") != "MESH":
                continue
            for folder in _mesh_texture_dirs(child):
                add_root(folder)
    objects = getattr(scene, "objects", None) if scene is not None else None
    if objects and not roots:
        for obj in objects:
            if getattr(obj, "type", "") != "MESH" or not _is_smash_mesh(obj):
                continue
            for folder in _mesh_texture_dirs(obj):
                add_root(folder)
    for root in roots:
        found = _walk_up_for_smash_folder(root)
        if found:
            return found
        found = _scan_for_smash_folder(root, 0)
        if found:
            return found
    return ""


def _folder_from_id(block):
    if block is None:
        return ""
    getter = getattr(block, "get", None)
    if getter is None:
        return ""
    for key in _MODEL_FOLDER_KEYS:
        path = getter(key, "")
        if _folder_has_numshb(path):
            return str(path).strip()
    return ""


def _folder_from_armature(obj):
    if obj is None:
        return ""
    found = _folder_from_id(obj)
    if found:
        return found
    return _folder_from_id(getattr(obj, "data", None))


def _store_model_folder(obj, folder):
    folder = (folder or "").strip()
    if obj is None or not folder:
        return
    try:
        obj[_MODEL_FOLDER_KEYS[0]] = folder
    except Exception:
        pass
    data = getattr(obj, "data", None)
    if data is not None:
        try:
            data[_MODEL_FOLDER_KEYS[0]] = folder
        except Exception:
            pass


def _guess_model_folder(scene):
    ssp = getattr(scene, "sub_scene_properties", None)
    candidates = []
    if ssp is not None:
        for attr in (
            "last_imported_model_path",
            "last_model_folder",
            "model_import_folder_path",
            "vanilla_nusktb",
            "vanilla_update_prc",
        ):
            value = (getattr(ssp, attr, "") or "").strip()
            if not value:
                continue
            if os.path.isfile(value):
                value = os.path.dirname(value)
            candidates.append(value)
    objects = getattr(scene, "objects", None) if scene is not None else None
    if objects:
        for obj in objects:
            if getattr(obj, "type", "") != "ARMATURE":
                continue
            folder = _folder_from_armature(obj)
            if folder:
                candidates.append(folder)
    for folder in candidates:
        if _folder_has_numshb(folder):
            return folder
    return ""


def _model_folder(scene):
    """Folder for this scene only. Never reuse a GPU folder from a previous .blend."""
    if _loaded_folder:
        return _loaded_folder
    if scene is None:
        return ""
    objects = getattr(scene, "objects", None)
    if objects:
        for obj in objects:
            if getattr(obj, "type", "") != "ARMATURE":
                continue
            folder = _folder_from_armature(obj)
            if _folder_has_numshb(folder):
                return folder
    return _guess_model_folder(scene)


def _apply_model_folder(scene, folder, armature=None):
    folder = (folder or "").strip()
    if not folder:
        return False
    ssp = getattr(scene, "sub_scene_properties", None)
    if ssp is not None:
        ssp.last_imported_model_path = folder
        try:
            ssp.last_model_folder = folder
        except Exception:
            pass
    if armature is not None and getattr(armature, "type", "") == "ARMATURE":
        _store_model_folder(armature, folder)
    elif scene is not None:
        for obj in getattr(scene, "objects", []) or []:
            if _is_smash_armature(obj):
                _store_model_folder(obj, folder)
                break
    global _loaded_folder
    if folder != _loaded_folder:
        _loaded_folder = ""
    return True


def _heal_model_folder(scene):
    """Read-only. Do not stamp a folder onto armatures — that leaked All Might into other files."""
    if scene is None:
        return ""
    return _model_folder(scene)


def _has_trans_or_hip(obj):
    if obj is None or getattr(obj, "type", "") != "ARMATURE":
        return False
    pose = getattr(obj, "pose", None)
    bones = getattr(pose, "bones", None) if pose is not None else None
    if not bones:
        data = getattr(obj, "data", None)
        bones = getattr(data, "bones", None)
    if not bones:
        return False
    for bone in bones:
        name = getattr(bone, "name", "") or ""
        if name.startswith(_EXTRA_BONE_PREFIX):
            continue
        if name.split(".")[0] in ("Trans", "Hip"):
            return True
    return False


def _is_smash_mesh(obj):
    if obj is None or getattr(obj, "type", "") != "MESH":
        return False
    getter = getattr(obj, "get", None)
    if getter is not None and getter(_NUMSHB_ORDER, None) is not None:
        return True
    return False


def _armature_owns_smash_mesh(arm):
    if arm is None:
        return False
    children = getattr(arm, "children_recursive", None)
    if children:
        for child in children:
            if _is_smash_mesh(child):
                return True
    scenes = getattr(arm, "users_scene", None) or ()
    scene = scenes[0] if scenes else None
    objects = getattr(scene, "objects", None) if scene is not None else None
    if not objects:
        return False
    for obj in objects:
        if not _is_smash_mesh(obj):
            continue
        try:
            if obj.find_armature() == arm:
                return True
        except Exception:
            pass
        for mod in getattr(obj, "modifiers", []) or []:
            if getattr(mod, "type", "") == "ARMATURE" and getattr(mod, "object", None) == arm:
                return True
    return False


def _is_smash_armature(obj):
    if obj is None or getattr(obj, "type", "") != "ARMATURE":
        return False
    try:
        key = int(obj.as_pointer())
    except Exception:
        key = id(obj)
    cached = _smash_arm_cache.get(key)
    if cached is not None:
        return cached
    result = False
    if _folder_from_armature(obj):
        result = True
    else:
        base = ((getattr(obj, "name", "") or "").split(".")[0] or "").lower()
        if base in ("smush_blender_import", "smash_blender_import"):
            result = True
        elif _armature_owns_smash_mesh(obj):
            result = True
        elif _has_trans_or_hip(obj):
            result = True
    _smash_arm_cache[key] = result
    return result


def _scene_has_smash_model(scene):
    objects = getattr(scene, "objects", None)
    if not objects:
        return False
    for obj in objects:
        if _is_smash_armature(obj):
            return True
    return False


def _engine_is_smash(scene=None):
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return False
    try:
        return scene.render.engine == ENGINE_ID
    except Exception:
        return False


def _viewport_enabled(scene=None):
    """True only when Smash Viewport is intentionally active.

    Requires the UI checkbox — a stuck SMASH_VIEWPORT engine alone must not
    keep depsgraph handlers tagging Rendered views forever.
    """
    scene = scene or _first_scene()
    if scene is None:
        return False
    ssp = getattr(scene, "sub_scene_properties", None)
    if ssp is None or not bool(getattr(ssp, "smash_viewport", False)):
        return False
    return _engine_is_smash(scene)


def _reset_extra_state():
    global _extra_fp, _extra_mesh_map, _extra_ok, _extra_smash_arms, _extra_blender_smash, _extra_gpu_order
    global _last_model_vis_state
    _extra_fp = None
    _extra_mesh_map = {}
    _extra_ok = False
    _extra_smash_arms = set()
    _extra_blender_smash = set()
    _extra_gpu_order = []
    _last_model_vis_state = None


def _refresh_smash_arm_cache(scene):
    global _smash_arm_count
    count = 0
    objects = getattr(scene, "objects", None) if scene is not None else None
    if objects is not None:
        try:
            count = len(objects)
        except Exception:
            count = 0
    if count != _smash_arm_count:
        _smash_arm_cache.clear()
        _smash_arm_count = count


def _clear_extra_models():
    global _last_pose_fp
    if _preview is not None and _has_extra_api() and (_extra_ok or _extra_mesh_map):
        try:
            _lib.ssbh_preview_clear_extra_models(_preview)
        except Exception:
            pass
    _reset_extra_state()
    _last_pose_fp = None


def _has_extra_api():
    return (
        _lib is not None
        and hasattr(_lib, "ssbh_preview_load_extra_folder")
        and hasattr(_lib, "ssbh_preview_clear_extra_models")
    )


def _load_extra_folder(path, prefix=""):
    fn = getattr(_lib, "ssbh_preview_load_extra_folder", None)
    if fn is None or _preview is None:
        return -1
    encoded = path.encode("utf-8")
    nargs = len(getattr(fn, "argtypes", ()) or ())
    if nargs >= 3:
        return fn(_preview, encoded, (prefix or "").encode("utf-8"))
    return fn(_preview, encoded)


def _retarget_driver_suppressed(scene, arm):
    """Never hide a bound source/target. Both fighters must stay in the viewport."""
    del scene, arm
    return False


def _gpu_model_visible(scene, arm, view_layer, space):
    """Honor Blender visibility, suppressing only the known retarget driver.

    Never infer duplicates from folder paths or active selection: users can
    legitimately load several copies of the same fighter and all must render.
    """
    return bool(
        arm is not None
        and any(not _mesh_hidden(mesh, view_layer, space) for mesh in _iter_armature_meshes(arm))
        and not _retarget_driver_suppressed(scene, arm)
    )


def _sync_gpu_model_visibility(context, force=False):
    """Match SSBH Editor's per-folder eye: skip draw/skin uploads for hidden models."""
    global _last_model_vis_state
    fn = getattr(_lib, "ssbh_preview_set_model_visible", None)
    if fn is None or _preview is None:
        return False
    scene = getattr(context, "scene", None)
    vl = getattr(context, "view_layer", None)
    space = _preview_space(context)
    primary = _primary_smash_armature(scene)
    start = 1 if _loaded_folder else 0
    states = []
    if _loaded_folder:
        states.append((0, 1 if primary is None or _gpu_model_visible(scene, primary, vl, space) else 0))
    objects = getattr(scene, "objects", None) if scene is not None else None
    ptr_to_obj = {}
    if objects:
        for obj in objects:
            if getattr(obj, "type", "") != "ARMATURE":
                continue
            try:
                ptr_to_obj[int(obj.as_pointer())] = obj
            except Exception:
                pass
    for index, ptr in enumerate(_extra_gpu_order, start=start):
        arm = ptr_to_obj.get(ptr)
        vis = 1 if _gpu_model_visible(scene, arm, vl, space) else 0
        states.append((index, vis))
    state = tuple(states)
    if not force and state == _last_model_vis_state:
        return False
    for index, vis in states:
        try:
            fn(_preview, index, vis)
        except Exception:
            return False
    _last_model_vis_state = state
    return True


def _folders_match(a, b):
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
            os.path.normpath(b)
        )
    except Exception:
        return False


def _context_armature(scene=None):
    """Active armature from the 3D View, not RenderEngine.view_draw's empty context."""
    del scene
    act = None
    found = _find_preview_view()
    if found is not None:
        window = found[0]
        vl = getattr(window, "view_layer", None) if window is not None else None
        objects = getattr(vl, "objects", None) if vl is not None else None
        act = getattr(objects, "active", None) if objects is not None else None
    if act is None:
        ctx = bpy.context
        vl = getattr(ctx, "view_layer", None)
        objects = getattr(vl, "objects", None) if vl is not None else None
        act = getattr(objects, "active", None) if objects is not None else None
        if act is None:
            act = getattr(ctx, "object", None)
    if act is not None and getattr(act, "type", "") == "ARMATURE":
        return act
    if act is not None and getattr(act, "type", "") == "MESH":
        arm = getattr(act, "parent", None)
        if arm is not None and getattr(arm, "type", "") == "ARMATURE":
            return arm
        return _mesh_armature(act)
    return None


def _armature_matches_loaded_folder(arma, folder):
    if arma is None or not _is_smash_armature(arma):
        return False
    if not folder:
        return True
    arm_folder = _folder_from_armature(arma)
    return bool(arm_folder and _folders_match(arm_folder, folder))


def _primary_smash_armature(scene=None):
    """Armature that poses the loaded .numshb. Prefer the selected Smash character."""
    global _primary_arm_obj
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    folder = _loaded_folder or _model_folder(scene)
    if not _folder_has_numshb(folder):
        _primary_arm_obj = None
        return None
    objects = getattr(scene, "objects", None) if scene is not None else None
    if not objects:
        _primary_arm_obj = None
        return None
    matches = []
    for arma in objects:
        if getattr(arma, "type", "") != "ARMATURE":
            continue
        if not _is_smash_armature(arma):
            continue
        arm_folder = _folder_from_armature(arma)
        if folder and arm_folder and not _folders_match(arm_folder, folder):
            continue
        matches.append(arma)
    if not matches:
        _primary_arm_obj = None
        return None
    bind_active = bool(scene is not None and getattr(scene, "expykit_bind_is_active", False))
    constrained = getattr(scene, "expykit_bound_source", None) if bind_active else None
    driver = getattr(scene, "expykit_bound_target", None) if bind_active else None
    if constrained is not None:
        for obj in matches:
            if obj is constrained:
                _primary_arm_obj = obj
                return obj
    preferred = _context_armature(scene)
    if preferred is not None and preferred is not driver:
        for obj in matches:
            if obj is preferred or getattr(obj, "parent", None) is preferred:
                _primary_arm_obj = obj
                return obj
            if _armature_constraint_targets(obj, preferred):
                _primary_arm_obj = obj
                return obj
        if preferred in matches:
            _primary_arm_obj = preferred
            return preferred
    for arma in matches:
        if arma is driver:
            continue
        ad = getattr(arma, "animation_data", None)
        if ad is not None and getattr(ad, "action", None) is not None:
            _primary_arm_obj = arma
            return arma
    _primary_arm_obj = matches[0]
    for arma in matches:
        if arma is not driver:
            _primary_arm_obj = arma
            return arma
    return matches[0]


def _is_primary_smash_armature(obj, scene=None):
    if obj is None:
        return False
    primary = _primary_smash_armature(scene)
    if primary is None:
        return False
    try:
        return int(obj.as_pointer()) == int(primary.as_pointer())
    except Exception:
        return obj == primary


def _preview_space(context=None):
    context = context or bpy.context
    space = getattr(context, "space_data", None)
    if space is not None and getattr(space, "type", "") == "VIEW_3D":
        return space
    found = _find_preview_view()
    if found is None:
        return None
    return found[2]


def _object_visible(obj, view_layer=None, space=None):
    """Outliner eye, view-layer restriction, collection, and this 3D View."""
    if obj is None:
        return False
    try:
        if obj.hide_get():
            return False
    except Exception:
        pass
    if bool(getattr(obj, "hide_viewport", False)):
        return False
    try:
        if view_layer is not None:
            if not obj.visible_get(view_layer=view_layer):
                return False
        elif not obj.visible_get():
            return False
    except Exception:
        pass
    if space is None:
        space = _preview_space()
    if space is not None:
        try:
            if not obj.visible_in_viewport_get(space):
                return False
        except Exception:
            pass
    return True


def _armature_is_active_or_selected(arm, context=None):
    """Active armature only. select_get() is true for every Smash import after a box-select."""
    if arm is None:
        return False
    context = context or bpy.context
    vl = getattr(context, "view_layer", None)
    objects = getattr(vl, "objects", None) if vl is not None else None
    act = getattr(objects, "active", None) if objects is not None else None
    if act is None:
        return False
    if act is arm:
        return True
    if getattr(act, "type", "") == "MESH":
        if getattr(act, "parent", None) is arm:
            return True
        try:
            if _mesh_armature(act) is arm:
                return True
        except Exception:
            pass
        return False
    if getattr(act, "type", "") != "ARMATURE":
        return False
    if getattr(arm, "parent", None) is act:
        return True
    return False


def _armature_constraint_targets(arm, ctrl):
    pose = getattr(arm, "pose", None)
    if pose is None or ctrl is None:
        return False
    try:
        for pbone in pose.bones:
            for con in pbone.constraints:
                if getattr(con, "target", None) is ctrl:
                    return True
    except Exception:
        return False
    return False


def _lambert_preview_armatures(context, view_layer=None, space=None):
    """Meshes we will Python-shade this frame. Hidden / non-active characters stay off."""
    context = context or bpy.context
    view_layer = view_layer or getattr(context, "view_layer", None)
    space = space if space is not None else _preview_space(context)
    scene = getattr(context, "scene", None)
    objects = getattr(scene, "objects", None) if scene is not None else None
    allowed = set()
    if objects is None:
        return allowed
    vl_objects = getattr(view_layer, "objects", None) if view_layer is not None else None
    act = getattr(vl_objects, "active", None) if vl_objects is not None else None
    ctrl = None
    if act is not None and getattr(act, "type", "") == "ARMATURE":
        ctrl = act
    elif act is not None and getattr(act, "type", "") == "MESH":
        ctrl = getattr(act, "parent", None)
        if ctrl is None or getattr(ctrl, "type", "") != "ARMATURE":
            ctrl = _mesh_armature(act)
    if ctrl is None or getattr(ctrl, "type", "") != "ARMATURE":
        return allowed
    if _object_visible(ctrl, view_layer, space):
        allowed.add(ctrl)
    for obj in objects:
        if obj is ctrl or getattr(obj, "type", "") != "ARMATURE":
            continue
        if not _object_visible(obj, view_layer, space):
            continue
        if getattr(obj, "parent", None) is ctrl or _armature_constraint_targets(obj, ctrl):
            allowed.add(obj)
    return allowed


def _smash_folder_extra(obj):
    """Another visible Smash import with its own .numshb folder."""
    if _is_anim_rig(obj) or not _is_smash_armature(obj):
        return False
    if _is_primary_smash_armature(obj):
        return False
    ctx = bpy.context
    if not _object_visible(obj, getattr(ctx, "view_layer", None), _preview_space(ctx)):
        return False
    folder = _folder_from_armature(obj)
    return bool(folder and _folder_has_numshb(folder))


def _skip_extra_mesh(obj):
    name = getattr(obj, "name", "") or ""
    if name.startswith("SUB_WGT_"):
        return True
    try:
        if obj.hide_get():
            return True
    except Exception:
        pass
    return False


def _gpu_covers_mesh(obj, arm):
    try:
        ptr = int(obj.as_pointer())
    except Exception:
        ptr = 0
    if ptr and ptr in _extra_mesh_map:
        return True
    try:
        ap = int(arm.as_pointer()) if arm is not None else 0
    except Exception:
        ap = 0
    if ap and ap in _extra_smash_arms:
        return True
    if _loaded_folder and _is_smash_mesh(obj):
        return True
    if _loaded_folder and arm is not None and _is_smash_armature(arm):
        return True
    return False


def _gpu_covers_scene():
    """True when ssbh_wgpu is shading the Smash character this frame."""
    return bool(_loaded_folder) or bool(_extra_ok) or bool(_extra_gpu_order)


def _gpu_pose_armature_ptrs(scene=None):
    """Armatures whose pose is uploaded to ssbh_wgpu this frame."""
    ptrs = set(_extra_gpu_order)
    ptrs |= _extra_smash_arms
    ptrs |= _extra_blender_smash
    primary = _primary_smash_armature(scene)
    if primary is not None:
        try:
            ptrs.add(int(primary.as_pointer()))
        except Exception:
            pass
    return ptrs


def _skip_extra_armature(obj):
    """GPU extras: every visible deform armature. Anim-rig-only objects are skipped."""
    if obj is None:
        return True
    name = getattr(obj, "name", "") or ""
    if name.startswith("_CINEMA") or name.startswith("Camera"):
        return True
    ctx = bpy.context
    if not _object_visible(obj, getattr(ctx, "view_layer", None), _preview_space(ctx)):
        return True
    loaded = _loaded_folder or _model_folder(getattr(ctx, "scene", None))
    if _folder_has_numshb(loaded):
        if _is_primary_smash_armature(obj):
            return True
        folder = _folder_from_armature(obj)
        if folder and _folders_match(folder, loaded):
            return True
        if _armature_owns_smash_mesh(obj) and not folder:
            return True
    if _is_anim_rig(obj):
        scene = getattr(ctx, "scene", None)
        try:
            from .smash_vp_extra import iter_bound_meshes
            for _mesh in iter_bound_meshes(scene, obj, _skip_extra_mesh):
                return False
        except Exception:
            pass
        return True
    return False


def _has_extra_candidates(scene):
    if scene is None:
        return False
    objects = getattr(scene, "objects", None)
    if not objects:
        return False
    try:
        for obj in objects:
            if getattr(obj, "type", "") != "ARMATURE":
                continue
            if not _skip_extra_armature(obj):
                return True
    except Exception:
        return False
    return False


def _extra_fingerprint(scene):
    from . import smash_vp_extra as extra
    parts = ["world_rest_v2"]
    vl = getattr(bpy.context, "view_layer", None)
    space = _preview_space(bpy.context)
    for arm in extra.iter_extra_armatures(scene, _skip_extra_armature):
        if not _gpu_model_visible(scene, arm, vl, space):
            parts.append(("hidden", int(arm.as_pointer())))
            continue
        folder = _folder_from_armature(arm)
        if _smash_folder_extra(arm):
            parts.append(("smash", int(arm.as_pointer()), arm.name, folder))
            continue
        meshes = tuple(
            (
                int(obj.as_pointer()),
                int(obj.data.as_pointer()) if obj.data else 0,
                len(obj.data.vertices) if obj.data else 0,
            )
            for obj in extra.iter_bound_meshes(scene, arm, _skip_extra_mesh)
        )
        if meshes:
            parts.append(("build", int(arm.as_pointer()), arm.name, meshes))
    return tuple(parts)


def _ensure_extra_models(context):
    """Load extra Smash folders (prefixed bones) and non-Smash Pokken-style folders."""
    global _extra_fp, _extra_mesh_map, _extra_ok, _extra_smash_arms, _extra_blender_smash, _extra_gpu_order, _last_error
    global _last_pose_fp, _last_vis_state
    if not _has_extra_api() or _preview is None:
        _reset_extra_state()
        return False
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    try:
        from . import smash_vp_extra as extra
        fingerprint = _extra_fingerprint(scene)
    except BaseException as exc:
        _last_error = f"GPU extra preview: {exc}"
        return False
    if fingerprint == _extra_fp and _extra_ok:
        _sync_gpu_model_visibility(context)
        return True
    if _lib.ssbh_preview_clear_extra_models(_preview) != 0:
        _last_error = _native_error() or "Failed to clear extra models"
        _reset_extra_state()
        return False
    global _last_mesh_transform_state
    _last_mesh_transform_state = None
    uploaded = {}
    smash_arms = set()
    blender_smash = set()
    gpu_order = []
    if not fingerprint:
        _extra_fp = fingerprint
        _extra_mesh_map = {}
        _extra_smash_arms = set()
        _extra_blender_smash = set()
        _extra_gpu_order = []
        _extra_ok = True
        _sync_gpu_model_visibility(context)
        return False
    notes = []
    vl = getattr(context, "view_layer", None)
    space = _preview_space(context)
    for arm in extra.iter_extra_armatures(scene, _skip_extra_armature):
        if not _gpu_model_visible(scene, arm, vl, space):
            continue
        folder = _folder_from_armature(arm)
        try:
            if _smash_folder_extra(arm):
                prefix = extra.bone_prefix(arm.name)
                if _load_extra_folder(folder, prefix) != 0:
                    notes.append(_native_error() or f"Failed to load extra {arm.name}")
                    continue
                smash_arms.add(int(arm.as_pointer()))
                gpu_order.append(int(arm.as_pointer()))
                uploaded[int(arm.as_pointer())] = ("smash", 0)
                continue
            meshes = list(extra.iter_bound_meshes(scene, arm, _skip_extra_mesh))
            if not meshes:
                continue
            smash_space = any(_is_smash_mesh(mesh) for mesh in meshes)
            path, items = extra.build_armature_folder(
                arm, meshes, smash_bones=smash_space
            )
            if not path:
                continue
            if _load_extra_folder(path, "") != 0:
                notes.append(_native_error() or f"Failed to load extra {arm.name}")
                continue
            gpu_order.append(int(arm.as_pointer()))
            if smash_space:
                blender_smash.add(int(arm.as_pointer()))
            for ptr, name, sub in items:
                uploaded[ptr] = (name, sub)
        except BaseException as exc:
            notes.append(f"{arm.name}: {exc}")
            continue
    if notes and not uploaded:
        _last_error = "GPU extra preview: " + "; ".join(notes[:3])
        _reset_extra_state()
        try:
            _lib.ssbh_preview_clear_extra_models(_preview)
        except Exception:
            pass
        return False
    if notes:
        _last_error = "GPU extra preview: " + "; ".join(notes[:3])
    _extra_fp = fingerprint
    _extra_mesh_map = uploaded
    _extra_smash_arms = smash_arms
    _extra_blender_smash = blender_smash
    _extra_gpu_order = gpu_order
    _extra_ok = True
    _last_pose_fp = None
    _last_vis_state = None
    _sync_gpu_model_visibility(context)
    return True


def _smash_arm_fp(scene):
    """Stable native-model identity used across Blender undo reconstruction.

    RNA pointers are implementation details and may change during undo. Treating
    them as model identity caused a complete wgpu teardown/reload on Ctrl+Z.
    Names, source folders, and mesh topology are stable for pose/property undos
    while still detecting imports, deletes, renames, and topology changes.
    """
    parts = []
    objects = getattr(scene, "objects", None) if scene is not None else None
    if not objects:
        return tuple()
    for obj in objects:
        if getattr(obj, "type", "") != "ARMATURE":
            continue
        if not _is_smash_armature(obj):
            continue
        meshes = []
        for mesh_obj in _iter_armature_meshes(obj):
            data = getattr(mesh_obj, "data", None)
            try:
                vertex_count = len(data.vertices) if data is not None else 0
                polygon_count = len(data.polygons) if data is not None else 0
            except Exception:
                vertex_count = polygon_count = 0
            meshes.append((
                str(getattr(mesh_obj, "name", "") or ""),
                str(getattr(data, "name", "") or ""),
                vertex_count,
                polygon_count,
            ))
        folder = (_folder_from_armature(obj) or "").strip()
        try:
            folder = os.path.normcase(os.path.normpath(folder)) if folder else ""
        except Exception:
            pass
        data = getattr(obj, "data", None)
        parts.append((
            str(getattr(obj, "name", "") or ""),
            str(getattr(data, "name", "") or ""),
            folder,
            tuple(sorted(meshes)),
        ))
    return tuple(sorted(parts))


def _ensure_model(scene):
    global _loaded_folder, _loaded_arm_fp, _last_error, _last_pose_fp, _last_vis_state, _last_cv31_state
    global _last_cam_key, _last_frame, _primary_arm_obj
    if _preview is not None and _loaded_folder:
        return True
    if not _scene_has_smash_model(scene):
        return False
    folder = _model_folder(scene)
    if not folder:
        return False
    arm_fp = _smash_arm_fp(scene)
    if folder == _loaded_folder and arm_fp and arm_fp == _loaded_arm_fp:
        return True
    preview = _ensure_preview()
    if not preview:
        return False
    rc = _lib.ssbh_preview_load_folder(preview, folder.encode("utf-8"))
    if rc != 0:
        _last_error = _native_error() or f"Failed to load {folder}"
        _loaded_folder = ""
        _loaded_arm_fp = None
        return False
    _loaded_folder = folder
    _loaded_arm_fp = arm_fp
    _primary_arm_obj = None
    _last_pose_fp = None
    _last_vis_state = None
    _last_cv31_state = None
    _last_cam_key = None
    _last_frame = None
    _last_error = ""
    _reset_extra_state()
    return True


def _mat4_col_major(matrix):
    return [
        matrix[row][col]
        for col in range(4)
        for row in range(4)
    ]


def _perspective_rh(fov_y, aspect, z_near, z_far):
    # Same matrix as glam::Mat4::perspective_rh used by SSBH Editor.
    f = 1.0 / math.tan(fov_y * 0.5)
    r = z_far / (z_near - z_far)
    return Matrix((
        (f / max(aspect, 1e-8), 0.0, 0.0, 0.0),
        (0.0, f, 0.0, 0.0),
        (0.0, 0.0, r, r * z_near),
        (0.0, 0.0, -1.0, 0.0),
    ))


def _is_persp_proj(rv3d):
    try:
        return abs(float(rv3d.window_matrix[3][2])) > 0.5
    except Exception:
        return True


def _ortho_half_height(rv3d):
    try:
        b = abs(float(rv3d.window_matrix[1][1]))
        if b > 1e-8:
            return 1.0 / b
    except Exception:
        pass
    return max(float(getattr(rv3d, "view_distance", 1.0)), 1.0)


def _ortho_half_extents(rv3d, aspect):
    half_h = max(_ortho_half_height(rv3d), 1e-6)
    try:
        a = abs(float(rv3d.window_matrix[0][0]))
        if a > 1e-8:
            return 1.0 / a, half_h
    except Exception:
        pass
    return half_h * max(aspect, 1e-8), half_h


def _smash_view(rv3d):
    return rv3d.view_matrix @ _Y_UP_TO_Z_UP


def _view_eye(matrix):
    try:
        return matrix.inverted().translation.copy()
    except ValueError:
        return matrix.translation.copy()


def _view_at_distance(rv3d, cam_dist):
    """Same look as Blender, camera pulled back along the view axis."""
    blender_view = rv3d.view_matrix.copy()
    pivot = rv3d.view_location.copy()
    try:
        offset = blender_view.inverted().translation - pivot
    except Exception:
        offset = Vector((0.0, 0.0, 0.0))
    if offset.length > 1e-6:
        direction = offset.normalized()
    else:
        try:
            direction = blender_view.inverted().to_3x3() @ Vector((0.0, 0.0, 1.0))
        except Exception:
            direction = Vector((0.0, 1.0, 0.0))
        if direction.length < 1e-6:
            direction = Vector((0.0, 1.0, 0.0))
        else:
            direction.normalize()
    new_cam = pivot + direction * cam_dist
    rot = blender_view.to_3x3()
    new_view = rot.to_4x4()
    new_view.translation = -(rot @ new_cam)
    return new_view


def _camera_to_smash(rv3d, region, width, height, space=None):
    """Orbit uses Blender's matrices. Locked ortho uses a long-lens perspective
    that matches the ortho frame at the look-at plane (Smash cannot light ortho).
    Blender's view_distance / overlays are not written.
    """
    aspect = float(width) / float(max(height, 1))
    if _is_persp_proj(rv3d):
        smash_view = _smash_view(rv3d)
        smash_proj = _GL_TO_WGPU_CLIP @ rv3d.window_matrix
        cam = _view_eye(smash_view)
        pos = (float(cam.x), float(cam.y), float(cam.z), 1.0)
    else:
        z_near = 1.0
        z_far = 400000.0
        if space is not None:
            try:
                z_far = max(float(space.clip_end), z_near + 100.0)
            except Exception:
                pass
        half_w, half_h = _ortho_half_extents(rv3d, aspect)
        cam_dist = max(float(getattr(rv3d, "view_distance", 1.0)), _ORTHO_EYE_MIN)
        fov_y = 2.0 * math.atan(half_h / cam_dist)
        fov_y = min(max(fov_y, math.radians(0.05)), math.radians(120.0))
        smash_view = _view_at_distance(rv3d, cam_dist) @ _Y_UP_TO_Z_UP
        smash_proj = _perspective_rh(fov_y, half_w / half_h, z_near, z_far)
        cam = _view_eye(smash_view)
        pos = (float(cam.x), float(cam.y), float(cam.z), 1.0)
    return _mat4_col_major(smash_view), _mat4_col_major(smash_proj), pos


def _restore_floor_overlay():
    return


def _restore_overlay_grids():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        _saved_grids.clear()
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            if space is None:
                continue
            saved = _saved_grids.pop(_id_key(space), None)
            if saved is None:
                continue
            overlay = getattr(space, "overlay", None)
            if overlay is None:
                continue
            ortho, floor = saved
            try:
                overlay.show_ortho_grid = ortho
                overlay.show_floor = floor
            except Exception:
                pass
    _saved_grids.clear()


def _skip_smash_bone(name):
    if not name:
        return True
    if name.startswith(_EXTRA_BONE_PREFIX) or name.startswith("SUB_"):
        return True
    return bool(_IK_BONE.match(name.split(".")[0]))


def _is_anim_rig(obj):
    if obj is None:
        return False
    if bool(obj.get(_ANIM_RIG_FLAG)):
        return True
    data = getattr(obj, "data", None)
    return bool(data is not None and data.get(_ANIM_RIG_FLAG))


def _iter_pose_armatures(depsgraph, context):
    """Pose only the armature that owns the loaded .numshb. Never the active extra character."""
    scene = getattr(context, "scene", None)
    arma = _primary_smash_armature(scene)
    if arma is not None:
        yield arma


def _collect_smash_bones(depsgraph, context):
    names = []
    matrices = []
    if not _loaded_folder:
        return names, matrices
    for arma in _iter_pose_armatures(depsgraph, context):
        pose = getattr(arma, "pose", None)
        if pose is None:
            continue
        world = arma.matrix_world
        for pbone in pose.bones:
            name = pbone.name
            if _skip_smash_bone(name):
                continue
            bone = pbone.bone
            if bone is not None and not bool(getattr(bone, "use_deform", True)):
                continue
            smash = _Z_UP_TO_Y_UP @ (world @ pbone.matrix) @ _Y_MAJOR_TO_X_MAJOR
            names.append(name)
            matrices.extend(_mat4_col_major(smash))
    return names, matrices


def _collect_extra_bones(context):
    if not _extra_ok:
        return [], []
    try:
        from .smash_vp_extra import extra_bone_name, extra_gpu_matrix, iter_extra_armatures
    except Exception:
        return [], []
    names = []
    matrices = []
    scene = getattr(context, "scene", None)
    if scene is None:
        return names, matrices
    for arma in iter_extra_armatures(scene, _skip_extra_armature):
        pose = getattr(arma, "pose", None)
        if pose is None:
            continue
        world = arma.matrix_world
        arm_name = arma.name
        try:
            arm_ptr = int(arma.as_pointer())
        except Exception:
            arm_ptr = 0
        file_extra = arm_ptr in _extra_smash_arms
        blender_smash = arm_ptr in _extra_blender_smash
        for pbone in pose.bones:
            if pbone.name.startswith(_EXTRA_BONE_PREFIX) or pbone.name.startswith("SUB_"):
                continue
            if _skip_smash_bone(pbone.name):
                continue
            bone = pbone.bone
            if bone is not None and not bool(getattr(bone, "use_deform", True)):
                continue
            smash = extra_gpu_matrix(
                world @ pbone.matrix,
                smash_bones=file_extra or blender_smash,
            )
            names.append(extra_bone_name(arm_name, pbone.name))
            matrices.extend(_mat4_col_major(smash))
    return names, matrices


def _pose_fingerprint(depsgraph, context):
    """Camera moves keep a stable key. Playback/scrub uses the frame so we do not
    sample dozens of pose matrices just to notice the timeline moved.
    """
    scene = getattr(context, "scene", None)
    frame = 0.0
    if scene is not None:
        try:
            frame = float(scene.frame_current_final)
        except Exception:
            frame = float(getattr(scene, "frame_current", 0))
    playing = bool(getattr(getattr(context, "screen", None), "is_animation_playing", False))
    ptrs = []
    for arma in _iter_pose_armatures(depsgraph, context):
        try:
            ptrs.append(int(arma.as_pointer()))
        except Exception:
            ptrs.append(0)
    if _extra_ok:
        from .smash_vp_extra import iter_extra_armatures
        if scene is not None:
            for arma in iter_extra_armatures(scene, _skip_extra_armature):
                try:
                    ptrs.append(int(arma.as_pointer()))
                except Exception:
                    ptrs.append(0)
    if playing or (_last_frame is not None and abs(frame - _last_frame) > 1e-4):
        return ("time", round(frame, 4), tuple(ptrs))
    parts = []
    for arma in _iter_pose_armatures(depsgraph, context):
        parts.append(_armature_pose_key(arma))
    if _extra_ok:
        from .smash_vp_extra import iter_extra_armatures
        if scene is not None:
            for arma in iter_extra_armatures(scene, _skip_extra_armature):
                parts.append(_armature_pose_key(arma))
    return ("edit", tuple(parts))


def _set_camera(preview, rv3d, region, width, height, scale, space=None):
    view, proj, pos = _camera_to_smash(rv3d, region, width, height, space)
    view_arr = (c_float * 16)(*view)
    proj_arr = (c_float * 16)(*proj)
    pos_arr = (c_float * 4)(*pos)
    return _lib.ssbh_preview_set_camera(
        preview,
        view_arr,
        proj_arr,
        pos_arr,
        float(width),
        float(height),
        float(scale),
    )


def _set_bones(preview, depsgraph, context):
    global _name_keep, _name_arr_keep, _bone_name_tuple
    names, matrices = _collect_smash_bones(depsgraph, context)
    extra_names, extra_matrices = _collect_extra_bones(context)
    if extra_names:
        names.extend(extra_names)
        matrices.extend(extra_matrices)
    if not names:
        return 0
    key = tuple(names)
    if key != _bone_name_tuple or len(_name_keep) != len(names):
        encoded = [n.encode("utf-8") for n in names]
        _name_keep = encoded
        _name_arr_keep = (c_char_p * len(encoded))(*encoded)
        _bone_name_tuple = key
    name_arr = _name_arr_keep
    mat_arr = (c_float * len(matrices))(*matrices)
    return _lib.ssbh_preview_set_world_transforms(
        preview,
        name_arr,
        mat_arr,
        len(names),
    )


def _smash_mesh_object_name(name):
    """Numshb mesh object name. Strip Blender's .001 only, keep Shape/_VIS_ names."""
    name = name or ""
    match = re.match(r"^(.*)\.(\d{3})$", name)
    return match.group(1) if match else name


def _smash_true_name(name):
    """Vis-track name: Blender suffix then Shape/_VIS_/_O_."""
    return re.split(r"Shape|_VIS_|_O_", _smash_mesh_object_name(name))[0] or (name or "")


def _custom_str(obj, key):
    getter = getattr(obj, "get", None)
    if getter is None:
        return ""
    value = getter(key, None)
    return str(value) if value else ""


def _numshb_objects(folder):
    """(name, subindex) in file order — same index as import `numshb order`."""
    folder = (folder or "").strip()
    if not folder:
        return []
    key = os.path.normcase(os.path.normpath(folder))
    cached = _numshb_object_cache.get(key)
    if cached is not None:
        return cached
    names = []
    path = os.path.join(folder, "model.numshb")
    if not os.path.isfile(path):
        try:
            for name in os.listdir(folder):
                if name.lower().endswith(".numshb"):
                    path = os.path.join(folder, name)
                    break
        except Exception:
            path = ""
    if path and os.path.isfile(path):
        try:
            import ssbh_data_py
            mesh = ssbh_data_py.mesh_data.read_mesh(path)
            names = [
                (obj.name or "", int(getattr(obj, "subindex", 0) or 0))
                for obj in getattr(mesh, "objects", []) or []
            ]
        except Exception:
            names = []
    _numshb_object_cache[key] = names
    return names


def _mesh_model_folder(obj):
    arm = getattr(obj, "parent", None)
    if arm is None or getattr(arm, "type", "") != "ARMATURE":
        try:
            arm = obj.find_armature()
        except Exception:
            arm = None
    folder = _folder_from_armature(arm) if arm is not None else ""
    return folder or _loaded_folder


def _smash_gpu_id(obj):
    """Numshb (name, subindex). Prefer file order so Blender renames still match SSBH Editor."""
    stored = _custom_str(obj, _NUMSHB_NAME)
    stored_sub = getattr(obj, "get", lambda *_: None)(_NUMSHB_SUB, None)
    if stored and stored_sub is not None:
        try:
            return _smash_mesh_object_name(stored), int(stored_sub)
        except Exception:
            pass
    getter = getattr(obj, "get", None)
    order = getter(_NUMSHB_ORDER, None) if getter is not None else None
    if order is not None:
        try:
            index = int(order)
        except Exception:
            index = -1
        entries = _numshb_objects(_mesh_model_folder(obj))
        if 0 <= index < len(entries):
            name, sub = entries[index]
            return _smash_mesh_object_name(name), int(sub)
    if stored:
        return _smash_mesh_object_name(stored), 0
    return _smash_mesh_object_name(getattr(obj, "name", "") or ""), 0


def _smash_gpu_name(obj):
    return _smash_gpu_id(obj)[0]


def _local_numshb_subindex(obj):
    """Numshb subindex among this armature's meshes. .001 on a 2nd import is not subindex 1."""
    stored = getattr(obj, "get", lambda *_: None)(_NUMSHB_SUB, None)
    if stored is not None:
        try:
            return int(stored)
        except Exception:
            pass
    smash = _smash_gpu_name(obj)
    if not smash:
        return 0
    arm = getattr(obj, "parent", None)
    if arm is None or getattr(arm, "type", "") != "ARMATURE":
        try:
            arm = obj.find_armature()
        except Exception:
            arm = None
    names = []
    if arm is not None:
        for mesh in _iter_armature_meshes(arm):
            if _smash_gpu_name(mesh) == smash:
                names.append(mesh.name or "")
    else:
        names.append(obj.name or "")
    names.sort()
    try:
        return names.index(obj.name or "")
    except ValueError:
        return 0


def _smash_mesh_keys(obj):
    return [_smash_gpu_id(obj)]


def _mesh_hidden(obj, view_layer, space=None):
    try:
        if obj.hide_get():
            return True
    except Exception:
        pass
    if bool(getattr(obj, "hide_viewport", False)):
        return True
    try:
        if view_layer is not None:
            if not obj.visible_get(view_layer=view_layer):
                return True
        elif not obj.visible_get():
            return True
    except Exception:
        pass
    if space is not None:
        try:
            if not obj.visible_in_viewport_get(space):
                return True
        except Exception:
            pass
    return False


def _vis_track_map(arm, depsgraph=None):
    """Smash vis tracks are the source of hide_viewport drivers. Read them directly
    so playback does not evaluate hide_viewport on every expression mesh.
    """
    arm = _evaluated_armature(arm, depsgraph) if depsgraph is not None else arm
    data = getattr(arm, "data", None)
    sap = getattr(data, "sub_anim_properties", None) if data is not None else None
    entries = getattr(sap, "vis_track_entries", None) if sap is not None else None
    if not entries:
        return None
    out = {}
    try:
        for entry in entries:
            name = getattr(entry, "name", "") or ""
            if name:
                out[name] = bool(getattr(entry, "value", True))
    except Exception:
        return None
    return out or None


def _iter_armature_meshes(arm):
    children = getattr(arm, "children", None)
    if not children:
        return
    stack = list(children)
    while stack:
        obj = stack.pop()
        nested = getattr(obj, "children", None)
        if nested:
            stack.extend(nested)
        if getattr(obj, "type", "") == "MESH":
            yield obj


def _gpu_mesh_hidden(obj, arm, tracks, view_layer, space):
    """Follow Outliner / vis drivers on this object. Do not use other meshes' vis tracks."""
    return _mesh_hidden(obj, view_layer, space)


def _iter_folder_smash_armatures(scene):
    """Primary, same-folder retarget duplicates, and GPU extras.

    Same-folder copies are skipped as extras (one GPU model), but their Blender
    mesh hide state still drives shared numshb visibility ids.
    """
    seen = set()

    def _take(arm):
        if arm is None or getattr(arm, "type", "") != "ARMATURE":
            return None
        try:
            key = int(arm.as_pointer())
        except Exception:
            key = id(arm)
        if key in seen:
            return None
        seen.add(key)
        return arm

    primary = _primary_smash_armature(scene)
    arm = _take(primary)
    if arm is not None:
        yield arm
    folder = _loaded_folder or _model_folder(scene)
    objects = getattr(scene, "objects", None) if scene is not None else None
    if objects and _folder_has_numshb(folder):
        for obj in objects:
            if getattr(obj, "type", "") != "ARMATURE" or not _is_smash_armature(obj):
                continue
            arm_folder = _folder_from_armature(obj)
            if arm_folder and _folders_match(arm_folder, folder):
                arm = _take(obj)
                if arm is not None:
                    yield arm
    try:
        from .smash_vp_extra import iter_extra_armatures
        for obj in iter_extra_armatures(scene, _skip_extra_armature):
            arm = _take(obj)
            if arm is not None:
                yield arm
    except Exception:
        pass


def _collect_mesh_visibility(context, space=None):
    names = []
    subindices = []
    visibles = []
    view_layer = getattr(context, "view_layer", None)
    scene = getattr(context, "scene", None)
    if scene is None:
        return names, subindices, visibles
    seen = {}

    def add_vis(key_name, sub, vis):
        if not key_name:
            return
        key = (key_name, int(sub))
        prev = seen.get(key)
        if prev is None:
            seen[key] = vis
        else:
            # Shared GPU mesh ids: a hidden retarget driver must not hide the
            # same mesh on the visible fighter after bake.
            seen[key] = 1 if (prev or vis) else 0

    def add_obj(obj, arm, tracks):
        try:
            ptr = int(obj.as_pointer())
        except Exception:
            ptr = id(obj)
        raw = obj.name or ""
        if raw.startswith("SUB_WGT_"):
            return
        extra = _extra_mesh_map.get(ptr)
        hidden = _gpu_mesh_hidden(obj, arm, tracks, view_layer, space)
        vis = 0 if hidden else 1
        if extra is not None:
            add_vis(extra[0], extra[1], vis)
            return
        if not _is_smash_mesh(obj) and arm is None:
            return
        for key_name, sub in _smash_mesh_keys(obj):
            add_vis(key_name, sub, vis)

    for arm in _iter_folder_smash_armatures(scene):
        tracks = _vis_track_map(arm)
        for obj in _iter_armature_meshes(arm):
            add_obj(obj, arm, tracks)
    if _extra_mesh_map:
        objects = getattr(scene, "objects", None)
        if objects:
            for obj in objects:
                if getattr(obj, "type", "") != "MESH":
                    continue
                try:
                    ptr = int(obj.as_pointer())
                except Exception:
                    continue
                if ptr not in _extra_mesh_map:
                    continue
                arm = getattr(obj, "parent", None)
                if arm is None or getattr(arm, "type", "") != "ARMATURE":
                    try:
                        arm = obj.find_armature()
                    except Exception:
                        arm = None
                add_obj(obj, arm, _vis_track_map(arm) if arm is not None else None)
    names = []
    subindices = []
    visibles = []
    for (key_name, sub), vis in seen.items():
        names.append(key_name)
        subindices.append(sub)
        visibles.append(vis)
    return names, subindices, visibles


def _set_mesh_visibility_data(preview, names, subindices, visibles):
    global _vis_name_keep
    encoded = [n.encode("utf-8") for n in names]
    _vis_name_keep = encoded
    if not encoded:
        return _lib.ssbh_preview_set_mesh_visibility(
            preview, None, None, None, 0
        )
    name_arr = (c_char_p * len(encoded))(*encoded)
    sub_arr = (c_uint * len(subindices))(*subindices)
    vis_arr = (c_ubyte * len(visibles))(*visibles)
    return _lib.ssbh_preview_set_mesh_visibility(
        preview,
        name_arr,
        sub_arr,
        vis_arr,
        len(encoded),
    )


def _set_mesh_visibility(preview, context):
    names, subindices, visibles = _collect_mesh_visibility(context)
    return _set_mesh_visibility_data(preview, names, subindices, visibles)


def _sync_mesh_transforms(preview, context, depsgraph):
    """Upload only changed mesh transforms; share armature inverses per draw."""
    global _last_mesh_transform_state
    setter = getattr(_lib, "ssbh_preview_set_mesh_transforms", None)
    if setter is None:
        return False
    primary = _primary_smash_armature(context.scene)
    model_indices = {ptr: i for i, ptr in enumerate(_extra_gpu_order, start=1 if _loaded_folder else 0)}
    previous = _last_mesh_transform_state or {}
    current = {}
    entries = []
    arm_inverses = {}
    for obj in context.scene.objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        arm = _mesh_armature(obj)
        model_index = 0 if arm is primary else model_indices.get(_id_key(arm))
        if model_index is None or arm is None:
            continue
        arm_key = _id_key(arm)
        if arm_key not in arm_inverses:
            eval_arm = _evaluated_armature(arm, depsgraph)
            arm_inverses[arm_key] = eval_arm.matrix_world.inverted_safe() @ _Y_UP_TO_Z_UP
        eval_obj = _evaluated_armature(obj, depsgraph)
        delta = _Z_UP_TO_Y_UP @ eval_obj.matrix_world @ arm_inverses[arm_key]
        extra = _extra_mesh_map.get(_id_key(obj))
        name, sub = extra if extra is not None else _smash_gpu_id(obj)
        key = (preview, model_index, name, sub)
        matrix = tuple(_mat4_col_major(delta))
        current[key] = matrix
        if previous.get(key) != matrix:
            entries.append((model_index, name, sub, matrix))
    if entries:
        count = len(entries)
        indices = (c_uint * count)(*(i for i, _, _, _ in entries))
        names = (c_char_p * count)(*(name.encode("utf-8") for _, name, _, _ in entries))
        subs = (c_uint * count)(*(sub for _, _, sub, _ in entries))
        matrices = (c_float * (16 * count))(*(v for _, _, _, matrix in entries for v in matrix))
        if setter(preview, indices, names, subs, matrices, count) != 0:
            raise RuntimeError(_native_error() or "Mesh transforms failed")
    _last_mesh_transform_state = current
    return bool(entries)


def _cv31_from_sap(sap, name):
    if sap is None:
        return None
    tracks = getattr(sap, "mat_tracks", None)
    if tracks is None:
        return None
    track = tracks.get(name)
    prop = track.properties.get("CustomVector31") if track else None
    if prop is None:
        return None
    cv = prop.custom_vector
    return (float(cv[0]), float(cv[1]), float(cv[2]), float(cv[3]))


def _material_track_labels(arm, track_name):
    """Exact .numatb labels first. Only add NUB_/Alp prefixes, never invent H-mesh names."""
    del arm
    labels = []
    seen = set()

    def add(label):
        label = (label or "").strip()
        if not label or label in seen:
            return
        seen.add(label)
        labels.append(label)

    add(track_name)
    raw = _smash_mesh_object_name(track_name)
    add(raw)
    if not raw.upper().startswith("NUB_") and not raw.upper().startswith("ALP_"):
        add("NUB_" + raw)
    return labels


def _evaluated_armature(arm, depsgraph):
    if arm is None or depsgraph is None:
        return arm
    try:
        return arm.evaluated_get(depsgraph)
    except Exception:
        return arm


def _collect_material_params(context, depsgraph=None):
    """SAP CustomVector / CustomFloat / CustomBool for every Smash armature.

    Reads the depsgraph-evaluated armature so material fcurves (hair SSJ colors)
    match the current frame the same way bones do.
    """
    scene = getattr(context, "scene", None)
    if scene is None:
        return []
    try:
        from .eye_rig import EYE_CTRL_BONE, EYE_TRACKS, compute_cv31
    except Exception:
        EYE_CTRL_BONE = ""
        EYE_TRACKS = ()
        compute_cv31 = None
    ssp = getattr(scene, "sub_scene_properties", None)
    live = bool(ssp is not None and getattr(ssp, "eye_look_live_preview", False))
    by_param = {}
    arms = list(_iter_folder_smash_armatures(scene))
    for obj in arms:
        eval_obj = _evaluated_armature(obj, depsgraph)
        data = getattr(eval_obj, "data", None) or getattr(obj, "data", None)
        sap = getattr(data, "sub_anim_properties", None) if data else None
        if sap is None:
            continue
        live_vals = None
        pose = getattr(eval_obj, "pose", None) or getattr(obj, "pose", None)
        pbone = pose.bones.get(EYE_CTRL_BONE) if (live and pose and EYE_CTRL_BONE) else None
        if pbone is not None and compute_cv31 is not None and ssp is not None:
            try:
                live_vals = compute_cv31(eval_obj, pbone, ssp)
            except Exception:
                live_vals = None
        for track in getattr(sap, "mat_tracks", []) or []:
            label = track.name or ""
            if not label:
                continue
            labels = _material_track_labels(obj, label)
            for prop in getattr(track, "properties", []) or []:
                name = prop.name or ""
                if not name:
                    continue
                kind = getattr(prop, "sub_type", "VECTOR")
                if kind == "VECTOR":
                    cv = prop.custom_vector
                    xyzw = (float(cv[0]), float(cv[1]), float(cv[2]), float(cv[3]))
                    if (
                        live_vals is not None
                        and name == "CustomVector31"
                        and label in EYE_TRACKS
                    ):
                        left_u, right_u, v, scale = live_vals
                        u = left_u if label == "EyeL" else right_u
                        sx, sy = xyzw[0], xyzw[1]
                        if scale is not None:
                            sx = sy = float(scale)
                        xyzw = (float(sx), float(sy), float(u), float(v))
                elif kind == "FLOAT":
                    xyzw = (float(prop.custom_float), 0.0, 0.0, 0.0)
                elif kind == "BOOL":
                    xyzw = (1.0 if prop.custom_bool else 0.0, 0.0, 0.0, 0.0)
                else:
                    continue
                bucket = by_param.setdefault(name, [])
                # Prefer the exact anim node name once; aliases are fallback only.
                bucket.append((labels[0], xyzw))
                for alias in labels[1:]:
                    bucket.append((alias, xyzw))
    return [(param, items) for param, items in by_param.items()]


def _nuanmb_name_candidates(text):
    """Action / UI names often embed the file, e.g. '… a00transformssjb.nuanmb SAP Data'."""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    seen = set()

    def add(name):
        name = (name or "").strip()
        if not name:
            return
        base = os.path.basename(name)
        for cand in (name, base):
            key = os.path.normcase(cand)
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)

    add(text)
    if not text.lower().endswith(".nuanmb"):
        add(text + ".nuanmb")
    for match in re.findall(r"[^\s/\\]+\.nuanmb", text, flags=re.IGNORECASE):
        add(match)
    return out


def _material_anim_path(context, arm=None):
    """Locate the .nuanmb that owns the current material tracks (SSBH Editor path).

    Prefer the armature's playing action (and its stored import path). The Animation
    Importer list selection is only a fallback — it often still highlights
    transformbase while the Action Editor is on transformss/ssjb, which left hair
    on the near-black CustomVector8 from base while Eye tracks still looked fine.
    """
    scene = getattr(context, "scene", None)
    ssp = getattr(scene, "sub_scene_properties", None) if scene is not None else None
    folder = ""
    if ssp is not None:
        folder = (getattr(ssp, "animation_import_folder_path", "") or "").strip()
    if arm is None:
        arm = _primary_smash_armature(scene)
    action_names = []
    for obj in (arm, getattr(arm, "data", None) if arm is not None else None):
        if obj is None:
            continue
        ad = getattr(obj, "animation_data", None)
        action = getattr(ad, "action", None) if ad is not None else None
        if action is None:
            continue
        getter = getattr(action, "get", None)
        stored = getter("sub_anim_source_path", "") if getter is not None else ""
        if stored and os.path.isfile(stored):
            return stored
        action_names.extend(_nuanmb_name_candidates(action.name or ""))
    list_names = []
    if ssp is not None:
        idx = int(getattr(ssp, "animation_import_files_index", -1) or -1)
        files = getattr(ssp, "animation_import_files", None)
        if files is not None and 0 <= idx < len(files):
            list_names.extend(_nuanmb_name_candidates(getattr(files[idx], "name", "") or ""))
    # Playing action first; importer highlight last.
    names = action_names + [n for n in list_names if n not in action_names]
    folders = []
    if folder:
        folders.append(folder)
    if arm is not None:
        model = _folder_from_armature(arm) or _loaded_folder
        if model:
            # model/body/c80 -> motion/body/c80
            guess = model.replace(os.sep + "model" + os.sep, os.sep + "motion" + os.sep)
            if guess != model:
                folders.append(guess)
            parent = os.path.dirname(model.rstrip("\\/"))
            body = os.path.join(os.path.dirname(parent), "motion", "body")
            if os.path.isdir(body):
                for name in os.listdir(body):
                    folders.append(os.path.join(body, name))
    seen = set()
    for fold in folders:
        fold = os.path.normpath(fold)
        key = os.path.normcase(fold)
        if key in seen or not os.path.isdir(fold):
            continue
        seen.add(key)
        for name in names:
            path = os.path.join(fold, os.path.basename(name))
            if os.path.isfile(path):
                return path
    return ""


def _apply_material_anim_file(preview, context, frame):
    """Drive GPU materials from the .nuanmb like SSBH Editor (not only Blender SAP)."""
    apply = getattr(_lib, "ssbh_preview_apply_material_anim", None) if _lib else None
    if apply is None:
        return False
    path = _material_anim_path(context)
    if not path:
        return False
    # Anim tracks are authored from frame 0; Blender keys sit on scene.frame_start+.
    scene = getattr(context, "scene", None)
    start = float(getattr(scene, "frame_start", 1) or 1) if scene is not None else 1.0
    anim_frame = max(0.0, float(frame) - start)
    rc = apply(preview, path.encode("utf-8"), anim_frame)
    if rc != 0:
        return False
    return True


def _set_material_params(preview, batches):
    setter = getattr(_lib, "ssbh_preview_set_custom_vector", None)
    if setter is None:
        return 0
    if not batches:
        return setter(preview, None, b"CustomVector31", None, 0)
    global _cv_name_keep
    kept = []
    for param, items in batches:
        names = [label for label, _xyzw in items]
        values = []
        for _label, xyzw in items:
            values.extend(xyzw)
        encoded = [n.encode("utf-8") for n in names]
        kept.append(encoded)
        name_arr = (c_char_p * len(encoded))(*encoded)
        val_arr = (c_float * len(values))(*values)
        rc = setter(
            preview,
            name_arr,
            param.encode("utf-8"),
            val_arr,
            len(encoded),
        )
        if rc != 0:
            _cv_name_keep = kept
            return rc
    _cv_name_keep = kept
    return 0


def _find_preview_view():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return None
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            if space is None or space.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            rv3d = getattr(space, "region_3d", None)
            if region is None or rv3d is None:
                continue
            return window, area, space, region, rv3d
    return None


def _apply_smash_color(scene):
    """Smash lighting is display-referred. AgX/Filmic wash it out."""
    if scene is None:
        return
    view = scene.view_settings
    key = _id_key(scene)
    if key not in _saved_color:
        _saved_color[key] = (
            str(view.view_transform),
            str(getattr(view, "look", "None")),
            float(getattr(view, "exposure", 0.0)),
        )
    current = str(view.view_transform)
    if current not in {"Standard", "sRGB"}:
        for name in ("Standard", "sRGB"):
            try:
                view.view_transform = name
                break
            except Exception:
                continue
    if str(getattr(view, "look", "None")) != "None":
        try:
            view.look = "None"
        except Exception:
            pass
    # Mac CPU blit + ssbh bloom reads as overexposed. Pull exposure a bit even
    # on older dylibs that still bloom.
    if sys.platform == "darwin":
        try:
            if float(getattr(view, "exposure", 0.0)) > -0.4:
                view.exposure = -0.4
        except Exception:
            pass


def _restore_smash_color(scene):
    if scene is None:
        return
    saved = _saved_color.pop(_id_key(scene), None)
    if saved is None:
        return
    if len(saved) >= 3:
        transform, look, exposure = saved[0], saved[1], saved[2]
    else:
        transform, look, exposure = saved[0], saved[1], 0.0
    view = scene.view_settings
    try:
        view.view_transform = transform
    except Exception:
        pass
    try:
        view.look = look
    except Exception:
        pass
    try:
        view.exposure = float(exposure)
    except Exception:
        pass


def _restore_engine(scene):
    if scene is None:
        return
    _restore_armature_mod_viewport()
    _restore_smash_color(scene)
    _set_smash_bones_in_front(False)
    _set_viewport_meshes_visible(True)
    if scene.render.engine != ENGINE_ID:
        return
    prev = _saved_engines.pop(_id_key(scene), "BLENDER_EEVEE")
    if prev == ENGINE_ID:
        prev = "BLENDER_EEVEE"
    try:
        scene.render.engine = prev
        return
    except Exception:
        pass
    for fallback in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = fallback
            return
        except Exception:
            continue


def _apply_engine(scene):
    if scene is None:
        return False
    key = _id_key(scene)
    current = scene.render.engine
    if key not in _saved_engines and current != ENGINE_ID:
        _saved_engines[key] = current
    try:
        scene.render.engine = ENGINE_ID
        return scene.render.engine == ENGINE_ID
    except Exception:
        return False


def _sync_checkbox(scene):
    global _ignore_update
    if scene is None:
        return
    ssp = getattr(scene, "sub_scene_properties", None)
    if ssp is None:
        return
    # Prefer the checkbox as source of truth. If the engine was left on Smash
    # while the checkbox is off, restore EEVEE/Cycles instead of flipping the UI.
    want = bool(getattr(ssp, "smash_viewport", False))
    is_smash = _engine_is_smash(scene)
    if want == is_smash:
        return
    if want and not is_smash:
        return
    if not want and is_smash:
        _restore_engine(scene)


def _on_engine_changed():
    scene = getattr(bpy.context, "scene", None)
    _sync_checkbox(scene)
    if _engine_is_smash(scene):
        _apply_smash_color(scene)
        _set_viewport_meshes_visible(True)
        _set_smash_bones_in_front(True)
        _use_rendered_shading()
        _remove_draw_handler()
        _ensure_timer()
        _sync_armature_mod_viewport()
        _tag_preview_redraw()
    else:
        _restore_smash_color(scene)
        _set_smash_bones_in_front(False)
        _set_viewport_meshes_visible(True)
        _stop_timer()
        shutdown_preview()


def _subscribe_engine():
    try:
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    except Exception:
        pass
    try:
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.RenderSettings, "engine"),
            owner=_MSGBUS_OWNER,
            args=(),
            notify=_on_engine_changed,
        )
    except Exception:
        pass


def _use_rendered_shading():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            if space is None or getattr(space, "type", "") != "VIEW_3D":
                continue
            shading = getattr(space, "shading", None)
            if shading is None:
                continue
            if getattr(shading, "type", "") == "RENDERED":
                continue
            try:
                shading.type = "RENDERED"
            except Exception:
                pass


def _is_smash_deform_mesh(obj, arm=None):
    if _is_smash_mesh(obj):
        return True
    if arm is not None:
        return _is_smash_armature(arm)
    parent = getattr(obj, "parent", None)
    if parent is not None and getattr(parent, "type", "") == "ARMATURE":
        return _is_smash_armature(parent)
    try:
        return _is_smash_armature(obj.find_armature())
    except Exception:
        return False


def _heal_smash_file_viewport_state():
    """Turn armature deform back on for Smash meshes saved while Smash Viewport was active.

    Modifier `show_viewport` and `display_type` are stored in the .blend. A file
    saved with Smash Viewport on can reopen with every character mesh stuck in
    rest pose (black T-pose) while the GPU overlay still follows the pose.

    Also heals expression / face meshes that stay visible in Solid view after
    loading vis animations — those often keep show_viewport=False from a prior
    Smash Viewport session and float at bind pose behind the head.
    """
    global _in_mod_sync
    _in_mod_sync = True
    try:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") != "MESH" or not _is_smash_deform_mesh(obj):
                continue
            for mod in getattr(obj, "modifiers", []) or []:
                if getattr(mod, "type", "") != "ARMATURE":
                    continue
                try:
                    if not bool(mod.show_viewport):
                        mod.show_viewport = True
                    if hasattr(mod, "show_in_editmode") and not bool(mod.show_in_editmode):
                        # keep editmode flag alone; only force viewport deform
                        pass
                except Exception:
                    pass
            try:
                if obj.display_type in {"WIRE", "BOUNDS"}:
                    obj.display_type = "TEXTURED"
            except Exception:
                pass
    finally:
        _in_mod_sync = False


def heal_solid_view_deform_after_anim():
    """Public hook: call after importing anims so Solid view faces follow the rig."""
    try:
        # If Smash Viewport is off, restore any leftover disabled deform mods
        if not _viewport_enabled():
            _restore_armature_mod_viewport()
            _heal_smash_file_viewport_state()
            return
        # Smash Viewport on but user is in Solid: still need CPU deform for faces
        wm = getattr(bpy.context, "window_manager", None)
        solid = False
        if wm is not None:
            for window in wm.windows:
                screen = getattr(window, "screen", None)
                if screen is None:
                    continue
                for area in screen.areas:
                    if area.type != "VIEW_3D":
                        continue
                    space = area.spaces.active
                    shading = getattr(space, "shading", None) if space else None
                    if shading is not None and getattr(shading, "type", "") != "RENDERED":
                        solid = True
                        break
        if solid:
            _heal_smash_file_viewport_state()
    except Exception:
        try:
            _heal_smash_file_viewport_state()
        except Exception:
            pass


def _restore_gpu_mesh_draw():
    """Put hide_get / hide_select / display_type back when Smash Viewport turns off."""
    global _saved_mesh_hide, _saved_mesh_select, _saved_mesh_display, _in_mod_sync
    if not _saved_mesh_hide and not _saved_mesh_select and not _saved_mesh_display:
        return
    _in_mod_sync = True
    try:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") != "MESH":
                continue
            try:
                ptr = int(obj.as_pointer())
            except Exception:
                continue
            hidden = _saved_mesh_hide.pop(ptr, None)
            if hidden is not None:
                try:
                    if bool(obj.hide_get()) != bool(hidden):
                        obj.hide_set(bool(hidden))
                except Exception:
                    pass
            selectable = _saved_mesh_select.pop(ptr, None)
            if selectable is not None:
                try:
                    want_hide_select = not bool(selectable)
                    if bool(obj.hide_select) != want_hide_select:
                        obj.hide_select = want_hide_select
                except Exception:
                    pass
            display = _saved_mesh_display.pop(ptr, None)
            if display is not None:
                try:
                    if obj.display_type != display:
                        obj.display_type = display
                except Exception:
                    pass
        _saved_mesh_hide.clear()
        _saved_mesh_select.clear()
        _saved_mesh_display.clear()
    finally:
        _in_mod_sync = False


def _apply_gpu_mesh_draw(obj, arm, gpu_ptrs):
    """Undo leftover WIRE/hide flags. Do not hide Smash meshes here.

    Hide_set on GPU-covered meshes removed hair/vis meshes and the bind source.
    """
    global _saved_mesh_hide, _saved_mesh_select, _saved_mesh_display
    del arm, gpu_ptrs
    try:
        ptr = int(obj.as_pointer())
    except Exception:
        return
    name = getattr(obj, "name", "") or ""
    if name.startswith("SUB_WGT_"):
        return
    if ptr not in _saved_mesh_hide:
        _saved_mesh_hide[ptr] = False
    if ptr not in _saved_mesh_select:
        _saved_mesh_select[ptr] = True
    if ptr not in _saved_mesh_display:
        try:
            display = str(obj.display_type)
        except Exception:
            display = "TEXTURED"
        if display in {"WIRE", "BOUNDS"}:
            display = "TEXTURED"
        _saved_mesh_display[ptr] = display
    try:
        if obj.display_type in {"WIRE", "BOUNDS"}:
            obj.display_type = _saved_mesh_display.get(ptr) or "TEXTURED"
    except Exception:
        pass


def _undo_old_mesh_filter():
    _set_viewport_meshes_visible(True)


def _restore_armature_mod_viewport():
    global _mod_show_saved, _in_mod_sync
    _restore_gpu_mesh_draw()
    if not _mod_show_saved:
        return
    _in_mod_sync = True
    try:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") != "MESH":
                continue
            try:
                ptr = int(obj.as_pointer())
            except Exception:
                continue
            for mod in getattr(obj, "modifiers", []) or []:
                if getattr(mod, "type", "") != "ARMATURE":
                    continue
                saved = _mod_show_saved.get((ptr, mod.name))
                if saved is None:
                    continue
                try:
                    if bool(mod.show_viewport) != bool(saved):
                        mod.show_viewport = bool(saved)
                except Exception:
                    pass
    finally:
        _in_mod_sync = False
        _mod_show_saved = {}


def _schedule_mesh_draw_sync():
    """Apply hide/wire/modifier overrides outside view_draw so Ctrl+Z stays valid."""
    global _mesh_sync_timer
    if _mesh_sync_timer:
        return

    def _run():
        global _mesh_sync_timer
        _mesh_sync_timer = False
        if _viewport_enabled():
            _sync_armature_mod_viewport()
        return None

    try:
        bpy.app.timers.register(_run, first_interval=0.0)
        _mesh_sync_timer = True
    except Exception:
        _sync_armature_mod_viewport()


def _sync_armature_mod_viewport(context=None):
    """CPU-skin every Smash mesh that is still drawn; skip deform only when hidden.

    Turning the armature modifier off while the mesh stays visible is what leaves
    a black T-pose next to the GPU character. Hidden meshes (Outliner eye, vis
    tracks, GPU duplicates) skip deform so playback stays cheap.
    """
    global _mod_show_saved, _in_mod_sync
    if _in_mod_sync or _ignore_update:
        return
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not _viewport_enabled(scene):
        _restore_armature_mod_viewport()
        return
    view_layer = getattr(context, "view_layer", None)
    space = _preview_space(context)
    objects = getattr(scene, "objects", None)
    if not objects:
        return
    gpu_ptrs = _gpu_pose_armature_ptrs(scene)
    _in_mod_sync = True
    try:
        for obj in objects:
            if getattr(obj, "type", "") != "MESH":
                continue
            name = obj.name or ""
            arm = None
            try:
                arm = getattr(obj, "parent", None)
                if arm is None or getattr(arm, "type", "") != "ARMATURE":
                    arm = _mesh_armature(obj)
            except Exception:
                arm = None
            if not name.startswith("SUB_WGT_"):
                _apply_gpu_mesh_draw(obj, arm, gpu_ptrs)
            try:
                ptr = int(obj.as_pointer())
            except Exception:
                continue
            if name.startswith("SUB_WGT_"):
                draw = False
            elif _mesh_hidden(obj, view_layer, space):
                draw = False
            else:
                draw = True
            for mod in getattr(obj, "modifiers", []) or []:
                if getattr(mod, "type", "") != "ARMATURE":
                    continue
                key = (ptr, mod.name)
                if key not in _mod_show_saved:
                    if _is_smash_deform_mesh(obj, arm):
                        _mod_show_saved[key] = True
                    else:
                        _mod_show_saved[key] = bool(getattr(mod, "show_viewport", True))
                want = bool(draw) if _is_smash_deform_mesh(obj, arm) else bool(
                    _mod_show_saved.get(key, True) and draw
                )
                try:
                    if bool(mod.show_viewport) != want:
                        mod.show_viewport = want
                except Exception:
                    pass
    finally:
        _in_mod_sync = False


def _set_smash_bones_in_front(enabled):
    """Keep Smash armatures drawing in front so overlay bones sit on the mesh."""
    global _saved_in_front
    if not enabled:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") != "ARMATURE":
                continue
            try:
                key = int(obj.as_pointer())
            except Exception:
                continue
            saved = _saved_in_front.pop(key, None)
            if saved is None:
                continue
            try:
                obj.show_in_front = saved
            except Exception:
                pass
        _saved_in_front.clear()
        return

    scene = getattr(bpy.context, "scene", None)
    objects = getattr(scene, "objects", None) if scene is not None else None
    if not objects:
        return
    extra = set(_extra_gpu_order)
    extra |= _extra_smash_arms
    extra |= _extra_blender_smash
    for obj in objects:
        if getattr(obj, "type", "") != "ARMATURE":
            continue
        try:
            key = int(obj.as_pointer())
        except Exception:
            continue
        if not (_is_primary_smash_armature(obj) or key in extra):
            continue
        if key not in _saved_in_front:
            try:
                _saved_in_front[key] = bool(obj.show_in_front)
            except Exception:
                _saved_in_front[key] = False
        try:
            if not obj.show_in_front:
                obj.show_in_front = True
        except Exception:
            pass


def _set_viewport_meshes_visible(visible=True):
    """Smash meshes stay in the viewport so they stay selectable and fill GPU holes.

    Never hide them for speed. That makes the character see-through and unpickable.
    """
    global _saved_mesh_filter
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        _saved_mesh_filter.clear()
        return
    del visible  # always on
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            if space is None or getattr(space, "type", "") != "VIEW_3D":
                continue
            try:
                if not space.show_object_viewport_mesh:
                    space.show_object_viewport_mesh = True
            except Exception:
                pass
    _saved_mesh_filter.clear()


def _set_overlay_grids_visible(_visible=True):
    """Leave Blender's floor, grid, and ortho grid alone."""
    return


def _enable_smash_viewport(scene):
    _heal_smash_file_viewport_state()
    _sync_bg_picker_from_blender(scene)
    load_native_library()
    _ensure_preview()
    _apply_engine(scene)
    _apply_smash_color(scene)
    _set_viewport_meshes_visible(True)
    _set_smash_bones_in_front(True)
    _use_rendered_shading()
    _remove_draw_handler()
    _ensure_timer()
    _sync_armature_mod_viewport()
    _tag_preview_redraw()


def _tag_preview_redraw():
    # Never force-redraw when Smash Viewport is off — that restarts EEVEE/Cycles
    # sampling in every Rendered 3D View.
    if not _viewport_enabled():
        return
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            shading = getattr(space, "shading", None) if space is not None else None
            if shading is None or getattr(shading, "type", "") != "RENDERED":
                continue
            tagged = False
            for region in area.regions:
                if region.type == "WINDOW":
                    region.tag_redraw()
                    tagged = True
            if not tagged:
                area.tag_redraw()


def _using_gpu():
    if _lib is None or _preview is None:
        return False
    try:
        return int(_lib.ssbh_preview_using_gpu(_preview)) == 1
    except Exception:
        return False


def _apply_viewport_look(preview, scene):
    global _applied_bg, _applied_light, _applied_light_frame, _last_error
    if preview is None or _lib is None or scene is None:
        return
    setter = getattr(_lib, "ssbh_preview_set_clear_color", None)
    if setter is not None:
        clear = (0.0, 0.0, 0.0, 0.0)
        if _applied_bg != clear:
            setter(preview, 0.0, 0.0, 0.0, 0.0)
            _applied_bg = clear
    ssp = getattr(scene, "sub_scene_properties", None)
    frame = 0.0
    try:
        frame = float(scene.frame_current_final)
    except Exception:
        frame = float(getattr(scene, "frame_current", 0))
    frame_fn = getattr(_lib, "ssbh_preview_set_lighting_frame", None)
    if frame_fn is not None:
        if _applied_light_frame is None or abs(frame - _applied_light_frame) > 1e-4:
            frame_fn(preview, float(frame))
            _applied_light_frame = frame
    path = ""
    if ssp is not None:
        path = (getattr(ssp, "smash_vp_light_path", "") or "").strip()
    load_fn = getattr(_lib, "ssbh_preview_load_lighting", None)
    clear_fn = getattr(_lib, "ssbh_preview_clear_lighting", None)
    if path == (_applied_light or ""):
        return
    if path and os.path.isfile(path) and load_fn is not None:
        if load_fn(preview, path.encode("utf-8")) == 0:
            _applied_light = path
        else:
            _last_error = _native_error() or "Failed to load stage lights"
    elif not path and clear_fn is not None:
        clear_fn(preview)
        _applied_light = ""


def _preview_view_from_context(context=None):
    context = context or bpy.context
    space = getattr(context, "space_data", None)
    region = getattr(context, "region", None)
    rv3d = getattr(context, "region_data", None)
    if (
        space is not None
        and getattr(space, "type", "") == "VIEW_3D"
        and region is not None
        and getattr(region, "type", "") == "WINDOW"
        and rv3d is not None
    ):
        return space, region, rv3d
    found = _find_preview_view()
    if found is None:
        return None
    return found[2], found[3], found[4]


def _camera_key(rv3d, width, height, scale):
    vm = rv3d.view_matrix
    return (
        int(width),
        int(height),
        round(scale, 4),
        str(getattr(rv3d, "view_perspective", "")),
        # Include the complete view rotation. Sampling only vm[0][0] misses
        # valid orbit changes and can leave the Smash render one camera move
        # behind Blender's overlays.
        round(vm[0][0], 5),
        round(vm[0][1], 5),
        round(vm[0][2], 5),
        round(vm[1][0], 5),
        round(vm[1][1], 5),
        round(vm[1][2], 5),
        round(vm[2][0], 5),
        round(vm[2][1], 5),
        round(vm[2][2], 5),
        round(vm[0][3], 4),
        round(vm[1][3], 4),
        round(vm[2][3], 4),
        round(float(getattr(rv3d, "view_distance", 0.0)), 4),
        round(float(getattr(rv3d, "view_camera_zoom", 0.0)), 4),
    )


def _prepare_preview(context=None, depsgraph=None):
    global _last_status, _last_error, _pixel_size, _last_tick_key
    global _last_pose_fp, _last_pose_arm_fp, _last_synced_arm_fp, _last_vis_state, _last_cv31_state
    global _last_cam_key, _last_frame
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not _viewport_enabled(scene):
        return None
    found = _preview_view_from_context(context)
    if found is None:
        return None
    space, region, rv3d = found
    dest_w = max(int(region.width), 1)
    dest_h = max(int(region.height), 1)
    scale = 1.0
    try:
        scale = float(context.preferences.system.ui_scale)
    except Exception:
        pass
    width = dest_w
    height = dest_h
    try:
        frame = float(scene.frame_current_final)
    except Exception:
        frame = float(getattr(scene, "frame_current", 0))
    playing = bool(getattr(getattr(context, "screen", None), "is_animation_playing", False))
    frame_changed = _last_frame is None or abs(frame - _last_frame) > 1e-4
    size_changed = _pixel_size != (width, height) or _last_tick_key is None
    loaded = _preview is not None and bool(_loaded_folder or _extra_ok)
    try:
        obj_count = len(scene.objects)
    except Exception:
        obj_count = 0
    count_changed = _smash_arm_count is None or obj_count != _smash_arm_count
    # Loaded previews skip scene/folder walks until the object list or size changes.
    skip_scene_walk = loaded and not size_changed and not count_changed
    if not skip_scene_walk:
        _refresh_smash_arm_cache(scene)
        folder = _model_folder(scene)
        has_smash = _folder_has_numshb(folder)
        if _loaded_folder and (not has_smash or folder != _loaded_folder):
            shutdown_preview()
        has_extra = _has_extra_candidates(scene)
        if not has_smash and not has_extra:
            if _preview is not None or _loaded_folder:
                shutdown_preview()
            return None
        preview = _ensure_preview()
        if not preview:
            return None
        smash_ok = _ensure_model(scene) if has_smash else False
        extra_ok = False
        if has_extra:
            try:
                extra_ok = _ensure_extra_models(context)
            except BaseException as exc:
                _last_error = f"GPU extra preview: {exc}"
        elif _extra_ok or _extra_mesh_map:
            _clear_extra_models()
        if not smash_ok and not extra_ok:
            return None
    else:
        preview = _preview
        if not preview:
            return None
    needs_gpu = size_changed
    if _sync_mesh_transforms(preview, context, depsgraph):
        needs_gpu = True
    # Object visibility and retarget-pair state can change without changing the
    # scene object count. Always compare the cheap per-model state so a hidden
    # native model cannot remain frozen behind the current character.
    if _sync_gpu_model_visibility(context):
        needs_gpu = True
    if size_changed:
        if _lib.ssbh_preview_resize(preview, width, height, scale) != 0:
            _last_error = _native_error() or "resize failed"
            return None
        _last_cam_key = None
    cam_key = _camera_key(rv3d, width, height, scale)
    cam_changed = cam_key != _last_cam_key
    if cam_changed:
        if _set_camera(preview, rv3d, region, width, height, scale, space) != 0:
            _last_error = _native_error() or "camera failed"
            return None
        _last_cam_key = cam_key
        needs_gpu = True
    camera_only = (
        cam_changed
        and not playing
        and not frame_changed
        and not size_changed
        and _last_pose_fp is not None
    )
    pose_arm = _primary_smash_armature(scene)
    try:
        pose_arm_fp = int(pose_arm.as_pointer()) if pose_arm is not None else 0
    except Exception:
        pose_arm_fp = 0
    if not camera_only and pose_arm_fp != _last_synced_arm_fp:
        _last_synced_arm_fp = pose_arm_fp
        _schedule_mesh_draw_sync()
    # Blender may ask the render engine to draw several times for one timeline
    # frame. Dirty handlers below cover pose edits and Action changes, so avoid
    # rescanning armatures/material tracks on every duplicate playback redraw.
    need_pose = (not camera_only) and (
        frame_changed or _last_pose_fp is None or pose_arm_fp != _last_pose_arm_fp
    )
    need_vis_mat = (
        frame_changed
        or _last_vis_state is None
        or _last_cv31_state is None
    )
    if need_pose:
        pose_fp = _pose_fingerprint(depsgraph, context)
        if pose_fp != _last_pose_fp or pose_arm_fp != _last_pose_arm_fp:
            if _set_bones(preview, depsgraph, context) != 0:
                _last_error = _native_error() or "bones failed"
                return None
            _last_pose_fp = pose_fp
            _last_pose_arm_fp = pose_arm_fp
            needs_gpu = True
    if need_vis_mat or not playing:
        vis_names, vis_subs, vis_vals = _collect_mesh_visibility(context, space)
        vis_state = tuple(zip(vis_names, vis_subs, vis_vals))
        if vis_state != _last_vis_state:
            if _set_mesh_visibility_data(preview, vis_names, vis_subs, vis_vals) != 0:
                _last_error = _native_error() or "visibility failed"
                return None
            _last_vis_state = vis_state
            needs_gpu = True
    if need_vis_mat:
        cv_batches = _collect_material_params(context, depsgraph)
        cv_state = tuple(
            (param, tuple((label, tuple(round(v, 5) for v in xyzw)) for label, xyzw in items))
            for param, items in cv_batches
        )
        anim_path = _material_anim_path(context)
        mat_state = (anim_path, round(frame, 4), cv_state)
        if mat_state != _last_cv31_state:
            # File anim matches SSBH Editor; SAP overlays keep Blender-evaluated
            # hair/eye CustomVectors after retarget bake relinks.
            if anim_path:
                _apply_material_anim_file(preview, context, frame)
            if cv_batches:
                if _set_material_params(preview, cv_batches) != 0:
                    _last_error = _native_error() or "materials failed"
                    return None
            elif not anim_path and _last_cv31_state is not None:
                if _set_material_params(preview, []) != 0:
                    _last_error = _native_error() or "materials failed"
                    return None
            _last_cv31_state = mat_state
            needs_gpu = True
    _apply_viewport_look(preview, scene)
    if frame_changed:
        _last_frame = frame
        needs_gpu = True
    _last_tick_key = (width, height)
    _pixel_size = (width, height)
    return preview, width, height, size_changed, region, rv3d, needs_gpu


def _cpu_render(preview, width, height, force=False, wait=False):
    global _last_status, _last_error, _pixels, _pixel_size, _pixel_version
    needed = width * height * 4
    if needed < 4:
        return False
    if len(_pixels) != needed:
        _pixels = bytearray(needed)
        force = True
    out_w = c_uint(0)
    out_h = c_uint(0)
    buf = (ctypes.c_uint8 * needed).from_buffer(_pixels)
    if wait and hasattr(_lib, "ssbh_preview_render_wait"):
        native_fn = _lib.ssbh_preview_render_wait
    else:
        native_fn = _lib.ssbh_preview_render if force else _lib.ssbh_preview_poll
    rc = native_fn(
        preview,
        ctypes.addressof(buf),
        needed,
        ctypes.byref(out_w),
        ctypes.byref(out_h),
    )
    if rc < 0:
        _last_error = _native_error() or "render failed"
        return False
    _pixel_size = (int(out_w.value or width), int(out_h.value or height))
    if rc == 0:
        _pixel_version += 1
    status = f"{_pixel_size[0]}x{_pixel_size[1]} ssbh_wgpu CPU"
    if _last_status != status:
        _last_status = status
    return True


def capture_rgba_frame(
    context=None,
    transparent=True,
    restore_background=True,
    flush=True,
    wait=False,
):
    """RGBA screenshot of Smash Viewport. Transparent pixels have alpha 0.

    Returns (width, height, bytes) or None if Smash Viewport is not drawing.
    """
    global _applied_bg, _pixels, _pixel_size
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not _viewport_enabled(scene):
        return None
    prepared = _prepare_preview(context)
    if prepared is None:
        return None
    preview, width, height = prepared[0], prepared[1], prepared[2]
    setter = getattr(_lib, "ssbh_preview_set_clear_color", None) if _lib else None
    ssp = getattr(scene, "sub_scene_properties", None)
    color = (0.0, 0.0, 0.0)
    if ssp is not None:
        bg = getattr(ssp, "smash_vp_bg_color", None)
        if bg is not None and len(bg) >= 3:
            color = (float(bg[0]), float(bg[1]), float(bg[2]))
    if transparent and setter is not None:
        setter(preview, 0.0, 0.0, 0.0, 0.0)
        _applied_bg = (0.0, 0.0, 0.0, 0.0)
    if not _cpu_render(preview, width, height, force=True, wait=wait):
        if restore_background:
            restore_opaque_background(context)
        return None
    if flush and not wait:
        _cpu_render(preview, width, height, force=True)
    w, h = int(_pixel_size[0] or width), int(_pixel_size[1] or height)
    needed = max(0, w * h * 4)
    data = bytes(_pixels[:needed]) if needed else b""
    if restore_background:
        restore_opaque_background(context)
    if not data:
        return None
    return w, h, data


def restore_opaque_background(context=None):
    global _applied_bg
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if _lib is None or _preview is None or scene is None:
        return
    setter = getattr(_lib, "ssbh_preview_set_clear_color", None)
    if setter is None:
        return
    setter(_preview, 0.0, 0.0, 0.0, 0.0)
    _applied_bg = (0.0, 0.0, 0.0, 0.0)


def _tick():
    prepared = _prepare_preview()
    if prepared is None:
        return
    preview, width, height, size_changed, _region, _rv3d, _needs_gpu = prepared
    _cpu_render(preview, width, height, force=size_changed)
    _tag_preview_redraw()


def _preview_timer():
    global _timer_running
    if not _viewport_enabled():
        shutdown_preview()
        _timer_running = False
        return None
    return None


@persistent
def _on_scene_redraw(*args):
    if _in_mod_sync or _ignore_update:
        return
    if not _viewport_enabled():
        return
    # Inspect only Blender's already-prepared update list. A full scene walk on
    # every depsgraph notification is much slower than SSBH Editor, while
    # ignoring updates entirely leaves same-frame Action/constraint changes
    # stale in the native renderer.
    depsgraph = next((arg for arg in reversed(args) if hasattr(arg, "updates")), None)
    if depsgraph is None:
        return
    pose_dirty = False
    channels_dirty = False
    try:
        updates = depsgraph.updates
    except Exception:
        return
    for update in updates:
        updated = getattr(update, "id", None)
        updated = getattr(updated, "original", updated)
        if updated is None:
            continue
        if isinstance(updated, bpy.types.Action):
            pose_dirty = True
            channels_dirty = True
            break
        if isinstance(updated, bpy.types.Armature):
            pose_dirty = True
            channels_dirty = True
            continue
        if isinstance(updated, bpy.types.Object):
            obj_type = getattr(updated, "type", "")
            if obj_type == "ARMATURE":
                pose_dirty = True
                channels_dirty = True
            elif obj_type == "MESH":
                if getattr(update, "is_updated_transform", False):
                    pose_dirty = True
                else:
                    playing = False
                    try:
                        playing = bool(
                            getattr(getattr(bpy.context, "screen", None), "is_animation_playing", False)
                        )
                    except Exception:
                        playing = False
                    # Vis drivers spam mesh updates every frame. Playback already
                    # refreshes vis on frame change; only dirty when paused.
                    if not playing:
                        channels_dirty = True
            continue
    if not pose_dirty and not channels_dirty:
        return
    global _last_pose_fp, _last_vis_state, _last_cv31_state
    if pose_dirty:
        _last_pose_fp = None
    if channels_dirty:
        _last_vis_state = None
        _last_cv31_state = None
    # Do NOT tag_redraw here. Mesh-modifier sync writes from view_draw already
    # fire depsgraph updates; tagging Rendered views turns that into an endless
    # sample-restart / flicker loop. Frame change and Blender's own redraws
    # are enough to refresh the Smash present.


@persistent
def _on_frame_change(*_args):
    if not _viewport_enabled():
        return
    _tag_preview_redraw()


def _ensure_timer():
    global _timer_running
    if _timer_running:
        return
    if _on_scene_redraw not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_scene_redraw)
    if _on_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change)
    _timer_running = True


def _stop_timer():
    global _timer_running
    _timer_running = False
    for coll in (
        bpy.app.handlers.depsgraph_update_post,
        bpy.app.handlers.frame_change_post,
    ):
        for fn in (_on_scene_redraw, _on_frame_change):
            try:
                coll.remove(fn)
            except Exception:
                pass
    try:
        if bpy.app.timers.is_registered(_preview_timer):
            bpy.app.timers.unregister(_preview_timer)
    except Exception:
        pass


def _blit_rgba(width, height, pixels, dest_w=None, dest_h=None):
    import gpu
    import numpy as np
    from gpu_extras.presets import draw_texture_2d

    global _float_pixels, _blit_tex, _blit_tex_key, _blit_ubyte_ok
    needed = width * height * 4
    if width < 1 or height < 1 or len(pixels) < needed:
        return
    dest_w = max(int(dest_w or width), 1)
    dest_h = max(int(dest_h or height), 1)
    tex_key = (_pixel_version, width, height)
    texture = _blit_tex
    if texture is None or _blit_tex_key != tex_key:
        src = np.frombuffer(pixels, dtype=np.uint8, count=needed)
        src = src.copy()
        src[3::4] = 255
        texture = None
        if _blit_ubyte_ok:
            try:
                ubyte = gpu.types.Buffer("UBYTE", needed, src)
                texture = gpu.types.GPUTexture((width, height), format="RGBA8", data=ubyte)
            except Exception:
                _blit_ubyte_ok = False
                texture = None
        if texture is None:
            if _float_pixels is None or _float_pixels.size != needed:
                _float_pixels = np.empty(needed, dtype=np.float32)
            np.multiply(src, np.float32(1.0 / 255.0), out=_float_pixels)
            buf = gpu.types.Buffer("FLOAT", needed, _float_pixels)
            texture = gpu.types.GPUTexture((width, height), format="RGBA8", data=buf)
        _blit_tex = texture
        _blit_tex_key = tex_key
    prev_blend = "NONE"
    try:
        prev_blend = gpu.state.blend_get()
    except Exception:
        pass
    gpu.state.depth_test_set("NONE")
    gpu.state.blend_set("NONE")
    identity = Matrix.Identity(4)
    proj = Matrix.Identity(4)
    proj[0][0] = 2.0 / float(dest_w)
    proj[1][1] = -2.0 / float(dest_h)
    proj[0][3] = -1.0
    proj[1][3] = 1.0
    try:
        with gpu.matrix.push_pop():
            gpu.matrix.load_projection_matrix(proj)
            try:
                gpu.matrix.load_matrix(identity)
            except Exception:
                gpu.matrix.load_identity()
            _draw_texture_2d_compat(texture, (0, 0), dest_w, dest_h)
    finally:
        try:
            gpu.state.blend_set(prev_blend or "NONE")
        except Exception:
            gpu.state.blend_set("NONE")


def update_smash_viewport(self, context):
    if _ignore_update:
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    enabled = bool(getattr(self, "smash_viewport", False))
    if enabled:
        if not native_plugin_path():
            global _last_error
            _last_error = (
                "Smash Viewport plugin not found. See native/README.md."
            )
        _enable_smash_viewport(scene)
        return
    # Fully tear down so EEVEE/Cycles Rendered shading can finish sampling.
    _stop_timer()
    if scene.render.engine == ENGINE_ID:
        _restore_engine(scene)
    _restore_armature_mod_viewport()
    _set_smash_bones_in_front(False)
    _set_viewport_meshes_visible(True)
    _restore_smash_color(scene)
    shutdown_preview()


def update_smash_vp_background(self, context):
    global _bg_picker_touched
    if _ignore_update:
        return
    _bg_picker_touched = True
    scene = getattr(context, "scene", None) if context is not None else None
    color = getattr(self, "smash_vp_bg_color", None)
    _apply_blender_background(scene, color)
    _tag_preview_redraw()


def update_smash_vp_lighting(_self, _context):
    global _applied_light
    _applied_light = None
    _tag_preview_redraw()


def apply_lighting_file(path, scene=None, force=False):
    """Load a lighting .nuanmb into Smash Viewport. Safe to call from Stage Tools."""
    global _applied_light, _applied_light_frame, _last_error
    scene = scene or getattr(bpy.context, "scene", None)
    path = (path or "").strip()
    ssp = getattr(scene, "sub_scene_properties", None) if scene is not None else None
    if ssp is not None and path:
        if ssp.smash_vp_light_path != path:
            ssp.smash_vp_light_path = path
        elif force:
            _applied_light = None
    if force:
        _applied_light = None
    preview = _preview
    load_fn = getattr(_lib, "ssbh_preview_load_lighting", None) if _lib else None
    if preview and path and os.path.isfile(path) and load_fn is not None:
        if load_fn(preview, path.encode("utf-8")) == 0:
            _applied_light = path
            frame_fn = getattr(_lib, "ssbh_preview_set_lighting_frame", None)
            if frame_fn is not None and scene is not None:
                try:
                    frame = float(scene.frame_current_final)
                except Exception:
                    frame = float(getattr(scene, "frame_current", 0))
                frame_fn(preview, float(frame))
                _applied_light_frame = frame
        else:
            _last_error = _native_error() or "Failed to load stage lights"
    _tag_preview_redraw()


def draw_smash_viewport_ui(layout, context):
    ssp = getattr(context.scene, "sub_scene_properties", None)
    if ssp is None:
        return
    box = layout.column()
    box.prop(ssp, "smash_viewport", text="Smash Viewport Engine")
    path = native_plugin_path()
    if path:
        box.label(text="Plugin: " + os.path.basename(path), icon="CHECKMARK")
    else:
        box.label(text="Plugin not built. See native/README.md", icon="ERROR")
    folder = _model_folder(context.scene)
    if not _scene_has_smash_model(context.scene):
        if _has_extra_candidates(context.scene):
            box.label(text="Folder-less models use the Smash engine", icon="INFO")
            box.label(text="(default / untextured materials).")
        else:
            box.label(text="No Smash model folder linked", icon="INFO")
    elif folder:
        box.label(text=os.path.basename(folder.rstrip("\\/")))
        if _has_extra_candidates(context.scene):
            box.label(text="Other characters use Smash engine defaults.")
    else:
        box.label(text="Old import: relink the .numshb folder", icon="ERROR")
    box.operator(
        SUB_OP_smash_vp_shade_setup.bl_idname,
        text="Reload Smash Model",
        icon="FILE_REFRESH",
    )
    box.operator(
        SUB_OP_smash_vp_relink_model.bl_idname,
        text="Relink Smash Model Folder",
        icon="FILEBROWSER",
    )
    box.label(text="Turns on Rendered shading so Smash is visible.")
    box.label(text="Uses Standard view transform (AgX washes Smash).")
    if _last_status:
        box.label(text=_last_status)
    if _last_error:
        box.label(text=_last_error, icon="ERROR")


def _unregister_old_classes():
    for name in (
        "SUB_OP_close_smash_viewport",
        "SUB_OP_open_smash_viewport",
        "SUB_OP_smash_vp_load_lighting",
        "SUB_OP_smash_vp_relink_model",
        "SUB_OP_smash_vp_shade_setup",
        "SUB_OP_smash_vp_reset_lighting",
        "RENDER_PT_smash_viewport",
        "SUB_RenderEngine_smash_viewport",
    ):
        cls = getattr(bpy.types, name, None)
        if cls is None:
            continue
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


def _iter_view3d_shading():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            shading = getattr(space, "shading", None) if space is not None else None
            if shading is not None:
                yield shading


def _read_blender_background(scene=None):
    """Current 3D View / world / theme backdrop. Used so import does not force black."""
    scene = scene or getattr(bpy.context, "scene", None)
    for shading in _iter_view3d_shading() or ():
        try:
            if str(getattr(shading, "background_type", "")) == "VIEWPORT":
                color = getattr(shading, "background_color", None)
                if color is not None and len(color) >= 3:
                    return (float(color[0]), float(color[1]), float(color[2]))
        except Exception:
            pass
    world = getattr(scene, "world", None) if scene is not None else None
    if world is not None:
        try:
            if world.use_nodes and world.node_tree:
                for node in world.node_tree.nodes:
                    if getattr(node, "type", "") == "BACKGROUND":
                        color = node.inputs[0].default_value
                        return (float(color[0]), float(color[1]), float(color[2]))
        except Exception:
            pass
        try:
            color = getattr(world, "color", None)
            if color is not None and len(color) >= 3:
                return (float(color[0]), float(color[1]), float(color[2]))
        except Exception:
            pass
    try:
        theme = bpy.context.preferences.themes[0]
        grad = theme.view_3d.space.gradients
        color = getattr(grad, "high_gradient", None) or getattr(grad, "gradient", None)
        if color is not None and len(color) >= 3:
            return (float(color[0]), float(color[1]), float(color[2]))
    except Exception:
        pass
    return (0.224, 0.224, 0.224)


def _apply_blender_background(scene, color):
    """Drive Blender's real backdrop from the Smash Viewport Background picker."""
    if scene is None or color is None or len(color) < 3:
        return
    rgb = (float(color[0]), float(color[1]), float(color[2]))
    for shading in _iter_view3d_shading() or ():
        try:
            if str(getattr(shading, "background_type", "")) == "VIEWPORT":
                shading.background_color = rgb
        except Exception:
            pass
    world = getattr(scene, "world", None)
    if world is None:
        try:
            world = bpy.data.worlds.new("World")
            scene.world = world
        except Exception:
            world = None
    if world is None:
        return
    try:
        world.color = rgb
    except Exception:
        pass
    try:
        if world.use_nodes and world.node_tree:
            for node in world.node_tree.nodes:
                if getattr(node, "type", "") == "BACKGROUND":
                    node.inputs[0].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
    except Exception:
        pass


def _sync_bg_picker_from_blender(scene):
    """Match the picker to Blender's backdrop unless the user already chose a color."""
    global _ignore_update, _bg_picker_touched
    if _bg_picker_touched:
        return
    ssp = getattr(scene, "sub_scene_properties", None) if scene is not None else None
    if ssp is None:
        return
    stored = getattr(ssp, "smash_vp_bg_color", None)
    if stored is not None and len(stored) >= 3:
        if float(stored[0]) > 0.001 or float(stored[1]) > 0.001 or float(stored[2]) > 0.001:
            _bg_picker_touched = True
            return
    current = _read_blender_background(scene)
    _ignore_update = True
    try:
        ssp.smash_vp_bg_color = current
    except Exception:
        pass
    _ignore_update = False


def _bg_rgba(scene=None):
    color = (0.0, 0.0, 0.0, 1.0)
    scene = scene or getattr(bpy.context, "scene", None)
    ssp = getattr(scene, "sub_scene_properties", None) if scene is not None else None
    if ssp is not None:
        bg = getattr(ssp, "smash_vp_bg_color", None)
        if bg is not None and len(bg) >= 3:
            color = (float(bg[0]), float(bg[1]), float(bg[2]), 1.0)
    return color


def _viewport_pixel_size(context=None):
    region = getattr(context, "region", None) if context is not None else None
    if region is None:
        region = getattr(bpy.context, "region", None)
    if region is not None:
        try:
            return max(int(region.width), 1), max(int(region.height), 1)
        except Exception:
            pass
    try:
        import gpu
        _x, _y, w, h = gpu.state.viewport_get()
        return max(int(w), 1), max(int(h), 1)
    except Exception:
        return 1, 1


def _fill_viewport_color(dest_w=None, dest_h=None, color=None):
    """Paint the 3D region with the Background picker color."""
    global _fill_batch, _fill_batch_size
    import gpu
    from gpu_extras.batch import batch_for_shader

    if dest_w is None or dest_h is None:
        dest_w, dest_h = _viewport_pixel_size()
    dest_w = max(int(dest_w), 1)
    dest_h = max(int(dest_h), 1)
    if color is None:
        color = _bg_rgba()
    try:
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    except Exception:
        shader = gpu.shader.from_builtin("2D_UNIFORM_COLOR")
    size = (dest_w, dest_h)
    if _fill_batch is None or _fill_batch_size != size:
        _fill_batch = batch_for_shader(
            shader,
            "TRI_FAN",
            {
                "pos": (
                    (0.0, 0.0),
                    (float(dest_w), 0.0),
                    (float(dest_w), float(dest_h)),
                    (0.0, float(dest_h)),
                )
            },
        )
        _fill_batch_size = size
    batch = _fill_batch
    prev_blend = "NONE"
    prev_depth = "NONE"
    try:
        prev_blend = gpu.state.blend_get()
    except Exception:
        pass
    try:
        prev_depth = gpu.state.depth_test_get()
    except Exception:
        pass
    identity = Matrix.Identity(4)
    proj = Matrix.Identity(4)
    proj[0][0] = 2.0 / float(dest_w)
    proj[1][1] = -2.0 / float(dest_h)
    proj[0][3] = -1.0
    proj[1][3] = 1.0
    try:
        gpu.state.depth_test_set("NONE")
        try:
            gpu.state.depth_mask_set(False)
        except Exception:
            pass
        gpu.state.blend_set("NONE")
        with gpu.matrix.push_pop():
            gpu.matrix.load_projection_matrix(proj)
            try:
                gpu.matrix.load_matrix(identity)
            except Exception:
                gpu.matrix.load_identity()
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
    except Exception:
        pass
    finally:
        try:
            gpu.state.blend_set(prev_blend or "NONE")
        except Exception:
            pass
        try:
            gpu.state.depth_mask_set(True)
        except Exception:
            pass
        try:
            gpu.state.depth_test_set(prev_depth or "NONE")
        except Exception:
            pass


def _shade_shader_get():
    global _shade_shader
    if _shade_shader is not None:
        return _shade_shader
    import gpu
    try:
        iface = gpu.types.GPUStageInterfaceInfo("sub_smash_shade")
        iface.smooth("VEC3", "v_normal")
        iface.smooth("VEC2", "v_uv")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant("MAT4", "ViewProjectionMatrix")
        info.push_constant("VEC3", "light_dir")
        info.push_constant("VEC3", "tint")
        info.push_constant("FLOAT", "use_tex")
        info.sampler(0, "FLOAT_2D", "image")
        info.vertex_in(0, "VEC3", "pos")
        info.vertex_in(1, "VEC3", "nor")
        info.vertex_in(2, "VEC2", "uv")
        info.vertex_out(iface)
        info.fragment_out(0, "VEC4", "fragColor")
        info.vertex_source(
            "void main()"
            "{"
            "  v_normal = nor;"
            "  v_uv = uv;"
            "  gl_Position = ViewProjectionMatrix * vec4(pos, 1.0);"
            "}"
        )
        info.fragment_source(
            "void main()"
            "{"
            "  vec3 base = tint;"
            "  if (use_tex > 0.5)"
            "    base = texture(image, v_uv).rgb;"
            "  float ndotl = max(0.18, abs(dot(normalize(v_normal), light_dir)));"
            "  fragColor = vec4(base * ndotl, 1.0);"
            "}"
        )
        _shade_shader = gpu.shader.create_from_info(info)
        return _shade_shader
    except Exception:
        _shade_shader = None
    for name in ("SMOOTH_COLOR", "3D_SMOOTH_COLOR"):
        try:
            _shade_shader = gpu.shader.from_builtin(name)
            return _shade_shader
        except Exception:
            continue
    return None


_COLOR_TEX_HINTS = (
    "_col", " col", "albedo", "diffuse", "basecolor", "base_color", "base color",
    "texture0", "_alb", "_d.", "_d_", "_dif", "tex_d",
)
_DATA_TEX_HINTS = (
    "_nor", "_nrm", "normal", "_prm", "_orm", "rough", "metal", "spec",
    "_ao", "cavity", "mask", "detail", "bump", "height", "emissive", " emit",
    "dummy", "#replace", "defaultnor", "defaultprm",
)


def _mesh_tint(obj):
    data = getattr(obj, "data", None)
    materials = getattr(data, "materials", None) if data is not None else None
    if materials:
        for item in materials:
            if item is not None:
                return _material_look(item)[2]
    return (0.72, 0.72, 0.72)


def _safe_tint(rgb):
    if max(rgb) < 0.04:
        return (0.72, 0.72, 0.72)
    return rgb


def _collect_tex_nodes(tree, nodes, seen):
    if tree is None:
        return
    marker = id(tree)
    if marker in seen:
        return
    seen.add(marker)
    for node in getattr(tree, "nodes", []) or []:
        if getattr(node, "type", "") == "TEX_IMAGE" and getattr(node, "image", None):
            nodes.append(node)
        elif getattr(node, "type", "") == "GROUP":
            _collect_tex_nodes(getattr(node, "node_tree", None), nodes, seen)


def _follow_color_socket(socket, depth=0, seen=None):
    if socket is None or depth > 8:
        return None
    if not getattr(socket, "is_linked", False):
        return None
    links = getattr(socket, "links", None)
    if not links:
        return None
    node = links[0].from_node
    try:
        marker = int(node.as_pointer())
    except Exception:
        marker = id(node)
    if seen is None:
        seen = set()
    if marker in seen:
        return None
    seen.add(marker)
    ntype = getattr(node, "type", "")
    if ntype == "TEX_IMAGE" and getattr(node, "image", None):
        return (node.image, node)
    if ntype == "BSDF_PRINCIPLED":
        return _follow_color_socket(node.inputs.get("Base Color"), depth + 1, seen)
    if ntype in {"EMISSION", "BSDF_DIFFUSE", "BSDF_GLOSSY", "BACKGROUND"}:
        return _follow_color_socket(node.inputs.get("Color"), depth + 1, seen)
    if ntype == "MIX_SHADER":
        for key in ("Shader", "Shader_001"):
            found = _follow_color_socket(node.inputs.get(key), depth + 1, seen)
            if found:
                return found
        return None
    if ntype in {"MIX", "MIX_RGB"}:
        for key in ("A", "B", "Color1", "Color2"):
            found = _follow_color_socket(node.inputs.get(key), depth + 1, seen)
            if found:
                return found
        return None
    if ntype == "GROUP":
        tree = getattr(node, "node_tree", None)
        from_socket = getattr(links[0], "from_socket", None)
        if tree is not None and from_socket is not None:
            for inner in tree.nodes:
                if getattr(inner, "type", "") != "GROUP_OUTPUT":
                    continue
                found = _follow_color_socket(inner.inputs.get(from_socket.name), depth + 1, seen)
                if found:
                    return found
        return None
    if ntype in {"GAMMA", "HUE_SAT", "BRIGHTCONTRAST", "CURVE_RGB", "INVERT"}:
        sock = node.inputs.get("Color") or (node.inputs[0] if node.inputs else None)
        return _follow_color_socket(sock, depth + 1, seen)
    return None


def _uv_name_from_tex_node(node):
    if node is None:
        return None
    vec = None
    try:
        vec = node.inputs.get("Vector")
    except Exception:
        vec = None
    if vec is None or not getattr(vec, "is_linked", False):
        return None
    src = vec.links[0].from_node
    if getattr(src, "type", "") == "UVMAP":
        return getattr(src, "uv_map", "") or None
    return None


def _image_name_blob(image, extra=""):
    parts = [extra]
    if image is not None:
        parts.append(getattr(image, "name", "") or "")
        parts.append(getattr(image, "filepath", "") or "")
    return " ".join(parts).lower()


def _image_is_data(image):
    try:
        name = (image.colorspace_settings.name or "").lower()
    except Exception:
        return False
    return name in {"non-color", "raw", "linear", "linear rec.709", "acescc"}


def _score_image(image, extra=""):
    if image is None:
        return 1000
    blob = _image_name_blob(image, extra)
    score = 20
    if any(hint in blob for hint in _COLOR_TEX_HINTS):
        score -= 30
    if any(hint in blob for hint in _DATA_TEX_HINTS):
        score += 50
    if _image_is_data(image):
        score += 40
    return score


def _principled_tint(mat):
    tree = getattr(mat, "node_tree", None)
    if tree is not None:
        for node in tree.nodes:
            if getattr(node, "type", "") != "BSDF_PRINCIPLED":
                continue
            sock = node.inputs.get("Base Color")
            if sock is None:
                break
            if not getattr(sock, "is_linked", False):
                color = sock.default_value
                return _safe_tint((float(color[0]), float(color[1]), float(color[2])))
            break
    try:
        color = mat.diffuse_color
        return _safe_tint((float(color[0]), float(color[1]), float(color[2])))
    except Exception:
        return (0.72, 0.72, 0.72)


def _material_look(mat):
    """Return (image, uv_name, tint) for a Blender material. Cached — do not walk trees every redraw."""
    try:
        key = int(mat.as_pointer())
    except Exception:
        key = id(mat)
    cached = _shade_look_cache.get(key)
    if cached is not None:
        return cached
    look = _material_look_uncached(mat)
    _shade_look_cache[key] = look
    return look


def _material_look_uncached(mat):
    tint = _principled_tint(mat)
    tree = getattr(mat, "node_tree", None)
    if tree is not None:
        output = None
        try:
            output = tree.get_output_node("EEVEE") or tree.get_output_node("ALL")
        except Exception:
            output = None
        found = None
        if output is not None:
            found = _follow_color_socket(output.inputs.get("Surface"))
        if found is None:
            for node in tree.nodes:
                ntype = getattr(node, "type", "")
                if ntype == "BSDF_PRINCIPLED":
                    found = _follow_color_socket(node.inputs.get("Base Color"))
                elif ntype == "EMISSION":
                    found = _follow_color_socket(node.inputs.get("Color"))
                if found:
                    break
        if found:
            return (found[0], _uv_name_from_tex_node(found[1]), tint)
        ranked = []
        nodes = []
        _collect_tex_nodes(tree, nodes, set())
        for node in nodes:
            extra = (getattr(node, "label", "") or "") + " " + (getattr(node, "name", "") or "")
            ranked.append((_score_image(node.image, extra), node.image, node))
        if ranked:
            ranked.sort(key=lambda item: item[0])
            if ranked[0][0] < 40:
                return (ranked[0][1], _uv_name_from_tex_node(ranked[0][2]), tint)
    sub = getattr(mat, "sub_matl_data", None)
    textures = getattr(sub, "textures", None) if sub is not None else None
    if textures:
        ranked = []
        for tex in textures:
            image = getattr(tex, "image", None)
            if image is None:
                continue
            extra = (getattr(tex, "node_name", "") or "") + " " + (getattr(tex, "name", "") or "")
            score = _score_image(image, extra)
            number = int(getattr(tex, "texture_number", 99) or 99)
            if number == 0:
                score -= 8
            ranked.append((score, image))
        if ranked:
            ranked.sort(key=lambda item: item[0])
            return (ranked[0][1], None, tint)
    return (None, None, tint)


def _mesh_image(obj):
    data = getattr(obj, "data", None)
    materials = getattr(data, "materials", None) if data is not None else None
    if not materials:
        return None
    for mat in materials:
        if mat is None:
            continue
        image, _uv, _tint = _material_look(mat)
        if image is not None:
            return image
    return None


def _white_gpu_tex():
    global _shade_white_tex
    if _shade_white_tex is not None:
        return _shade_white_tex
    import gpu
    import numpy as np
    pixels = np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    buf = gpu.types.Buffer("FLOAT", 4, pixels)
    _shade_white_tex = gpu.types.GPUTexture((1, 1), format="RGBA16F", data=buf)
    return _shade_white_tex


def _gpu_image(image):
    import gpu
    if image is None:
        return _white_gpu_tex()
    key = int(image.as_pointer())
    cached = _shade_image_cache.get(key)
    if cached is not None:
        return cached
    try:
        texture = gpu.texture.from_image(image)
    except Exception:
        texture = _white_gpu_tex()
    _shade_image_cache[key] = texture
    return texture


def needs_smash_viewport_setup(scene=None):
    """True when Smash Viewport is off, or this Smash import still has no .numshb folder."""
    scene = scene or getattr(bpy.context, "scene", None)
    if scene is None:
        return False
    has_smash = _scene_has_smash_model(scene)
    if not has_smash:
        objects = getattr(scene, "objects", None)
        if objects:
            has_smash = any(_is_smash_mesh(obj) for obj in objects)
    if not has_smash:
        return False
    if not _engine_is_smash(scene):
        return True
    return not _folder_has_numshb(_model_folder(scene))


def _iter_blender_shade_meshes(context, skip_smash, space=None):
    scene = getattr(context, "scene", None)
    objects = getattr(scene, "objects", None) if scene is not None else None
    if not objects:
        return
    view_layer = getattr(context, "view_layer", None)
    allowed = _lambert_preview_armatures(context, view_layer, space)
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        name = obj.name or ""
        if name.startswith("SUB_WGT_"):
            continue
        try:
            if int(obj.as_pointer()) in _extra_mesh_map:
                continue
        except Exception:
            pass
        try:
            arm = getattr(obj, "parent", None)
            if arm is None or getattr(arm, "type", "") != "ARMATURE":
                arm = _mesh_armature(obj)
        except Exception:
            arm = None
        if skip_smash:
            if arm is not None:
                if _gpu_covers_mesh(obj, arm):
                    continue
                if arm not in allowed:
                    continue
            elif _gpu_covers_mesh(obj, None):
                continue
            smash_arm = False
            try:
                smash_arm = _is_smash_mesh(obj) or _is_smash_armature(arm)
            except Exception:
                smash_arm = _is_smash_mesh(obj)
            if smash_arm:
                continue
        if _mesh_hidden(obj, view_layer, space):
            continue
        yield obj


def _mesh_armature(obj):
    try:
        key = int(obj.as_pointer())
    except Exception:
        key = id(obj)
    if key in _shade_arm_cache:
        return _shade_arm_cache[key]
    arm = None
    try:
        arm = obj.find_armature()
    except Exception:
        arm = None
    _shade_arm_cache[key] = arm
    return arm


def _mesh_has_armature_mod(obj):
    try:
        key = int(obj.as_pointer())
    except Exception:
        key = id(obj)
    if key in _shade_mod_cache:
        return _shade_mod_cache[key]
    has = False
    for mod in getattr(obj, "modifiers", []) or []:
        if getattr(mod, "type", "") == "ARMATURE" and getattr(mod, "object", None) is not None:
            if getattr(mod, "show_viewport", True):
                has = True
                break
    _shade_mod_cache[key] = has
    return has


def _armature_pose_key(arm):
    """Stamp location and rotation so a Hip/arm twist still invalidates the cache."""
    pose = getattr(arm, "pose", None)
    if pose is None:
        return 0
    bones = pose.bones
    count = len(bones)
    if count == 0:
        return 0
    parts = [count]
    try:
        wt = arm.matrix_world.translation
        wq = arm.matrix_world.to_quaternion()
        ws = arm.matrix_world.to_scale()
        parts.append((
            round(wt.x, 4),
            round(wt.y, 4),
            round(wt.z, 4),
            round(wq.x, 4),
            round(wq.y, 4),
            round(wq.z, 4),
            round(wq.w, 4),
            round(ws.x, 4),
            round(ws.y, 4),
            round(ws.z, 4),
        ))
    except Exception:
        pass
    for name in ("Trans", "Rot", "Hip"):
        pbone = bones.get(name)
        if pbone is None:
            continue
        t = pbone.matrix.translation
        q = pbone.matrix.to_quaternion()
        parts.append((
            name,
            round(t.x, 3),
            round(t.y, 3),
            round(t.z, 3),
            round(q.x, 3),
            round(q.y, 3),
            round(q.z, 3),
            round(q.w, 3),
        ))
    step = max(1, count // 40)
    for index in range(0, count, step):
        try:
            pbone = bones[index]
        except Exception:
            continue
        if _skip_smash_bone(pbone.name):
            continue
        t = pbone.matrix.translation
        q = pbone.matrix.to_quaternion()
        parts.append((
            index,
            round(t.x, 3),
            round(t.y, 3),
            round(t.z, 3),
            round(q.x, 3),
            round(q.y, 3),
            round(q.z, 3),
        ))
    try:
        active = arm.data.bones.active
    except Exception:
        active = None
    if active is not None:
        pbone = bones.get(active.name)
        if pbone is not None:
            t = pbone.matrix.translation
            q = pbone.matrix.to_quaternion()
            parts.append((
                "active",
                active.name,
                round(t.x, 3),
                round(t.y, 3),
                round(t.z, 3),
                round(q.x, 3),
                round(q.y, 3),
                round(q.z, 3),
                round(q.w, 3),
            ))
    return tuple(parts)


def _world_stamp(world):
    try:
        t = world.to_translation()
        r = world.to_quaternion()
    except Exception:
        return (0.0,)
    return (
        round(t.x, 4),
        round(t.y, 4),
        round(t.z, 4),
        round(r.w, 4),
        round(r.x, 4),
        round(r.y, 4),
        round(r.z, 4),
    )


def _pick_uv_layer(mesh, preferred_name):
    layers = getattr(mesh, "uv_layers", None)
    if not layers:
        return None
    if preferred_name:
        layer = layers.get(preferred_name)
        if layer is not None:
            return layer
    for name in ("map1", "UVMap", "uv0", "UV0", "uv", "TEXCOORD_0"):
        layer = layers.get(name)
        if layer is not None:
            return layer
    try:
        return layers[0]
    except Exception:
        return getattr(layers, "active", None)


def _uv_corners(mesh, loop_idx, preferred_name):
    import numpy as np
    uv = np.zeros((len(loop_idx), 2), dtype=np.float32)
    layer = _pick_uv_layer(mesh, preferred_name)
    if layer is None:
        return uv
    try:
        raw = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        layer.data.foreach_get("uv", raw)
        return raw.reshape(-1, 2)[loop_idx]
    except Exception:
        return uv


def _material_looks_for_mesh(mesh, obj):
    materials = getattr(mesh, "materials", None) or getattr(getattr(obj, "data", None), "materials", None)
    looks = []
    if materials:
        for mat in materials:
            if mat is None:
                looks.append((None, None, (0.72, 0.72, 0.72)))
            else:
                looks.append(_material_look(mat))
    if not looks:
        looks.append((None, None, (0.72, 0.72, 0.72)))
    return looks


def _shade_extract_parts(mesh, obj):
    import numpy as np

    try:
        mesh.calc_loop_triangles()
    except Exception:
        pass
    tris = mesh.loop_triangles
    verts = mesh.vertices
    if not tris or not verts:
        return []
    coords = np.empty(len(verts) * 3, dtype=np.float32)
    verts.foreach_get("co", coords)
    normals = np.empty(len(verts) * 3, dtype=np.float32)
    verts.foreach_get("normal", normals)
    vert_idx = np.empty(len(tris) * 3, dtype=np.int32)
    tris.foreach_get("vertices", vert_idx)
    pos = coords.reshape(-1, 3)[vert_idx]
    nor = normals.reshape(-1, 3)[vert_idx]
    loop_idx = np.empty(len(tris) * 3, dtype=np.int32)
    try:
        tris.foreach_get("loops", loop_idx)
    except Exception:
        loop_idx = vert_idx
    looks = _material_looks_for_mesh(mesh, obj)
    unique = {}
    for slot, look in enumerate(looks):
        image, uv_name, tint = look
        key = (int(image.as_pointer()) if image is not None else 0, uv_name or "", tint)
        unique.setdefault(key, {"slots": [], "look": look})
        unique[key]["slots"].append(slot)

    def part(pos_g, nor_g, uv_g, look):
        return (pos_g, nor_g, uv_g, look[2], look[0])

    if len(unique) <= 1:
        look = looks[0]
        uv = _uv_corners(mesh, loop_idx, look[1])
        return [part(pos, nor, uv, look)]

    mat_idx = np.zeros(len(tris), dtype=np.int32)
    try:
        tris.foreach_get("material_index", mat_idx)
    except Exception:
        pass
    parts = []
    for group in unique.values():
        look = group["look"]
        mask = np.isin(mat_idx, np.array(group["slots"], dtype=np.int32))
        if not mask.any():
            continue
        corner = np.repeat(mask, 3)
        uv = _uv_corners(mesh, loop_idx, look[1])
        parts.append(part(pos[corner], nor[corner], uv[corner], look))
    return parts


def _shade_cache_entry(obj, context, depsgraph, evaluate, pose_key=0):
    data = getattr(obj, "data", None)
    if data is None:
        return None
    materials = getattr(data, "materials", None)
    mat_key = tuple(int(mat.as_pointer()) if mat is not None else 0 for mat in (materials or []))
    key = (
        "eval" if evaluate else "rest",
        int(obj.as_pointer()),
        int(data.as_pointer()),
        len(data.vertices),
        pose_key if evaluate else 0,
        mat_key,
    )
    cached = _shade_cache.get(obj.as_pointer())
    if cached is not None and cached[0] == key:
        return cached
    mesh = data
    eval_obj = obj
    if evaluate:
        try:
            eval_obj = obj.evaluated_get(depsgraph)
            mesh = eval_obj.to_mesh()
        except Exception:
            mesh = data
            eval_obj = obj
    try:
        parts = _shade_extract_parts(mesh, obj)
    except Exception:
        parts = []
    finally:
        if evaluate and mesh is not data:
            try:
                eval_obj.to_mesh_clear()
            except Exception:
                pass
    if not parts:
        return None
    try:
        world = eval_obj.matrix_world.copy()
    except Exception:
        world = obj.matrix_world.copy()
    entry = (key, parts, world)
    _shade_cache[obj.as_pointer()] = entry
    return entry


def _apply_world_part(pos, nor, world):
    import numpy as np

    matrix = np.array(world, dtype=np.float32)
    count = len(pos)
    homo = np.empty((count, 4), dtype=np.float32)
    homo[:, :3] = pos
    homo[:, 3] = 1.0
    pos_w = (homo @ matrix.T)[:, :3]
    normal = np.array(world.to_3x3().inverted_safe().transposed(), dtype=np.float32)
    nor_w = nor @ normal.T
    return pos_w, nor_w


def _merged_shade_batches(entries, shader):
    global _shade_merged
    import numpy as np
    from gpu_extras.batch import batch_for_shader

    stamp = []
    prepared = []
    for obj_ptr, key, parts, world in entries:
        stamp.append((obj_ptr, key, _world_stamp(world)))
        prepared.append((parts, world))
    stamp = tuple(stamp)
    cached = _shade_merged
    if cached is not None and cached[0] == stamp:
        return cached[1]

    groups = {}
    for parts, world in prepared:
        for pos, nor, uv, tint, image in parts:
            pos_w, nor_w = _apply_world_part(pos, nor, world)
            image_ptr = int(image.as_pointer()) if image is not None else 0
            group_key = (image_ptr, tint if image is None else (1.0, 1.0, 1.0))
            bucket = groups.setdefault(group_key, {"pos": [], "nor": [], "uv": [], "image": image, "tint": tint if image is None else (1.0, 1.0, 1.0)})
            bucket["pos"].append(pos_w)
            bucket["nor"].append(nor_w)
            bucket["uv"].append(uv)

    draws = []
    for bucket in groups.values():
        pos = np.concatenate(bucket["pos"]) if len(bucket["pos"]) > 1 else bucket["pos"][0]
        nor = np.concatenate(bucket["nor"]) if len(bucket["nor"]) > 1 else bucket["nor"][0]
        uv = np.concatenate(bucket["uv"]) if len(bucket["uv"]) > 1 else bucket["uv"][0]
        try:
            batch = batch_for_shader(shader, "TRIS", {"pos": pos, "nor": nor, "uv": uv})
        except Exception:
            batch = None
        if batch is not None:
            draws.append((batch, bucket["tint"], bucket["image"]))
    _shade_merged = (stamp, draws)
    return draws


def _draw_blender_meshes(context, depsgraph, skip_smash):
    """Lambert-shade Blender meshes. Does not touch the native Smash renderer."""
    global _shade_merged
    import gpu

    shader = _shade_shader_get()
    if shader is None:
        return
    found = _preview_view_from_context(context)
    if found is None:
        return
    space, _region, rv3d = found
    if depsgraph is None:
        depsgraph = context.evaluated_depsgraph_get()
    view = rv3d.view_matrix
    light = Vector((-view[2][0], -view[2][1], -view[2][2] + 0.35))
    if light.length_squared < 1e-8:
        light = Vector((0.35, 0.55, 0.8))
    light.normalize()
    light_t = (float(light.x), float(light.y), float(light.z))
    viewproj = rv3d.window_matrix @ rv3d.view_matrix
    meshes = list(_iter_blender_shade_meshes(context, skip_smash, space))
    pose_keys = {}
    entries = []
    keep = set()
    for obj in meshes:
        keep.add(obj.as_pointer())
        evaluate = _mesh_has_armature_mod(obj)
        pose_key = 0
        if evaluate:
            arm = _mesh_armature(obj)
            if arm is not None:
                arm_ptr = arm.as_pointer()
                pose_key = pose_keys.get(arm_ptr)
                if pose_key is None:
                    pose_key = _armature_pose_key(arm)
                    pose_keys[arm_ptr] = pose_key
        entry = _shade_cache_entry(obj, context, depsgraph, evaluate, pose_key)
        if entry is None:
            continue
        key, parts, _stored_world = entry
        try:
            world = obj.matrix_world
        except Exception:
            world = _stored_world
        entries.append((obj.as_pointer(), key, parts, world))
    extra = [ptr for ptr in _shade_cache if ptr not in keep]
    if extra:
        _shade_merged = None
    for ptr in extra:
        _shade_cache.pop(ptr, None)
        _shade_arm_cache.pop(ptr, None)
        _shade_mod_cache.pop(ptr, None)
    if not entries:
        return
    custom = hasattr(shader, "uniform_float")
    draws = _merged_shade_batches(entries, shader) if custom else []
    prev_blend = "NONE"
    prev_depth = "NONE"
    try:
        prev_blend = gpu.state.blend_get()
    except Exception:
        pass
    try:
        prev_depth = gpu.state.depth_test_get()
    except Exception:
        pass
    try:
        gpu.state.blend_set("NONE")
        gpu.state.depth_test_set("LESS_EQUAL")
        try:
            gpu.state.depth_mask_set(True)
        except Exception:
            pass
        shader.bind()
        if custom and draws:
            try:
                shader.uniform_float("ViewProjectionMatrix", viewproj)
                shader.uniform_float("light_dir", light_t)
            except Exception:
                custom = False
        if custom and draws:
            for batch, tint, image in draws:
                shader.uniform_float("tint", tint)
                gpu_tex = _gpu_image(image)
                try:
                    shader.uniform_sampler("image", gpu_tex)
                except Exception:
                    try:
                        shader.uniform_sampler("image", _white_gpu_tex())
                    except Exception:
                        pass
                try:
                    shader.uniform_float("use_tex", 1.0 if image is not None else 0.0)
                except Exception:
                    pass
                batch.draw(shader)
        else:
            with gpu.matrix.push_pop():
                gpu.matrix.load_projection_matrix(rv3d.window_matrix)
                try:
                    gpu.matrix.load_matrix(rv3d.view_matrix)
                except Exception:
                    gpu.matrix.load_identity()
                for _ptr, _key, parts, world in entries:
                    from gpu_extras.batch import batch_for_shader
                    for pos, nor, uv, _tint, _image in parts:
                        pos_w, nor_w = _apply_world_part(pos, nor, world)
                        batch = batch_for_shader(shader, "TRIS", {"pos": pos_w, "nor": nor_w, "uv": uv})
                        batch.draw(shader)
    except Exception:
        pass
    finally:
        try:
            gpu.state.blend_set(prev_blend or "NONE")
        except Exception:
            pass
        try:
            gpu.state.depth_test_set(prev_depth or "NONE")
        except Exception:
            pass


def _draw_smash_overlay(context=None, depsgraph=None):
    global _last_error, _last_status, _last_draw_mono
    try:
        _draw_smash_overlay_inner(context, depsgraph)
    except BaseException as exc:
        _last_error = str(exc)


def _draw_smash_overlay_inner(context=None, depsgraph=None):
    global _last_error, _last_status, _last_draw_mono, _gpu_failed, _reload_after_undo
    if not _viewport_enabled():
        return
    context = context or bpy.context
    _last_draw_mono = time.monotonic()
    prepared = _prepare_preview(context, depsgraph)
    dest_w, dest_h = _viewport_pixel_size(context)
    if prepared is not None:
        _gpu_preview, width, height, size_changed, region, rv3d, needs_gpu = prepared
        dest_w = width
        dest_h = height
        if region is not None:
            dest_w = max(int(region.width), 1)
            dest_h = max(int(region.height), 1)
    _fill_viewport_color(dest_w, dest_h)
    gpu_cover = _gpu_covers_scene()
    if not gpu_cover:
        _draw_blender_meshes(context, depsgraph, skip_smash=False)
    if prepared is None:
        return
    preview, width, height, size_changed, region, rv3d, needs_gpu = prepared
    if size_changed:
        _gpu_failed = False
    gpu_rc = -2
    flags = (1 if needs_gpu else 0) | 2
    if not _gpu_failed:
        try:
            gpu_rc = int(
                _lib.ssbh_preview_present_gl(
                    preview, dest_w, dest_h, flags
                )
            )
        except Exception as exc:
            _last_error = str(exc)
            gpu_rc = -1
        if gpu_rc == 0:
            _reload_after_undo = False
            _last_error = ""
            status = f"{width}x{height} ssbh_wgpu GPU"
            if _extra_gpu_order:
                status += f" + {len(_extra_gpu_order)} extra"
            if _last_status != status:
                _last_status = status
            return
        if _reload_after_undo:
            _reload_after_undo = False
            shutdown_preview()
            try:
                bpy.app.timers.register(_resume_if_enabled, first_interval=0.0)
            except Exception:
                pass
            return
        _gpu_failed = True
        gpu_note = _native_error()
        if gpu_note:
            _last_error = gpu_note
    dest_w = max(int(dest_w), 1)
    dest_h = max(int(dest_h), 1)
    _cpu_render(preview, width, height, force=bool(needs_gpu or size_changed))
    tex_w, tex_h = _pixel_size
    if tex_w < 1 or tex_h < 1 or not _pixels:
        _draw_blender_meshes(
            context,
            depsgraph,
            skip_smash=bool(_loaded_folder) or bool(_extra_gpu_order),
        )
        return
    try:
        _blit_rgba(tex_w, tex_h, _pixels, dest_w, dest_h)
    except Exception as exc:
        _last_error = f"Blit failed: {exc}"
    _draw_blender_meshes(
        context,
        depsgraph,
        skip_smash=bool(_loaded_folder) or bool(_extra_gpu_order),
    )


def _ensure_draw_handler():
    """POST_PIXEL present covered bones and keyed out the background. Unused."""
    _remove_draw_handler()


def _remove_draw_handler():
    handler = getattr(bpy.types.SpaceView3D, _DRAW_HANDLER_ATTR, None)
    if handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handler, "WINDOW")
        except Exception:
            pass
        try:
            delattr(bpy.types.SpaceView3D, _DRAW_HANDLER_ATTR)
        except Exception:
            pass


def _restore_engines_safe():
    try:
        scenes = list(bpy.data.scenes)
    except Exception:
        return
    for scene in scenes:
        try:
            _restore_engine(scene)
        except Exception:
            continue


class SUB_OP_smash_vp_load_lighting(bpy.types.Operator, ImportHelper):
    bl_idname = "sub.smash_vp_load_lighting"
    bl_label = "Load Stage Lights"
    bl_description = (
        "Load a stage light.nuanmb the same way SSBH Editor does "
        "(often stage/light/light00.nuanmb or light.nuanmb)"
    )
    bl_options = {"REGISTER"}

    filename_ext = ".nuanmb"
    filter_glob: StringProperty(default="*.nuanmb", options={"HIDDEN"})

    def invoke(self, context, event):
        ssp = getattr(context.scene, "sub_scene_properties", None)
        last = ""
        if ssp is not None:
            last = (
                getattr(ssp, "smash_vp_light_path", "")
                or getattr(ssp, "last_stage_light_dir", "")
            )
        if last:
            if os.path.isfile(last):
                self.filepath = last
            elif os.path.isdir(last):
                for name in (
                    "light.nuanmb",
                    "light00.nuanmb",
                    os.path.join("light", "light00.nuanmb"),
                    os.path.join("light", "light.nuanmb"),
                ):
                    cand = os.path.join(last, name)
                    if os.path.isfile(cand):
                        self.filepath = cand
                        break
                else:
                    self.filepath = os.path.join(last, "light.nuanmb")
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        global _applied_light
        ssp = getattr(context.scene, "sub_scene_properties", None)
        path = self.filepath
        if ssp is not None:
            ssp.smash_vp_light_path = path
            ssp.last_stage_light_dir = os.path.dirname(path)
        try:
            from .stage_tools.light_nuanmb import hold_live_smash_sync
            hold_live_smash_sync()
        except Exception:
            pass
        _applied_light = None
        preview = _preview
        load_fn = getattr(_lib, "ssbh_preview_load_lighting", None) if _lib else None
        if preview and load_fn:
            if load_fn(preview, path.encode("utf-8")) != 0:
                self.report({"ERROR"}, _native_error() or "Failed to load lighting")
                return {"CANCELLED"}
            _applied_light = path
        _tag_preview_redraw()
        self.report({"INFO"}, "Loaded " + os.path.basename(path))
        return {"FINISHED"}


class SUB_OP_smash_vp_relink_model(bpy.types.Operator, ImportHelper):
    bl_idname = "sub.smash_vp_relink_model"
    bl_label = "Relink Smash Model Folder"
    bl_description = (
        "Point Smash Viewport at the original folder that contains model.numshb. "
        "Needed for .blend files imported before Smash Viewport stored that path"
    )
    bl_options = {"REGISTER"}

    filename_ext = ".numshb"
    filter_glob: StringProperty(default="*.numshb", options={"HIDDEN"})

    def invoke(self, context, event):
        ssp = getattr(context.scene, "sub_scene_properties", None)
        start = ""
        if ssp is not None:
            start = (
                getattr(ssp, "last_imported_model_path", "")
                or getattr(ssp, "last_model_folder", "")
                or getattr(ssp, "vanilla_nusktb", "")
            )
        if start:
            if os.path.isfile(start):
                self.filepath = start
            elif os.path.isdir(start):
                self.filepath = os.path.join(start, "model.numshb")
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        folder = (self.filepath or "").strip()
        if os.path.isfile(folder):
            folder = os.path.dirname(folder)
        if not _folder_has_numshb(folder):
            self.report({"ERROR"}, "That folder has no .numshb")
            return {"CANCELLED"}
        obj = getattr(context, "object", None)
        arm = obj if obj is not None and getattr(obj, "type", "") == "ARMATURE" else None
        if arm is None or not _is_smash_armature(arm):
            try:
                if obj is not None:
                    arm = obj.find_armature()
            except Exception:
                arm = None
        _apply_model_folder(context.scene, folder, arm)
        _tag_preview_redraw()
        self.report({"INFO"}, "Smash Viewport will load " + os.path.basename(folder))
        return {"FINISHED"}


class SUB_OP_smash_vp_shade_setup(bpy.types.Operator, ImportHelper):
    bl_idname = "sub.smash_vp_shade_setup"
    bl_label = "Reload Smash Model"
    bl_description = (
        "Pick the folder with model.numshb and reload Smash Viewport from those files"
    )
    bl_options = {"REGISTER"}

    filename_ext = ".numshb"
    filter_glob: StringProperty(default="*.numshb", options={"HIDDEN"})

    def invoke(self, context, event):
        arm = _reload_armature(context)
        start = _find_smash_model_folder(context, arm)
        if not start:
            ssp = getattr(context.scene, "sub_scene_properties", None)
            if ssp is not None:
                start = (
                    getattr(ssp, "model_import_folder_path", "")
                    or getattr(ssp, "last_imported_model_path", "")
                    or getattr(ssp, "last_model_folder", "")
                    or getattr(ssp, "vanilla_nusktb", "")
                )
        start = (start or "").strip()
        if start:
            if os.path.isfile(start):
                self.filepath = start
            elif os.path.isdir(start):
                numshb = os.path.join(start, "model.numshb")
                self.filepath = numshb if os.path.isfile(numshb) else start
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        folder = (self.filepath or "").strip()
        if os.path.isfile(folder):
            folder = os.path.dirname(folder)
        if not _folder_has_numshb(folder):
            self.report({"ERROR"}, "That folder has no .numshb")
            return {"CANCELLED"}
        scene = context.scene
        ssp = getattr(scene, "sub_scene_properties", None)
        if ssp is not None and not bool(getattr(ssp, "smash_viewport", False)):
            ssp.smash_viewport = True
        load_native_library()
        arm = _reload_armature(context)
        _apply_model_folder(scene, folder, arm)
        shutdown_preview()
        _enable_smash_viewport(scene)
        _tag_preview_redraw()
        self.report({"INFO"}, "Reloaded Smash Viewport from " + os.path.basename(folder.rstrip("\\/")))
        return {"FINISHED"}


class SUB_OP_smash_vp_reset_lighting(bpy.types.Operator):
    bl_idname = "sub.smash_vp_reset_lighting"
    bl_label = "Training Lights"
    bl_description = "Reset Smash Viewport lighting to the default training-stage lights"
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _applied_light
        ssp = getattr(context.scene, "sub_scene_properties", None)
        if ssp is not None:
            ssp.smash_vp_light_path = ""
        _applied_light = None
        try:
            from .stage_tools.light_nuanmb import hold_live_smash_sync
            hold_live_smash_sync()
        except Exception:
            pass
        clear_fn = getattr(_lib, "ssbh_preview_clear_lighting", None) if _lib else None
        if _preview and clear_fn:
            clear_fn(_preview)
            _applied_light = ""
        _tag_preview_redraw()
        return {"FINISHED"}


class RENDER_PT_smash_viewport(bpy.types.Panel):
    bl_label = "Smash Viewport"
    bl_idname = "RENDER_PT_smash_viewport"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"
    COMPAT_ENGINES = {ENGINE_ID}

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

    @classmethod
    def poll(cls, context):
        return getattr(context, "engine", "") in cls.COMPAT_ENGINES

    def draw(self, context):
        self.layout.use_property_decorate = False
        layout = self.layout
        ssp = getattr(context.scene, "sub_scene_properties", None)
        if ssp is None:
            return
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(ssp, "smash_vp_bg_color", text="Background")
        col = layout.column(align=True)
        col.prop(ssp, "smash_vp_light_path", text="Stage Lights")
        row = layout.row(align=True)
        row.operator(
            SUB_OP_smash_vp_load_lighting.bl_idname,
            text="Load Stage Lights",
            icon="IMPORT",
        )
        row.operator(
            SUB_OP_smash_vp_reset_lighting.bl_idname,
            text="Training Lights",
            icon="LOOP_BACK",
        )
        path = (getattr(ssp, "smash_vp_light_path", "") or "").strip()
        if path:
            if os.path.basename(path).startswith("smash_stage_lights"):
                layout.label(text="Using Stage Tools lights")
            else:
                layout.label(text=os.path.basename(path))
        else:
            layout.label(text="Using training-stage lights")
        plugin = native_plugin_path()
        if _lib is None:
            layout.label(text=_lib_error or "Native plugin not loaded", icon="ERROR")
        elif plugin:
            layout.label(text="GPU: " + os.path.basename(plugin))
        if _loaded_folder or _extra_gpu_order:
            gpu_n = (1 if _loaded_folder else 0) + len(_extra_gpu_order)
            layout.label(text="GPU models: %s" % gpu_n)
        folder = _model_folder(context.scene)
        if folder:
            layout.label(text="Model: " + os.path.basename(folder.rstrip("\\/")))
        elif _scene_has_smash_model(context.scene) or _has_extra_candidates(context.scene):
            layout.label(text="No .numshb linked — using Smash engine defaults")
        mat_anim = _material_anim_path(context)
        if mat_anim:
            layout.label(text="Mat anim: " + os.path.basename(mat_anim))
        layout.operator(
            SUB_OP_smash_vp_shade_setup.bl_idname,
            text="Reload Smash Model",
            icon="FILE_REFRESH",
        )
        layout.operator(
            SUB_OP_smash_vp_relink_model.bl_idname,
            text="Relink Smash Model Folder",
            icon="FILEBROWSER",
        )



class SUB_RenderEngine_smash_viewport(bpy.types.RenderEngine):
    bl_idname = ENGINE_ID
    bl_label = "Smash Viewport"
    bl_use_preview = False
    bl_use_gpu_context = True
    bl_use_postprocess = False
    bl_use_eevee_viewport = False

    def render(self, depsgraph):
        return

    def view_update(self, context, depsgraph):
        return

    def view_draw(self, context, depsgraph):
        # Present Smash before overlay. Clear color is the Background picker;
        # Blender's floor and grid are left unchanged.
        _draw_smash_overlay(context, depsgraph)


def _resume_if_enabled():
    global _resume_attempts, _ignore_update
    scene = _first_scene()
    if scene is None:
        _resume_attempts += 1
        return 0.2 if _resume_attempts < 25 else None
    if _scene_wants_smash(scene):
        _enable_smash_viewport(scene)
        if not _has_rendered_3d_view() and _resume_attempts < 25:
            _resume_attempts += 1
            return 0.2
        _resume_attempts = 0
        return None
    # Checkbox off: make sure a leftover Smash engine cannot keep sampling busy.
    if _engine_is_smash(scene):
        _restore_engine(scene)
    _stop_timer()
    _sync_checkbox(scene)
    _resume_attempts = 0
    return None


@persistent
def _on_undo_redo(_dummy):
    """Refresh pose, extras, and viewport overrides after Blender rebuilds IDs."""
    global _reload_after_undo, _gpu_failed
    if _ignore_update:
        return
    scene = _first_scene()
    if scene is None or not _viewport_enabled(scene):
        return
    _gpu_failed = False
    _drop_viewport_object_maps()
    _clear_undo_object_caches()
    _clear_extra_models()
    invalidate_animation_state(redraw=False)
    if _smash_arm_fp(scene) != _loaded_arm_fp:
        shutdown_preview()
        try:
            bpy.app.timers.register(_resume_if_enabled, first_interval=0.05)
        except Exception:
            pass
        return
    _reload_after_undo = True
    try:
        bpy.app.timers.register(_after_undo_viewport_refresh, first_interval=0.0)
    except Exception:
        _after_undo_viewport_refresh()
    _tag_preview_redraw()


def _after_undo_viewport_refresh():
    global _reload_after_undo
    scene = _first_scene()
    if scene is None or not _viewport_enabled(scene):
        return None
    _heal_smash_file_viewport_state()
    _sync_armature_mod_viewport()
    invalidate_animation_state(redraw=True)
    _reload_after_undo = False
    return None


@persistent
def _save_pre(_dummy):
    """Do not write Smash Viewport's temporary hide/wire/modifier flags into the .blend."""
    global _ignore_update
    if _ignore_update:
        return
    _ignore_update = True
    try:
        _restore_armature_mod_viewport()
    finally:
        _ignore_update = False


@persistent
def _save_post(_dummy):
    scene = _first_scene()
    if scene is not None and _viewport_enabled(scene):
        _sync_armature_mod_viewport()


@persistent
def _load_post(_dummy):
    shutdown_preview()
    _heal_smash_file_viewport_state()
    _subscribe_engine()
    _clear_shade_caches()
    _resume_if_enabled()


_SMASH_VP_CLASSES = (
    SUB_OP_smash_vp_load_lighting,
    SUB_OP_smash_vp_relink_model,
    SUB_OP_smash_vp_shade_setup,
    SUB_OP_smash_vp_reset_lighting,
    RENDER_PT_smash_viewport,
    SUB_RenderEngine_smash_viewport,
)

_OUTPUT_ENGINE_MARKERS = frozenset((
    "BLENDER_EEVEE",
    "BLENDER_EEVEE_NEXT",
    "BLENDER_WORKBENCH",
    "BLENDER_RENDER",
))
_patched_output_panels = []


def _compat_engines_add(cls, engine):
    engines = getattr(cls, "COMPAT_ENGINES", None)
    if engines is None:
        return False
    if engine in engines:
        return True
    try:
        engines.add(engine)
        return engine in engines
    except Exception:
        pass
    try:
        cls.COMPAT_ENGINES = set(engines)
        cls.COMPAT_ENGINES.add(engine)
        return True
    except Exception:
        return False


def _compat_engines_discard(cls, engine):
    engines = getattr(cls, "COMPAT_ENGINES", None)
    if engines is None or engine not in engines:
        return
    try:
        engines.discard(engine)
        return
    except Exception:
        pass
    try:
        cls.COMPAT_ENGINES = set(engines) - {engine}
    except Exception:
        pass


def _is_output_compat_panel(cls):
    if cls is None:
        return False
    if getattr(cls, "bl_space_type", "") != "PROPERTIES":
        return False
    if getattr(cls, "bl_context", "") != "output":
        return False
    engines = getattr(cls, "COMPAT_ENGINES", None)
    if not engines:
        return False
    return bool(_OUTPUT_ENGINE_MARKERS.intersection(engines))


def _patch_output_panels(enable):
    """Output tab panels only list EEVEE/Workbench. Add this engine so FPS/resolution stay visible."""
    global _patched_output_panels
    if enable:
        _patched_output_panels = []
        # Only touch Panel types. Walking every bpy.types name on 4.2 can
        # force-construct broken third-party RNA (e.g. PSA_UL_*) and raise
        # metaclass conflicts during addon register.
        for name in dir(bpy.types):
            if "_PT_" not in name:
                continue
            try:
                cls = getattr(bpy.types, name, None)
            except Exception:
                continue
            if not _is_output_compat_panel(cls):
                continue
            if _compat_engines_add(cls, ENGINE_ID):
                _patched_output_panels.append(cls)
        return
    for cls in _patched_output_panels:
        _compat_engines_discard(cls, ENGINE_ID)
    _patched_output_panels = []


def register():
    _unregister_old_classes()
    _remove_draw_handler()
    for cls in _SMASH_VP_CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass
    _patch_output_panels(True)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    if _save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_save_pre)
    if _save_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_save_post)
    for coll in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_undo_redo not in coll:
            coll.append(_on_undo_redo)
    _subscribe_engine()
    bpy.app.timers.register(_resume_if_enabled, first_interval=0.15)


def unregister():
    _patch_output_panels(False)
    _stop_timer()
    shutdown_preview()
    _set_smash_bones_in_front(False)
    _set_viewport_meshes_visible(True)
    _remove_draw_handler()
    try:
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    except Exception:
        pass
    try:
        bpy.app.handlers.load_post.remove(_load_post)
    except Exception:
        pass
    try:
        bpy.app.handlers.save_pre.remove(_save_pre)
    except Exception:
        pass
    try:
        bpy.app.handlers.save_post.remove(_save_post)
    except Exception:
        pass
    for coll in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        try:
            coll.remove(_on_undo_redo)
        except Exception:
            pass
    _restore_engines_safe()
    for cls in reversed(_SMASH_VP_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    _unregister_old_classes()


def _draw_texture_2d_compat(texture, position, width, height):
    """draw_texture_2d that keeps correct gamma on Blender 5.0+.

    5.0 requires textures from gpu.texture.from_image() drawn inside a Python
    draw handler to declare the target colour space, or they render washed out.
    """
    from gpu_extras.presets import draw_texture_2d
    try:
        draw_texture_2d(texture, position, width, height,
                        is_scene_linear_with_rec709_srgb_target=True)
    except TypeError:
        draw_texture_2d(texture, position, width, height)  # Blender 4.x
