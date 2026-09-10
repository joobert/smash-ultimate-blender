"""Exercise real add-on enable and Ultimate panel registration in factory Blender.

blender --background --factory-startup --python-exit-code 1 --python tests/test_addon_registration_blender.py
"""
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

import addon_utils
import bpy

# Some existing modules install bundled presets during import. Keep all test
# configuration/library writes away from the user's real Blender profile.
profile = tempfile.TemporaryDirectory(prefix='sub_registration_test_')
for variable, leaf in (('BLENDER_USER_SCRIPTS', 'scripts'), ('BLENDER_USER_CONFIG', 'config')):
    path = Path(profile.name) / leaf
    path.mkdir()
    os.environ[variable] = str(path)
assert Path(bpy.utils.user_resource('SCRIPTS')).is_relative_to(profile.name)

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'sub_registration_test'
spec = importlib.util.spec_from_file_location(MODULE, ROOT / '__init__.py',
                                             submodule_search_locations=[str(ROOT)])
addon = importlib.util.module_from_spec(spec)
sys.modules[MODULE] = addon
spec.loader.exec_module(addon)
addon.__time__ = (ROOT / '__init__.py').stat().st_mtime

# Suppress only unrelated network/user-library I/O, not registration or panels.
updater = importlib.import_module(MODULE + '.source.updater.version_check')
updater.check_for_newer_version = lambda: None
labels = importlib.import_module(MODULE + '.source.param_labels')
labels.ensure_param_labels = lambda: ROOT / 'ParamLabels.csv'
labels.load_param_labels = lambda: ROOT / 'ParamLabels.csv'
retarget_presets = importlib.import_module(MODULE + '.expy_kit.preset_handler')
retarget_presets.install_presets = lambda: None

errors = []


def on_error(_error):
    import traceback
    errors.append(traceback.format_exc())


enabled = addon_utils.enable(MODULE, default_set=False, handle_error=on_error)
assert enabled is addon and not errors, '\n'.join(errors)

floor = importlib.import_module(MODULE + '.source.extras.ik_floor_contact')
presets = importlib.import_module(MODULE + '.source.extras.panel_presets')
order = importlib.import_module(MODULE + '.source.panel_order')
assert bpy.app.timers.is_registered(floor._restore_contacts)
floor._restore_contacts()
presets.ensure_default_presets(bpy.context.scene, force_builtins=True)
presets._seed_presets_timer()

registered = {name for name, _, _ in order.discover_panels()}
required = {'SUB_PT_import_model', 'SUB_PT_export_model', 'SUB_PT_import_anim',
            'SUB_PT_export_anim', 'SUB_PT_animation_tools', 'SUB_PT_model_tools',
            'SUB_PT_collection_presets', 'SUB_PT_retargeting_main'}
assert required <= registered, ('Missing Ultimate panels', required - registered)
for name in required:
    assert presets.panel_allowed(bpy.context, name), ('Hidden Ultimate panel', name)
assert bpy.types.Panel.bl_rna_get_subclass_py('SUB_PT_animation_tools').poll(bpy.context)
assert bpy.types.Panel.bl_rna_get_subclass_py('SUB_PT_panel_presets') is not None

addon_utils.disable(MODULE, default_set=False, handle_error=on_error)
assert not errors, '\n'.join(errors)
assert not bpy.app.timers.is_registered(floor._restore_contacts)
assert floor._floor_load_post not in bpy.app.handlers.load_post
print('ADDON REGISTRATION TEST PASSED:', len(registered), 'Ultimate panels')
profile.cleanup()
