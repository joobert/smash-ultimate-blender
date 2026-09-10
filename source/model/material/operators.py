import bpy
from bpy.types import Operator
from bpy.props import StringProperty
from .load_from_shader_label import is_valid_shader_label, create_sub_matl_data_from_shader_label

class SUB_OP_change_render_pass(Operator):
    bl_description = 'Change which render pass draws the selected Smash material'
    bl_idname = 'sub.change_render_pass'
    bl_label = 'Change Render Pass'

    def execute(self, context):
        return {'FINISHED'} 

class SUB_OP_create_sub_matl_data_from_shader_label(Operator):
    bl_description = 'Create Smash material parameters for the specified shader label'
    bl_idname = 'sub.create_sub_matl_data_from_shader_label'
    bl_label = 'Create New Material from Shader Label'
    
    new_shader_label: StringProperty(
        name="New Shader Label",
        description="The New Shader Label",
        default="SFX_PBS_0100000008008269_opaque"
        )
    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.object.type != 'MESH':
            return False
        if context.object.active_material is None:
            return False
        if context.object.active_material.sub_matl_data is None:
            return False
        if context.object.active_material.sub_matl_data.shader_label == "":
            return False
        return True
    
    def execute(self, context):
        if not is_valid_shader_label(self, self.new_shader_label):
            return{'CANCELLED'}
        create_sub_matl_data_from_shader_label(context.object.active_material, self.new_shader_label)
        return {'FINISHED'} 
    
    def invoke(self, context, event):
        wm = context.window_manager
        self.new_shader_label = context.object.active_material.sub_matl_data.shader_label
        return wm.invoke_props_dialog(self)

class SUB_OP_apply_material_preset(Operator):
    bl_description = 'Apply a shader label preset to the selected Smash material'
    bl_idname = 'sub.change_shader_label'
    bl_label = 'Change Shader Label'

    def execute(self, context):
        return {'FINISHED'} 
    
from .convert_blender_material import convert_blender_material, rename_mesh_attributes_of_meshes_using_material
from .convert_smash_material import (
    convert_smash_material_to_principled,
    has_smash_material_data,
    has_prm_texture,
    is_converted_to_principled,
    revert_to_smash_material,
    find_target_armature,
    smash_materials_on_armature,
    armature_has_unconverted_smash_materials,
    armature_has_converted_smash_materials,
)
class SUB_OP_convert_blender_material(Operator):
    bl_description = 'Convert the selected Blender material to Smash, generate a PRM texture, and reuse its normal map'
    bl_idname = 'sub.convert_blender_material'
    bl_label = 'Convert Blender Material (Creates PRM, uses existing normal)'
    bl_options = {'REGISTER', 'INTERNAL'}
    
    bake_size: bpy.props.IntProperty(
        name="Texture Size",
        description="Size of the generated PRM texture (width and height)",
        default=1024,
        min=64,
        max=8192,
        step=1,
        subtype='PIXEL'
    )
    
    def draw(self, context):
        layout = self.layout
        layout.label(text="Warning: Creating textures is resource-intensive.", icon='ERROR')
        layout.label(text="Larger textures require more memory and time.")
        layout.separator()
        
        # Display requirements
        box = layout.box()
        box.label(text="Requirements:", icon='INFO')
        box.label(text="• Cycles render engine must be enabled")
        box.label(text="• GPU acceleration recommended for speed")
        
        # Display the custom size input field
        layout.separator()
        layout.prop(self, "bake_size")
    
    def execute(self, context):
        rename_mesh_attributes_of_meshes_using_material(self, context.object.active_material)
        convert_blender_material(self, context.object.active_material, self.bake_size)
        return {'FINISHED'} 
        
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

class SUB_OP_convert_blender_material_no_textures(Operator):
    bl_description = 'Convert the selected Blender material to Smash using only its diffuse texture'
    bl_idname = 'sub.convert_blender_material_no_textures'
    bl_label = 'Convert Blender Material (Diffuse only)'

    def execute(self, context):
        rename_mesh_attributes_of_meshes_using_material(self, context.object.active_material)
        # Import the original implementation without texture creation
        from .convert_blender_material_original import convert_blender_material_original
        convert_blender_material_original(self, context.object.active_material)
        return {'FINISHED'} 

class SUB_OP_set_texture_size(Operator):
    bl_description = 'Set the texture dimensions used by material texture conversion'
    bl_idname = 'sub.set_texture_size'
    bl_label = 'Set Texture Size'
    bl_options = {'INTERNAL'}
    
    size: bpy.props.IntProperty(default=1024)
    operator_id: bpy.props.StringProperty(default="sub.convert_blender_material")
    
    def execute(self, context):
        # Find the active operator and set its size
        if hasattr(context, 'window_manager'):
            for area in context.screen.areas:
                if area.type == 'PROPERTIES':
                    for space in area.spaces:
                        if space.type == 'PROPERTIES':
                            for region in area.regions:
                                if region.type == 'WINDOW':
                                    override = context.copy()
                                    override['area'] = area
                                    override['region'] = region
                                    override['space_data'] = space
                                    bpy.context.window_manager.operator_properties_last(self.operator_id).bake_size = self.size
        return {'FINISHED'}

class SUB_OP_copy_from_ult_material(Operator):
    bl_description = 'Copy Smash material parameters from another material'
    bl_idname = 'sub.copy_from_ult_material'
    bl_label = 'Copy From Other Material'

    def execute(self, context):
        return {'FINISHED'} 

class SUB_OP_fix_solid_view_display(bpy.types.Operator):
    bl_idname = "smash_ultimate.fix_solid_view_display"
    bl_label = "Fix Solid View Display"
    bl_description = "Fixes materials that aren't displaying properly in Solid view mode, especially for materials using multiple UV maps"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.material is not None
    
    def execute(self, context):
        material = context.material
        if not material:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        
        # Import the setup functions
        from .create_blender_materials_from_matl import setup_material_for_solid_view, setup_eye_material_for_solid_view
        
        # Apply fixes for Solid view display
        setup_material_for_solid_view(material)
        
        # Special handling for eye materials
        if 'Eye' in material.name:
            setup_eye_material_for_solid_view(material)
        
        # Force viewport update
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = space.shading.type  # Force refresh
        
        self.report({'INFO'}, f"Fixed Solid view display for material '{material.name}'")
        return {'FINISHED'}

class SUB_OP_fix_all_materials_solid_view(bpy.types.Operator):
    bl_idname = "smash_ultimate.fix_all_materials_solid_view"
    bl_label = "Fix All Materials for Solid View"
    bl_description = "Fixes all materials in the scene to display properly in Solid view mode"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Import the setup functions
        from .create_blender_materials_from_matl import setup_material_for_solid_view, setup_eye_material_for_solid_view
        
        fixed_count = 0
        
        for material in bpy.data.materials:
            if material.use_nodes and material.node_tree:
                # Apply fixes for Solid view display
                setup_material_for_solid_view(material)
                
                # Special handling for eye materials
                if 'Eye' in material.name:
                    setup_eye_material_for_solid_view(material)
                
                fixed_count += 1
        
        # Force viewport update
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = space.shading.type  # Force refresh
        
        self.report({'INFO'}, f"Fixed Solid view display for {fixed_count} materials")
        return {'FINISHED'} 

class SUB_OP_fix_uvset_solid_view(bpy.types.Operator):
    bl_idname = "smash_ultimate.fix_uvset_solid_view"
    bl_label = "Fix uvSet Textures in Solid View"
    bl_description = "Specifically fixes textures using the uvSet UV map to display properly in Solid view mode"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.material is not None
    
    def execute(self, context):
        material = context.material
        if not material or not material.use_nodes or not material.node_tree:
            self.report({'ERROR'}, "No material with nodes selected")
            return {'CANCELLED'}
        
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        # Find all texture nodes that use uvSet
        uvset_texture_nodes = []
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                # Check if this texture is connected to a uvSet UV map
                for link in links:
                    if link.to_node == node and link.from_node.type == 'UVMAP':
                        if hasattr(link.from_node, 'uv_map') and link.from_node.uv_map == 'uvSet':
                            uvset_texture_nodes.append(node)
                            break
        
        if not uvset_texture_nodes:
            self.report({'WARNING'}, "No textures using uvSet UV map found")
            return {'CANCELLED'}
        
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
        connected_count = 0
        for tex_node in uvset_texture_nodes:
            # Check if texture is already connected to principled
            already_connected = False
            for link in links:
                if link.from_node == tex_node and link.to_node == principled_node:
                    already_connected = True
                    break
            
            if not already_connected:
                # Connect texture to base color if not already connected
                if not principled_node.inputs['Base Color'].links:
                    links.new(tex_node.outputs['Color'], principled_node.inputs['Base Color'])
                    connected_count += 1
                else:
                    # If base color is already connected, create a mix node
                    mix_node = nodes.new('ShaderNodeMixRGB')
                    mix_node.location = (principled_node.location[0] - 300, principled_node.location[1])
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs[0].default_value = 1.0  # Factor
                    
                    # Connect existing base color to mix node
                    existing_link = principled_node.inputs['Base Color'].links[0]
                    links.new(existing_link.from_node.outputs[existing_link.from_socket.name], mix_node.inputs[1])
                    
                    # Connect new texture to mix node
                    links.new(tex_node.outputs['Color'], mix_node.inputs[2])
                    
                    # Connect mix node to principled
                    links.new(mix_node.outputs[0], principled_node.inputs['Base Color'])
                    connected_count += 1
        
        # Ensure principled BSDF is connected to material output
        output_nodes = [n for n in nodes if n.type == 'OUTPUT_MATERIAL']
        for output_node in output_nodes:
            if output_node.target == 'EEVEE':
                if not output_node.inputs[0].links:
                    links.new(principled_node.outputs[0], output_node.inputs[0])
                elif output_node.inputs[0].links[0].from_node != principled_node:
                    # If connected to something else, create a mix shader
                    mix_shader = nodes.new('ShaderNodeMixShader')
                    mix_shader.location = (output_node.location[0] - 200, output_node.location[1])
                    mix_shader.inputs[0].default_value = 0.5  # Factor
                    
                    # Connect existing shader to mix
                    existing_link = output_node.inputs[0].links[0]
                    links.new(existing_link.from_node.outputs[existing_link.from_socket.name], mix_shader.inputs[1])
                    
                    # Connect principled to mix
                    links.new(principled_node.outputs[0], mix_shader.inputs[2])
                    
                    # Connect mix to output
                    links.new(mix_shader.outputs[0], output_node.inputs[0])
        
        # Force viewport update
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = space.shading.type  # Force refresh
        
        self.report({'INFO'}, f"Fixed {connected_count} uvSet textures for Solid view display")
        return {'FINISHED'} 

class SUB_OP_fix_eye_uvset_solid_view(bpy.types.Operator):
    bl_idname = "smash_ultimate.fix_eye_uvset_solid_view"
    bl_label = "Fix Eye uvSet Textures in Solid View"
    bl_description = "Specifically fixes eye materials with uvSet textures to display properly in Solid view mode"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.material is not None and 'Eye' in context.material.name
    
    def execute(self, context):
        material = context.material
        if not material or not material.use_nodes or not material.node_tree:
            self.report({'ERROR'}, "No eye material with nodes selected")
            return {'CANCELLED'}
        
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        # Find all texture nodes that use uvSet
        uvset_texture_nodes = []
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                # Check if this texture is connected to a uvSet UV map
                for link in links:
                    if link.to_node == node and link.from_node.type == 'UVMAP':
                        if hasattr(link.from_node, 'uv_map') and link.from_node.uv_map == 'uvSet':
                            uvset_texture_nodes.append(node)
                            break
        
        if not uvset_texture_nodes:
            self.report({'WARNING'}, "No uvSet textures found in eye material")
            return {'CANCELLED'}
        
        # For eye materials, we need to ensure the uvSet textures are properly connected
        # to the material output for Solid view display
        
        # Find the material output node
        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL' and node.target == 'EEVEE':
                output_node = node
                break
        
        if not output_node:
            # Create output node if it doesn't exist
            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.target = 'EEVEE'
            output_node.location = (600, 0)
        
        # Create a principled BSDF specifically for the uvSet textures
        principled_node = nodes.new('ShaderNodeBsdfPrincipled')
        principled_node.location = (300, 0)
        
        # Connect uvSet textures to the principled BSDF
        connected_count = 0
        for tex_node in uvset_texture_nodes:
            # Connect texture to base color
            links.new(tex_node.outputs['Color'], principled_node.inputs['Base Color'])
            connected_count += 1
        
        # Connect principled BSDF to material output
        links.new(principled_node.outputs[0], output_node.inputs[0])
        
        # Set material properties for eye display
        material.use_backface_culling = False  # Eyes should be visible from both sides
        material.blend_method = 'OPAQUE'  # Ensure proper display in Solid view
        
        # Force viewport update
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = space.shading.type  # Force refresh
        
        self.report({'INFO'}, f"Fixed {connected_count} uvSet textures in eye material for Solid view display")
        return {'FINISHED'} 

class SUB_OP_fix_eye_dual_uv_solid_view(bpy.types.Operator):
    bl_idname = "smash_ultimate.fix_eye_dual_uv_solid_view"
    bl_label = "Fix Eye Dual UV Maps in Solid View"
    bl_description = "Combines map1 (white eye) and uvSet (pupil) textures for proper Solid view display"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.material is not None and 'Eye' in context.material.name
    
    def execute(self, context):
        material = context.material
        if not material or not material.use_nodes or not material.node_tree:
            self.report({'ERROR'}, "No eye material with nodes selected")
            return {'CANCELLED'}
        
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
        
        if not map1_texture_nodes and not uvset_texture_nodes:
            self.report({'WARNING'}, "No map1 or uvSet textures found in eye material")
            return {'CANCELLED'}
        
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
        
        # Connect map1 textures (white eye) to base color
        if map1_texture_nodes:
            # Use the first map1 texture as base color
            links.new(map1_texture_nodes[0].outputs['Color'], principled_node.inputs['Base Color'])
            
            # If there are multiple map1 textures, mix them
            if len(map1_texture_nodes) > 1:
                for i, tex_node in enumerate(map1_texture_nodes[1:], 1):
                    mix_node = nodes.new('ShaderNodeMixRGB')
                    mix_node.location = (principled_node.location[0] - 300 * i, principled_node.location[1])
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs[0].default_value = 1.0
                    
                    # Connect previous result to mix node
                    if i == 1:
                        links.new(map1_texture_nodes[0].outputs['Color'], mix_node.inputs[1])
                    else:
                        prev_mix = nodes[f"mix_map1_{i-1}"]
                        links.new(prev_mix.outputs[0], mix_node.inputs[1])
                    
                    # Connect new texture to mix node
                    links.new(tex_node.outputs['Color'], mix_node.inputs[2])
                    mix_node.name = f"mix_map1_{i}"
                    
                    # Connect final mix to principled
                    if i == len(map1_texture_nodes) - 1:
                        links.new(mix_node.outputs[0], principled_node.inputs['Base Color'])
        
        # Connect uvSet textures (pupil) using mix nodes
        if uvset_texture_nodes:
            if map1_texture_nodes:
                # Mix uvSet textures with existing base color
                for i, tex_node in enumerate(uvset_texture_nodes):
                    mix_node = nodes.new('ShaderNodeMixRGB')
                    mix_node.location = (principled_node.location[0] - 300 * (len(map1_texture_nodes) + i), principled_node.location[1])
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
            else:
                # No map1 textures, use uvSet as base color
                links.new(uvset_texture_nodes[0].outputs['Color'], principled_node.inputs['Base Color'])
                
                # If multiple uvSet textures, mix them
                if len(uvset_texture_nodes) > 1:
                    for i, tex_node in enumerate(uvset_texture_nodes[1:], 1):
                        mix_node = nodes.new('ShaderNodeMixRGB')
                        mix_node.location = (principled_node.location[0] - 300 * i, principled_node.location[1])
                        mix_node.blend_type = 'MULTIPLY'
                        mix_node.inputs[0].default_value = 1.0
                        
                        # Connect previous result to mix node
                        if i == 1:
                            links.new(uvset_texture_nodes[0].outputs['Color'], mix_node.inputs[1])
                        else:
                            prev_mix = nodes[f"mix_uvset_{i-1}"]
                            links.new(prev_mix.outputs[0], mix_node.inputs[1])
                        
                        # Connect new texture to mix node
                        links.new(tex_node.outputs['Color'], mix_node.inputs[2])
                        mix_node.name = f"mix_uvset_{i}"
                        
                        # Connect final mix to principled
                        if i == len(uvset_texture_nodes) - 1:
                            links.new(mix_node.outputs[0], principled_node.inputs['Base Color'])
        
        # Connect principled BSDF to material output
        links.new(principled_node.outputs[0], output_node.inputs[0])
        
        # Set material properties for eye display
        material.use_backface_culling = False  # Eyes should be visible from both sides
        material.blend_method = 'OPAQUE'  # Ensure proper display in Solid view
        
        # Force viewport update
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = space.shading.type  # Force refresh
        
        map1_count = len(map1_texture_nodes)
        uvset_count = len(uvset_texture_nodes)
        self.report({'INFO'}, f"Combined {map1_count} map1 textures (white eye) and {uvset_count} uvSet textures (pupil) for Solid view display")
        return {'FINISHED'}

class SUB_OP_convert_smash_material(Operator):
    bl_idname = 'sub.convert_smash_material'
    bl_label = 'Convert Smash Material to Principled BSDF'
    bl_description = 'Convert a Smash Ultimate material to a standard Principled BSDF with decomposed PRM texture'
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        material = context.object.active_material if context.object else None
        # Only show if material has Smash data AND has NOT been converted yet
        return material is not None and has_smash_material_data(material) and not is_converted_to_principled(material)
    
    def execute(self, context):
        material = context.object.active_material
        if not material:
            self.report({'ERROR'}, "No active material")
            return {'CANCELLED'}
        
        if not has_smash_material_data(material):
            self.report({'ERROR'}, f"Material '{material.name}' is not a Smash Ultimate material")
            return {'CANCELLED'}
        
        # Perform the conversion
        success = convert_smash_material_to_principled(self, material)
        
        if success:
            self.report({'INFO'}, f"Successfully converted Smash material '{material.name}' to Principled BSDF")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Failed to convert material '{material.name}'")
            return {'CANCELLED'}


class SUB_OP_revert_smash_material(Operator):
    bl_idname = 'sub.revert_smash_material'
    bl_label = 'Revert to Smash Material'
    bl_description = 'Revert a converted Principled BSDF material back to the original Smash Ultimate material'
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        material = context.object.active_material if context.object else None
        # Only show if material has been converted to Principled BSDF
        return material is not None and is_converted_to_principled(material)
    
    def execute(self, context):
        material = context.object.active_material
        if not material:
            self.report({'ERROR'}, "No active material")
            return {'CANCELLED'}
        
        if not is_converted_to_principled(material):
            self.report({'ERROR'}, f"Material '{material.name}' is not a converted Smash material")
            return {'CANCELLED'}
        
        # Perform the reversion
        success = revert_to_smash_material(self, material)
        
        if success:
            self.report({'INFO'}, f"Successfully reverted material '{material.name}' to Smash Ultimate material")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Failed to revert material '{material.name}'")
            return {'CANCELLED'}


class SUB_OP_convert_armature_smash_materials(Operator):
    bl_idname = "sub.convert_armature_smash_materials"
    bl_label = "Convert All to Principled BSDF"
    bl_description = "Convert every Smash material on the selected armature's meshes to Principled BSDF"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = find_target_armature(context)
        return armature is not None and armature_has_unconverted_smash_materials(armature)

    def execute(self, context):
        armature = find_target_armature(context)
        if armature is None:
            self.report({"ERROR"}, "Select an armature")
            return {"CANCELLED"}
        converted = 0
        failed = 0
        for material in smash_materials_on_armature(armature):
            if is_converted_to_principled(material):
                continue
            if convert_smash_material_to_principled(self, material):
                converted += 1
            else:
                failed += 1
        if converted:
            self.report({"INFO"}, f"Converted {converted} material(s) on '{armature.name}' to Principled BSDF")
            return {"FINISHED"}
        self.report({"ERROR"}, f"Failed to convert materials on '{armature.name}' ({failed} failed)")
        return {"CANCELLED"}


class SUB_OP_revert_armature_smash_materials(Operator):
    bl_idname = "sub.revert_armature_smash_materials"
    bl_label = "Revert All to Smash Material"
    bl_description = "Rebuild the original Smash shaders for every converted material on the selected armature"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = find_target_armature(context)
        return armature is not None and armature_has_converted_smash_materials(armature)

    def execute(self, context):
        armature = find_target_armature(context)
        if armature is None:
            self.report({"ERROR"}, "Select an armature")
            return {"CANCELLED"}
        reverted = 0
        failed = 0
        for material in smash_materials_on_armature(armature):
            if not is_converted_to_principled(material):
                continue
            if revert_to_smash_material(self, material):
                reverted += 1
            else:
                failed += 1
        if reverted:
            self.report({"INFO"}, f"Reverted {reverted} material(s) on '{armature.name}' to Smash shaders")
            return {"FINISHED"}
        self.report({"ERROR"}, f"Failed to revert materials on '{armature.name}' ({failed} failed)")
        return {"CANCELLED"}
