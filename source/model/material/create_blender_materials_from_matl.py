import bpy
import os
import sqlite3
import re 

from concurrent.futures import ThreadPoolExecutor, as_completed

from bpy.types import ShaderNodeTexImage, ShaderNodeUVMap, ShaderNodeValue, ShaderNodeOutputMaterial, ShaderNodeVertexColor, Operator
from bpy_extras import image_utils

from enum import Enum
from pathlib import Path
from subprocess import CalledProcessError

from ....dependencies import ssbh_data_py
from .matl_params import texture_param_name_to_socket_params, vec4_param_name_to_socket_params
from .sub_matl_data import *
from .texture.convert_nutexb_to_png import convert_nutexb_to_png
from .texture.default_textures import generated_default_texture_name_value

"""generated_default_texture_name_value: dict[str, tuple[float, float, float, float]] = {
     "/common/shader/sfxpbs/default_black": (0, 0, 0, 0),
     "/common/shader/sfxpbs/default_color": (1,1,1,1),
     "/common/shader/sfxpbs/default_color2": (1,1,1,1),
     "/common/shader/sfxpbs/default_color3": (1,1,1,1),
     "/common/shader/sfxpbs/default_color4": (1,1,1,1),
     "/common/shader/sfxpbs/default_diffuse2": (1,1,0,1), # This is supposed to be a yellow and white checkerboad texture, but i can't imagine any user actually using it tbh tbh
     "/common/shader/sfxpbs/default_gray": (.5, .5, .5, 1),
     "/common/shader/sfxpbs/default_metallicbg": (0, 1, 1, .25),
     "/common/shader/sfxpbs/default_normal": (0.5, 0.5, 1, 1),
     "/common/shader/sfxpbs/default_params": (0, 1, 1, .25),
     "/common/shader/sfxpbs/default_params_r000_g025_b100": (0, 0.25, 1, 1),
     "/common/shader/sfxpbs/default_params_r100_g025_b100": (1, 0.25, 1, 1),
     "/common/shader/sfxpbs/default_params2": (1,1,1,1),
     "/common/shader/sfxpbs/default_params3": (0, 0.5, 1, .25),
     "/common/shader/sfxpbs/default_specular": (0.25 , 0.25, 0.25, 1.0),
     "/common/shader/sfxpbs/default_white": (1.0, 1.0, 1.0, 1.0),
     "/common/shader/sfxpbs/fighter/default_normal": (0.5, 0.5, 1.0, 1.0),
     "/common/shader/sfxpbs/fighter/default_params": (0.0, 1.0, 1.0, 0.25),
     "#replace_cubemap": (1,1,1,1), # Not correct, but it needs to be here in case the user wants to use it without importing a model first
}"""

def get_shader_db_file_path():
    # This file was generated with duplicates removed to optimize space.
    # https://github.com/ScanMountGoat/Smush-Material-Research#shader-database
    this_file_path = Path(__file__)
    return this_file_path.parent.joinpath('shader_file').joinpath('Nufx.db').resolve()

def create_default_texture(texture_name: str, value: tuple[float, float, float, float]):
    if texture_name not in bpy.data.images.keys():
        image = bpy.data.images.new(texture_name, 8, 8, alpha=True, is_data=True)
        image.generated_color = value
        image.use_fake_user = True

def create_default_textures():
    for texture_name, value in generated_default_texture_name_value.items():
        create_default_texture(texture_name, value)

def _index_model_dir(model_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Scan the model folder once and index its textures by lowercase stem.

    The old code globbed the folder four times per texture, which on a fighter
    with ~50 textures meant a couple hundred redundant directory scans.
    """
    nutexb_paths: dict[str, Path] = {}
    png_paths: dict[str, Path] = {}
    try:
        entries = list(os.scandir(model_dir))
    except OSError:
        return nutexb_paths, png_paths
    for entry in entries:
        name = entry.name.lower()
        if name.endswith('.nutexb'):
            nutexb_paths.setdefault(name[:-len('.nutexb')], Path(entry.path))
        elif name.endswith('.png'):
            png_paths.setdefault(name[:-len('.png')], Path(entry.path))
    return nutexb_paths, png_paths


def get_matching_nutexb_path(texture_name: str, model_dir: Path) -> Path | None:
    return _index_model_dir(Path(model_dir))[0].get(texture_name.lower())


def get_matching_png_path(texture_name: str, model_dir: Path) -> Path | None:
    return _index_model_dir(Path(model_dir))[1].get(texture_name.lower())


def _convert_nutexb_files(conversions: list[tuple[str, Path, Path]]) -> dict[str, Exception]:
    """Run ultimate_tex_cli over every texture at once.

    Each conversion is its own external process, so the work is dominated by
    process startup and disk IO rather than the GIL. Converting them one at a
    time was by far the largest cost of importing a model.
    """
    results: dict[str, Exception] = {}
    if not conversions:
        return results
    if len(conversions) == 1:
        texture_name, nutexb_path, png_path = conversions[0]
        try:
            convert_nutexb_to_png(nutexb_path, png_path)
        except Exception as e:
            results[texture_name] = e
        return results

    max_workers = min(len(conversions), (os.cpu_count() or 4) * 2, 16)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(convert_nutexb_to_png, nutexb_path, png_path): texture_name
            for texture_name, nutexb_path, png_path in conversions
        }
        for future in as_completed(futures):
            texture_name = futures[future]
            try:
                future.result()
            except Exception as e:
                results[texture_name] = e
    return results


def import_texture_to_blender(operator: bpy.types.Operator, texture_name: str, model_dir: Path,
                              nutexb_paths: dict[str, Path] = None,
                              png_paths: dict[str, Path] = None,
                              conversion_errors: dict[str, Exception] = None,
                              temp_png_paths: dict[str, Path] = None) -> bpy.types.Image:
    '''In order for users to be able to export and re-load from the same folder, the priority will be .nutexb, then .png

    The nutexb -> png conversions happen up front in import_material_images so they
    can run in parallel; this just consumes their results.
    '''
    # Check if the image being referenced is a default image
    default_texture_names: set[str] = set(generated_default_texture_name_value.keys())
    if texture_name.lower() in default_texture_names:
        # Default textures were just generated, so they should be in the .blend already
        return bpy.data.images[texture_name.lower()]

    if nutexb_paths is None or png_paths is None:
        nutexb_paths, png_paths = _index_model_dir(Path(model_dir))
        if temp_png_paths is None:
            # Called outside the batch path, so do this one conversion here.
            nutexb_path = nutexb_paths.get(texture_name.lower())
            temp_png_paths = {}
            conversion_errors = {}
            if nutexb_path is not None:
                temp_png_paths[texture_name] = Path(model_dir) / (texture_name + "_temp.png")
                conversion_errors = _convert_nutexb_files(
                    [(texture_name, nutexb_path, temp_png_paths[texture_name])]
                )
    conversion_errors = conversion_errors or {}
    temp_png_paths = temp_png_paths or {}

    # The image wasn't a default image, so will need to create the new image
    image = bpy.data.images.new(texture_name, 8, 8) # Ignore the x/y values here, its just because the new image is of type "Generated" before we change it to "File"
    image.name = texture_name
    #image.type = 'FILE' # Its read-only
    image.source = 'FILE'

    matching_nutexb_path = nutexb_paths.get(texture_name.lower())
    matching_png_path = png_paths.get(texture_name.lower())

    def use_converted_png() -> bool:
        '''Pack the freshly converted png, returning False if the conversion failed.'''
        temp_png_file_path = temp_png_paths.get(texture_name)
        if temp_png_file_path is None or texture_name in conversion_errors:
            return False
        image.filepath = str(temp_png_file_path)
        image.pack()
        try:
            temp_png_file_path.unlink()
        except Exception as e:
            operator.report({"WARNING"}, f"Failed to remove temporary png file `{temp_png_file_path.name}`, error=`{e}`")
        return True

    match (matching_nutexb_path is not None, matching_png_path is not None):
        case (True, True):
            if not use_converted_png():
                error = conversion_errors.get(texture_name)
                operator.report({"INFO"}, f"Failed to convert .nutexb `{matching_nutexb_path.name}` to PNG, but the .PNG was available so that will be used instead. Error=`{error}`")
                image.filepath = str(matching_png_path)
                # The image wont be packed since its an existing external file.
        case (True, False):
            if not use_converted_png():
                error = conversion_errors.get(texture_name)
                operator.report({"WARNING"}, f"Failed to convert .nutexb `{matching_nutexb_path.name}` to PNG, please manually convert the .nutexb to a .png and place it in the folder. Error=`{error}`")
        case (False, True):
            image.filepath = str(matching_png_path)
        case (False, False):
            operator.report({"WARNING"}, f"No .nutexb or .png was found for texture `{texture_name}`! Please include the .nutexb or .png")

    return image

def import_material_images(operator: bpy.types.Operator, ssbh_matl: ssbh_data_py.matl_data.MatlData, model_dir:str ) -> dict[str, bpy.types.Image]:
    texture_name_to_image_dict: dict[str, bpy.types.Image] = {}
    texture_names_in_matl = {tex.data for mat in ssbh_matl.entries for tex in mat.textures}

    model_dir_path = Path(model_dir)
    nutexb_paths, png_paths = _index_model_dir(model_dir_path)
    default_texture_names: set[str] = set(generated_default_texture_name_value.keys())

    # Convert every .nutexb we need up front so the external converter processes
    # run concurrently instead of one blocking call per texture.
    conversions: list[tuple[str, Path, Path]] = []
    temp_png_paths: dict[str, Path] = {}
    for texture_name in texture_names_in_matl:
        if texture_name.lower() in default_texture_names:
            continue
        nutexb_path = nutexb_paths.get(texture_name.lower())
        if nutexb_path is None:
            continue
        temp_png_file_path = model_dir_path / (texture_name + "_temp.png")
        temp_png_paths[texture_name] = temp_png_file_path
        conversions.append((texture_name, nutexb_path, temp_png_file_path))

    conversion_errors = _convert_nutexb_files(conversions)

    for texture_name in texture_names_in_matl:
        texture_name_to_image_dict[texture_name] = import_texture_to_blender(
            operator, texture_name, model_dir_path,
            nutexb_paths, png_paths, conversion_errors, temp_png_paths,
        )

    return texture_name_to_image_dict

def get_discard_shaders():
    global discard_shaders
    try:
        discard_shaders
    except NameError:
        this_file_path = Path(__file__)
        discard_shaders_file = this_file_path.parent.joinpath('shader_file').joinpath('shaders_discard_v13.0.1.txt').resolve()
        with open(discard_shaders_file, 'r') as f:
            discard_shaders = {line.strip() for line in f.readlines()}
    return discard_shaders

def get_blend_method(shader_label: str, blend_states: list[SUB_PG_matl_blend_state]):
    # TODO: Access blenders internal enum instead? Or use a cleaner enum method
    BlendMethod = Enum('BlendMethod', 'OPAQUE HASHED CLIP BLEND')
    if len(blend_states) != 1: # no vanilla ultimate shader has more than one blend state
        return BlendMethod.OPAQUE.name
    
    discard_shaders = get_discard_shaders()
    # Trims the trailing '_OPAQUE', '_SORT', etc.
    if shader_label[:len('SFX_PBS_0000000000000080')] in discard_shaders:
        return BlendMethod.CLIP.name
    
    blend_state_0 = blend_states[0]
    if blend_state_0.alpha_sample_to_coverage is True:
        return BlendMethod.HASHED.name
    
    if blend_state_0.destination_color == ssbh_data_py.matl_data.BlendFactor.OneMinusSourceAlpha.name:
        # In theory, its supposed to be 'BLEND', but this more often than not looks wrong.
        # I don't understand shaders well enough to really know why it doesn't work in EEVEE.
        # So return 'HASHED' instead for now. The user can manually adjust to 'BLEND' after import.
        return BlendMethod.HASHED.name
    
    return BlendMethod.OPAQUE.name

def setup_blender_material_settings(material: bpy.types.Material):
    sub_matl_data: SUB_PG_sub_matl_data = material.sub_matl_data
    # Blend Method
    material.blend_method = get_blend_method(sub_matl_data.shader_label, sub_matl_data.blend_states)
    # Back Face Culling
    if len(sub_matl_data.rasterizer_states) == 1:
        if sub_matl_data.rasterizer_states[0].cull_mode == ssbh_data_py.matl_data.CullMode.Back.name:
            material.use_backface_culling = True

def get_matched_sampler(sub_matl_data: SUB_PG_sub_matl_data, texture: SUB_PG_matl_texture):
    sampler: SUB_PG_matl_sampler
    for sampler in sub_matl_data.samplers:
        if texture.texture_number == sampler.sampler_number:
            return sampler

def setup_sub_matl_data_node_drivers(sub_matl_data: SUB_PG_sub_matl_data):
    material: bpy.types.Material = sub_matl_data.id_data
    sub_matl_vector: SUB_PG_matl_vector
    for vector_index, sub_matl_vector in enumerate(sub_matl_data.vectors):
        for axis_index, axis in enumerate(['X', 'Y', 'Z', 'W']):
            value_node_name = f"{sub_matl_vector.param_id_name}_{axis}"
            value_node: ShaderNodeValue = material.node_tree.nodes.get(value_node_name)
            if value_node is None:
                continue
            # Setup Driver
            driver_fcurve: bpy.types.FCurve = value_node.outputs[0].driver_add('default_value')
            var = driver_fcurve.driver.variables.new()
            var.name = 'var'
            target = var.targets[0]
            target.id_type = 'MATERIAL'
            target.id = material
            target.data_path = f'sub_matl_data.vectors[{vector_index}].value[{axis_index}]'
            driver_fcurve.driver.expression = f'{var.name}'

    sub_matl_float: SUB_PG_matl_float
    for float_index, sub_matl_float in enumerate(sub_matl_data.floats):
        value_node: ShaderNodeValue = material.node_tree.nodes.get(sub_matl_float.param_id_name)
        if value_node is None:
            continue
        # Setup Driver
        driver_fcurve: bpy.types.FCurve = value_node.outputs[0].driver_add('default_value')
        var = driver_fcurve.driver.variables.new()
        var.name = 'var'
        target = var.targets[0]
        target.id_type = 'MATERIAL'
        target.id = material
        target.data_path = f'sub_matl_data.floats[{float_index}].value'
        driver_fcurve.driver.expression = f'{var.name}'


def setup_blender_material_node_tree(material: bpy.types.Material):
    from .master_shader import create_master_shader, get_master_shader_name
    sub_matl_data: SUB_PG_sub_matl_data = material.sub_matl_data
    
    # Make Master Shader if its not already made
    create_master_shader()
    
    # Clone Master Shader
    master_shader_name = get_master_shader_name()
    master_node_group = bpy.data.node_groups.get(master_shader_name)
    #clone_group = master_node_group.copy()

    # Setup Clone
    #clone_group.name = sub_matl_data.shader_label

    # Prep the node_tree for the new nodes
    material.use_nodes = True
    material.node_tree.nodes.clear()

    # Add the new Nodes
    nodes = material.node_tree.nodes
    
    cycles_output: ShaderNodeOutputMaterial = nodes.new('ShaderNodeOutputMaterial')
    cycles_output.name = 'cycles_output'
    cycles_output.label = 'Cycles Output'
    cycles_output.target = 'CYCLES'
    cycles_output.location = (400,350)

    eevee_output: ShaderNodeOutputMaterial = nodes.new('ShaderNodeOutputMaterial')
    eevee_output.name = 'eevee_output'
    eevee_output.label = 'EEVEE Output'
    eevee_output.target = 'EEVEE'
    eevee_output.location = (400,200)

    node_group_node = nodes.new('ShaderNodeGroup')
    node_group_node.name = 'smash_ultimate_shader'
    node_group_node.label = sub_matl_data.shader_label
    node_group_node.width = 600
    node_group_node.location = (-300, 300)
    #node_group_node.node_tree = clone_group
    node_group_node.node_tree = master_node_group

    links = material.node_tree.links
    links.new(node_group_node.outputs[0], cycles_output.inputs[0])
    links.new(node_group_node.outputs[1], eevee_output.inputs[0])

    texture: SUB_PG_matl_texture
    ParamId = ssbh_data_py.matl_data.ParamId
    created_node_rows = 0
    texture_node_row_width = 500
    layer_1_texture_names = {
        ParamId.Texture0.name,
        ParamId.Texture2.name,
        ParamId.Texture3.name,
        ParamId.Texture4.name,
        ParamId.Texture5.name,
        ParamId.Texture6.name,
        ParamId.Texture7.name,
        ParamId.Texture8.name,
        ParamId.Texture9.name,
        ParamId.Texture10.name,
        ParamId.Texture16.name,
    }
    layer_2_texture_names = {
        ParamId.Texture1.name,
        ParamId.Texture11.name,
        ParamId.Texture14.name,
    }
    layer_3_texture_names = {
        ParamId.Texture12.name
    }
    layer_4_texture_names = {
        ParamId.Texture13.name
    }
    layer_1_uv_transform_nodes = set()
    layer_2_uv_transform_nodes = set()
    layer_3_uv_transform_nodes = set()
    layer_4_uv_transform_nodes = set()
    sprite_sheet_nodes = set()
    for texture in sub_matl_data.textures:
        # Create Texture Node
        texture_node: ShaderNodeTexImage = nodes.new('ShaderNodeTexImage')
        texture_node.location = (-800, 1000 - (texture_node_row_width * created_node_rows))
        texture_node.name = texture.node_name
        texture_node.label = texture.ui_name
        texture_node.image = texture.image
        texture_node.show_options = False

        # For now, manually set the colorspace types....
        linear_textures_names = {ParamId.Texture4.name, ParamId.Texture6.name}
        if texture.node_name in linear_textures_names and texture_node.image:
            texture_node.image.colorspace_settings.name = 'Non-Color'
            texture_node.image.alpha_mode = 'CHANNEL_PACKED'
        
        # Create UV Map Node
        uv_map_node: ShaderNodeUVMap = nodes.new("ShaderNodeUVMap")
        uv_map_node.name = f'{texture.node_name}_uv_map'
        uv_map_node.location = (texture_node.location[0] - 1500, texture_node.location[1])
        uv_map_node.label = f'{texture.node_name} UV Map'
        
        # For now, manually set the UV maps
        bake1_texture_names = {ParamId.Texture3.name, ParamId.Texture9.name}
        uvset_texture_names = {ParamId.Texture1.name, ParamId.Texture11.name, ParamId.Texture14.name}
        if texture.node_name in bake1_texture_names:
            uv_map_node.uv_map = 'bake1'
        elif texture.node_name in uvset_texture_names:
            uv_map_node.uv_map = 'uvSet'
        else:
            uv_map_node.uv_map = 'map1'
        
        # Create UV Transform Node
        # Also set the default_values here. I know it makes more sense to have the default_values
        # be in the init func of the node itself, but it just doesn't work there lol
        from .shader_nodes import custom_uv_transform_node
        uv_transform_node = nodes.new(custom_uv_transform_node.SUB_CSN_ultimate_uv_transform.bl_idname)
        uv_transform_node.name = 'uv_transform_node'
        uv_transform_node.label = 'UV Transform' + texture.node_name.split('Texture')[1]
        uv_transform_node.location = (texture_node.location[0] - 1200, texture_node.location[1])
        uv_transform_node.inputs[0].default_value = 1.0 # Scale X
        uv_transform_node.inputs[1].default_value = 1.0 # Scale Y
        if texture.node_name in layer_1_texture_names:
            layer_1_uv_transform_nodes.add(uv_transform_node)
        elif texture.node_name in layer_2_texture_names:
            layer_2_uv_transform_nodes.add(uv_transform_node)
        elif texture.node_name in layer_3_texture_names:
            layer_3_uv_transform_nodes.add(uv_transform_node)

        # Create Sprite Sheet Param Node
        from .shader_nodes import custom_sprite_sheet_params_node
        sprite_sheet_node = nodes.new(custom_sprite_sheet_params_node.SUB_CSN_ultimate_sprite_sheet_params.bl_idname)
        sprite_sheet_node.name = 'sprite_sheet_node'
        sprite_sheet_node.label = 'Sprite Sheet Params'
        sprite_sheet_node.location = (texture_node.location[0] - 900, texture_node.location[1])
        sprite_sheet_node.inputs[0].default_value = 1.0 # Column Count
        sprite_sheet_node.inputs[1].default_value = 1.0 # Row Count
        sprite_sheet_node.inputs[2].default_value = 1.0 # Active Sprite Count
        sprite_sheet_node.width = 250
        sprite_sheet_nodes.add(sprite_sheet_node)
        # Create Sampler Node
        from .shader_nodes import custom_sampler_node
        
        matched_sampler: SUB_PG_matl_sampler = get_matched_sampler(sub_matl_data, texture)
        sampler_node:custom_sampler_node.SUB_CSN_ultimate_sampler = nodes.new(custom_sampler_node.SUB_CSN_ultimate_sampler.bl_idname)
        sampler_node.name = 'sampler_node'
        sampler_node.label = 'Sampler' + texture.node_name.split('Texture')[1]
        sampler_node.location = (texture_node.location[0] - 600, texture_node.location[1])
        sampler_node.width = 500

        sampler_node.wrap_s = matched_sampler.wrap_s
        sampler_node.wrap_t = matched_sampler.wrap_t
        sampler_node.wrap_r = matched_sampler.wrap_r
        sampler_node.min_filter = matched_sampler.min_filter
        sampler_node.mag_filter = matched_sampler.mag_filter
        sampler_node.anisotropic_filtering = matched_sampler.max_anisotropy is not None
        sampler_node.max_anisotropy = matched_sampler.max_anisotropy if matched_sampler.max_anisotropy else 'One'
        sampler_node.border_color = matched_sampler.border_color
        sampler_node.lod_bias = matched_sampler.lod_bias 

        sampler_node.show_options = False

        # Now that the samplers loaded we can assign the texture filtering
        if matched_sampler.mag_filter == 'Nearest':
            texture_node.interpolation = 'Closest'

        # Link these nodes together
        links.new(uv_map_node.outputs[0], uv_transform_node.inputs[4])
        links.new(uv_transform_node.outputs[0], sprite_sheet_node.inputs[4])
        links.new(sprite_sheet_node.outputs[0], sampler_node.inputs[0])
        links.new(sampler_node.outputs[0], texture_node.inputs[0])
        links.new(texture_node.outputs['Color'], node_group_node.inputs[texture_param_name_to_socket_params[texture.node_name].rgb_socket_name])
        links.new(texture_node.outputs['Alpha'], node_group_node.inputs[texture_param_name_to_socket_params[texture.node_name].alpha_socket_name])

        created_node_rows = created_node_rows + 1

    created_value_rows = 0
    vector: SUB_PG_matl_vector
    for vector_index, vector in enumerate(sub_matl_data.vectors):
        if vector.param_id_name == ParamId.CustomVector47.name:
            node_group_node.inputs['use_custom_vector_47'].default_value = 1.0

        for axis_index, axis in enumerate(['X', 'Y', 'Z', 'W']):
            # Create the value node
            value_node: ShaderNodeValue = nodes.new('ShaderNodeValue')
            value_node.name = f"{vector.param_id_name}_{axis}"
            value_node.label = f"{vector.ui_name} {axis}"
            value_node.location = (-1300 + (200 * axis_index), 1000 - (texture_node_row_width * (created_node_rows-1)) - 300 - (100 * created_value_rows))
            
            socket_params = vec4_param_name_to_socket_params[vector.param_id_name]
            if axis == 'X':
                socket_name = socket_params.x_socket_name
            elif axis == 'Y':
                socket_name = socket_params.y_socket_name
            elif axis == 'Z':
                socket_name = socket_params.z_socket_name
            elif axis == 'W':
                socket_name = socket_params.w_socket_name

            links.new(node_group_node.inputs[socket_name], value_node.outputs[0])

            if vector.param_id_name == ssbh_data_py.matl_data.ParamId.CustomVector6.name:
                for node in layer_1_uv_transform_nodes:
                    links.new(value_node.outputs[0], node.inputs[axis_index])
            elif vector.param_id_name == ssbh_data_py.matl_data.ParamId.CustomVector31.name:
                for node in layer_2_uv_transform_nodes:
                    links.new(value_node.outputs[0], node.inputs[axis_index])
            elif vector.param_id_name == ssbh_data_py.matl_data.ParamId.CustomVector32.name:
                for node in layer_3_uv_transform_nodes:
                    links.new(value_node.outputs[0], node.inputs[axis_index])
            elif vector.param_id_name == ssbh_data_py.matl_data.ParamId.CustomVector18.name:
                for node in sprite_sheet_nodes:
                    links.new(value_node.outputs[0], node.inputs[axis_index])
        created_value_rows = created_value_rows + 1

    for vertex_attribute in sub_matl_data.vertex_attributes:
        if vertex_attribute.name == 'colorSet1':
            # Create Node
            vertex_color_node: ShaderNodeVertexColor = nodes.new('ShaderNodeVertexColor')
            vertex_color_node.name = vertex_attribute.name
            vertex_color_node.label = vertex_attribute.name
            vertex_color_node.location = (-1300, 1000 - (texture_node_row_width * (created_node_rows-1)) - 300 - (100 * created_value_rows))
            vertex_color_node.layer_name = 'colorSet1'
            # Link Node
            links.new(vertex_color_node.outputs[0], node_group_node.inputs['colorSet1 RGB'])
            links.new(vertex_color_node.outputs[1], node_group_node.inputs['colorSet1 Alpha'])
            # Adjust row counter (for proper placement in UI)
            created_value_rows = created_value_rows + 1
        elif vertex_attribute.name == 'colorSet5':
            # Create Node
            vertex_color_node: ShaderNodeVertexColor = nodes.new('ShaderNodeVertexColor')
            vertex_color_node.name = vertex_attribute.name
            vertex_color_node.label = vertex_attribute.name
            vertex_color_node.location = (-1300, 1000 - (texture_node_row_width * (created_node_rows-1)) - 300 - (100 * created_value_rows))
            vertex_color_node.layer_name = 'colorSet5'
            # Link Node
            links.new(vertex_color_node.outputs[0], node_group_node.inputs['colorSet5 RGB'])
            links.new(vertex_color_node.outputs[1], node_group_node.inputs['colorSet5 Alpha'])
            # Adjust row counter (for proper placement in UI)
            created_value_rows = created_value_rows + 1

    sub_matl_float: SUB_PG_matl_float
    for sub_matl_float in sub_matl_data.floats:
        # Create Node
        value_node: ShaderNodeValue = nodes.new('ShaderNodeValue')
        value_node.name = sub_matl_float.param_id_name
        value_node.label = sub_matl_float.param_id_name
        value_node.location = (-1300, 1000 - (texture_node_row_width * (created_node_rows-1)) - 300 - (100 * created_value_rows))
        # Link Node
        links.new(value_node.outputs[0], node_group_node.inputs[sub_matl_float.ui_name])
        # Adjust row counter (for proper placement in UI)
        created_value_rows = created_value_rows + 1

    setup_sub_matl_data_node_drivers(sub_matl_data)
    node_group_node.show_options = False
    for input in node_group_node.inputs:
        if input.is_linked is False:
            input.hide = True


def get_vertex_attributes(shader_name:str)->list[str]:
    # Query the shader database for attribute information.
    # Using SQLite is much faster than iterating through the JSON dump.
    with sqlite3.connect(get_shader_db_file_path()) as con:
        # Construct a query to find all the vertex attributes for this shader.
        # Invalid shaders will return an empty list.
        sql = """
            SELECT v.AttributeName 
            FROM VertexAttribute v 
            INNER JOIN ShaderProgram s ON v.ShaderProgramID = s.ID 
            WHERE s.Name = ?
            """
        # The database has a single entry for each program, so don't include the render pass tag.
        return [row[0] for row in con.execute(sql, (shader_name[:len('SFX_PBS_0000000000000080')],)).fetchall()]
    
def setup_material_for_solid_view(material: bpy.types.Material):
    """
    Ensures materials are properly configured to display correctly in Solid view mode.
    This is particularly important for materials using multiple UV maps.
    """
    if not material.use_nodes or not material.node_tree:
        return
    
    # Ensure the material has proper display settings for Solid view
    material.use_backface_culling = True
    
    # Set up proper blend method for viewport display
    if material.blend_method == 'OPAQUE':
        # For opaque materials, ensure they display properly in Solid view
        material.use_screen_refraction = False
        material.use_refraction_depth = False
    
    # Check if material uses multiple UV maps and ensure proper setup
    nodes = material.node_tree.nodes
    uv_maps_used = set()
    
    for node in nodes:
        if node.type == 'UVMAP':
            if hasattr(node, 'uv_map') and node.uv_map:
                uv_maps_used.add(node.uv_map)
    
    # If multiple UV maps are used, ensure proper material setup
    if len(uv_maps_used) > 1:
        # Ensure the material has proper viewport display settings
        material.use_nodes = True
        
        # Set up proper material output for viewport display
        output_nodes = [n for n in nodes if n.type == 'OUTPUT_MATERIAL']
        if output_nodes:
            for output_node in output_nodes:
                # Ensure the output is properly connected for viewport display
                if output_node.target == 'EEVEE':
                    # Make sure EEVEE output is properly configured
                    if not output_node.inputs[0].links:
                        # If no surface input is connected, create a basic shader
                        principled = nodes.new('ShaderNodeBsdfPrincipled')
                        principled.location = (output_node.location[0] - 300, output_node.location[1])
                        material.node_tree.links.new(principled.outputs[0], output_node.inputs[0])
    
    # Special handling for materials with uvSet UV map
    if 'uvSet' in uv_maps_used:
        # Find all texture nodes that use uvSet
        uvset_texture_nodes = []
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                # Check if this texture is connected to a uvSet UV map
                for link in material.node_tree.links:
                    if link.to_node == node and link.from_node.type == 'UVMAP':
                        if hasattr(link.from_node, 'uv_map') and link.from_node.uv_map == 'uvSet':
                            uvset_texture_nodes.append(node)
                            break
        
        # Ensure uvSet textures are properly connected to the material output
        if uvset_texture_nodes:
            # Find or create a principled BSDF node
            principled_node = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    principled_node = node
                    break
            
            if not principled_node:
                principled_node = nodes.new('ShaderNodeBsdfPrincipled')
                principled_node.location = (0, 0)
            
            # Connect uvSet textures to the principled BSDF
            for tex_node in uvset_texture_nodes:
                # Check if texture is already connected
                connected = False
                for link in material.node_tree.links:
                    if link.from_node == tex_node and link.to_node == principled_node:
                        connected = True
                        break
                
                if not connected:
                    # Connect texture to base color if not already connected
                    if not principled_node.inputs['Base Color'].links:
                        material.node_tree.links.new(tex_node.outputs['Color'], principled_node.inputs['Base Color'])
                    else:
                        # If base color is already connected, create a mix node
                        mix_node = nodes.new('ShaderNodeMixRGB')
                        mix_node.location = (principled_node.location[0] - 300, principled_node.location[1])
                        mix_node.blend_type = 'MULTIPLY'
                        mix_node.inputs[0].default_value = 1.0  # Factor
                        
                        # Connect existing base color to mix node
                        existing_link = principled_node.inputs['Base Color'].links[0]
                        material.node_tree.links.new(existing_link.from_node.outputs[existing_link.from_socket.name], mix_node.inputs[1])
                        
                        # Connect new texture to mix node
                        material.node_tree.links.new(tex_node.outputs['Color'], mix_node.inputs[2])
                        
                        # Connect mix node to principled
                        material.node_tree.links.new(mix_node.outputs[0], principled_node.inputs['Base Color'])
            
            # Ensure principled BSDF is connected to material output
            output_nodes = [n for n in nodes if n.type == 'OUTPUT_MATERIAL']
            for output_node in output_nodes:
                if output_node.target == 'EEVEE':
                    if not output_node.inputs[0].links:
                        material.node_tree.links.new(principled_node.outputs[0], output_node.inputs[0])
                    elif output_node.inputs[0].links[0].from_node != principled_node:
                        # If connected to something else, create a mix shader
                        mix_shader = nodes.new('ShaderNodeMixShader')
                        mix_shader.location = (output_node.location[0] - 200, output_node.location[1])
                        mix_shader.inputs[0].default_value = 0.5  # Factor
                        
                        # Connect existing shader to mix
                        existing_link = output_node.inputs[0].links[0]
                        material.node_tree.links.new(existing_link.from_node.outputs[existing_link.from_socket.name], mix_shader.inputs[1])
                        
                        # Connect principled to mix
                        material.node_tree.links.new(principled_node.outputs[0], mix_shader.inputs[2])
                        
                        # Connect mix to output
                        material.node_tree.links.new(mix_shader.outputs[0], output_node.inputs[0])

def setup_eye_material_for_solid_view(material: bpy.types.Material):
    """
    Special handling for eye materials to ensure they display properly in Solid view mode.
    Eye materials often use multiple UV maps and need special configuration.
    """
    if not material.use_nodes or not material.node_tree:
        return
    
    # Eye materials often need special handling for viewport display
    material.use_backface_culling = False  # Eyes often need to be visible from both sides
    
    # Ensure proper blend method for eye materials
    if material.blend_method == 'OPAQUE':
        # For eye materials, we might need to adjust the blend method
        # Check if the material has transparency
        nodes = material.node_tree.nodes
        has_transparency = False
        
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                if node.image.alpha_mode != 'NONE':
                    has_transparency = True
                    break
        
        if has_transparency:
            material.blend_method = 'HASHED'
    
    # Special handling for dual UV map eye materials (map1 + uvSet)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    
    # Find textures for both UV maps
    map1_texture_nodes = []
    uvset_texture_nodes = []
    
    for node in nodes:
        if node.type == 'TEX_IMAGE' and node.image:
            # Check which UV map this texture uses
            for link in links:
                if link.to_node == node and link.from_node.type == 'UVMAP':
                    if hasattr(link.from_node, 'uv_map'):
                        if link.from_node.uv_map == 'map1':
                            map1_texture_nodes.append(node)
                        elif link.from_node.uv_map == 'uvSet':
                            uvset_texture_nodes.append(node)
                        break
    
    # If we have both map1 and uvSet textures, combine them properly
    if map1_texture_nodes and uvset_texture_nodes:
        # Find or create material output node
        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL' and node.target == 'EEVEE':
                output_node = node
                break
        
        if not output_node:
            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.target = 'EEVEE'
            output_node.location = (800, 0)
        
        # Create a principled BSDF for the combined result
        principled_node = nodes.new('ShaderNodeBsdfPrincipled')
        principled_node.location = (600, 0)
        
        # Connect map1 textures (white eye) as base
        if map1_texture_nodes:
            links.new(map1_texture_nodes[0].outputs['Color'], principled_node.inputs['Base Color'])
        
        # Connect uvSet textures (pupil) using mix nodes
        if uvset_texture_nodes:
            for i, tex_node in enumerate(uvset_texture_nodes):
                mix_node = nodes.new('ShaderNodeMixRGB')
                mix_node.location = (principled_node.location[0] - 300 * (i + 1), principled_node.location[1])
                mix_node.blend_type = 'MULTIPLY'
                mix_node.inputs[0].default_value = 1.0
                
                # Connect current base color to mix node
                if i == 0:
                    links.new(principled_node.inputs['Base Color'].links[0].from_node.outputs[principled_node.inputs['Base Color'].links[0].from_socket.name], mix_node.inputs[1])
                else:
                    prev_mix = nodes[f"mix_uvset_{i-1}"]
                    links.new(prev_mix.outputs[0], mix_node.inputs[1])
                
                # Connect uvSet texture to mix node
                links.new(tex_node.outputs['Color'], mix_node.inputs[2])
                mix_node.name = f"mix_uvset_{i}"
                
                # Connect final mix to principled
                if i == len(uvset_texture_nodes) - 1:
                    links.new(mix_node.outputs[0], principled_node.inputs['Base Color'])
        
        # Connect principled BSDF to material output
        links.new(principled_node.outputs[0], output_node.inputs[0])
    
    # Ensure proper UV map setup for eye materials
    for node in nodes:
        if node.type == 'UVMAP':
            # Make sure UV map nodes are properly configured
            if hasattr(node, 'uv_map') and node.uv_map:
                # Ensure the UV map is properly assigned
                if node.uv_map == 'uvSet':
                    # For eye materials using uvSet, ensure proper texture assignment
                    for tex_node in nodes:
                        if tex_node.type == 'TEX_IMAGE' and tex_node.image:
                            # Ensure texture is properly connected
                            if not tex_node.outputs['Color'].links:
                                # Connect to a basic shader if not connected
                                principled = nodes.new('ShaderNodeBsdfPrincipled')
                                principled.location = (tex_node.location[0] + 300, tex_node.location[1])
                                links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])

def create_blender_materials_from_matl(operator: bpy.types.Operator, ssbh_matl: ssbh_data_py.matl_data.MatlData, model_dir: str = None) -> dict[str, bpy.types.Material]:
    '''
    Creates a blender material with the sub_matl_data filled out for every entry in the ssbh_matl.
    Returns a dictionary mapping the material_label to the created blender material to handle multiple models 
    having the same material name.
    '''
    # Setup default textures if not already made
    create_default_textures()
    # Make new Blender Materials
    material_label_to_material: dict[str, bpy.types.Material] = \
        {entry.material_label : bpy.data.materials.new(entry.material_label) for entry in ssbh_matl.entries}
    # Import images 
    if model_dir is None:
        model_dir = bpy.context.scene.sub_scene_properties.model_import_folder_path
    texture_name_to_image_dict = import_material_images(operator, ssbh_matl, model_dir)
    # Fill out the sub_matl_data of each material
    for entry in ssbh_matl.entries:
        sub_matl_data: SUB_PG_sub_matl_data = material_label_to_material[entry.material_label].sub_matl_data
        sub_matl_data.set_shader_label(entry.shader_label)
        sub_matl_data.add_bools(entry.booleans)
        sub_matl_data.add_floats(entry.floats)
        sub_matl_data.add_vectors(entry.vectors)
        sub_matl_data.add_textures(entry.textures, texture_name_to_image_dict)
        sub_matl_data.add_samplers(entry.samplers)
        sub_matl_data.add_blend_states(entry.blend_states)
        sub_matl_data.add_rasterizer_states(entry.rasterizer_states)
        attrs = get_vertex_attributes(entry.shader_label)
        sub_matl_data.add_vertex_attributes(attrs)
        
        # Setup the material node tree
        setup_blender_material_node_tree(material_label_to_material[entry.material_label])
        
        # Configure material for proper Solid view display
        setup_material_for_solid_view(material_label_to_material[entry.material_label])
        
        # Special handling for eye materials
        if 'Eye' in entry.material_label:
            setup_eye_material_for_solid_view(material_label_to_material[entry.material_label])
        
        # Set blend method based on shader and blend states
        blend_method = get_blend_method(entry.shader_label, sub_matl_data.blend_states)
        material_label_to_material[entry.material_label].blend_method = blend_method
        
        # Set backface culling based on rasterizer states
        if len(sub_matl_data.rasterizer_states) > 0:
            if sub_matl_data.rasterizer_states[0].cull_mode == 'Back':
                material_label_to_material[entry.material_label].use_backface_culling = True

    # Eye materials implicitly use extra materials despite no mesh being explicitly assigned.
    # Need to track these to preserve them on export.
    for material_label, material in material_label_to_material.items():
        sub_matl_data: SUB_PG_sub_matl_data = material.sub_matl_data
        if (match := re.match(r"(Eye[L|R])(\d?)", material_label)):
            label_no_digit, optional_digit = match.groups(default='')
            for linked_material_suffix in ('L', 'D', 'G'):
                if (linked_material := material_label_to_material.get(f'{label_no_digit}{linked_material_suffix}{optional_digit}')):
                    new_linked_material: SUB_PG_matl_linked_material = sub_matl_data.linked_materials.add()
                    new_linked_material.blender_material = linked_material

    # Make the blender material settings
    for material_label, material in material_label_to_material.items():
        setup_blender_material_settings(material)

    return material_label_to_material
