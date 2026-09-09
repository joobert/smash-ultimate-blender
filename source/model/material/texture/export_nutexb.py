import bpy
import os
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

from pathlib import Path
from subprocess import run, CalledProcessError

from .convert_nutexb_to_png import get_ultimate_tex_path
from ..create_matl_from_blender_materials import has_sub_matl_data, get_linked_materials
from .default_textures import generated_default_texture_name_value
from ...export_model import would_trimmed_names_be_unique, trim_name

def export_nutexb_from_blender_materials(operator: bpy.types.Operator, materials: set[bpy.types.Material], export_dir: Path):
    images: set[bpy.types.Image] = set()
    
    linked_materials = get_linked_materials(materials)

    for material in materials | linked_materials:
        if has_sub_matl_data(material):
            for texture in material.sub_matl_data.textures:
                if texture.image.name in generated_default_texture_name_value:
                    operator.report({'INFO'}, f'Not exporting {texture.image.name}, as it is a default texture.')
                    continue
                images.add(texture.image)

    texture_names = {image.name for image in images}

    trim_names = would_trimmed_names_be_unique(texture_names)

    # Only external encoders run on workers. Blender image access and reporting
    # must remain on the calling thread. Bound both encoders and staged PNGs.
    workers = min(4, os.cpu_count() or 1, max(1, len(images)))
    encoder = get_ultimate_tex_path()
    with TemporaryDirectory(prefix='.sub-textures-', dir=export_dir) as staging:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = []
            def finish(job):
                future, name, path = job
                try:
                    future.result()
                except CalledProcessError as e:
                    operator.report({'WARNING'}, f"failed to export `{name}` as .NUTEXB, error = {e.stderr}")
                finally:
                    path.unlink(missing_ok=True)

            for index, image in enumerate(sorted(images, key=lambda image: image.name)):
                if len(pending) >= workers:
                    finish(pending.pop(0))
                temp_image_path = Path(staging) / f'{index}.png'
                # Incase a user attempts to export placeholder images.
                if not image.packed_file:
                    if image.source == 'FILE':
                        if image.filepath == '':
                            operator.report({'WARNING'}, f"The image `{image.name}` is just a placeholder in blender (likely due to a failed import), so it has no data and cannot be exported.")
                            continue
                # For some image types, such as DDS, blender fails to save using "save", but "save_render" still works.
                original_format = image.file_format
                try:
                    image.file_format = 'PNG'
                    image.save(filepath=str(temp_image_path))
                except Exception as e:
                    operator.report({'WARNING'}, f"Unable to save the blender image {image.name} to disk using `save`, but will attempt using `save_render`. Error = {e}")
                    try:
                        image.save_render(filepath=str(temp_image_path))
                    except Exception as e:
                        operator.report({'ERROR'}, f"Failed to save the blender image `{image.name}` to disk using either `save` or `save_render`. Error = {e}")
                        continue
                finally:
                    image.file_format = original_format

                nutexb_filepath: Path
                if trim_names:
                    nutexb_filepath = export_dir.joinpath(trim_name(image.name) + ".nutexb")
                else:
                    nutexb_filepath = export_dir.joinpath(image.name + ".nutexb")

                format: str
                if image.colorspace_settings.name == 'sRGB':
                    format = "BC7Srgb"
                elif image.colorspace_settings.name == 'Non-Color':
                    format = "BC7Unorm"
                else:
                    operator.report({'WARNING'}, f"Image `{image.name}` has unsupported color space of `{image.colorspace_settings.name}`, defaulting to BC7Unorm")
                    format = "BC7Unorm"

                future = pool.submit(run, [encoder, str(temp_image_path), str(nutexb_filepath),
                                           "--format", format], capture_output=True, check=True)
                pending.append((future, image.name, temp_image_path))
            for job in pending:
                finish(job)
