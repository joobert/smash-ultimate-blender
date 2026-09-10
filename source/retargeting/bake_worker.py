"""Background Blender entry point. Results contain arrays only, never RNA pointers."""
import importlib
import json
import os
from pathlib import Path
import sys
import types

import bpy
import numpy as np


def load_baker():
    # Import the small bake engine without executing the add-on's UI package
    # initializers. Enabled add-ons still register normally when Blender starts.
    root = Path(__file__).resolve().parents[2]
    package = '_ultimate_bake_worker'
    for suffix, path in [('', root), ('.source', root / 'source'),
                         ('.source.anim', root / 'source/anim'),
                         ('.source.retargeting', root / 'source/retargeting')]:
        module = types.ModuleType(package + suffix)
        module.__path__ = [str(path)]
        sys.modules[module.__name__] = module
    return importlib.import_module(package + '.source.retargeting.fast_bake')


def main():
    config = Path(sys.argv[sys.argv.index('--') + 1])
    job = json.loads(config.read_text(encoding='utf-8'))
    baker = load_baker()
    scene = bpy.data.scenes[job['scene']]
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = scene.view_layers[job['view_layer']]
    source, dest = bpy.data.objects[job['source']], bpy.data.objects[job['dest']]
    source.animation_data_create()
    dest.animation_data_create()
    for item in job['actions']:
        frames, modes, channels = baker._sample_action(
            bpy.context, source, dest, bpy.data.actions[item['name']],
            job['bones'], *job['bases'], item['slot'])
        arrays = dict(frames=frames, modes=np.asarray(modes))
        for i, (loc, rot, scale) in enumerate(channels):
            arrays.update({f'loc{i}': loc, f'rot{i}': rot, f'scale{i}': scale})
        output = config.parent / f"result-{item['index']}.npz"
        temporary = output.with_suffix('.partial')
        with open(temporary, 'wb') as stream:
            np.savez(stream, **arrays)
        os.replace(temporary, output)


if __name__ == '__main__':
    main()
