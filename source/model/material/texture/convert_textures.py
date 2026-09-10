import bpy
import os
from pathlib import Path
from mathutils import Color
import tempfile
import numpy as np  # Add NumPy import for faster array processing
from multiprocessing.pool import ThreadPool  # For parallel processing
import time
# PIL dependency removed - using Blender native functionality instead

def process_image_in_parallel(func, args_list, max_workers=4):
    """
    Process multiple images in parallel using a thread pool
    
    Args:
        func: The function to run in parallel
        args_list: List of argument tuples to pass to the function
        max_workers: Maximum number of parallel workers
    
    Returns:
        List of results from the function calls
    """
    print(f"Processing {len(args_list)} images in parallel with {max_workers} workers")
    start_time = time.time()
    
    # Create thread pool
    pool = ThreadPool(processes=max_workers)
    
    # Map function over arguments
    results = pool.starmap(func, args_list)
    
    # Close the pool
    pool.close()
    pool.join()
    
    elapsed_time = time.time() - start_time
    print(f"Parallel processing completed in {elapsed_time:.2f} seconds")
    
    return results

def find_principled_bsdf_node(material):
    """Find Principled BSDF node in the material's node tree"""
    if not material or not material.node_tree:
        return None
    
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    
    return None

def get_input_texture(node, input_name):
    """Get texture from an input socket of a node"""
    if not node:
        return None
    
    input_socket = node.inputs.get(input_name)
    if not input_socket or not input_socket.links:
        return None
    
    # Trace back through links
    from_node = input_socket.links[0].from_node
    
    # If it's an image texture node, return the image
    if from_node.type == 'TEX_IMAGE' and from_node.image:
        return from_node.image
    
    return None

def get_input_value(node, input_name):
    """Get value from an input socket of a node"""
    if not node:
        return None
    
    input_socket = node.inputs.get(input_name)
    if not input_socket:
        return None
    
    # If connected, can't get direct value
    if input_socket.links:
        return None
    
    return input_socket.default_value

def bake_texture(material, attribute, size=1024, temp_dir=None):
    """
    Bake a specific attribute of a material to a new texture
    
    Args:
        material: The material to bake
        attribute: The attribute to bake ('diffuse', 'normal', 'metallic', etc.)
        size: The size of the baked texture
        temp_dir: Directory to save the temporary image
    
    Returns:
        The baked image
    """
    start_time = time.time()
    print(f"Baking {attribute} for material {material.name} at size {size}x{size}")
    
    # Create a temporary file if temp_dir is specified
    if temp_dir:
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
        temp_file = os.path.join(temp_dir, f"bake_{material.name}_{attribute}.png")
    else:
        # Create a dummy file for the target path
        temp_file = os.path.join(tempfile.gettempdir(), f"bake_{material.name}_{attribute}.png")
    
    print(f"Temporary bake file: {temp_file}")
    
    # Get active object and check materials
    active_object = bpy.context.active_object
    if not active_object:
        raise ValueError("No active object")
        
    # We'll restore the active material later
    original_active_material = None
    if active_object and active_object.active_material:
        original_active_material = active_object.active_material
    
    # Temporarily set our target material as the active material
    # Find an object using this material or the active object
    found_object = None
    for obj in bpy.context.scene.objects:
        if obj.type == 'MESH':
            for mat in obj.material_slots:
                if mat.material == material:
                    found_object = obj
                    break
            if found_object:
                break
    
    if not found_object:
        # If we can't find an object with this material, use the active object
        # and temporarily assign the material to it
        if not active_object or active_object.type != 'MESH':
            raise ValueError("No suitable object found to apply material for baking")
        
        found_object = active_object
        
        # Temporarily add a material slot if needed
        material_slot_added = False
        if len(found_object.material_slots) == 0:
            bpy.ops.object.material_slot_add()
            material_slot_added = True
        
        # Store the original material to restore later
        original_material = found_object.material_slots[found_object.active_material_index].material
        
        # Assign our material
        found_object.material_slots[found_object.active_material_index].material = material
    
    # Set as active object
    bpy.context.view_layer.objects.active = found_object
    
    # Create a new temporary image to bake to
    if attribute in ['normal', 'metallic', 'roughness', 'specular', 'ao']:
        # Technical textures get Non-Color space
        colorspace = 'Non-Color'
    else:
        # Color textures get sRGB space
        colorspace = 'sRGB'
    
    temp_image = bpy.data.images.new(
        f"bake_{material.name}_{attribute}",
        width=size,
        height=size,
        alpha=True
    )
    temp_image.filepath = temp_file
    temp_image.colorspace_settings.name = colorspace
    
    # Wait for dependency graph to update
    bpy.context.view_layer.update()
    
    # Store original render settings
    original_engine = bpy.context.scene.render.engine
    original_samples = None
    if hasattr(bpy.context.scene.cycles, 'samples'):
        original_samples = bpy.context.scene.cycles.samples
    
    # Configure for baking
    bpy.context.scene.render.engine = 'CYCLES'
    
    # Reduce samples for quicker baking
    if hasattr(bpy.context.scene.cycles, 'samples'):
        bpy.context.scene.cycles.samples = 32  # Lower for quicker baking
    
    # Configure bake settings
    bpy.context.scene.render.bake.use_pass_direct = False
    bpy.context.scene.render.bake.use_pass_indirect = False
    bpy.context.scene.render.bake.use_pass_color = True
    
    # Different settings for different attributes
    if attribute == 'normal':
        bake_type = 'NORMAL'
        bpy.context.scene.render.bake.normal_space = 'TANGENT'
    elif attribute == 'ao':
        bake_type = 'AO'
    elif attribute == 'roughness':
        bake_type = 'ROUGHNESS'
    elif attribute == 'metallic':
        bake_type = 'EMISSION'  # Blender doesn't have a direct metallic bake, use emission
    elif attribute == 'specular':
        bake_type = 'EMISSION'  # Blender doesn't have a direct specular bake, use emission
    else:
        bake_type = 'DIFFUSE'
    
    try:
        # Prepare material for baking based on attribute
        if attribute in ['metallic', 'roughness', 'specular']:
            # For PBR attributes that don't have direct baking, we need to create a temporary node setup
            # Save existing node setup
            original_use_nodes = material.use_nodes
            original_nodes = []
            original_links = []
            
            if material.use_nodes:
                for node in material.node_tree.nodes:
                    original_nodes.append(node)
                for link in material.node_tree.links:
                    original_links.append((link.from_socket, link.to_socket))
            
            # Enable nodes
            material.use_nodes = True
            
            # Create emission node for baking attribute
            emission = material.node_tree.nodes.new('ShaderNodeEmission')
            output = None
            
            # Find the Principled BSDF
            principled = find_principled_bsdf_node(material)
            
            if principled:
                # Find material output
                for node in material.node_tree.nodes:
                    if node.type == 'OUTPUT_MATERIAL' and node.is_active_output:
                        output = node
                        break
                
                if not output:
                    output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
                    output.is_active_output = True
                
                # Connect attribute to emission
                if attribute == 'metallic':
                    # Try to get existing value or connection
                    input_socket = principled.inputs.get("Metallic")
                elif attribute == 'roughness':
                    input_socket = principled.inputs.get("Roughness")
                elif attribute == 'specular':
                    input_socket = principled.inputs.get("Specular")
                
                if input_socket:
                    # If input is linked, connect the same texture to emission
                    if input_socket.is_linked:
                        from_socket = input_socket.links[0].from_socket
                        material.node_tree.links.new(from_socket, emission.inputs[0])
                    else:
                        # Otherwise use the value directly
                        emission.inputs[0].default_value = (
                            input_socket.default_value,
                            input_socket.default_value,
                            input_socket.default_value,
                            1.0
                        )
                
                # Connect emission to output
                material.node_tree.links.new(emission.outputs[0], output.inputs[0])
        
        # Set active image in all UV editors
        for area in bpy.context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.spaces.active.image = temp_image
        
        # Bake
        print(f"Starting {attribute} bake with type {bake_type}...")
        bpy.ops.object.bake(
            type=bake_type,
            width=size,
            height=size,
            margin=16,
            use_clear=True,
            save_mode='INTERNAL'
        )
        
        print(f"Bake completed for {attribute}.")
        
        # Save baked image
        temp_image.save()
        
    except Exception as e:
        print(f"Error during baking: {str(e)}")
        raise
    
    finally:
        # Restore original material connections if we modified them
        if attribute in ['metallic', 'roughness', 'specular'] and material.use_nodes:
            # Clear nodes
            material.node_tree.nodes.clear()
            
            # Restore original nodes
            for node in original_nodes:
                material.node_tree.nodes.append(node)
            
            # Restore links
            for from_socket, to_socket in original_links:
                try:
                    material.node_tree.links.new(from_socket, to_socket)
                except:
                    # Some sockets might not exist anymore
                    pass
            
            # Restore use_nodes setting
            material.use_nodes = original_use_nodes
        
        # Restore original material if we temporarily assigned one
        if not found_object == active_object and original_active_material:
            bpy.context.view_layer.objects.active = active_object
            active_object.active_material = original_active_material
        elif 'original_material' in locals():
            found_object.material_slots[found_object.active_material_index].material = original_material
            if material_slot_added:
                bpy.ops.object.material_slot_remove()
        
        # Restore render settings
        bpy.context.scene.render.engine = original_engine
        if original_samples is not None and hasattr(bpy.context.scene.cycles, 'samples'):
            bpy.context.scene.cycles.samples = original_samples
    
    # Return the baked image
    elapsed_time = time.time() - start_time
    print(f"Baking completed in {elapsed_time:.2f} seconds")
    return temp_image

def extract_pbr_data_from_material(material, bake_size=1024):
    """Extract PBR data from a Blender material"""
    principled = find_principled_bsdf_node(material)
    if not principled:
        raise ValueError(f"Material '{material.name}' doesn't have a Principled BSDF node")
    
    result = {}
    
    # Extract normal map
    normal_img = get_input_texture(principled, "Normal")
    if normal_img:
        result['normal'] = normal_img
    else:
        # Bake normal map if there's no direct texture
        # But only if there's a normal input value that's not 0
        normal_val = get_input_value(principled, "Normal")
        if normal_val and normal_val > 0.01:
            result['normal'] = bake_texture(material, 'normal', bake_size)
    
    # Extract metallic
    metallic_img = get_input_texture(principled, "Metallic")
    if metallic_img:
        result['metallic'] = metallic_img
    else:
        # Get metallic value or bake if necessary
        metallic_val = get_input_value(principled, "Metallic")
        if metallic_val is not None:
            # Create a simple metallic texture
            metallic_img = bpy.data.images.new(f"{material.name}_metallic", width=2, height=2, alpha=True)
            # Fill with solid color based on metallic value
            pixels = [metallic_val, metallic_val, metallic_val, 1.0] * 4
            metallic_img.pixels = pixels
            result['metallic'] = metallic_img
        else:
            # Complex node setup, need to bake
            result['metallic'] = bake_texture(material, 'metallic', bake_size)
    
    # Extract roughness
    roughness_img = get_input_texture(principled, "Roughness")
    if roughness_img:
        result['roughness'] = roughness_img
    else:
        # Get roughness value or bake if necessary
        roughness_val = get_input_value(principled, "Roughness")
        if roughness_val is not None:
            # Create a simple roughness texture
            roughness_img = bpy.data.images.new(f"{material.name}_roughness", width=2, height=2, alpha=True)
            # Fill with solid color based on roughness value
            pixels = [roughness_val, roughness_val, roughness_val, 1.0] * 4
            roughness_img.pixels = pixels
            result['roughness'] = roughness_img
        else:
            # Complex node setup, need to bake
            result['roughness'] = bake_texture(material, 'roughness', bake_size)
    
    # Extract specular
    specular_img = get_input_texture(principled, "Specular")
    if specular_img:
        result['specular'] = specular_img
    else:
        # Get specular value
        specular_val = get_input_value(principled, "Specular")
        if specular_val is not None:
            # Create a simple specular texture
            specular_img = bpy.data.images.new(f"{material.name}_specular", width=2, height=2, alpha=True)
            # Fill with solid color based on specular value
            pixels = [specular_val, specular_val, specular_val, 1.0] * 4
            specular_img.pixels = pixels
            result['specular'] = specular_img
        else:
            # Complex node setup, need to bake
            result['specular'] = bake_texture(material, 'specular', bake_size)
    
    # Try to find Ambient Occlusion
    # AO is often not connected to principled directly, so we need to check other nodes
    ao_img = None
    for node in material.node_tree.nodes:
        if node.type == 'AMBIENT_OCCLUSION':
            if node.outputs['AO'].links:
                # Try to trace back if this is connected to a texture
                ao_socket = node.inputs.get('Color')
                if ao_socket and ao_socket.links:
                    from_node = ao_socket.links[0].from_node
                    if from_node.type == 'TEX_IMAGE' and from_node.image:
                        ao_img = from_node.image
                        break
    
    if ao_img:
        result['ao'] = ao_img
    else:
        # Bake AO if we couldn't find it
        result['ao'] = bake_texture(material, 'ao', bake_size)
    
    return result

def create_simple_texture(name, size=1024, color=(0.5, 0.5, 0.5, 1.0), color_space='Non-Color'):
    """
    Create a simple texture with a solid color
    
    Args:
        name: Name of the texture
        size: Size of the texture (width and height)
        color: RGBA color tuple
        color_space: Colorspace to use ('sRGB' or 'Non-Color')
    
    Returns:
        Created image
    """
    print(f"Creating simple texture: {name} with size {size}x{size}")
    img = bpy.data.images.new(name, width=size, height=size, alpha=True)
    
    # Create a NumPy array of the correct size and fill with the color
    total_pixels = size * size * 4
    pixels_np = np.tile(np.array(color, dtype=np.float32), size * size).reshape(size * size, 4)
    
    # Convert to a flat array for Blender
    pixels_flat = pixels_np.flatten()
    
    # Assign pixels to the image
    img.pixels.foreach_set(pixels_flat, total_pixels)
    
    # Set the color space
    img.colorspace_settings.name = color_space
    
    return img

def extract_pbr_data_simple(material):
    """Extract basic PBR data from a Principled BSDF without baking"""
    print(f"Using simple extraction method for material: {material.name}")
    principled = find_principled_bsdf_node(material)
    if not principled:
        raise ValueError(f"Material '{material.name}' doesn't have a Principled BSDF node")
    
    result = {}
    
    # Get all input values at once for efficiency
    inputs = {
        "Normal": (0.5, 0.5, 1.0, 1.0),  # Default normal map (neutral - flat surface)
        "Metallic": 0.0,                 # Default metallic (non-metal)
        "Roughness": 0.5,                # Default roughness (semi-rough)
        "Specular": 0.5,                 # Default specular (medium)
        "Base Color": (0.8, 0.8, 0.8, 1.0) # Default albedo (light gray)
    }
    
    # Extract values from Principled BSDF
    for input_name in inputs.keys():
        value = get_input_value(principled, input_name)
        if value is not None:
            if isinstance(inputs[input_name], tuple):
                # For color inputs, get the color
                if hasattr(principled.inputs[input_name], "default_value"):
                    # Copy the color but ensure alpha is 1.0
                    color_value = list(principled.inputs[input_name].default_value)
                    if len(color_value) == 4:
                        color_value[3] = 1.0
                    inputs[input_name] = tuple(color_value)
            else:
                # For scalar inputs, store the value
                inputs[input_name] = value
    
    # Create textures efficiently
    texture_size = 1024
    
    # Create normal map - Use Non-Color space
    normal_img = create_simple_texture(
        f"{material.name}_normal", 
        texture_size, 
        inputs["Normal"],
        'Non-Color'
    )
    result['normal'] = normal_img
    
    # Create metallic map - Use Non-Color space
    metallic_val = inputs["Metallic"]
    metallic_img = create_simple_texture(
        f"{material.name}_metallic", 
        texture_size, 
        (metallic_val, metallic_val, metallic_val, 1.0),
        'Non-Color'
    )
    result['metallic'] = metallic_img
    
    # Create roughness map - Use Non-Color space
    roughness_val = inputs["Roughness"]
    roughness_img = create_simple_texture(
        f"{material.name}_roughness", 
        texture_size, 
        (roughness_val, roughness_val, roughness_val, 1.0),
        'Non-Color'
    )
    result['roughness'] = roughness_img
    
    # Create specular map - Use Non-Color space
    specular_val = inputs["Specular"]
    specular_img = create_simple_texture(
        f"{material.name}_specular", 
        texture_size, 
        (specular_val, specular_val, specular_val, 1.0),
        'Non-Color'
    )
    result['specular'] = specular_img
    
    # Create default AO map (no occlusion) - Use Non-Color space
    ao_img = create_simple_texture(
        f"{material.name}_ao", 
        texture_size, 
        (1.0, 1.0, 1.0, 1.0),
        'Non-Color'
    )
    result['ao'] = ao_img
    
    # Create color/albedo map - Use sRGB space for color
    color_val = inputs["Base Color"]
    color_img = create_simple_texture(
        f"{material.name}_color", 
        texture_size, 
        color_val,
        'sRGB'
    )
    result['color'] = color_img
    
    print(f"Successfully created simple PBR textures for {material.name} at {texture_size}x{texture_size}")
    print(f"Values used - Metallic: {metallic_val:.2f}, Roughness: {roughness_val:.2f}, Specular: {specular_val:.2f}")
    
    return result

def create_nor_from_material(material, output_path=None, directx_format=False, bake_size=1024):
    """
    Create a NOR texture from a Blender material using optimized NumPy operations
    
    Args:
        material: The Blender material to extract data from
        output_path: Where to save the NOR texture
        directx_format: Whether to handle normal maps as DirectX format
        bake_size: Size to use for baking textures
    
    Returns:
        Path to the created NOR texture
    """
    start_time = time.time()
    if not output_path:
        output_path = os.path.join(tempfile.gettempdir(), f"{material.name}_NOR.png")
    
    print(f"Creating NOR texture for material: {material.name}")
    print(f"Output path: {output_path}")
    
    # Try to extract PBR data with full baking
    try:
        print("Trying full PBR data extraction...")
        pbr_data = extract_pbr_data_from_material(material, bake_size)
    except Exception as e:
        print(f"Full extraction failed: {str(e)}")
        print("Falling back to simple extraction method...")
        pbr_data = extract_pbr_data_simple(material)
    
    # Check if we have the required textures
    if 'normal' not in pbr_data:
        print("No normal map data found, creating default normal map")
        pbr_data['normal'] = create_simple_texture(f"{material.name}_normal", 1024, (0.5, 0.5, 1.0, 1.0))
    
    # Create a new image for the output NOR texture
    normal_img = pbr_data['normal']
    width, height = normal_img.size
    print(f"Creating NOR texture with size {width}x{height}")
    nor_img = bpy.data.images.new(f"{material.name}_NOR", width=width, height=height, alpha=True)
    
    # Get pixel data as NumPy arrays
    pixel_count = width * height * 4
    normal_pixels_np = np.zeros(pixel_count, dtype=np.float32)
    normal_img.pixels.foreach_get(normal_pixels_np, pixel_count)
    normal_pixels_np = normal_pixels_np.reshape(width * height, 4)
    
    # Create output array
    nor_pixels_np = np.zeros_like(normal_pixels_np)
    
    # Get AO pixels for cavity map if available
    cavity_pixels_np = None
    if 'ao' in pbr_data:
        ao_img = pbr_data['ao']
        # Check if AO image has same dimensions
        if ao_img.size[0] == width and ao_img.size[1] == height:
            cavity_pixels_np = np.zeros(pixel_count, dtype=np.float32)
            ao_img.pixels.foreach_get(cavity_pixels_np, pixel_count)
            cavity_pixels_np = cavity_pixels_np.reshape(width * height, 4)
        else:
            print("AO texture has different dimensions, using default white")
    else:
        print("No AO data found for cavity map, using default white")
    
    # Process pixel data using NumPy operations
    # Red channel (X+) - copy directly from normal map
    nor_pixels_np[:, 0] = normal_pixels_np[:, 0]
    
    # Green channel (Y+)
    if directx_format:
        # For DirectX normal maps (Y-), flip the green channel
        nor_pixels_np[:, 1] = 1.0 - normal_pixels_np[:, 1]
    else:
        # For OpenGL normal maps (Y+), use as is
        nor_pixels_np[:, 1] = normal_pixels_np[:, 1]
    
    # Blue channel (transition blend) - use flat white by default
    nor_pixels_np[:, 2] = 1.0
    
    # Alpha channel (cavity map) - use AO if available, otherwise flat white
    if cavity_pixels_np is not None:
        nor_pixels_np[:, 3] = cavity_pixels_np[:, 0]  # Use red channel of AO map
    else:
        nor_pixels_np[:, 3] = 1.0
    
    # Flatten the array for Blender
    nor_pixels_flat = nor_pixels_np.flatten()
    
    # Assign the processed pixels to the output image
    nor_img.pixels.foreach_set(nor_pixels_flat, pixel_count)
    
    # Save the output image
    nor_img.filepath_raw = output_path
    nor_img.file_format = 'PNG'
    print(f"Saving NOR texture to: {output_path}")
    nor_img.save()
    
    # Clean up
    bpy.data.images.remove(nor_img)
    
    elapsed_time = time.time() - start_time
    print(f"NOR texture creation completed successfully in {elapsed_time:.2f} seconds")
    return output_path

def find_fpv3_material_node(material):
    """Find FPv3 Material node in a material's node tree"""
    if not material.use_nodes or not material.node_tree:
        return None
    
    # Look for specific node types that could be the FPv3 Material
    for node in material.node_tree.nodes:
        # Check for group nodes with FPv3 in the name or label
        if node.type == 'GROUP' and ('FPv3' in node.name or 'FPv3' in node.label or 'Fortnite' in node.name):
            return node
        
        # Check for custom nodes with fpv3 in the identifier
        if hasattr(node, 'bl_idname') and ('fpv3' in node.bl_idname.lower() or 'fortnite' in node.bl_idname.lower()):
            return node
        
        # Check if it's a shader node with specific Fortnite inputs
        if node.type == 'BSDF_PRINCIPLED' or node.type == 'GROUP':
            # Check for common Fortnite-specific input names
            fortnite_inputs = ['Base Color', 'Base Roughness', 'Metallic', 'Specular', 'Emissive', 'SubSurface']
            fortnite_input_count = sum(1 for name in fortnite_inputs if name in node.inputs)
            
            # If it has most of the Fortnite inputs, it's likely the FPv3 Material
            if fortnite_input_count >= 4:
                # Additional check: Look for connected textures with _M, _S, _D naming pattern
                has_fortnite_textures = False
                for input_socket in node.inputs.values():
                    if input_socket.is_linked:
                        from_node = input_socket.links[0].from_node
                        if from_node.type == 'TEX_IMAGE' and from_node.image:
                            if any(suffix in from_node.image.name for suffix in ['_M', '_S', '_D', '_N']):
                                has_fortnite_textures = True
                                break
                
                if has_fortnite_textures:
                    print(f"Detected Fortnite FPv3 Material node: {node.name}")
                    return node
    
    # Check output connections - sometimes the FPv3 Material is connected to a Material Output node
    for node in material.node_tree.nodes:
        if node.type == 'OUTPUT_MATERIAL' and node.inputs['Surface'].is_linked:
            from_node = node.inputs['Surface'].links[0].from_node
            # If it's a custom node or has specific properties
            if from_node.type == 'GROUP' or from_node.type == 'BSDF_PRINCIPLED':
                # Look for connected textures with Fortnite naming patterns
                has_fortnite_textures = False
                for input_socket in from_node.inputs.values():
                    if input_socket.is_linked:
                        tex_node = input_socket.links[0].from_node
                        if tex_node.type == 'TEX_IMAGE' and tex_node.image:
                            if any(suffix in tex_node.image.name for suffix in ['_M', '_S', '_D', '_N']):
                                has_fortnite_textures = True
                                print(f"Found Fortnite textures connected to {from_node.name}")
                                return from_node
    
    return None

def extract_fortnite_textures(material):
    """Extract Fortnite-specific textures from a material with FPv3 shader"""
    if not material.use_nodes or not material.node_tree:
        return None, None, None
    
    # Look for textures with specific names or connections
    m_texture = None  # The _M texture (contains AO in red, subsurface in blue)
    s_texture = None  # The _S texture (contains specular in red, roughness in green, metal in blue)
    d_texture = None  # The _D texture (diffuse/albedo)
    
    for node in material.node_tree.nodes:
        if node.type != 'TEX_IMAGE' or not node.image:
            continue
            
        # Check image name for _M, _S, or _D patterns
        if node.image.name.endswith('_M') or '_M.' in node.image.name:
            m_texture = node.image
        elif node.image.name.endswith('_S') or '_S.' in node.image.name:
            s_texture = node.image
        elif node.image.name.endswith('_D') or '_D.' in node.image.name:
            d_texture = node.image
    
    # If we couldn't find by name, try by connections
    if not any([m_texture, s_texture, d_texture]):
        fpv3_node = find_fpv3_material_node(material)
        if fpv3_node:
            for input_name in fpv3_node.inputs.keys():
                input_socket = fpv3_node.inputs.get(input_name)
                if not input_socket or not input_socket.is_linked:
                    continue
                
                from_node = input_socket.links[0].from_node
                if from_node.type != 'TEX_IMAGE' or not from_node.image:
                    continue
                
                # Try to identify by input names
                if any(x in input_name.lower() for x in ['mask', 'ao', 'subsurface', 'sss']):
                    m_texture = from_node.image
                elif any(x in input_name.lower() for x in ['specular', 'roughness', 'metallic', 'metal']):
                    s_texture = from_node.image
                elif any(x in input_name.lower() for x in ['diffuse', 'albedo', 'color', 'base color', 'basecolor']):
                    d_texture = from_node.image
    
    return m_texture, s_texture, d_texture

def visualize_texture_channels(m_texture, s_texture, is_skin_material, output_path):
    """
    Create a debug visualization showing how Fortnite texture channels map to the PRM texture.
    
    Args:
        m_texture: The _M texture image
        s_texture: The _S texture image
        is_skin_material: Whether this is a skin material
        output_path: The output path for the visualization
    """
    try:
        # Only proceed if PIL is available
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        import os
        
        # Determine the size of the visualization
        viz_width = 1024
        viz_height = 800
        
        # Create a black background image
        viz_img = Image.new('RGB', (viz_width, viz_height), (30, 30, 30))
        draw = ImageDraw.Draw(viz_img)
        
        # Try to get a font
        font = None
        try:
            # Try to get a system font
            font = ImageFont.truetype("arial.ttf", 14)
        except:
            try:
                # Try a fallback
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
            except:
                # Use default font
                font = ImageFont.load_default()
        
        # Add title
        title = "Fortnite Texture to PRM Channel Mapping"
        draw.text((viz_width//2 - 200, 20), title, fill=(255, 255, 255), font=font)
        
        # Define the layout
        texture_size = 200
        spacing = 20
        
        # Load and resize the input textures
        m_img = None
        s_img = None
        
        if m_texture:
            # Convert Blender image to PIL
            m_pixels = np.array(m_texture.pixels[:]) * 255
            m_pixels = m_pixels.astype(np.uint8).reshape((m_texture.size[1], m_texture.size[0], 4))
            m_img = Image.fromarray(m_pixels, 'RGBA')
            m_img = m_img.resize((texture_size, texture_size), Image.LANCZOS)
        
        if s_texture:
            # Convert Blender image to PIL
            s_pixels = np.array(s_texture.pixels[:]) * 255
            s_pixels = s_pixels.astype(np.uint8).reshape((s_texture.size[1], s_texture.size[0], 4))
            s_img = Image.fromarray(s_pixels, 'RGBA')
            s_img = s_img.resize((texture_size, texture_size), Image.LANCZOS)
        
        # Draw the input textures
        y_pos = 70
        
        # Draw _M texture if available
        if m_img:
            draw.text((spacing, y_pos), "_M Texture", fill=(255, 255, 255), font=font)
            viz_img.paste(m_img, (spacing, y_pos + 20))
            
            # Draw channel indicators
            channel_y = y_pos + 20 + texture_size + 10
            draw.text((spacing, channel_y), "Red → PRM Blue (AO)", fill=(255, 0, 0), font=font)
        
        # Draw _S texture if available
        if s_img:
            draw.text((spacing + texture_size + spacing, y_pos), "_S Texture", fill=(255, 255, 255), font=font)
            viz_img.paste(s_img, (spacing + texture_size + spacing, y_pos + 20))
            
            # Draw channel indicators
            channel_y = y_pos + 20 + texture_size + 10
            draw.text((spacing + texture_size + spacing, channel_y), "Red → PRM Alpha (Specular)", fill=(255, 0, 0), font=font)
            draw.text((spacing + texture_size + spacing, channel_y + 20), "Green → PRM Red (Metalness)", fill=(0, 255, 0), font=font)
            draw.text((spacing + texture_size + spacing, channel_y + 40), "Blue → PRM Green (Roughness)", fill=(0, 0, 255), font=font)
        
        # Draw PRM channel diagram
        prm_x = spacing + texture_size * 2 + spacing * 2
        prm_y = y_pos + 20
        
        # Draw PRM texture box
        draw.rectangle((prm_x, prm_y, prm_x + texture_size, prm_y + texture_size), outline=(255, 255, 255))
        draw.text((prm_x, y_pos), "PRM Texture", fill=(255, 255, 255), font=font)
        
        # Draw PRM channel labels
        draw.text((prm_x + 10, prm_y + 40), "R: Metalness", fill=(255, 0, 0), font=font)
        draw.text((prm_x + 10, prm_y + 60), "G: Roughness", fill=(0, 255, 0), font=font)
        draw.text((prm_x + 10, prm_y + 80), "B: Ambient Occlusion", fill=(0, 0, 255), font=font)
        draw.text((prm_x + 10, prm_y + 100), "A: Specular", fill=(255, 255, 255), font=font)
        
        # Add note about FPv3 material
        draw.text((prm_x, prm_y + texture_size + 20), "Using GIMP Channel Mapping Workflow", fill=(255, 255, 0), font=font)
        
        # Draw arrows showing the mapping
        # From _M Red to PRM Blue
        if m_img:
            draw.line((spacing + texture_size//2, y_pos + 20 + texture_size//4, 
                        prm_x, prm_y + 80), fill=(255, 0, 0), width=2)
        
        # From _S Red to PRM Alpha
        if s_img:
            draw.line((spacing + texture_size + spacing + texture_size//2, y_pos + 20 + texture_size//4, 
                        prm_x, prm_y + 100), fill=(255, 0, 0), width=2)
            
            # From _S Green to PRM Red (Metalness)
            draw.line((spacing + texture_size + spacing + texture_size//2, y_pos + 20 + texture_size//2, 
                        prm_x, prm_y + 40), fill=(0, 255, 0), width=2)
            
            # From _S Blue to PRM Green (Roughness)
            draw.line((spacing + texture_size + spacing + texture_size//2, y_pos + 20 + texture_size*3//4, 
                        prm_x, prm_y + 60), fill=(0, 0, 255), width=2)
        
        # Add legend at bottom
        legend_y = y_pos + 20 + texture_size + 80
        draw.text((spacing, legend_y), "Channel Mapping Legend:", fill=(255, 255, 255), font=font)
        draw.text((spacing, legend_y + 20), "M texture Red → PRM Blue (Ambient Occlusion)", fill=(255, 255, 255), font=font)
        draw.text((spacing, legend_y + 40), "S texture Green → PRM Red (Metalness)", fill=(255, 255, 255), font=font)
        draw.text((spacing, legend_y + 60), "S texture Blue → PRM Green (Roughness)", fill=(255, 255, 255), font=font)
        draw.text((spacing, legend_y + 80), "S texture Red → PRM Alpha (Specular)", fill=(255, 255, 255), font=font)
        
        # Save the visualization
        viz_img.save(output_path)
        print(f"Channel mapping visualization saved to {output_path}")
        return True
    
    except Exception as e:
        print(f"Failed to create visualization: {e}")
        import traceback
        traceback.print_exc()
        return False

def create_prm_from_material(material, output_path=None, bake_size=1024):
    """
    Create a PRM texture from a Blender material. The PRM texture contains:
    - Red channel: Metalness (0-1)
    - Green channel: Roughness (0-1)
    - Blue channel: Ambient Occlusion (0-1)
    - Alpha channel: Specular (0-1, scaled by 0.2)
    
    Args:
        material: The Blender material to create a PRM texture from
        output_path: Optional path to save the PRM texture to. If None, returns the Blender image.
        bake_size: Size of the texture to create (square)
        
    Returns:
        If output_path is provided: The path to the saved PRM texture
        If output_path is None: The Blender image object
    """
    import time
    start_time = time.time()
    print(f"Starting PRM creation for material '{material.name}'...")

    # If bake_size is too large, reduce it to prevent memory issues
    if bake_size > 2048:
        print(f"Warning: Reducing bake size from {bake_size} to 2048 to prevent memory issues")
        bake_size = 2048
    
    # Define default values based on Smash Ultimate defaults
    default_metalness = 0.0    # No metalness by default
    default_roughness = 0.5    # Mid roughness by default
    default_ao = 1.0           # Full ambient occlusion by default
    default_specular = 0.16    # Specular value scaled by 0.2 (0.8 * 0.2 = 0.16)
    
    # Create a new image for the PRM texture
    image_name = f"{material.name}_PRM"
    
    # Check if image already exists and reuse it
    existing_img = bpy.data.images.get(image_name)
    if existing_img and existing_img.size[0] == bake_size and existing_img.size[1] == bake_size:
        print(f"Reusing existing PRM image for material '{material.name}'")
        prm_img = existing_img
    else:
        print(f"Creating new PRM image for material '{material.name}'")
        prm_img = bpy.data.images.new(image_name, width=bake_size, height=bake_size, alpha=True)
    
    prm_img.colorspace_settings.name = 'Non-Color'  # Important for non-color data

    # Initialize arrays for each channel
    metalness = np.full((bake_size, bake_size), default_metalness, dtype=np.float32)
    roughness = np.full((bake_size, bake_size), default_roughness, dtype=np.float32)
    ao = np.full((bake_size, bake_size), default_ao, dtype=np.float32)
    specular = np.full((bake_size, bake_size), default_specular, dtype=np.float32)
    
    try:
        # Set a timeout to prevent infinite processing
        timeout = 60  # seconds
        
        # First check for FPv3 Material from Fortnite
        print(f"Checking for FPv3 Material node in '{material.name}'...")
        fpv3_node = find_fpv3_material_node(material)
        if fpv3_node:
            print(f"Found FPv3 Material node '{fpv3_node.name}' in material '{material.name}'")
            
            # Check if this is a skin material by looking at subsurface inputs
            is_skin_material = False
            if hasattr(fpv3_node.inputs.get('Subsurface', None), 'default_value'):
                is_skin_material = fpv3_node.inputs['Subsurface'].default_value > 0.01
            elif hasattr(fpv3_node.inputs.get('Subsurface Weight', None), 'default_value'):
                is_skin_material = fpv3_node.inputs['Subsurface Weight'].default_value > 0.01
            
            print(f"Material '{material.name}' is {'a skin material' if is_skin_material else 'not a skin material'}")
            
            # Extract Fortnite-specific textures
            print(f"Extracting Fortnite textures for '{material.name}'...")
            m_texture, s_texture, d_texture = extract_fortnite_textures(material)
            
            # Create visualization of channel mapping if output path is provided
            if output_path and m_texture and s_texture:
                import os
                viz_path = os.path.splitext(output_path)[0] + "_mapping.png"
                print(f"Creating channel mapping visualization at '{viz_path}'")
                visualize_texture_channels(m_texture, s_texture, is_skin_material, viz_path)
            
            if m_texture or s_texture:
                print(f"Found Fortnite textures for material '{material.name}'")
                print(f"M texture: {m_texture.name if m_texture else 'None'}")
                print(f"S texture: {s_texture.name if s_texture else 'None'}")
                print(f"D texture: {d_texture.name if d_texture else 'None'}")
                
                # --- IMPROVED TEXTURE PROCESSING ---
                processing_start = time.time()
                print(f"Processing textures for '{material.name}'...")
                
                # First load and resize textures if needed
                m_pixels = None
                s_pixels = None
                
                # Process _M texture if found
                if m_texture:
                    try:
                        print(f"Processing _M texture '{m_texture.name}'...")
                        # Get the image dimensions
                        m_width, m_height = m_texture.size
                        
                        # For PIL-based resizing
                        try:
                            from PIL import Image
                            import io
                            
                            # Export image to bytes - Fix the BytesIO error
                            # Instead of using save_render, we'll create a temporary file
                            import tempfile
                            import os
                            temp_path = os.path.join(tempfile.gettempdir(), f"temp_{m_texture.name}")
                            m_texture.filepath_raw = temp_path
                            m_texture.file_format = 'PNG'
                            m_texture.save()
                            
                            # Open with PIL and resize
                            img = Image.open(temp_path)
                            img = img.resize((bake_size, bake_size), Image.LANCZOS)
                            
                            # Flip the image vertically to correct orientation
                            img = img.transpose(Image.FLIP_TOP_BOTTOM)
                            
                            # Clean up temp file
                            try:
                                os.remove(temp_path)
                            except Exception as e:
                                print(f"Warning: Could not remove temporary file: {e}")
                            
                            # Convert to numpy array
                            m_pixels = np.array(img) / 255.0
                            print(f"Processed _M texture using PIL in {time.time() - processing_start:.2f} seconds")
                            
                        except ImportError:
                            # Fallback: Use numpy-based resizing
                            print("PIL not available, using numpy for _M texture resizing")
                            m_raw = np.array(m_texture.pixels[:]).reshape((m_height, m_width, 4))
                            
                            # Create a new array for the resized image
                            m_pixels = np.zeros((bake_size, bake_size, 4))
                            
                            # Simple nearest-neighbor resize with vertical flip
                            for y in range(bake_size):
                                # Check for timeout
                                if time.time() - start_time > timeout:
                                    print(f"Timeout reached while processing _M texture")
                                    raise TimeoutError("Texture processing timeout")
                                    
                                # Flip y-coordinate for vertical flip
                                flipped_y = bake_size - y - 1
                                
                                for x in range(bake_size):
                                    orig_y = int(y * m_height / bake_size)
                                    orig_x = int(x * m_width / bake_size)
                                    m_pixels[flipped_y, x] = m_raw[orig_y, orig_x]
                            print(f"Processed _M texture using numpy in {time.time() - processing_start:.2f} seconds")
                    except Exception as e:
                        print(f"Failed to process _M texture: {e}")
                        import traceback
                        traceback.print_exc()
                
                # Process _S texture if found
                if s_texture:
                    try:
                        s_processing_start = time.time()
                        print(f"Processing _S texture '{s_texture.name}'...")
                        # Get the image dimensions
                        s_width, s_height = s_texture.size
                        
                        # For PIL-based resizing
                        try:
                            from PIL import Image
                            import io
                            
                            # Export image to bytes - Fix the BytesIO error
                            # Instead of using save_render, we'll create a temporary file
                            import tempfile
                            import os
                            temp_path = os.path.join(tempfile.gettempdir(), f"temp_{s_texture.name}")
                            s_texture.filepath_raw = temp_path
                            s_texture.file_format = 'PNG'
                            s_texture.save()
                            
                            # Open with PIL and resize
                            img = Image.open(temp_path)
                            img = img.resize((bake_size, bake_size), Image.LANCZOS)
                            
                            # Flip the image vertically to correct orientation
                            img = img.transpose(Image.FLIP_TOP_BOTTOM)
                            
                            # Clean up temp file
                            try:
                                os.remove(temp_path)
                            except Exception as e:
                                print(f"Warning: Could not remove temporary file: {e}")
                            
                            # Convert to numpy array
                            s_pixels = np.array(img) / 255.0
                            print(f"Processed _S texture using PIL in {time.time() - s_processing_start:.2f} seconds")
                            
                        except ImportError:
                            # Fallback: Use numpy-based resizing
                            print("PIL not available, using numpy for _S texture resizing")
                            s_raw = np.array(s_texture.pixels[:]).reshape((s_height, s_width, 4))
                            
                            # Create a new array for the resized image
                            s_pixels = np.zeros((bake_size, bake_size, 4))
                            
                            # Simple nearest-neighbor resize with vertical flip
                            for y in range(bake_size):
                                # Check for timeout
                                if time.time() - start_time > timeout:
                                    print(f"Timeout reached while processing _S texture")
                                    raise TimeoutError("Texture processing timeout")
                                    
                                # Flip y-coordinate for vertical flip
                                flipped_y = bake_size - y - 1
                                
                                for x in range(bake_size):
                                    orig_y = int(y * s_height / bake_size)
                                    orig_x = int(x * s_width / bake_size)
                                    s_pixels[flipped_y, x] = s_raw[orig_y, orig_x]
                            print(f"Processed _S texture using numpy in {time.time() - s_processing_start:.2f} seconds")
                    except Exception as e:
                        print(f"Failed to process _S texture: {e}")
                        import traceback
                        traceback.print_exc()
                
                # Now apply the channel mapping according to the exact rules:
                print(f"Applying channel mapping for '{material.name}'...")
                
                mapping_start = time.time()
                
                # Reshape the PRM arrays to 2D for easier manipulation
                metalness_2d = metalness.reshape((bake_size, bake_size))
                roughness_2d = roughness.reshape((bake_size, bake_size))
                ao_2d = ao.reshape((bake_size, bake_size))
                specular_2d = specular.reshape((bake_size, bake_size))
                
                # Apply the correct channel mapping for Fortnite textures:
                # Debug the S texture channels first
                is_valid_fortnite_s_texture = False
                if s_pixels is not None:
                    print(f"S texture shape: {s_pixels.shape}")
                    red_stats = (np.min(s_pixels[:,:,0]), np.max(s_pixels[:,:,0]), np.mean(s_pixels[:,:,0]))
                    green_stats = (np.min(s_pixels[:,:,1]), np.max(s_pixels[:,:,1]), np.mean(s_pixels[:,:,1]))
                    blue_stats = (np.min(s_pixels[:,:,2]), np.max(s_pixels[:,:,2]), np.mean(s_pixels[:,:,2]))
                    
                    print(f"S texture Red channel stats: min={red_stats[0]:.3f}, max={red_stats[1]:.3f}, mean={red_stats[2]:.3f}")
                    print(f"S texture Green channel stats: min={green_stats[0]:.3f}, max={green_stats[1]:.3f}, mean={green_stats[2]:.3f}")
                    print(f"S texture Blue channel stats: min={blue_stats[0]:.3f}, max={blue_stats[1]:.3f}, mean={blue_stats[2]:.3f}")
                    
                    # Check if this is actually a valid Fortnite S texture (channels should be different)
                    tolerance = 0.01  # Small tolerance for floating point comparison
                    has_different_channels = (abs(red_stats[2] - green_stats[2]) > tolerance or 
                                            abs(red_stats[2] - blue_stats[2]) > tolerance or 
                                            abs(green_stats[2] - blue_stats[2]) > tolerance)
                    
                    # Check if user has explicitly set Principled BSDF slider values that differ from defaults
                    principled_node = find_principled_bsdf_node(material)
                    user_wants_slider_values = False
                    if principled_node:
                        # Check if user has set non-default metallic value and it's not linked to a texture
                        metal_input = principled_node.inputs.get('Metallic')
                        roughness_input = principled_node.inputs.get('Roughness')
                        specular_input = principled_node.inputs.get('Specular')
                        
                        # Check if any of the main inputs are unlinked and have non-default values
                        metallic_is_explicit = (metal_input and not metal_input.is_linked and 
                                              abs(metal_input.default_value - 0.0) < 0.1)  # User set metallic to 0
                        roughness_is_explicit = (roughness_input and not roughness_input.is_linked and 
                                                abs(roughness_input.default_value - 0.5) > 0.1)  # User changed from default 0.5
                        
                        if metallic_is_explicit or roughness_is_explicit:
                            user_wants_slider_values = True
                            print(f"User has set explicit slider values (Metallic={metal_input.default_value if metal_input else 'N/A'}, Roughness={roughness_input.default_value if roughness_input else 'N/A'}) - prioritizing slider values over texture channels")
                    
                    if has_different_channels and not user_wants_slider_values:
                        is_valid_fortnite_s_texture = True
                        print("S texture appears to be a valid Fortnite texture with separate R/G/B channels")
                    else:
                        if not has_different_channels:
                            print("S texture has identical R/G/B channels - this is not a real Fortnite texture, falling back to regular PBR handling")
                        else:
                            print("S texture has different channels but user has set explicit slider values - falling back to regular PBR handling")
                
                # For skin materials, metalness should be very low (skin is not metallic)
                # For Fortnite S textures: Red=Specular, Green=Roughness, Blue=Metalness
                # But for skin, we might want to override metalness to be very low
                is_skin_material = 'skin' in material.name.lower() or 'flesh' in material.name.lower()
                
                # Only apply Fortnite channel mapping if it's a valid Fortnite texture
                if is_valid_fortnite_s_texture:
                    # R: blue channel from S texture (metalness) - but override for skin
                    if s_pixels is not None:
                        if is_skin_material:
                            print("Material appears to be skin - setting metalness to very low values")
                            # For skin, metalness should be nearly 0
                            metalness_2d = np.clip(s_pixels[:, :, 2] * 0.1, 0.0, 0.05)  # Severely reduce metalness for skin
                        else:
                            print("Mapping S texture Blue channel to PRM Red (Metalness)")
                            metalness_2d = s_pixels[:, :, 2]  # Blue channel to Metalness
                    
                    # G: green channel from S texture (roughness)
                    if s_pixels is not None:
                        print("Mapping S texture Green channel to PRM Green (Roughness)")
                        roughness_2d = s_pixels[:, :, 1]  # Green channel to Roughness
                    
                    # B: red channel from M texture (ambient occlusion)
                    if m_pixels is not None:
                        print("Mapping M texture Red channel to PRM Blue (Ambient Occlusion)")
                        ao_2d = m_pixels[:, :, 0]  # Red channel to AO
                    
                    # A: red channel from S texture (specular)
                    if s_pixels is not None:
                        print("Mapping S texture Red channel to PRM Alpha (Specular)")
                        specular_2d = s_pixels[:, :, 0] * 0.2  # Red channel to Specular (scaled by 0.2)
                else:
                    # Safe fallback: extract slider values directly without any risky baking
                    print("S texture is not a valid Fortnite texture, using slider values directly (no baking to avoid modifying existing textures)")
                    
                    # Extract values directly from Principled BSDF sliders - much safer than baking
                    principled_node = find_principled_bsdf_node(material)
                    if principled_node:
                        # Get metalness from slider
                        metal_input = principled_node.inputs.get('Metallic')
                        if metal_input and not metal_input.is_linked:
                            metallic_value = metal_input.default_value
                            print(f"Using Metallic slider value: {metallic_value}")
                            metalness_2d = np.full((bake_size, bake_size), metallic_value, dtype=np.float32)
                        else:
                            print("Metallic input is linked to a texture or not found, using default value")
                            metalness_2d = np.full((bake_size, bake_size), default_metalness, dtype=np.float32)
                            
                        # Get roughness from slider
                        roughness_input = principled_node.inputs.get('Roughness')
                        if roughness_input and not roughness_input.is_linked:
                            roughness_value = roughness_input.default_value
                            print(f"Using Roughness slider value: {roughness_value}")
                            roughness_2d = np.full((bake_size, bake_size), roughness_value, dtype=np.float32)
                        else:
                            print("Roughness input is linked to a texture or not found, using default value")
                            roughness_2d = np.full((bake_size, bake_size), default_roughness, dtype=np.float32)
                        
                        # Get specular from slider (scale by 0.2 for Smash Ultimate)
                        specular_input = principled_node.inputs.get('Specular')
                        if specular_input and not specular_input.is_linked:
                            specular_value = specular_input.default_value * 0.2  # Scale for Smash Ultimate
                            print(f"Using Specular slider value: {specular_value} (scaled from {specular_input.default_value})")
                            specular_2d = np.full((bake_size, bake_size), specular_value, dtype=np.float32)
                        else:
                            print("Specular input is linked to a texture or not found, using default value")
                            specular_2d = np.full((bake_size, bake_size), default_specular * 0.2, dtype=np.float32)
                        
                        # Use default AO (no complex baking that could affect other textures)
                        ao_2d = np.full((bake_size, bake_size), default_ao, dtype=np.float32)
                        print(f"Using default AO value: {default_ao}")
                        
                    else:
                        print("No Principled BSDF found, using absolute defaults")
                        # Use absolute defaults
                        metalness_2d = np.full((bake_size, bake_size), default_metalness, dtype=np.float32)
                        roughness_2d = np.full((bake_size, bake_size), default_roughness, dtype=np.float32)
                        specular_2d = np.full((bake_size, bake_size), default_specular * 0.2, dtype=np.float32)
                        ao_2d = np.full((bake_size, bake_size), default_ao, dtype=np.float32)
                
                # Flatten the arrays back for pixel assignment
                metalness = metalness_2d.flatten()
                roughness = roughness_2d.flatten()
                ao = ao_2d.flatten()
                specular = specular_2d.flatten()
                
                print(f"Channel mapping completed in {time.time() - mapping_start:.2f} seconds")
                
                print(f"PRM texture created from Fortnite textures for '{material.name}'")
                
            else:
                # Original FPv3 material handling code (without specific _M and _S textures)
                # This handles materials that use the FPv3 node but don't use the standard texture naming
                # Look for texture inputs specific to FPv3 Material
                print(f"No specific Fortnite textures found, looking for inputs on FPv3 node...")
                metallic_img = None
                roughness_img = None
                ao_img = None
                specular_value = default_specular  # Default specular value for Smash Ultimate
                
                # Process each input to find connected textures
                for input_name in fpv3_node.inputs.keys():
                    input_socket = fpv3_node.inputs.get(input_name)
                    
                    if not input_socket:
                        continue
                    
                    # Check for connected texture
                    if input_socket.is_linked:
                        from_node = input_socket.links[0].from_node
                        if from_node.type == 'TEX_IMAGE' and from_node.image:
                            # Map FPv3 inputs to PRM channels
                            if 'metal' in input_name.lower() or input_name.endswith('_M'):
                                metallic_img = from_node.image
                                print(f"Found metallic texture: {metallic_img.name}")
                            elif 'rough' in input_name.lower() or input_name.endswith('_R'):
                                roughness_img = from_node.image
                                print(f"Found roughness texture: {roughness_img.name}")
                            elif 'ao' in input_name.lower() or 'ambient' in input_name.lower() or input_name.endswith('_AO'):
                                ao_img = from_node.image
                                print(f"Found AO texture: {ao_img.name}")
                    elif hasattr(input_socket, 'default_value'):
                        # For non-texture inputs, get the values directly
                        if 'metal' in input_name.lower():
                            metalness = np.full((bake_size, bake_size), input_socket.default_value, dtype=np.float32)
                            print(f"Using metallic value from input: {input_socket.default_value}")
                        elif 'rough' in input_name.lower():
                            roughness = np.full((bake_size, bake_size), input_socket.default_value, dtype=np.float32)
                            print(f"Using roughness value from input: {input_socket.default_value}")
                        elif 'specular' in input_name.lower():
                            # Adjust specular to Smash Ultimate's range (0-0.2)
                            specular_value = min(input_socket.default_value * 0.2, 0.2)
                            specular = np.full((bake_size, bake_size), specular_value, dtype=np.float32)
                            print(f"Using specular value from input: {specular_value}")
                
                # Process metallic texture if found
                if metallic_img:
                    try:
                        print(f"Processing metallic texture '{metallic_img.name}'...")
                        metallic_pixels = np.array(metallic_img.pixels[:]).reshape((-1, 4))
                        # Extract grayscale value (could be in red channel only for some textures)
                        if len(metallic_pixels) == bake_size * bake_size:
                            metalness = metallic_pixels[:, 0].reshape((bake_size, bake_size))
                        else:
                            # Need to resize
                            metalness = extract_texture_from_input(
                                fpv3_node.inputs.get(next(name for name in fpv3_node.inputs.keys() if 'metal' in name.lower())),
                                bake_size
                            )
                            if metalness is None:
                                # Fallback to default
                                metalness = np.full((bake_size, bake_size), default_metalness, dtype=np.float32)
                    except Exception as e:
                        print(f"Failed to process metallic texture: {e}")
                
                # Process roughness texture if found
                if roughness_img:
                    try:
                        print(f"Processing roughness texture '{roughness_img.name}'...")
                        roughness_pixels = np.array(roughness_img.pixels[:]).reshape((-1, 4))
                        # Extract grayscale value (could be in green channel for some textures)
                        if len(roughness_pixels) == bake_size * bake_size:
                            roughness = roughness_pixels[:, 0].reshape((bake_size, bake_size))
                        else:
                            # Need to resize
                            roughness = extract_texture_from_input(
                                fpv3_node.inputs.get(next(name for name in fpv3_node.inputs.keys() if 'rough' in name.lower())),
                                bake_size
                            )
                            if roughness is None:
                                # Fallback to default
                                roughness = np.full((bake_size, bake_size), default_roughness, dtype=np.float32)
                    except Exception as e:
                        print(f"Failed to process roughness texture: {e}")
                
                # Process AO texture if found
                if ao_img:
                    try:
                        print(f"Processing AO texture '{ao_img.name}'...")
                        ao_pixels = np.array(ao_img.pixels[:]).reshape((-1, 4))
                        # Extract grayscale value
                        if len(ao_pixels) == bake_size * bake_size:
                            ao = ao_pixels[:, 0].reshape((bake_size, bake_size))
                        else:
                            # Need to resize
                            ao = extract_texture_from_input(
                                fpv3_node.inputs.get(next(name for name in fpv3_node.inputs.keys() if 'ao' in name.lower() or 'ambient' in name.lower())),
                                bake_size
                            )
                            if ao is None:
                                # Fallback to default
                                ao = np.full((bake_size, bake_size), default_ao, dtype=np.float32)
                    except Exception as e:
                        print(f"Failed to process AO texture: {e}")

        else:
            print(f"No FPv3 Material node found, checking for Principled BSDF...")
            # Original Principled BSDF handling code
            # Extract values from Principled BSDF if it exists
            if material.use_nodes and material.node_tree:
                principled_node = None
                
                # Find Principled BSDF node
                for node in material.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        principled_node = node
                        break
                
                if principled_node:
                    print(f"Found Principled BSDF node '{principled_node.name}'")
                    
                    # Track whether we're using textures or flat values
                    has_metalness_texture = False
                    has_roughness_texture = False
                    has_specular_texture = False
                    has_ao_texture = False
                    
                    # Extract metalness value
                    metal_input = principled_node.inputs.get('Metallic')
                    if metal_input:
                        if metal_input.is_linked:
                            print(f"Metallic input is linked to a texture")
                            # Try to get texture from this input
                            metal_value = extract_texture_from_input(metal_input, bake_size)
                            if metal_value is not None:
                                metalness = metal_value
                                has_metalness_texture = True
                            else:
                                print(f"Failed to extract metallic texture, using default value: {default_metalness}")
                                metalness = np.full((bake_size, bake_size), default_metalness, dtype=np.float32)
                        else:
                            # Use the direct slider value
                            metallic_value = metal_input.default_value
                            print(f"Using Metallic slider value: {metallic_value}")
                            metalness = np.full((bake_size, bake_size), metallic_value, dtype=np.float32)
                    
                    # Extract roughness value
                    roughness_input = principled_node.inputs.get('Roughness')
                    if roughness_input:
                        if roughness_input.is_linked:
                            print(f"Roughness input is linked to a texture")
                            # Try to get texture from this input
                            roughness_value = extract_texture_from_input(roughness_input, bake_size)
                            if roughness_value is not None:
                                roughness = roughness_value
                                has_roughness_texture = True
                            else:
                                print(f"Failed to extract roughness texture, using default value: {default_roughness}")
                                roughness = np.full((bake_size, bake_size), default_roughness, dtype=np.float32)
                        else:
                            # Use the direct slider value
                            roughness_value = roughness_input.default_value
                            print(f"Using Roughness slider value: {roughness_value}")
                            roughness = np.full((bake_size, bake_size), roughness_value, dtype=np.float32)
                    
                    # Extract specular value (scaled by 0.2 as per Smash spec)
                    specular_input = principled_node.inputs.get('Specular')
                    if specular_input:
                        if specular_input.is_linked:
                            print(f"Specular input is linked to a texture")
                            # Try to get texture from this input
                            specular_value = extract_texture_from_input(specular_input, bake_size)
                            if specular_value is not None:
                                # Scale by 0.2 for Smash Ultimate's range
                                specular = specular_value * 0.2
                                has_specular_texture = True
                            else:
                                print(f"Failed to extract specular texture, using default value: {default_specular}")
                                specular = np.full((bake_size, bake_size), default_specular, dtype=np.float32)
                        else:
                            # Use the direct slider value, scaled by 0.2 for Smash Ultimate's range
                            specular_value = specular_input.default_value * 0.2
                            print(f"Using Specular slider value: {specular_input.default_value} (scaled to {specular_value} for Smash Ultimate)")
                            specular = np.full((bake_size, bake_size), specular_value, dtype=np.float32)
                    
                    # Look for AO texture
                    # This could be connected to the material in various ways
                    for node in material.node_tree.nodes:
                        if node.type == 'AMBIENT_OCCLUSION':
                            if node.outputs.get('AO') and node.outputs['AO'].links:
                                print(f"Found connected Ambient Occlusion node")
                                # Try to get the AO color
                                ao_color_input = node.inputs.get('Color')
                                if ao_color_input and ao_color_input.is_linked:
                                    ao_value = extract_texture_from_input(ao_color_input, bake_size)
                                    if ao_value is not None:
                                        ao = ao_value
                                        has_ao_texture = True
                                        print(f"Using AO texture from Ambient Occlusion node")
                                        break
                        elif node.type == 'TEX_IMAGE' and node.image:
                            # Check if image name suggests it's an AO map
                            if any(x in node.image.name.lower() for x in ['ao', 'ambient', 'occlusion']):
                                print(f"Found potential AO texture: {node.image.name}")
                                try:
                                    ao_pixels = np.array(node.image.pixels[:]).reshape((-1, 4))
                                    if len(ao_pixels) == bake_size * bake_size:
                                        ao = np.mean(ao_pixels[:, :3], axis=1).reshape((bake_size, bake_size))
                                        has_ao_texture = True
                                        print(f"Using AO texture: {node.image.name}")
                                        break
                                    else:
                                        # Resize the texture
                                        w, h = node.image.size
                                        x0 = np.floor(np.linspace(0, w-1, bake_size)).astype(int)
                                        y0 = np.floor(np.linspace(0, h-1, bake_size)).astype(int)
                                        
                                        # Convert to 2D array for easier indexing
                                        gray_2d = np.mean(np.array(node.image.pixels[:]).reshape((h, w, 4))[:,:,:3], axis=2)
                                        
                                        # Get the values at the pixel coordinates
                                        resized_values = np.zeros((bake_size, bake_size))
                                        for i in range(bake_size):
                                            for j in range(bake_size):
                                                resized_values[i, j] = gray_2d[y0[i], x0[j]]
                                        
                                        ao = resized_values.flatten()
                                        has_ao_texture = True
                                        print(f"Using resized AO texture: {node.image.name}")
                                    break
                                except Exception as e:
                                    print(f"Failed to process AO texture: {e}")
                    
                    if not has_ao_texture:
                        print(f"No AO texture found, using default value: {default_ao}")
                    
                    # Log the final values being used
                    print(f"Final PRM values for '{material.name}':")
                    print(f"  - Metalness: {'texture' if has_metalness_texture else f'flat value {np.mean(metalness):.3f}'}")
                    print(f"  - Roughness: {'texture' if has_roughness_texture else f'flat value {np.mean(roughness):.3f}'}")
                    print(f"  - AO: {'texture' if has_ao_texture else f'flat value {np.mean(ao):.3f}'}")
                    print(f"  - Specular: {'texture' if has_specular_texture else f'flat value {np.mean(specular):.3f}'}")
                else:
                    print(f"No Principled BSDF node found in material '{material.name}'")
        
        # Combine channels to create the PRM texture
        print(f"Combining channels for final PRM texture...")
        combination_start = time.time()
        
        # Reshape arrays to 1D for pixels assignment
        metalness_flat = metalness.flatten()
        roughness_flat = roughness.flatten()
        ao_flat = ao.flatten()
        specular_flat = specular.flatten()
        
        # Debug: Log the actual values being used in the final PRM
        print(f"Final PRM texture values:")
        print(f"  - Metalness: min={np.min(metalness_flat):.3f}, max={np.max(metalness_flat):.3f}, mean={np.mean(metalness_flat):.3f}")
        print(f"  - Roughness: min={np.min(roughness_flat):.3f}, max={np.max(roughness_flat):.3f}, mean={np.mean(roughness_flat):.3f}")
        print(f"  - AO: min={np.min(ao_flat):.3f}, max={np.max(ao_flat):.3f}, mean={np.mean(ao_flat):.3f}")
        print(f"  - Specular: min={np.min(specular_flat):.3f}, max={np.max(specular_flat):.3f}, mean={np.mean(specular_flat):.3f}")
        
        # Interleave the channel data (RGBA format)
        pixel_count = bake_size * bake_size
        pixels = np.empty(pixel_count * 4, dtype=np.float32)
        
        # Assign channels in the correct order
        pixels[0::4] = metalness_flat  # R channel = Metalness
        pixels[1::4] = roughness_flat  # G channel = Roughness
        pixels[2::4] = ao_flat         # B channel = AO
        pixels[3::4] = specular_flat   # A channel = Specular
        
        # Set the pixels
        print(f"Setting pixel data for PRM texture...")
        prm_img.pixels = pixels.tolist()
        print(f"Pixel data set in {time.time() - combination_start:.2f} seconds")
        
        # Pack the image if we're keeping it in Blender
        if not output_path:
            if not prm_img.packed_file:
                print(f"Packing PRM texture...")
                prm_img.pack()
            print(f"PRM texture creation completed in {time.time() - start_time:.2f} seconds")
            return prm_img
        
        # Save to external file if requested
        print(f"Saving PRM texture to {output_path}...")
        prm_img.filepath_raw = output_path
        prm_img.file_format = 'PNG'
        prm_img.save()
        
        print(f"PRM texture creation completed in {time.time() - start_time:.2f} seconds")
        return output_path
    
    except Exception as e:
        print(f"Error creating PRM texture: {e}")
        import traceback
        traceback.print_exc()
        if output_path:
            # Create a default PRM texture as fallback
            print(f"Creating default PRM texture due to error...")
            create_default_prm_texture(output_path, bake_size)
            return output_path
        else:
            # Fill with default values as fallback
            print(f"Using default values for PRM texture due to error...")
            pixels = []
            for i in range(bake_size * bake_size):
                pixels.extend([default_metalness, default_roughness, default_ao, default_specular])
            prm_img.pixels = pixels
            if not prm_img.packed_file:
                prm_img.pack()
            return prm_img

def extract_texture_from_input(input_socket, target_size):
    """
    Attempt to extract a texture from an input socket
    
    Args:
        input_socket: The input socket to extract from
        target_size: The target texture size
        
    Returns:
        numpy array of pixel values or None if extraction fails
    """
    if not input_socket.links:
        return None
    
    # Find the source node
    from_node = input_socket.links[0].from_node
    
    # If it's a texture node with an image
    if from_node.type == 'TEX_IMAGE' and from_node.image:
        try:
            # Get the image pixels
            img = from_node.image
            pixels = np.array(img.pixels[:]).reshape((-1, 4))
            
            # Check if image size matches target size
            if pixels.shape[0] != target_size * target_size:
                # Extract grayscale value (average of RGB)
                gray = np.mean(pixels[:, :3], axis=1)
                
                # Resize using Blender's built-in functionality instead of PIL
                orig_width, orig_height = img.size
                
                # Create new image at target size
                resized_img = bpy.data.images.new(
                    f"{img.name}_resized", 
                    width=target_size, 
                    height=target_size, 
                    alpha=True
                )
                
                # Use numpy to scale the pixels
                # This is a simple resize method - can be improved with better interpolation
                gray_2d = gray.reshape(orig_height, orig_width)
                
                # Simple bilinear sampling to resize
                y_indices = np.linspace(0, orig_height-1, target_size)
                x_indices = np.linspace(0, orig_width-1, target_size)
                
                # Floor the indices to get the pixel coordinates
                y0 = np.floor(y_indices).astype(int)
                x0 = np.floor(x_indices).astype(int)
                
                # Ensure we don't go out of bounds
                y0 = np.clip(y0, 0, orig_height-1)
                x0 = np.clip(x0, 0, orig_width-1)
                
                # Get the values at the pixel coordinates
                resized_values = np.zeros((target_size, target_size))
                for i in range(target_size):
                    for j in range(target_size):
                        resized_values[i, j] = gray_2d[y0[i], x0[j]]
                
                return resized_values.flatten()
            else:
                # Extract grayscale value
                return np.mean(pixels[:, :3], axis=1).reshape((target_size, target_size))
        except Exception as e:
            print(f"Failed to process texture: {e}")
            return None
    
    # For more complex node setups, we would need to bake
    return None

def create_default_prm_texture(output_path, size=1024):
    """Create a default PRM texture with reasonable values using Blender's native functionality"""
    # Create a new image
    img = bpy.data.images.new(f"default_PRM", width=size, height=size, alpha=True)
    
    # Set the default values:
    # Red (Metalness): 0.0
    # Green (Roughness): 0.5
    # Blue (Ambient Occlusion): 1.0
    # Alpha (Specular): 0.16
    
    # Create array of pixel values
    pixel_count = size * size
    pixels = np.zeros(pixel_count * 4, dtype=np.float32)
    
    # Set default values for each channel
    pixels[0::4] = 0.0  # Red (Metalness)
    pixels[1::4] = 0.5  # Green (Roughness)
    pixels[2::4] = 1.0  # Blue (AO)
    pixels[3::4] = 0.16  # Alpha (Specular)
    
    # Assign to image
    img.pixels = pixels.tolist()
    
    # Save to file
    img.filepath_raw = output_path
    img.file_format = 'PNG'
    img.save()

class ULTIMATE_OT_create_nor_from_material(bpy.types.Operator):
    """Create a NOR texture from the active material's Principled BSDF shader"""
    bl_description = "Create a NOR texture from the active material's Principled BSDF shader"
    bl_idname = "ultimate.create_nor_from_material"
    bl_label = "Create NOR from Material"
    bl_options = {'REGISTER', 'UNDO'}
    
    output_path: bpy.props.StringProperty(
        name="Output Path",
        description="Where to save the NOR texture",
        subtype='FILE_PATH'
    )
    
    directx_format: bpy.props.BoolProperty(
        name="DirectX Format (Y-)",
        description="Enable if your normal map uses DirectX format with Y-",
        default=False
    )
    
    bake_size: bpy.props.IntProperty(
        name="Bake Size",
        description="Size of textures to bake if needed",
        default=1024,
        min=128,
        max=4096
    )
    
    show_warning: bpy.props.BoolProperty(default=True)
    
    def invoke(self, context, event):
        # Show warning first if this is the initial invoke
        if self.show_warning:
            self.show_warning = False
            return context.window_manager.invoke_confirm(
                self, event, 
                message="Creating a NOR texture is resource-intensive and may take time depending on your computer."
            )
        
        # Set a default output path based on the active material name
        if context.active_object and context.active_object.active_material:
            material_name = context.active_object.active_material.name
            self.output_path = os.path.join(tempfile.gettempdir(), f"{material_name}_NOR.png")
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        if not context.active_object or not context.active_object.active_material:
            self.report({'ERROR'}, "No active material selected")
            return {'CANCELLED'}
        
        material = context.active_object.active_material
        
        try:
            output = create_nor_from_material(
                material,
                self.output_path,
                self.directx_format,
                self.bake_size
            )
            self.report({'INFO'}, f"NOR texture created at {output}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Error creating NOR texture: {str(e)}")
            return {'CANCELLED'}

class ULTIMATE_OT_create_prm_from_material(bpy.types.Operator):
    """Create a PRM texture from the active material's Principled BSDF shader"""
    bl_description = "Create a PRM texture from the active material's Principled BSDF shader"
    bl_idname = "ultimate.create_prm_from_material"
    bl_label = "Create PRM from Material"
    bl_options = {'REGISTER', 'UNDO'}
    
    output_path: bpy.props.StringProperty(
        name="Output Path",
        description="Where to save the PRM texture",
        subtype='FILE_PATH'
    )
    
    bake_size: bpy.props.IntProperty(
        name="Bake Size",
        description="Size of textures to bake if needed",
        default=1024,
        min=128,
        max=4096
    )
    
    show_warning: bpy.props.BoolProperty(default=True)
    
    def invoke(self, context, event):
        # Show warning first if this is the initial invoke
        if self.show_warning:
            self.show_warning = False
            return context.window_manager.invoke_confirm(
                self, event, 
                message="Creating a PRM texture is resource-intensive and may take time depending on your computer."
            )
        
        # Set a default output path based on the active material name
        if context.active_object and context.active_object.active_material:
            material_name = context.active_object.active_material.name
            self.output_path = os.path.join(tempfile.gettempdir(), f"{material_name}_PRM.png")
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        if not context.active_object or not context.active_object.active_material:
            self.report({'ERROR'}, "No active material selected")
            return {'CANCELLED'}
        
        material = context.active_object.active_material
        
        try:
            output = create_prm_from_material(
                material,
                self.output_path,
                self.bake_size
            )
            self.report({'INFO'}, f"PRM texture created at {output}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Error creating PRM texture: {str(e)}")
            return {'CANCELLED'}

# List of classes to register
classes = (
    ULTIMATE_OT_create_nor_from_material,
    ULTIMATE_OT_create_prm_from_material,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register() 