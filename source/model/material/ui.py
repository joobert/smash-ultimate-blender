import bpy
from bpy.types import Panel, Menu

from . import operators
from .sub_matl_data import SUB_PG_sub_matl_data
 
class MaterialPanel(Panel):
    '''
    This class is made to avoid repeating these lines in every single panel
    '''
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"
    
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
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        if sub_matl_data.shader_label == "":
            return False 
        return True

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)
    
class SUB_PT_matl_data_master(MaterialPanel):
    bl_label = "Ultimate Material Data"
    bl_idname = "SUB_PT_matl_data_master"

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        if context.object.type != 'MESH':
            return False
        if context.object.active_material is None:
            return False
        return True
    
    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout
        if sub_matl_data.shader_label == "":
            row = layout.row()
            row.label(text="The current blender material is not an ultimate material!")
            row = layout.row()
            row.label(text="The blender material will be replaced with a default smash material on export")
            row = layout.row()
            row.label(text="You can alternatively choose to convert the existing material to an ultimate material.")
            row = layout.row()
            row.operator_context = 'INVOKE_DEFAULT'
            row.operator(operators.SUB_OP_convert_blender_material.bl_idname)
            row.scale_y = 1.0
            row.scale_x = 2
            row = layout.row()
            row.operator(operators.SUB_OP_convert_blender_material_no_textures.bl_idname)
            row.scale_y = 1.0
            row.scale_x = 2
            return
        box = layout.box()
        box.prop(sub_matl_data, "shader_label", emboss=False)
        box.menu(SUB_MT_material_specials.bl_idname)
        row = layout.row()
        row.operator_context = 'INVOKE_DEFAULT'
        row.operator("sub.optimize_textures", icon='IMAGE_DATA')
        
        # Check if material has been converted to Principled BSDF
        from .convert_smash_material import is_converted_to_principled
        material = context.object.active_material
        
        if is_converted_to_principled(material):
            # Show Revert button when converted
            row = layout.row()
            row.operator(operators.SUB_OP_revert_smash_material.bl_idname, text="Revert to Smash Material", icon='LOOP_BACK')
            row.scale_y = 1.0
        else:
            # Add Convert Smash Material button
            row = layout.row()
            row.operator(operators.SUB_OP_convert_smash_material.bl_idname, text="Convert to Principled BSDF", icon='MATERIAL')
            row.scale_y = 1.0

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_PT_matl_data_bools(MaterialPanel):
    bl_label = "Bools"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout
        box = layout.box()
        for matl_bool in sub_matl_data.bools:
            row = box.row()
            row.alignment = 'EXPAND'
            row.label(text=matl_bool.ui_name)
            row = row.row()
            row.alignment = 'RIGHT'
            row.prop(matl_bool, "value", text="")

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


class SUB_PT_matl_data_floats(MaterialPanel):
    bl_label = "Floats"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout
        box = layout.box()
        for matl_float in sub_matl_data.floats:
            row = box.row()
            row.alignment = 'EXPAND'
            row.label(text=matl_float.ui_name)
            row = row.row()
            row.alignment = 'RIGHT'
            row.prop(matl_float, "value", text="")

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)
            
class SUB_PT_matl_data_vectors(MaterialPanel):
    bl_label = "Vectors"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname

    def draw(self, context):
        layout = self.layout
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        box = layout.box()
        for vector in sub_matl_data.vectors:
            row = box.row()
            sub_row = row.row()
            sub_row.alignment = 'EXPAND'
            sub_row.label(text=vector.ui_name)
            sub_row = row.row(align=True)
            sub_row.alignment = 'RIGHT'
            sub_row.prop(vector, "value", text="")
            sub_row.prop(vector, "value", text="", index=0)
            sub_row.prop(vector, "value", text="", index=1)
            sub_row.prop(vector, "value", text="", index=2)
            sub_row.prop(vector, "value", text="", index=3)

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_PT_matl_data_textures(MaterialPanel):
    bl_label = "Textures"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname

    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout

        box = layout.box()
        for texture in sub_matl_data.textures:
            tex_row = box.row()
            tex_name_subrow = tex_row.row()
            tex_name_subrow.alignment = 'EXPAND'
            if texture.image is not None and texture.image.preview is not None:
                tex_name_subrow.label(text=texture.ui_name, translate=False, icon_value=texture.image.preview.icon_id)
            else:
                tex_name_subrow.label(text=texture.ui_name, translate=False, icon="IMAGE_DATA")
            prop_subrow = tex_row.row()
            prop_subrow.alignment = 'RIGHT'
            prop_subrow.scale_x = 1.5
            prop_subrow.prop(texture, "image", text="")
            if texture.image is not None:
                width, height = texture.image.size
                box.label(text=f"{width} x {height}" if width and height else "Image data unavailable")

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_PT_matl_data_samplers(MaterialPanel):
    bl_label = "Samplers"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout

        for sampler in sub_matl_data.samplers:
            box = layout.box()
            row = box.row()
            row.label(text=sampler.ui_name)
            row = box.row()
            cf = row.column_flow(columns=4)
            cf.label(text='Wrap Settings')
            cf.separator()
            cf.label(text='Filter Settings')
            cf.separator()
            cf.label(text='Other Settings')
            cf.prop(sampler, 'wrap_s')
            cf.separator()
            cf.prop(sampler, 'min_filter')
            cf.separator()
            sub_row = cf.row() # Prevents border color from taking up 2 rows
            sub_row.prop(sampler, 'border_color')
            cf.prop(sampler, 'wrap_t')
            cf.separator()
            cf.prop(sampler, 'mag_filter')
            cf.separator()
            cf.prop(sampler, 'lod_bias')
            cf.prop(sampler, 'wrap_r')
            cf.separator()
            cf.prop(sampler, 'anisotropic_filtering')
            cf.separator()
            cf.prop(sampler, 'max_anisotropy')

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_PT_matl_data_blend_states(MaterialPanel):
    bl_label = "Blend States"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout

        for blend_state in sub_matl_data.blend_states:
            box = layout.box()
            row = box.row()
            row.label(text=blend_state.ui_name)
            row = box.row()
            row.label(text="Source Color")
            row.prop(blend_state, "source_color", text="")
            row = box.row()
            row.label(text='Destination Color')
            row.prop(blend_state, "destination_color", text="")
            row = box.row()
            row.label(text="Alpha Sample To Coverage")
            row.prop(blend_state, "alpha_sample_to_coverage", text="")

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


class SUB_PT_matl_data_rasterizer_states(MaterialPanel):
    bl_label = "Rasterizer States"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout

        for rasterizer_state in sub_matl_data.rasterizer_states:
            box = layout.box()
            row = box.row()
            row.label(text=rasterizer_state.ui_name)
            row = box.row()
            row.label(text="Cull Mode")
            row.prop(rasterizer_state, "cull_mode", text="")
            row = box.row()
            row.label(text='Depth Bias')
            row.prop(rasterizer_state, "depth_bias", text="")
            row = box.row()
            row.label(text="Fill Mode")
            row.prop(rasterizer_state, "fill_mode", text="")

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_PT_matl_data_linked_materials(MaterialPanel):
    bl_label = "Linked Materials"
    bl_parent_id = SUB_PT_matl_data_master.bl_idname
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        sub_matl_data: SUB_PG_sub_matl_data = context.object.active_material.sub_matl_data
        layout = self.layout

        box = layout.box()
        if len(sub_matl_data.linked_materials) == 0:
            row = box.row()
            row.label(text="No extra materials linked to this material. (This is normal)")
            return
        
        row = box.row()
        row.label(text="The following materials are linked and will be exported even if no mesh is using them.")

        for linked_material in sub_matl_data.linked_materials:
            row = box.row()
            row.label(text=f"Linked Blender Material: '{linked_material.blender_material.name}'", icon='MATERIAL')

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)
            # TODO: Allow Editing?


class SUB_MT_material_specials(Menu):
    bl_label = "Material Specials"
    bl_idname = "SUB_MT_material_specials"

    def draw(self, context):
        layout = self.layout
        
        layout.operator(operators.SUB_OP_change_render_pass.bl_idname, icon="RENDERLAYERS")
        layout.operator(operators.SUB_OP_create_sub_matl_data_from_shader_label.bl_idname, icon="SHADERFX")


class PG_PT_smash_texture_materials(Panel):
    bl_label = "Materials"
    bl_parent_id = "PG_PT_smash_texture"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'
    bl_options = {'DEFAULT_CLOSED'}
    
    @classmethod
    def poll(cls, context):
        material = context.material
        return material is not None
    
    def draw(self, context):
        layout = self.layout
        material = context.material
        
        # Only display options if we have a material
        if not material:
            return
        
        # Show material conversion button
        layout.operator(operators.SUB_OP_convert_blender_material.bl_idname, text="Convert to Smash Ultimate Material")
        
        # Add Solid view fix buttons
        box = layout.box()
        box.label(text="Viewport Display Fixes:")
        row = box.row()
        row.operator(operators.SUB_OP_fix_solid_view_display.bl_idname, text="Fix This Material for Solid View")
        row = box.row()
        row.operator(operators.SUB_OP_fix_uvset_solid_view.bl_idname, text="Fix uvSet Textures in Solid View")
        
        # Show eye-specific fix only for eye materials
        if 'Eye' in material.name:
            row = box.row()
            row.operator(operators.SUB_OP_fix_eye_uvset_solid_view.bl_idname, text="Fix Eye uvSet Textures in Solid View")
            row = box.row()
            row.operator(operators.SUB_OP_fix_eye_dual_uv_solid_view.bl_idname, text="Fix Eye Dual UV Maps in Solid View")
        
        row = box.row()
        row.operator(operators.SUB_OP_fix_all_materials_solid_view.bl_idname, text="Fix All Materials for Solid View")
        
        # Add a notice about supported material types
        box = layout.box()
        box.label(text="Supported Material Types:")
        box.label(text="• Principled BSDF")
        box.label(text="• Fortnite FPv3 Material")
        
        # Add Fortnite channel mapping info
        fortnite_box = layout.box()
        fortnite_box.label(text="Fortnite Texture Channel Mapping:")
        row = fortnite_box.row()
        row.label(text="_M Red = PRM Blue (Ambient Occlusion)")
        row = fortnite_box.row()
        row.label(text="_M Blue = PRM Red (Skin/Subsurface)")
        row = fortnite_box.row()
        row.label(text="_S Red = PRM Alpha (Specular)")
        row = fortnite_box.row()
        row.label(text="_S Green = PRM Green (Roughness)")
        row = fortnite_box.row()
        row.label(text="_S Blue = PRM Red (Metal)")
        row = fortnite_box.row()
        row.label(text="_D = Diffuse/Color Texture")
        
        # Show current material info
        if hasattr(material, 'sub_matl_data'):
            # Draw sub material properties
            sub_material = material.sub_matl_data
            if sub_material:
                box = layout.box()
                box.label(text=f"Shader Label: {sub_material.shader_label}")
                
                # Draw texture slots
                if len(sub_material.textures):
                    texture_box = layout.box()
                    texture_box.label(text="Texture Slots:")
                    
                    for texture_name, texture in sub_material.textures.items():
                        row = texture_box.row()
                        row.label(text=texture_name)
                        if texture.image:
                            row.label(text=texture.image.name)
                        else:
                            row.label(text="None")
                
                # Option to export material
                layout.operator(operators.SUB_OP_export_material_to_matl.bl_idname, text="Export Material to MATL")

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)


class SUB_OP_visualize_texture_mapping(bpy.types.Operator):
    bl_idname = "smash_ultimate.visualize_texture_mapping"
    bl_label = "Visualize Texture Channel Mapping"
    bl_description = "Creates a visualization of how Fortnite texture channels map to the PRM texture"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.material is not None
    
    def execute(self, context):
        material = context.material
        
        # Check if this is a Fortnite material
        from ..material.texture.convert_textures import find_fpv3_material_node, extract_fortnite_textures, visualize_texture_channels
        
        fpv3_node = find_fpv3_material_node(material)
        if not fpv3_node:
            self.report({'ERROR'}, "This is not a Fortnite FPv3 material")
            return {'CANCELLED'}
        
        # Check if this is a skin material
        is_skin_material = False
        if hasattr(fpv3_node.inputs.get('Subsurface', None), 'default_value'):
            is_skin_material = fpv3_node.inputs['Subsurface'].default_value > 0.01
        elif hasattr(fpv3_node.inputs.get('Subsurface Weight', None), 'default_value'):
            is_skin_material = fpv3_node.inputs['Subsurface Weight'].default_value > 0.01
        
        # Extract textures
        m_texture, s_texture, d_texture = extract_fortnite_textures(material)
        
        if not m_texture and not s_texture:
            self.report({'ERROR'}, "No Fortnite textures found (_M or _S textures)")
            return {'CANCELLED'}
        
        # Create temp directory if it doesn't exist
        import tempfile
        import os
        temp_dir = os.path.join(tempfile.gettempdir(), "smash_ultimate_blender")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Create visualization
        viz_path = os.path.join(temp_dir, f"{material.name}_texture_mapping.png")
        if visualize_texture_channels(m_texture, s_texture, is_skin_material, viz_path):
            # Open the image in Blender's image editor
            try:
                # Create a new image editor area
                for area in context.screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        # Found an image editor, use it
                        break
                else:
                    # No image editor found, try to create one
                    self.report({'WARNING'}, f"Visualization saved to {viz_path}, but couldn't find an image editor to display it")
                    return {'FINISHED'}
                
                # Load the image
                viz_img = bpy.data.images.load(viz_path)
                
                # Set it as the active image in the editor
                area.spaces.active.image = viz_img
                
                self.report({'INFO'}, f"Visualization created and displayed")
            except Exception as e:
                self.report({'WARNING'}, f"Visualization saved to {viz_path}, but couldn't display it: {e}")
        else:
            self.report({'ERROR'}, "Failed to create visualization")
            return {'CANCELLED'}
        
        return {'FINISHED'}


# Add the operator to the panel
def draw_fortnite_tools(self, context):
    layout = self.layout
    material = context.material
    
    # Check if this might be a Fortnite material
    if material and material.use_nodes and material.node_tree:
        for node in material.node_tree.nodes:
            if node.type == 'GROUP' and ('FPv3' in node.name or 'FPv3' in node.label):
                box = layout.box()
                box.label(text="Fortnite Material Tools")
                
                # Add texture size settings
                row = box.row(align=True)
                row.label(text="Texture Size:")
                for size in [256, 512, 1024, 2048]:
                    op = row.operator("sub.set_texture_size", text=str(size))
                    op.size = size
                    op.operator_id = "sub.convert_blender_material"
                
                row = box.row(align=True)
                row.operator("smash_ultimate.visualize_texture_mapping", icon='TEXTURE')
                break

# Add the button to the material panel
def register():
    bpy.utils.register_class(SUB_OP_visualize_texture_mapping)
    bpy.types.MATERIAL_PT_context_material.append(draw_fortnite_tools)

def unregister():
    bpy.utils.unregister_class(SUB_OP_visualize_texture_mapping)
    bpy.types.MATERIAL_PT_context_material.remove(draw_fortnite_tools)


