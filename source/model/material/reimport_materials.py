import bpy
import re
import os
from pathlib import Path
from bpy.types import Panel, Operator
from bpy.props import StringProperty, BoolProperty, EnumProperty
from ....dependencies import ssbh_data_py
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ...blender_property_extensions import SubSceneProperties

class SUB_PT_reimport_materials(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Material Re-Importer'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        self.layout.use_property_decorate = False
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        layout = self.layout
        layout.use_property_split = False

        row = layout.row(align=True)
        row.label(text='Select an Armature')
        row = layout.row(align=True)
        row.prop(ssp, 'material_reimport_arma', icon='ARMATURE_DATA')
        if not ssp.material_reimport_arma:
            return
        if '' == ssp.material_reimport_folder:
            row = layout.row(align=True)
            row.operator('sub.mat_reimport_dir_selector', icon='ZOOM_ALL', text='Select folder w/ .NUMATB & textures')
        else:
            row = layout.row(align=True)
            row.label(text=f'Textures Folder= "{ssp.material_reimport_folder}"')
            if '' == ssp.material_reimport_numatb_path:
                row = layout.row(align=True)
                row.alert = True
                row.label(text='No .numatb file found!', icon='ERROR')
            else:
                row = layout.row(align=True)
                row.label(text=f'.numatb file: "{Path(ssp.material_reimport_numatb_path).name}"', icon='FILE')
                row = layout.row(align=True)
                row.operator('sub.mat_reimport_numatb_selector', icon='ZOOM_ALL', text='Re-select .numatb')
                row = layout.row(align=True)
            row = layout.row(align=True)
            row.operator('sub.mat_reimport_dir_selector', icon='ZOOM_ALL', text='Re-Select folder')
            row = layout.row(align=True)
            row.operator('sub.reimport_materials', icon='IMPORT', text='Re-Import materials')

        layout.separator()
        box = layout.box()
        box.label(text='Copy materials from different armature', icon='MATERIAL')
        row = box.row(align=True)
        row.prop(ssp, 'material_reimport_copy_source_arma', icon='ARMATURE_DATA', text='Source')
        row = box.row(align=True)
        row.operator('sub.copy_materials_from_armature', icon='PASTEDOWN', text='Copy Materials')

    def draw_header_preset(self, context):
        from ...ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_OP_mat_reimport_directory_selector(Operator):
    bl_idname = 'sub.mat_reimport_dir_selector'
    bl_label = 'Confirm folder'
    bl_description = 'Choose the folder containing materials and textures to re-import'

    filter_glob: StringProperty(
        default='*.numatb; *.png',
        options={'HIDDEN'}
    )
    directory: StringProperty(
        subtype="DIR_PATH"
    )

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        ssp.material_reimport_folder = self.directory
        numatb_files = {f for f in os.listdir(ssp.material_reimport_folder) if f.endswith('.numatb')}
        if len(numatb_files) == 0:
            ssp.material_reimport_numatb_path = ''
        elif len(numatb_files) == 1:
            ssp.material_reimport_numatb_path = os.path.join(ssp.material_reimport_folder, numatb_files.pop())
        else:
            if {'model.numatb'} & numatb_files:
                ssp.material_reimport_numatb_path = os.path.join(ssp.material_reimport_folder, 'model.numatb')
            else:
                ssp.material_reimport_numatb_path = os.path.join(ssp.material_reimport_folder, numatb_files.pop())
        return {'FINISHED'}   

class SUB_OP_mat_reimport_numatb_selector(Operator):
    bl_idname = 'sub.mat_reimport_numatb_selector'
    bl_label = 'Confirm .numatb'
    bl_description = 'Choose the .numatb file to use when re-importing materials'

    filter_glob: StringProperty(
        default='*.numatb;',
        options={'HIDDEN'}
    )
    filepath: StringProperty(
        subtype="FILE_PATH"
    )
    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    def execute(self, context):
        context.scene.sub_scene_properties.material_reimport_numatb_path = self.filepath
        return {'FINISHED'}   

class SUB_OP_reimport_materials(Operator):
    bl_description = 'Reload Smash materials from disk onto the selected model'
    bl_idname = 'sub.reimport_materials'
    bl_label = 'Reimport Materials'

    @classmethod
    def poll(cls, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        return ssp.material_reimport_numatb_path != '' and ssp.material_reimport_folder != ''

    def execute(self, context):
        reimport_materials(self, context)
        return {'FINISHED'}


class SUB_OP_copy_materials_from_armature(Operator):
    bl_idname = 'sub.copy_materials_from_armature'
    bl_label = 'Copy Materials From Armature'
    bl_description = 'Copy materials onto this armature from another armature for meshes with the same name'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ssp: SubSceneProperties = context.scene.sub_scene_properties
        target = ssp.material_reimport_arma
        source = ssp.material_reimport_copy_source_arma
        return (
            target is not None
            and source is not None
            and target != source
            and target.type == 'ARMATURE'
            and source.type == 'ARMATURE'
        )

    def execute(self, context):
        copy_materials_from_armature(self, context)
        return {'FINISHED'}


def _armature_mesh_children(arma: bpy.types.Object) -> list[bpy.types.Object]:
    return [child for child in arma.children if child.type == 'MESH']


def _mesh_name_key(name: str) -> str:
    from ..export_model import trim_name
    return trim_name(name)


def copy_materials_from_armature(operator: Operator, context):
    ssp: SubSceneProperties = context.scene.sub_scene_properties
    target_arma: bpy.types.Object = ssp.material_reimport_arma
    source_arma: bpy.types.Object = ssp.material_reimport_copy_source_arma

    source_by_name: dict[str, bpy.types.Object] = {}
    ambiguous_keys: set[str] = set()
    for mesh in _armature_mesh_children(source_arma):
        key = _mesh_name_key(mesh.name)
        if key in source_by_name:
            ambiguous_keys.add(key)
            continue
        source_by_name[key] = mesh
    for key in ambiguous_keys:
        source_by_name.pop(key, None)
        operator.report(
            {'WARNING'},
            f'Source armature has multiple meshes named "{key}" (ignoring duplicates).',
        )

    copied = 0
    skipped_no_match = 0
    for target_mesh in _armature_mesh_children(target_arma):
        source_mesh = source_by_name.get(_mesh_name_key(target_mesh.name))
        if source_mesh is None:
            skipped_no_match += 1
            continue

        # Read effective slots, including materials linked to the source object.
        materials = [slot.material for slot in source_mesh.material_slots]
        if not materials or not any(material is not None for material in materials):
            operator.report({'WARNING'}, f'Source mesh "{source_mesh.name}" has no materials; skipped.')
            continue

        # Material slots belong to the mesh datablock. Isolate linked duplicates
        # so changing this target cannot also change the source or other objects.
        if target_mesh.data.users > 1:
            target_mesh.data = target_mesh.data.copy()
        polygon_material_indices = [polygon.material_index for polygon in target_mesh.data.polygons]
        target_mesh.data.materials.clear()
        for material in materials:
            target_mesh.data.materials.append(material)
        for slot, material in zip(target_mesh.material_slots, materials):
            slot.link = 'DATA'
            slot.material = material
        # Clearing slots can reset face assignments. Retain valid target indices.
        for polygon, index in zip(target_mesh.data.polygons, polygon_material_indices):
            polygon.material_index = index if index < len(materials) else 0
        copied += 1

    if copied == 0:
        operator.report({'WARNING'}, 'No matching meshes with usable materials; no materials were copied.')
    else:
        extra = f' ({skipped_no_match} mesh(es) had no name match)' if skipped_no_match else ''
        operator.report({'INFO'}, f'Copied materials onto {copied} mesh(es){extra}.')


def reimport_materials(operator: Operator, context):
    from .create_blender_materials_from_matl import create_blender_materials_from_matl
    from ..export_model import would_trimmed_names_be_unique, trim_name, get_problematic_names

    ssp: SubSceneProperties = context.scene.sub_scene_properties
    arma: bpy.types.Object = ssp.material_reimport_arma 
    mesh_objects: set[bpy.types.Object] = {child for child in arma.children if child.type == 'MESH'}
    materials: set[bpy.types.Material] = {material_slot.material for mesh_object in mesh_objects for material_slot in mesh_object.material_slots}
    material_names: set[str] = {material.name for material in materials}
    if not would_trimmed_names_be_unique(material_names):
        problematic_names = get_problematic_names(material_names)
        for problematic_name in problematic_names:
            message = f'The material name of "{problematic_name}" is not a unique name after trimming! Cannot reimport Materials! (Trimmed name is "{trim_name(problematic_name)}")'
            operator.report({'WARNING'}, message)
        return
    
    ssbh_matl = ssbh_data_py.matl_data.read_matl(str(ssp.material_reimport_numatb_path))
    material_label_to_material = create_blender_materials_from_matl(operator, ssbh_matl, ssp.material_reimport_folder)
    for mesh_object in mesh_objects:
        for material_slot in mesh_object.material_slots:
            new_material = material_label_to_material.get(trim_name(material_slot.material.name))
            if new_material is not None:
                material_slot.material = new_material

    
