# BPY Imports
import bpy
from bpy.types import (
    Operator, Context, CopyTransformsConstraint, CopyLocationConstraint, TrackToConstraint, Collection, Object, Mesh, Armature)
from bpy.props import (
    IntProperty, StringProperty, EnumProperty, BoolProperty, FloatProperty, CollectionProperty, PointerProperty, FloatVectorProperty)
# Standard Library Imports
from pathlib import Path
from math import radians, degrees
from mathutils import Vector
# 3rd Party Imports
from ...dependencies import pyprc
# Local Project Imports
from ..extras import create_meshes
from ..param_labels import add_hash_label, load_param_labels
from .sub_swing_data import *
import os
import importlib.util

# Global variable to store the active mismatch operator instance
_active_mismatch_operator = None

def add_hash_to_param_labels(hash_value: str, label: str, labels_paths=None):
    """Add a new hash-label pair to the user ParamLabels.csv if it doesn't already exist."""
    return add_hash_label(label, extra_paths=labels_paths or ())

def save_swing_bone_chain_hashes(chain_name: str, start_bone_name: str, end_bone_name: str, bone_names: list):
    """Save all relevant hashes for a swing bone chain to ParamLabels.csv"""
    # Save chain name hash
    add_hash_to_param_labels(chain_name, chain_name)
    
    # Save bone name hashes (lowercase as they're used in the PRC)
    add_hash_to_param_labels(start_bone_name.lower(), start_bone_name.lower())
    add_hash_to_param_labels(end_bone_name.lower(), end_bone_name.lower())
    
    # Save individual bone name hashes and collision list hashes
    for bone_name in bone_names:
        bone_lower = bone_name.lower()
        add_hash_to_param_labels(bone_lower, bone_lower)
        # Also save the collision list hash (bone_name + "col")
        collision_hash = bone_lower + "col"
        add_hash_to_param_labels(collision_hash, collision_hash)

def get_preset_bone_enum_items(self, context):
    """Generate enum items for preset bone selection"""
    global _active_mismatch_operator
    
    items = [('NONE', 'None', 'Skip this target bone', 'X', 0)]
    
    if _active_mismatch_operator:
        for i in range(_active_mismatch_operator.preset_bone_count):
            items.append((str(i), f"Preset Bone {i+1}", f"Apply values from preset bone {i+1}", 'BONE_DATA', i+1))
    
    return items

class SUB_PG_bone_mapping(bpy.types.PropertyGroup):
    """Property group for individual bone mapping"""
    target_bone_name: bpy.props.StringProperty(
        name="Target Bone",
        description="Name of the target bone"
    )
    preset_bone_index: bpy.props.IntProperty(
        name="Preset Bone Index",
        description="Index of preset bone to map from (-1 for none)",
        default=-1,
        min=-1
    )
    preset_bone_selection: bpy.props.EnumProperty(
        name="Preset Bone",
        description="Select which preset bone to map from",
        items=get_preset_bone_enum_items,
        update=lambda self, context: self.update_preset_bone_index(context)
    )
    
    def update_preset_bone_index(self, context):
        """Update the preset_bone_index when the enum selection changes"""
        if self.preset_bone_selection == 'NONE':
            self.preset_bone_index = -1
        else:
            self.preset_bone_index = int(self.preset_bone_selection)

def fill_armature_swing_bones(context):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    ssd.armature_swing_bones.clear()
    arma_data: bpy.types.Armature = context.object.data
    bone: bpy.types.Bone
    for bone in arma_data.bones:
        if bone.name.startswith('S_') and not bone.name.endswith('_null'):
            armature_swing_bone = ssd.armature_swing_bones.add()
            armature_swing_bone.name = bone.name

def fill_armature_swing_bone_children(context: Context, swing_bone_name: str):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    ssd.armature_swing_bone_children.clear()
    # Re-add the original back into the list, since a chain of 1 is valid.
    swing_bone_child = ssd.armature_swing_bone_children.add()
    swing_bone_child.name = swing_bone_name
    arma_data: bpy.types.Armature = context.object.data
    bone: bpy.types.Bone
    for bone in arma_data.bones[swing_bone_name].children_recursive:
        if bone.name.startswith('S_') and not bone.name.endswith('_null'):
            swing_bone_child = ssd.armature_swing_bone_children.add()
            swing_bone_child.name = bone.name

def start_bone_name_update(self, context):
    if self.start_bone_name != '':
        fill_armature_swing_bone_children(context, self.start_bone_name)

def is_one_child_only_chain(bone: bpy.types.Bone) -> tuple[bool, bpy.types.Bone]:
    if len(bone.children) == 0:
        return True, None
    if len(bone.children) != 1:
        return False, bone
    return is_one_child_only_chain(bone.children[0])

def is_end_bone_valid(end_bone: bpy.types.Bone) -> bool:
    return len(end_bone.children) == 1

class SUB_OP_swing_bone_chain_add(Operator):
    bl_idname = 'sub.swing_bone_chain_add'
    bl_label = 'Add Swing Bone Chain'
    bl_options = {'REGISTER', 'UNDO'}

    start_bone_name: StringProperty(name='Start Bone Name', default='', update=start_bone_name_update)
    end_bone_name: StringProperty(name='End Bone Name', default='')

    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.start_bone_name = ''
        self.end_bone_name = ''
        fill_armature_swing_bones(context)
        return wm.invoke_props_dialog(self)

    def draw(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        layout = self.layout
        row = layout.row()
        row.prop_search(self, 'start_bone_name', ssd, 'armature_swing_bones', text='Start Bone', icon='BONE_DATA')

        row = layout.row()
        if self.start_bone_name == '':
            row.enabled = False
        row.prop_search(self, 'end_bone_name', ssd, 'armature_swing_bone_children', text='End Bone', icon='BONE_DATA')

    def execute(self, context):
        if any(bone_name == '' for bone_name in (self.start_bone_name, self.end_bone_name)):
            self.report({'ERROR'}, 'Start Bone or End Bone not specified, cancelling.')
            return {'CANCELLED'}
        
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        arma_data: bpy.types.Armature = context.object.data
        start_bone: bpy.types.Bone = arma_data.bones.get(self.start_bone_name)
        end_bone = arma_data.bones.get(self.end_bone_name)
        if start_bone is None:
            self.report({'ERROR'}, f'Somehow the specified start bone {self.start_bone_name} is no longer in the armature.')
            return {'CANCELLED'}
        if end_bone is None:
            self.report({'ERROR'}, f'Somehow the specified end bone {self.end_bone_name} is no longer in the armature.')
            return {'CANCELLED'}

        is_valid_chain, problematic_bone = is_one_child_only_chain(start_bone)
        if not is_valid_chain:
            self.report({'ERROR'}, f"Chain can't start at {start_bone.name}, the bone {problematic_bone.name} has multiple children!")
            return {'CANCELLED'}
        
        end_bone = arma_data.bones.get(self.end_bone_name)
        if not is_end_bone_valid(end_bone):
            self.report({'ERROR'}, f"Chain can't end at {end_bone.name}, that bone has no child! (Add a '_null' swing bone if its the last bone in the chain)")
            return {'CANCELLED'}

        bones_in_chain: list[bpy.types.Bone] = []
        for blender_bone in [start_bone] + start_bone.children_recursive:
            if blender_bone.name == end_bone.children[0].name:
                break
            bones_in_chain.append(blender_bone)

        if end_bone not in bones_in_chain:
            self.report({'ERROR'}, f'Somehow the specified end bone {end_bone.name} was not a child of {start_bone.name}')
            return {'CANCELLED'}
        
        for blender_bone in bones_in_chain:
            sub_blender_bone_data: SUB_PG_blender_bone_data = blender_bone.sub_swing_blender_bone_data
            chain_index = sub_blender_bone_data.swing_bone_chain_index
            if chain_index != -1:
                self.report({'ERROR'}, f"Chain can't contain {blender_bone.name}, that bone is already in the swing bone chain '{sub_swing_data.swing_bone_chains[chain_index].name}'")
                return {'CANCELLED'}


        new_chain: SUB_PG_swing_bone_chain = sub_swing_data.swing_bone_chains.add()
        new_chain.name = self.start_bone_name[2:].lower()
        new_chain.start_bone_name = self.start_bone_name
        new_chain.end_bone_name = self.end_bone_name
        new_chain_index = sub_swing_data.swing_bone_chains.find(new_chain.name)

        master_collection: Collection = get_swing_mesh_master_collection(context, context.object)
        master_collection_props: SUB_PG_sub_swing_master_collection_props = master_collection.sub_swing_collection_props
        chain_collection = new_swing_collection(new_chain.name)
        chain_swing_bones_collection = new_swing_collection(f'{new_chain.name} swing bones')
        chain_collision_collection = new_swing_collection(f'{new_chain.name} collisions')
        master_collection_props.swing_chains_collection.children.link(chain_collection)
        chain_collection.children.link(chain_swing_bones_collection)
        chain_collection.children.link(chain_collision_collection)

        for bone_index, blender_bone in enumerate(bones_in_chain):
            new_swing_bone: SUB_PG_swing_bone = new_chain.swing_bones.add()
            new_swing_bone.name = blender_bone.name
            sub_blender_bone_data: SUB_PG_blender_bone_data = blender_bone.sub_swing_blender_bone_data
            sub_blender_bone_data.swing_bone_chain_index = sub_swing_data.swing_bone_chains.find(new_chain.name)
            sub_blender_bone_data.swing_bone_index = bone_index
            cap = create_meshes.make_capsule_object(
                new_swing_bone.name,
                new_swing_bone.collision_size[0],
                new_swing_bone.collision_size[1],
                (0,0,0),
                (0,0,0),
                blender_bone,
                blender_bone.children[0],
            )
            new_swing_bone.blender_object = cap
            parent_swing_bone_collision(context.object, cap, new_chain_index, bone_index)
            chain_swing_bones_collection.objects.link(cap)
            swing_bone_collision_collection = new_swing_collection(f'{new_chain.name} {new_swing_bone.name} collisions')
            chain_collision_collection.children.link(swing_bone_collision_collection)
        sub_swing_data.active_swing_bone_chain_index = len(sub_swing_data.swing_bone_chains)-1

        # Save hashes to ParamLabels.csv
        bone_names = [bone.name for bone in bones_in_chain]
        save_swing_bone_chain_hashes(new_chain.name, new_chain.start_bone_name, new_chain.end_bone_name, bone_names)
        self.report({'INFO'}, f"Created swing bone chain '{new_chain.name}' and saved hashes to ParamLabels.csv")

        return {'FINISHED'}

class SUB_OP_swing_bone_chain_auto_detect(Operator):
    bl_idname = 'sub.swing_bone_chain_auto_detect'
    bl_label = 'Auto-Detect Swing Bone Chains'
    bl_description = 'Automatically detect and add all valid swing bone chains from the armature'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        arma_data: bpy.types.Armature = context.object.data
        
        detected_chains = []
        chains_added = 0
        chains_skipped = 0
        
        # Find all potential start bones (S_ bones that aren't already in chains and don't have S_ parents)
        potential_start_bones = []
        for bone in arma_data.bones:
            if not bone.name.startswith('S_') or bone.name.endswith('_null'):
                continue
                
            # Skip if already in a chain
            if bone.sub_swing_blender_bone_data.swing_bone_chain_index != -1:
                continue
                
            # Check if this bone has a parent that's also an S_ bone (not a root)
            is_chain_start = True
            if bone.parent:
                if bone.parent.name.startswith('S_') and not bone.parent.name.endswith('_null'):
                    is_chain_start = False
            
            if is_chain_start:
                potential_start_bones.append(bone)
        
        # For each potential start bone, try to detect a complete chain
        for start_bone in potential_start_bones:
            chain_info = self.detect_chain_from_start_bone(start_bone, arma_data)
            if chain_info:
                detected_chains.append(chain_info)
        
        # Create chains for all detected valid chains
        for chain_info in detected_chains:
            start_bone, end_bone, bones_in_chain = chain_info
            
            # Double-check that none of the bones are already in a chain
            bones_already_used = False
            for bone in bones_in_chain:
                if bone.sub_swing_blender_bone_data.swing_bone_chain_index != -1:
                    bones_already_used = True
                    break
            
            if bones_already_used:
                chains_skipped += 1
                continue
            
            # Create the chain using the same logic as manual creation
            try:
                new_chain: SUB_PG_swing_bone_chain = sub_swing_data.swing_bone_chains.add()
                new_chain.name = start_bone.name[2:].lower()  # Remove 'S_' prefix
                new_chain.start_bone_name = start_bone.name
                new_chain.end_bone_name = end_bone.name
                new_chain_index = sub_swing_data.swing_bone_chains.find(new_chain.name)

                # Create collections
                master_collection: Collection = get_swing_mesh_master_collection(context, context.object)
                master_collection_props: SUB_PG_sub_swing_master_collection_props = master_collection.sub_swing_collection_props
                chain_collection = new_swing_collection(new_chain.name)
                chain_swing_bones_collection = new_swing_collection(f'{new_chain.name} swing bones')
                chain_collision_collection = new_swing_collection(f'{new_chain.name} collisions')
                master_collection_props.swing_chains_collection.children.link(chain_collection)
                chain_collection.children.link(chain_swing_bones_collection)
                chain_collection.children.link(chain_collision_collection)

                # Add swing bones to the chain
                for bone_index, blender_bone in enumerate(bones_in_chain):
                    new_swing_bone: SUB_PG_swing_bone = new_chain.swing_bones.add()
                    new_swing_bone.name = blender_bone.name
                    sub_blender_bone_data: SUB_PG_blender_bone_data = blender_bone.sub_swing_blender_bone_data
                    sub_blender_bone_data.swing_bone_chain_index = new_chain_index
                    sub_blender_bone_data.swing_bone_index = bone_index
                    
                    # Create collision mesh
                    cap = create_meshes.make_capsule_object(
                        new_swing_bone.name,
                        new_swing_bone.collision_size[0],
                        new_swing_bone.collision_size[1],
                        (0,0,0),
                        (0,0,0),
                        blender_bone,
                        blender_bone.children[0],
                    )
                    new_swing_bone.blender_object = cap
                    parent_swing_bone_collision(context.object, cap, new_chain_index, bone_index)
                    chain_swing_bones_collection.objects.link(cap)
                    swing_bone_collision_collection = new_swing_collection(f'{new_chain.name} {new_swing_bone.name} collisions')
                    chain_collision_collection.children.link(swing_bone_collision_collection)
                
                # Save hashes to ParamLabels.csv for this chain
                bone_names = [bone.name for bone in bones_in_chain]
                save_swing_bone_chain_hashes(new_chain.name, new_chain.start_bone_name, new_chain.end_bone_name, bone_names)
                
                chains_added += 1
                
            except Exception as e:
                self.report({'WARNING'}, f"Failed to create chain starting from {start_bone.name}: {str(e)}")
                chains_skipped += 1
        
        # Set active chain to the last added one
        if chains_added > 0:
            sub_swing_data.active_swing_bone_chain_index = len(sub_swing_data.swing_bone_chains) - 1
        
        # Report results
        if chains_added > 0:
            self.report({'INFO'}, f"Successfully detected and added {chains_added} swing bone chain(s). {chains_skipped} skipped.")
        else:
            self.report({'INFO'}, "No new swing bone chains detected.")
        
        return {'FINISHED'}
    
    def detect_chain_from_start_bone(self, start_bone: bpy.types.Bone, arma_data: bpy.types.Armature):
        """
        Detect a complete swing bone chain starting from the given bone.
        Returns (start_bone, end_bone, bones_in_chain) if valid, None otherwise.
        """
        # Check if the chain is linear (each bone has only one child)
        is_valid_chain, problematic_bone = is_one_child_only_chain(start_bone)
        if not is_valid_chain:
            return None
        
        # Find the end bone by following the chain
        current_bone = start_bone
        bones_in_chain = [start_bone]
        
        # Follow the chain until we find a valid end bone
        while current_bone.children:
            if len(current_bone.children) != 1:
                return None  # Invalid chain structure
            
            child_bone = current_bone.children[0]
            
            # Check if this child is a valid end bone (has a _null child or no children)
            if child_bone.name.startswith('S_'):
                if child_bone.name.endswith('_null'):
                    # Don't include the _null bone in the chain, but it's a valid end marker
                    end_bone = current_bone
                    break
                else:
                    # Continue the chain
                    bones_in_chain.append(child_bone)
                    current_bone = child_bone
            else:
                # Non-S_ bone found - current_bone is the end
                end_bone = current_bone
                break
        else:
            # No children found - current_bone is the end
            end_bone = current_bone
        
        # Validate that we have at least one bone and a valid end
        if len(bones_in_chain) == 0:
            return None
        
        # Check if the end bone is valid (has a child - either _null or non-S_)
        if not is_end_bone_valid(end_bone):
            return None
        
        return (start_bone, end_bone, bones_in_chain)

class SUB_OP_swing_bone_chain_remove(Operator):
    bl_idname = 'sub.swing_bone_chain_remove'
    bl_label = 'Remove Swing Bone Chain'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        
        return len(sub_swing_data.swing_bone_chains) > 0
    
    def execute(self, context):
        arma_data: bpy.types.Armature = context.object.data
        sub_swing_data: SUB_PG_sub_swing_data = arma_data.sub_swing_data
        active_swing_bone_chain_index = sub_swing_data.active_swing_bone_chain_index
        for bone in arma_data.bones:
            sub_blender_bone_data: SUB_PG_blender_bone_data = bone.sub_swing_blender_bone_data
            if sub_blender_bone_data.swing_bone_chain_index == active_swing_bone_chain_index:
                sub_blender_bone_data.swing_bone_chain_index = -1
                sub_blender_bone_data.swing_bone_index = -1
            if sub_blender_bone_data.swing_bone_chain_index > active_swing_bone_chain_index:
                sub_blender_bone_data.swing_bone_chain_index -= 1

        sub_swing_data.swing_bone_chains.remove(active_swing_bone_chain_index)
        sub_swing_data.active_swing_bone_chain_index = min(active_swing_bone_chain_index, len(sub_swing_data.swing_bone_chains)-1)
        
        return {'FINISHED'}

class SUB_OP_swing_bone_chain_length_edit(Operator):
    bl_idname = 'sub.swing_bone_chain_length_edit'
    bl_label = 'Edit Start/End of Swing Bone Chain'

    def execute(self, context):
        return {'FINISHED'}  
    

def get_spheres_enum(self, context):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
    active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
    existing_indices = {c.collision_index for c in active_swing_bone.collisions if c.collision_type == 'SPHERE'}
    return [(s.name, s.name, s.name) for index, s in enumerate(ssd.spheres) if index not in existing_indices]

class SUB_OP_swing_bone_collision_add_sphere(Operator):
    bl_idname = 'sub.swing_bone_collision_add_sphere'
    bl_label = 'Add Swing Bone Sphere Collision'
    bl_property = 'sphere'
    
    sphere: EnumProperty(items=get_spheres_enum)

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.spheres) > 0
        

    def invoke(self, context: Context, event):
        wm = context.window_manager
        wm.invoke_search_popup(self)
        return {'FINISHED'}

    def execute(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
        active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        sphere_index = ssd.spheres.find(self.sphere)
        new_collision: SUB_PG_swing_bone_collision = active_swing_bone.collisions.add()
        new_collision.collision_type = 'SPHERE'
        new_collision.collision_index = sphere_index
        return {'FINISHED'}

def get_ovals_enum(self, context):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
    active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
    existing_indices = {c.collision_index for c in active_swing_bone.collisions if c.collision_type == 'OVAL'}
    return [(s.name, s.name, s.name) for index, s in enumerate(ssd.ovals) if index not in existing_indices]

class SUB_OP_swing_bone_collision_add_oval(Operator):
    bl_idname = 'sub.swing_bone_collision_add_oval'
    bl_label = 'Add Swing Bone Oval Collision'
    bl_property = 'oval'
    
    oval: EnumProperty(items=get_ovals_enum)

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.ovals) > 0
        
    def invoke(self, context: Context, event):
        wm = context.window_manager
        wm.invoke_search_popup(self)
        return {'FINISHED'}

    def execute(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
        active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        oval_index = ssd.ovals.find(self.oval)
        new_collision: SUB_PG_swing_bone_collision = active_swing_bone.collisions.add()
        new_collision.collision_type = 'OVAL'
        new_collision.collision_index = oval_index
        return {'FINISHED'}

def get_ellipsoids_enum(self, context):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
    active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
    existing_indices = {c.collision_index for c in active_swing_bone.collisions if c.collision_type == 'ELLIPSOID'}
    return [(s.name, s.name, s.name) for index, s in enumerate(ssd.ellipsoids) if index not in existing_indices]

class SUB_OP_swing_bone_collision_add_ellipsoid(Operator):
    bl_idname = 'sub.swing_bone_collision_add_ellipsoid'
    bl_label = 'Add Swing Bone Ellipsoid Collision'
    bl_property = 'ellipsoid'
    
    ellipsoid: EnumProperty(items=get_ellipsoids_enum)

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.ellipsoids) > 0
        
    def invoke(self, context: Context, event):
        wm = context.window_manager
        wm.invoke_search_popup(self)
        return {'FINISHED'}

    def execute(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
        active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        ellipsoid_index = ssd.ellipsoids.find(self.ellipsoid)
        new_collision: SUB_PG_swing_bone_collision = active_swing_bone.collisions.add()
        new_collision.collision_type = 'ELLIPSOID'
        new_collision.collision_index = ellipsoid_index
        return {'FINISHED'}

def get_capsules_enum(self, context):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
    active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
    existing_indices = {c.collision_index for c in active_swing_bone.collisions if c.collision_type == 'CAPSULE'}
    return [(s.name, s.name, s.name) for index, s in enumerate(ssd.capsules) if index not in existing_indices]

class SUB_OP_swing_bone_collision_add_capsule(Operator):
    bl_idname = 'sub.swing_bone_collision_add_capsule'
    bl_label = 'Add Swing Bone Capsule Collision'
    bl_property = 'capsule'
    
    capsule: EnumProperty(items=get_capsules_enum)

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.capsules) > 0

    def invoke(self, context: Context, event):
        wm = context.window_manager
        wm.invoke_search_popup(self)
        return {'FINISHED'}

    def execute(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
        active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        capsule_index = ssd.capsules.find(self.capsule)
        new_collision: SUB_PG_swing_bone_collision = active_swing_bone.collisions.add()
        new_collision.collision_type = 'CAPSULE'
        new_collision.collision_index = capsule_index
        return {'FINISHED'}

def get_planes_enum(self, context):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
    active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
    existing_indices = {c.collision_index for c in active_swing_bone.collisions if c.collision_type == 'PLANE'}
    return [(s.name, s.name, s.name) for index, s in enumerate(ssd.planes) if index not in existing_indices]

class SUB_OP_swing_bone_collision_add_plane(Operator):
    bl_idname = 'sub.swing_bone_collision_add_plane'
    bl_label = 'Add Swing Bone Plane Collision'
    bl_property = 'plane'
    
    plane: EnumProperty(items=get_planes_enum)

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.planes) > 0

    def invoke(self, context: Context, event):
        wm = context.window_manager
        wm.invoke_search_popup(self)
        return {'FINISHED'}

    def execute(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
        active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        plane_index = ssd.planes.find(self.plane)
        new_collision: SUB_PG_swing_bone_collision = active_swing_bone.collisions.add()
        new_collision.collision_type = 'PLANE'
        new_collision.collision_index = plane_index
        return {'FINISHED'}               

class SUB_OP_swing_bone_collision_remove(Operator):
    bl_idname = 'sub.swing_bone_collision_remove'
    bl_label = 'Remove Swing Bone Collision'

    @classmethod
    def poll(cls, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        try:
            active_swing_bone_chain: SUB_PG_swing_bone_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
            active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        except (IndexError, AttributeError):
            return False
        return len(active_swing_bone.collisions) > 0

    def execute(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_swing_bone_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains[ssd.active_swing_bone_chain_index]
        active_swing_bone: SUB_PG_swing_bone = active_swing_bone_chain.swing_bones[active_swing_bone_chain.active_swing_bone_index]
        active_swing_bone.collisions.remove(active_swing_bone.active_collision_index)
        i = active_swing_bone.active_collision_index
        active_swing_bone.active_collision_index = min(max(0, i-1), len(active_swing_bone.collisions))
        return {'FINISHED'} 


class SUB_OP_swing_data_sphere_add(Operator):
    bl_idname = 'sub.swing_data_sphere_add'
    bl_label = 'Add Sphere Collision'
    
    sphere_name: StringProperty(name="Sphere Name", default="")
    bone_name: StringProperty(name='Bone Name', default='', )
    offset: FloatVectorProperty(name="Offset", size=3, subtype='XYZ_LENGTH')
    radius: FloatProperty(name="Radius", subtype='DISTANCE')

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.sphere_name = ""
        self.bone_name = ""
        self.offset = (0.0, 0.0, 0.0)
        self.radius = 1.0

        return wm.invoke_props_dialog(self)

    def draw(self, context):
        ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        layout = self.layout
        
        layout.row().prop(self, "sphere_name")

        layout.row().prop_search(self, 'bone_name', context.object.data, 'bones', text='Bone', icon='BONE_DATA')

        layout.row().prop(self, "offset")
        
        layout.row().prop(self, "radius")


    def execute(self, context):
        arma_obj: Object = context.object
        arma_data: bpy.types.Armature = arma_obj.data

        sub_swing_data: SUB_PG_sub_swing_data = arma_data.sub_swing_data

        if self.sphere_name == "":
            return {'CANCELLED'}
        if self.bone_name == "":
            return {'CANCELLED'}
        
        new_sphere: SUB_PG_swing_sphere = sub_swing_data.spheres.add()
        new_sphere.name = self.sphere_name
        new_sphere.bone = self.bone_name
        new_sphere.offset = self.offset
        new_sphere.radius = self.radius
        new_sphere.blender_object = create_meshes.make_sphere_object_2(
                            new_sphere.name,
                            new_sphere.radius,
                            new_sphere.offset,
                            arma_data.bones.get(self.bone_name))
        
        new_sphere_index = sub_swing_data.spheres.find(new_sphere.name)

        parent_swing_child_to_parent_obj_armature_deform(
            parent = context.object,
            child = new_sphere.blender_object,
            type = 'SPHERE',
            index = new_sphere_index)
        
        master_collection: Collection = get_swing_mesh_master_collection(context, context.object)
        master_collection_props: SUB_PG_sub_swing_master_collection_props = master_collection.sub_swing_collection_props

        master_collection_props.spheres_collection.objects.link(new_sphere.blender_object)

        sub_swing_data.active_sphere_index = new_sphere_index

        # Save collision name hash to ParamLabels.csv
        add_hash_to_param_labels(new_sphere.name, new_sphere.name)
        # Also save the bone name hash (lowercase as used in PRC)
        add_hash_to_param_labels(new_sphere.bone.lower(), new_sphere.bone.lower())

        return {'FINISHED'}

def remove_active_collision_from_collection(collision_type: str, sub_swing_data: SUB_PG_sub_swing_data):
    if collision_type == "SPHERE":
        active_index = sub_swing_data.active_sphere_index
        collision_collection = sub_swing_data.spheres
    elif collision_type == "OVAL":
        active_index = sub_swing_data.active_oval_index
        collision_collection = sub_swing_data.ovals
    elif collision_type == "ELLIPSOID":
        active_index = sub_swing_data.active_ellipsoid_index
        collision_collection = sub_swing_data.ellipsoids
    elif collision_type == "CAPSULE":
        active_index = sub_swing_data.active_capsule_index
        collision_collection = sub_swing_data.capsules
    elif collision_type == "PLANE":
        active_index = sub_swing_data.active_plane_index
        collision_collection = sub_swing_data.planes

    swing_bone_chain: SUB_PG_swing_bone_chain
    swing_bone: SUB_PG_swing_bone
    swing_bone_collision: SUB_PG_swing_bone_collision
    for swing_bone_chain in sub_swing_data.swing_bone_chains:
        for swing_bone in swing_bone_chain.swing_bones:
            collision_index_to_remove: int = -1
            for swing_bone_all_collision_index, swing_bone_collision in enumerate(swing_bone.collisions):
                if swing_bone_collision.collision_type == collision_type:
                    if swing_bone_collision.collision_index > active_index:
                        swing_bone_collision.collision_index -= 1
                    elif swing_bone_collision.collision_index == active_index:
                        collision_index_to_remove = swing_bone_all_collision_index
            if collision_index_to_remove != -1:
                swing_bone.collisions.remove(collision_index_to_remove)
                swing_bone.active_collision_index = max(0, min(swing_bone.active_collision_index, len(swing_bone.collisions)-1))
    
    collision_collection.remove(active_index)
    new_index = max(0, min(active_index, len(collision_collection)-1))  
    if collision_type == "SPHERE":
        sub_swing_data.active_sphere_index = new_index
    elif collision_type == "OVAL":
        sub_swing_data.active_oval_index = new_index
    elif collision_type == "ELLIPSOID":
        sub_swing_data.active_ellipsoid_index = new_index
    elif collision_type == "CAPSULE":
        sub_swing_data.active_capsule_index = new_index
    elif collision_type == "PLANE":
        sub_swing_data.active_plane_index = new_index
      

class SUB_OP_swing_data_sphere_remove(Operator):
    bl_idname = 'sub.swing_data_sphere_remove'
    bl_label = 'Remove Sphere Collision'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.spheres) > 0
        
    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_sphere: SUB_PG_swing_sphere = sub_swing_data.spheres[sub_swing_data.active_sphere_index]

        bpy.data.meshes.remove(active_sphere.blender_object.data)
        #bpy.data.objects.remove(active_sphere.blender_object) # Deleting the mesh automatically deletes the object too?
        
        remove_active_collision_from_collection("SPHERE", sub_swing_data)
        return {'FINISHED'}   
    


class SUB_OP_swing_data_oval_add(Operator):
    bl_idname = 'sub.swing_data_oval_add'
    bl_label = 'Add Oval Collision'

    oval_name: StringProperty(name='Oval Name')
    start_bone_name: StringProperty(name='Start Bone Name')
    end_bone_name: StringProperty(name='End Bone Name')
    radius: FloatProperty(name='Radius')
    start_offset: FloatVectorProperty(name='Start Offset', subtype='XYZ', size=3)
    end_offset: FloatVectorProperty(name='End Offset', subtype='XYZ', size=3)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.oval_name = ""
        self.start_bone_name = ""
        self.end_bone_name = ""
        self.radius = 1.0
        self.start_offset = (0.0, 0.0, 0.0)
        self.end_offset = (0.0, 0.0, 0.0)

        return wm.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        
        layout.row().prop(self, "oval_name")

        layout.row().prop_search(self, 'start_bone_name', context.object.data, 'bones', text='Start Bone', icon='BONE_DATA')

        layout.row().prop_search(self, 'end_bone_name', context.object.data, 'bones', text='End Bone', icon='BONE_DATA')

        layout.row().prop(self, "radius")

        layout.row().prop(self, "start_offset")

        layout.row().prop(self, "end_offset")   

    def execute(self, context):
        arma_obj: Object = context.object
        arma_data: Armature = arma_obj.data

        sub_swing_data: SUB_PG_sub_swing_data = arma_data.sub_swing_data
        if self.oval_name == "":
            return {'CANCELLED'}
        if self.start_bone_name == "":
            return {'CANCELLED'}
        if self.end_bone_name == "":
            return {'CANCELLED'}
        
        new_oval: SUB_PG_swing_oval = sub_swing_data.ovals.add()
        new_oval.name = self.oval_name
        new_oval.start_bone_name = self.start_bone_name
        new_oval.end_bone_name = self.end_bone_name
        new_oval.radius = self.radius
        new_oval.start_offset = self.start_offset
        new_oval.end_offset = self.end_offset
        new_oval.blender_object = create_meshes.make_capsule_object(
            name = new_oval.name,
            start_radius = new_oval.radius,
            end_radius = new_oval.radius,
            start_offset = new_oval.start_offset,
            end_offset = new_oval.end_offset,
            start_bone = arma_data.bones.get(self.start_bone_name),
            end_bone = arma_data.bones.get(self.end_bone_name),)
        
        new_oval_index = sub_swing_data.ovals.find(new_oval.name)

        parent_swing_child_to_parent_obj_armature_deform(
            parent = arma_obj,
            child = new_oval.blender_object,
            type = 'OVAL',
            index = new_oval_index,)

        master_collection_props: SUB_PG_sub_swing_master_collection_props = get_swing_mesh_master_collection(
            context = context, 
            arma_obj = arma_obj).sub_swing_collection_props
        
        master_collection_props.ovals_collection.objects.link(new_oval.blender_object)

        sub_swing_data.active_oval_index = new_oval_index

        # Save collision name hash to ParamLabels.csv
        add_hash_to_param_labels(new_oval.name, new_oval.name)
        # Also save the bone name hashes (lowercase as used in PRC)
        add_hash_to_param_labels(new_oval.start_bone_name.lower(), new_oval.start_bone_name.lower())
        add_hash_to_param_labels(new_oval.end_bone_name.lower(), new_oval.end_bone_name.lower())

        return {'FINISHED'}



class SUB_OP_swing_data_oval_remove(Operator):
    bl_idname = 'sub.swing_data_oval_remove'
    bl_label = 'Remove Oval Collision'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.ovals) > 0
        
    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_oval: SUB_PG_swing_oval = sub_swing_data.ovals[sub_swing_data.active_oval_index]

        bpy.data.meshes.remove(active_oval.blender_object.data)

        remove_active_collision_from_collection('OVAL', context.object.data.sub_swing_data)
        return {'FINISHED'}
    


class SUB_OP_swing_data_ellipsoid_add(Operator):
    bl_idname = 'sub.swing_data_ellipsoid_add'
    bl_label = 'Add Ellipsoid Collision'

    ellipsoid_name: StringProperty(name='Ellipoid Name Hash40')
    bone_name: StringProperty(name='Bone Name')
    offset: FloatVectorProperty(name='Offset', subtype='XYZ', size=3)
    rotation: FloatVectorProperty(name='Rotation', subtype='XYZ', size=3)
    scale: FloatVectorProperty(name='Scale', subtype='XYZ', size=3)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.ellipsoid_name = ""
        self.bone_name = ""
        self.offset = (0.0, 0.0, 0.0)
        self.rotation = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)

        return wm.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout

        layout.row().prop(self, "ellipsoid_name")

        layout.row().prop_search(self, 'bone_name', context.object.data, 'bones', text='Bone', icon='BONE_DATA')

        layout.row().prop(self, "offset")

        layout.row().prop(self, "rotation")

        layout.row().prop(self, "scale")   

    def execute(self, context):
        arma_obj: Object = context.object
        arma_data: Armature = arma_obj.data

        sub_swing_data: SUB_PG_sub_swing_data = arma_data.sub_swing_data
        if self.ellipsoid_name == "":
            return {'CANCELLED'}
        if self.bone_name == "":
            return {'CANCELLED'}
        
        new_ellipsoid: SUB_PG_swing_ellipsoid = sub_swing_data.ellipsoids.add()
        new_ellipsoid.name = self.ellipsoid_name
        new_ellipsoid.bone_name = self.bone_name
        new_ellipsoid.offset = self.offset
        new_ellipsoid.rotation = self.rotation
        new_ellipsoid.scale = self.scale
        new_ellipsoid.blender_object = create_meshes.make_ellipsoid_object(
            name = new_ellipsoid.name,
            offset = new_ellipsoid.offset,
            rotation = new_ellipsoid.rotation,
            scale = new_ellipsoid.scale,
            bone = arma_data.bones.get(new_ellipsoid.bone_name))
        
        new_ellipsoid_index = sub_swing_data.ellipsoids.find(new_ellipsoid.name)

        parent_swing_child_to_parent_obj_armature_deform(
            parent = arma_obj,
            child = new_ellipsoid.blender_object,
            type = 'ELLIPSOID',
            index = new_ellipsoid_index,)
        
        master_collection_props: SUB_PG_sub_swing_master_collection_props = get_swing_mesh_master_collection(
            context = context, 
            arma_obj = arma_obj).sub_swing_collection_props
        
        master_collection_props.ellipsoids_collection.objects.link(new_ellipsoid.blender_object)

        sub_swing_data.active_ellipsoid_index = new_ellipsoid_index

        # Save collision name hash to ParamLabels.csv
        add_hash_to_param_labels(new_ellipsoid.name, new_ellipsoid.name)
        # Also save the bone name hash (lowercase as used in PRC)
        add_hash_to_param_labels(new_ellipsoid.bone_name.lower(), new_ellipsoid.bone_name.lower())

        return {'FINISHED'}

class SUB_OP_swing_data_ellipsoid_remove(Operator):
    bl_idname = 'sub.swing_data_ellipsoid_remove'
    bl_label = 'Remove Ellipsoids Collision'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.ellipsoids) > 0
        
    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_ellipsoid: SUB_PG_swing_oval = sub_swing_data.ellipsoids[sub_swing_data.active_ellipsoid_index]

        bpy.data.meshes.remove(active_ellipsoid.blender_object.data)

        remove_active_collision_from_collection('ELLIPSOID', context.object.data.sub_swing_data)
        return {'FINISHED'}
    

class SUB_OP_swing_data_capsule_add(Operator):
    bl_idname = 'sub.swing_data_capsule_add'
    bl_label = 'Add Capsule Collision'

    capsule_name: StringProperty(name='Capsule Name')
    start_bone_name: StringProperty(name='Start Bone Name')
    end_bone_name: StringProperty(name='End Bone Name')
    start_offset: FloatVectorProperty(name='Start Offset', subtype='XYZ', size=3)
    end_offset: FloatVectorProperty(name='End Offset', subtype='XYZ', size=3)
    start_radius: FloatProperty(name='Start Radius')
    end_radius: FloatProperty(name='End Radius')

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'
    
    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.capsule_name = ""
        self.start_bone_name = ""
        self.end_bone_name = ""
        self.start_offset = (0.0, 0.0, 0.0)
        self.end_offset = (0.0, 0.0, 0.0)
        self.start_radius = 1.0
        self.end_radius = 1.0

        return wm.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        
        layout.row().prop(self, "capsule_name")

        layout.row().prop_search(self, 'start_bone_name', context.object.data, 'bones', text='Start Bone', icon='BONE_DATA')

        layout.row().prop_search(self, 'end_bone_name', context.object.data, 'bones', text='End Bone', icon='BONE_DATA')

        layout.row().prop(self, "start_offset")

        layout.row().prop(self, "end_offset")

        layout.row().prop(self, "start_radius")

        layout.row().prop(self, "end_radius")      

    def execute(self, context):
        arma_obj: Object = context.object
        arma_data: Armature = arma_obj.data

        sub_swing_data: SUB_PG_sub_swing_data = arma_data.sub_swing_data
        if self.capsule_name == "":
            return {'CANCELLED'}
        if self.start_bone_name == "":
            return {'CANCELLED'}
        if self.end_bone_name == "":
            return {'CANCELLED'}
        
        new_capsule: SUB_PG_swing_capsule = sub_swing_data.capsules.add()
        new_capsule.name            = self.capsule_name
        new_capsule.start_bone_name = self.start_bone_name
        new_capsule.end_bone_name   = self.end_bone_name
        new_capsule.start_offset    = self.start_offset
        new_capsule.end_offset      = self.end_offset
        new_capsule.start_radius    = self.start_radius
        new_capsule.end_radius      = self.end_radius
        new_capsule.blender_object = create_meshes.make_capsule_object(
            name = new_capsule.name,
            start_radius=new_capsule.start_radius,
            end_radius=new_capsule.end_radius,
            start_offset=new_capsule.start_offset,
            end_offset=new_capsule.end_offset,
            start_bone=arma_data.bones.get(self.start_bone_name),
            end_bone=arma_data.bones.get(self.end_bone_name))
        
        new_capsule_index = sub_swing_data.capsules.find(new_capsule.name)

        parent_swing_child_to_parent_obj_armature_deform(
            parent = arma_obj,
            child = new_capsule.blender_object,
            type = 'CAPSULE',
            index = new_capsule_index,)

        master_collection_props: SUB_PG_sub_swing_master_collection_props = get_swing_mesh_master_collection(
            context = context, 
            arma_obj = arma_obj).sub_swing_collection_props
        
        master_collection_props.capsules_collection.objects.link(new_capsule.blender_object)

        sub_swing_data.active_capsule_index = new_capsule_index
        
        # Save collision name hash to ParamLabels.csv
        add_hash_to_param_labels(new_capsule.name, new_capsule.name)
        # Also save the bone name hashes (lowercase as used in PRC)
        add_hash_to_param_labels(new_capsule.start_bone_name.lower(), new_capsule.start_bone_name.lower())
        add_hash_to_param_labels(new_capsule.end_bone_name.lower(), new_capsule.end_bone_name.lower())
        
        return {'FINISHED'}

class SUB_OP_swing_data_capsule_remove(Operator):
    bl_idname = 'sub.swing_data_capsule_remove'
    bl_label = 'Remove Capsule Collision'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.capsules) > 0
        
    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_capsule: SUB_PG_swing_oval = sub_swing_data.capsules[sub_swing_data.active_capsule_index]

        bpy.data.meshes.remove(active_capsule.blender_object.data)

        remove_active_collision_from_collection('CAPSULE', context.object.data.sub_swing_data)
        return {'FINISHED'}
    

class SUB_OP_swing_data_plane_add(Operator):
    bl_idname = 'sub.swing_data_plane_add'
    bl_label = 'Add Plane Collision'

    plane_name: StringProperty(name='Plane Name',)
    bone_name: StringProperty(name='Bone Name')
    nx: FloatProperty(name='nx')
    ny: FloatProperty(name='ny')
    nz: FloatProperty(name='nz')
    distance: FloatProperty(name='d')

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.plane_name = ""
        self.bone_name = ""
        self.nx = 1.0
        self.ny = 1.0
        self.nz = 1.0
        self.distance = 1.0

        return wm.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        
        layout.row().prop(self, "plane_name")

        layout.row().prop_search(self, 'bone_name', context.object.data, 'bones', text='Bone', icon='BONE_DATA')

        layout.row().prop(self, "nx")

        layout.row().prop(self, "ny")

        layout.row().prop(self, "nz")

        layout.row().prop(self, "distance")         

    def execute(self, context):
        arma_obj: Object = context.object
        arma_data: Armature = arma_obj.data

        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        if self.plane_name == "":
            return {'CANCELLED'}
        if self.bone_name == "":
            return {'CANCELLED'}
        
        new_plane: SUB_PG_swing_plane = sub_swing_data.planes.add()
        new_plane.name = self.plane_name
        new_plane.bone_name = self.bone_name
        new_plane.nx = self.nx
        new_plane.ny = self.ny
        new_plane.nz = self.nz
        new_plane.distance = self.distance
        new_plane.blender_object = create_meshes.make_plane_object(
            name= new_plane.name,
            bone= arma_data.bones.get(new_plane.bone_name),
            nx= new_plane.nx,
            ny= new_plane.ny,
            nz= new_plane.nz,
            d= new_plane.distance,)
        
        new_plane_index = sub_swing_data.planes.find(new_plane.name)

        parent_swing_child_to_parent_obj_armature_deform(
            parent = arma_obj,
            child = new_plane.blender_object,
            type = 'PLANE',
            index = new_plane_index,)
        
        master_collection_props: SUB_PG_sub_swing_master_collection_props = get_swing_mesh_master_collection(
            context = context, 
            arma_obj = arma_obj).sub_swing_collection_props
        
        master_collection_props.planes_collection.objects.link(new_plane.blender_object)

        sub_swing_data.active_plane_index = new_plane_index

        # Save collision name hash to ParamLabels.csv
        add_hash_to_param_labels(new_plane.name, new_plane.name)
        # Also save the bone name hash (lowercase as used in PRC)
        add_hash_to_param_labels(new_plane.bone_name.lower(), new_plane.bone_name.lower())

        return {'FINISHED'}

class SUB_OP_swing_data_plane_remove(Operator):
    bl_idname = 'sub.swing_data_plane_remove'
    bl_label = 'Remove Plane Collision'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.planes) > 0
        
    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        active_plane: SUB_PG_swing_plane = sub_swing_data.planes[sub_swing_data.active_plane_index]

        bpy.data.meshes.remove(active_plane.blender_object.data)

        remove_active_collision_from_collection('PLANE', context.object.data.sub_swing_data)
        return {'FINISHED'}
    

class SUB_OP_swing_data_connection_add(Operator):
    bl_idname = 'sub.swing_data_connection_add'
    bl_label = 'Add Swing Bone Connection Collision'

    start_bone_name: StringProperty(name='Start Bone Name Hash40')
    end_bone_name: StringProperty(name='End Bone Name Hash40')
    radius: FloatProperty(name='Radius')
    length: FloatProperty(name='Length')

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def invoke(self, context: Context, event):
        wm = context.window_manager
        self.start_bone_name = ""
        self.end_bone_name = ""
        self.radius = 1.0
        self.length = 1.0
        fill_armature_swing_bones(context)
        return wm.invoke_props_dialog(self)

    def draw(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        layout = self.layout
        
        layout.row().prop_search(self, 'start_bone_name', sub_swing_data, 'armature_swing_bones', text='Start Bone Name', icon='BONE_DATA')

        layout.row().prop_search(self, 'end_bone_name', sub_swing_data, 'armature_swing_bones', text='End Bone Name', icon='BONE_DATA')

        layout.row().prop(self, "radius")

        layout.row().prop(self, "length")       

    def execute(self, context):
        arma_obj: Object = context.object
        arma_data: Armature = arma_obj.data

        sub_swing_data: SUB_PG_sub_swing_data = arma_data.sub_swing_data
        if self.start_bone_name == "":
            return {'CANCELLED'}
        if self.end_bone_name == "":
            return {'CANCELLED'}
        
        new_connection: SUB_PG_swing_connection = sub_swing_data.connections.add()
        new_connection.start_bone_name = self.start_bone_name
        new_connection.end_bone_name = self.end_bone_name
        new_connection.radius = self.radius
        new_connection.length = self.length
        new_connection.blender_object = create_meshes.make_connection_obj(
            connection_name= f'{new_connection.start_bone_name} -> {new_connection.end_bone_name}',
            radius= new_connection.radius,
            start_bone=arma_data.bones.get(new_connection.start_bone_name),
            end_bone=arma_data.bones.get(new_connection.end_bone_name),
        )

        new_connection_index = sub_swing_data.connections.find(new_connection.name)

        parent_swing_child_to_parent_obj_armature_deform(
            parent = arma_obj,
            child = new_connection.blender_object,
            type = 'CAPSULE',
            index = new_connection_index,)

        master_collection_props: SUB_PG_sub_swing_master_collection_props = get_swing_mesh_master_collection(
            context = context, 
            arma_obj = arma_obj).sub_swing_collection_props
        
        master_collection_props.connections_collection.objects.link(new_connection.blender_object)

        sub_swing_data.active_connection_index = new_connection_index

        # Save bone name hashes to ParamLabels.csv (lowercase as used in PRC)
        add_hash_to_param_labels(new_connection.start_bone_name.lower(), new_connection.start_bone_name.lower())
        add_hash_to_param_labels(new_connection.end_bone_name.lower(), new_connection.end_bone_name.lower())

        return {'FINISHED'}

class SUB_OP_swing_data_connection_remove(Operator):
    bl_idname = 'sub.swing_data_connection_remove'
    bl_label = 'Remove Swing Bone Connection Collision'

    @classmethod
    def poll(cls, context):
        try:
            sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
        except:
            return False
        else:
            return len(sub_swing_data.connections) > 0
        
    def execute(self, context):
        sub_swing_data: SUB_PG_sub_swing_data = context.object.data.sub_swing_data

        active_connection: SUB_PG_swing_connection = sub_swing_data.connections[sub_swing_data.active_connection_index]

        bpy.data.meshes.remove(active_connection.blender_object.data)

        active_connection_index = sub_swing_data.active_connection_index
        sub_swing_data.connections.remove(active_connection_index)
        sub_swing_data.active_connection_index = max(0, min(active_connection_index, len(sub_swing_data.connections)-1))  
        return {'FINISHED'}


class SUB_OP_swing_import(Operator):
    bl_idname = 'sub.swing_import'
    bl_label = 'Import swing.prc'
    bl_description = 'Choose and import a swing.prc file for the active armature'

    filter_glob: StringProperty(
        default='*.prc',
        options={'HIDDEN'},
    )

    filepath: StringProperty(subtype='FILE_PATH')

    #rename_uncracked_things: BoolProperty(default=True)

    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        return context.object.type == 'ARMATURE'

    def invoke(self, context, _event):
        ssp = context.scene.sub_scene_properties
        
        # First priority: Use the last swing directory if available
        if ssp.last_swing_directory:
            import os
            self.filepath = os.path.join(ssp.last_swing_directory, 'swing.prc')
        # Second priority: Try to derive motion path from last imported model path
        elif ssp.last_imported_model_path:
            import os
            from pathlib import Path
            
            try:
                model_path = Path(ssp.last_imported_model_path)
                # Convert model path to motion path by replacing '/model/' with '/motion/'
                model_path_str = str(model_path)
                if '/model/' in model_path_str or '\\model\\' in model_path_str:
                    # Handle both forward and backward slashes
                    motion_path_str = model_path_str.replace('/model/', '/motion/').replace('\\model\\', '\\motion\\')
                    motion_path = Path(motion_path_str)
                    
                    if motion_path.exists():
                        self.filepath = str(motion_path / 'swing.prc')
                        self.report({'INFO'}, f"Auto-navigated to motion folder: {motion_path}")
                    else:
                        self.filepath = 'swing.prc'
                        self.report({'WARNING'}, f"Motion folder not found: {motion_path}")
                else:
                    self.filepath = 'swing.prc'
            except Exception as e:
                self.filepath = 'swing.prc'
                self.report({'WARNING'}, f"Could not derive motion path: {str(e)}")
        else:
            self.filepath = 'swing.prc'
            
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        # Save the directory to the scene property
        import os
        ssp = context.scene.sub_scene_properties
        ssp.last_swing_directory = os.path.dirname(self.filepath)
        
        swing_prc_import(self, context, self.filepath)
        arma_obj = context.object
        collection = get_swing_mesh_master_collection(context, arma_obj)
        setup_bone_meshes(self, context, collection)
        return {'FINISHED'}
    
def struct_get(param_struct, input, fallback=None):
    return dict(param_struct).get(pyprc.hash(input), fallback)

def struct_get_str(param_struct, input):
    h = struct_get(param_struct, input)
    return str(h.value) if h is not None else None 

def struct_get_val(param_struct, input):
    r = struct_get(param_struct, input)
    return r.value if r is not None else None

def swing_prc_import(operator: Operator, context: Context, filepath: str):
    arma_obj: bpy.types.Object = context.object
    arma_data: bpy.types.Armature = arma_obj.data
    ssd: SUB_PG_sub_swing_data = arma_data.sub_swing_data
    
    # Clear existing swing data & Blender Objects before importing
    master_collection = get_swing_mesh_master_collection(context, arma_obj)
    if master_collection:
        # Delete child objects from collections first
        collections_to_clear = [
            master_collection.sub_swing_collection_props.spheres_collection,
            master_collection.sub_swing_collection_props.ovals_collection,
            master_collection.sub_swing_collection_props.ellipsoids_collection,
            master_collection.sub_swing_collection_props.capsules_collection,
            master_collection.sub_swing_collection_props.planes_collection,
            master_collection.sub_swing_collection_props.connections_collection,
        ]
        swing_chains_collection = master_collection.sub_swing_collection_props.swing_chains_collection
        if swing_chains_collection:
            collections_to_clear.append(swing_chains_collection)
            for chain_coll in swing_chains_collection.children:
                collections_to_clear.append(chain_coll)
                for bone_coll_parent in chain_coll.children: # e.g., "chain_name swing bones", "chain_name collisions"
                    collections_to_clear.append(bone_coll_parent)
                    for bone_coll in bone_coll_parent.children: # e.g., individual bone collision collections
                        collections_to_clear.append(bone_coll)
        
        for coll in collections_to_clear:
            if coll: # Check if the collection reference exists
                for obj in list(coll.objects): # Iterate over a copy for safe removal
                    bpy.data.objects.remove(obj, do_unlink=True)
        
        # Delete the collections themselves after clearing objects
        # Child collections must be removed before parent collections
        if swing_chains_collection:
            for chain_coll in list(swing_chains_collection.children): # Iterate over a copy
                 for bone_coll_parent in list(chain_coll.children):
                     for bone_coll in list(bone_coll_parent.children):
                         bpy.data.collections.remove(bone_coll)
                     bpy.data.collections.remove(bone_coll_parent)
                 bpy.data.collections.remove(chain_coll)
            bpy.data.collections.remove(swing_chains_collection)

        collision_shapes_coll = master_collection.sub_swing_collection_props.collision_shapes_collection
        if collision_shapes_coll:
            for coll in list(collision_shapes_coll.children): # Iterate over a copy
                bpy.data.collections.remove(coll)
            bpy.data.collections.remove(collision_shapes_coll)
        
        # Finally, remove the master collection itself
        bpy.data.collections.remove(master_collection)

    # Clear Property Group Data
    ssd.spheres.clear()
    ssd.ovals.clear()
    ssd.ellipsoids.clear()
    ssd.capsules.clear()
    ssd.planes.clear()
    ssd.connections.clear()
    ssd.swing_bone_chains.clear()
    
    # Reset bone swing metadata
    for bone in arma_data.bones:
        bone.sub_swing_blender_bone_data.swing_bone_chain_index = -1
        bone.sub_swing_blender_bone_data.swing_bone_index = -1
    
    prc_root = pyprc.param(filepath)
    load_param_labels()

    raw_hash_to_blender_bone = {pyprc.hash(bone.name.lower()) : bone for bone in arma_data.bones}
    
    try:
        prc_spheres = list(dict(prc_root).get(pyprc.hash('spheres')))
    except:
        operator.report({'ERROR'}, 'No "spheres" list in the prc!')
        return

    for prc_sphere in prc_spheres:
        matched_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_sphere, 'bonename').value)
        if matched_bone is None:
            operator.report({'WARNING'}, f"Could not match bone for a sphere. {struct_get_str(prc_sphere, 'name')}, {struct_get_str(prc_sphere, 'bonename')}")
            continue
        new_sphere: SUB_PG_swing_sphere = ssd.spheres.add()
        new_sphere.name = struct_get_str(prc_sphere, 'name')
        new_sphere.bone = matched_bone.name
        new_sphere.offset.x = -(struct_get_val(prc_sphere, 'cy'))
        new_sphere.offset.y = struct_get_val(prc_sphere, 'cx')
        new_sphere.offset.z = struct_get_val(prc_sphere, 'cz')
        new_sphere.radius = struct_get_val(prc_sphere, 'radius')

    
    try:
        prc_ovals = list(dict(prc_root).get(pyprc.hash('ovals')))
    except:
        operator.report({'ERROR'}, 'No "ovals" list in the prc!')
        return

    for prc_oval in prc_ovals:
        matched_start_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_oval, 'start_bonename').value)
        matched_end_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_oval, 'end_bonename').value)
        if matched_start_bone is None or matched_end_bone is None:
            start_bone_hash = struct_get_str(prc_oval, 'start_bonename')
            end_bone_hash = struct_get_str(prc_oval, 'end_bonename')
            operator.report({'WARNING'}, f'Could not match bones for a oval. {start_bone_hash=}, {end_bone_hash=}')
            continue
        new_oval: SUB_PG_swing_oval = ssd.ovals.add()
        new_oval.name = struct_get_str(prc_oval, 'name')
        new_oval.start_bone_name = matched_start_bone.name
        new_oval.end_bone_name = matched_end_bone.name
        new_oval.radius = struct_get_val(prc_oval, 'radius')
        new_oval.start_offset.x = -(struct_get_val(prc_oval, 'start_offset_y'))
        new_oval.start_offset.y = struct_get_val(prc_oval, 'start_offset_x')
        new_oval.start_offset.z = struct_get_val(prc_oval, 'start_offset_z')
        new_oval.end_offset.x = -(struct_get_val(prc_oval, 'end_offset_y'))
        new_oval.end_offset.y = struct_get_val(prc_oval, 'end_offset_x') 
        new_oval.end_offset.z = struct_get_val(prc_oval, 'end_offset_z')

    try:
        prc_ellipsoids = list(dict(prc_root).get(pyprc.hash('ellipsoids')))
    except:
        operator.report({'ERROR'}, 'No "ellipsoids" list in the prc!')
        return
    
    for prc_ellipsoid in prc_ellipsoids:
        matched_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_ellipsoid, 'bonename').value)
        if matched_bone is None:
            operator.report({'WARNING'}, f"Could not match bone for a ellipsoid. {struct_get_str(prc_ellipsoid, 'name')}, {struct_get_str(prc_ellipsoid, 'bonename')}")
            continue
        new_ellipsoid: SUB_PG_swing_ellipsoid = ssd.ellipsoids.add()
        new_ellipsoid.name = struct_get_str(prc_ellipsoid, 'name')
        new_ellipsoid.bone_name = matched_bone.name
        new_ellipsoid.offset.x =  -(struct_get_val(prc_ellipsoid, 'cy')) 
        new_ellipsoid.offset.y = struct_get_val(prc_ellipsoid, 'cx')
        new_ellipsoid.offset.z = struct_get_val(prc_ellipsoid, 'cz')
        new_ellipsoid.rotation.x =  -(struct_get_val(prc_ellipsoid, 'ry'))
        new_ellipsoid.rotation.y = struct_get_val(prc_ellipsoid, 'rx')
        new_ellipsoid.rotation.z = struct_get_val(prc_ellipsoid, 'rz')
        new_ellipsoid.scale.x = struct_get_val(prc_ellipsoid, 'sy')
        new_ellipsoid.scale.y = struct_get_val(prc_ellipsoid, 'sx')
        new_ellipsoid.scale.z = struct_get_val(prc_ellipsoid, 'sz')
    
    try:
        prc_capsules = list(dict(prc_root).get(pyprc.hash('capsules')))
    except:
        operator.report({'ERROR'}, 'No "capsules" list in the prc!')
        return
    
    for prc_capsule in prc_capsules:
        matched_start_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_capsule, 'start_bonename').value)
        matched_end_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_capsule, 'end_bonename').value)
        if matched_start_bone is None or matched_end_bone is None:
            start_bone_hash = struct_get_str(prc_capsule, 'start_bonename')
            end_bone_hash = struct_get_str(prc_capsule, 'end_bonename')
            operator.report({'WARNING'}, f'Could not match bones for a capsule. {start_bone_hash=}, {end_bone_hash=}')
            continue
        new_capsule: SUB_PG_swing_capsule = ssd.capsules.add()
        new_capsule.name = struct_get_str(prc_capsule, 'name')
        new_capsule.start_bone_name = matched_start_bone.name
        new_capsule.end_bone_name = matched_end_bone.name
        new_capsule.start_offset.x = -(struct_get_val(prc_capsule, 'start_offset_y'))
        new_capsule.start_offset.y = struct_get_val(prc_capsule, 'start_offset_x') 
        new_capsule.start_offset.z = struct_get_val(prc_capsule, 'start_offset_z')
        new_capsule.end_offset.x = -(struct_get_val(prc_capsule, 'end_offset_y'))
        new_capsule.end_offset.y = struct_get_val(prc_capsule, 'end_offset_x') 
        new_capsule.end_offset.z = struct_get_val(prc_capsule, 'end_offset_z')
        new_capsule.start_radius = struct_get_val(prc_capsule, 'start_radius')
        new_capsule.end_radius = struct_get_val(prc_capsule, 'end_radius')

    try:
        prc_planes = list(dict(prc_root).get(pyprc.hash('planes')))
    except:
        operator.report({'ERROR'}, 'No "planes" list in the prc!')
        return

    for prc_plane in prc_planes:
        matched_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_plane, 'bonename').value)
        if matched_bone is None:
            operator.report({'WARNING'}, f"Could not match bone for a plane. {struct_get_str(prc_plane, 'name')}, {struct_get_str(prc_plane, 'bonename')}")
            continue
        new_plane: SUB_PG_swing_plane = ssd.planes.add()
        new_plane.name = struct_get_str(prc_plane, 'name')
        new_plane.bone_name = matched_bone.name
        new_plane.nx = -(struct_get_val(prc_plane, 'ny'))
        new_plane.ny = struct_get_val(prc_plane, 'nx')
        new_plane.nz = struct_get_val(prc_plane, 'nz')
        new_plane.distance = struct_get_val(prc_plane, 'distance')

    try:
        prc_connections = list(dict(prc_root).get(pyprc.hash('connections')))
    except:
        operator.report({'ERROR'}, 'No "connections" list in the prc!')
        return

    for prc_connection in prc_connections:
        matched_start_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_connection, 'start_bonename').value)
        matched_end_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(prc_connection, 'end_bonename').value)
        if matched_start_bone is None:
            start_bone_hash = struct_get_str(prc_connection, 'start_bonename')
            end_bone_hash = struct_get_str(prc_connection, 'end_bonename')
            operator.report({'WARNING'}, f'Could not match start bone for a connection. {start_bone_hash=}, {end_bone_hash=}')
            continue
        elif matched_end_bone is None:
            start_bone_hash = struct_get_str(prc_connection, 'start_bonename')
            end_bone_hash = struct_get_str(prc_connection, 'end_bonename')
            operator.report({'WARNING'}, f'Could not match end bone for a connction. {start_bone_hash=}, {end_bone_hash=}')
            continue
        new_connection: SUB_PG_swing_connection = ssd.connections.add()
        new_connection.start_bone_name = matched_start_bone.name
        new_connection.end_bone_name = matched_end_bone.name
        new_connection.radius = struct_get_val(prc_connection, 'radius')
        new_connection.length = struct_get_val(prc_connection, 'length')
    
    try:
        prc_swing_bone_chains = list(dict(prc_root).get(pyprc.hash('swingbones')))
    except:
        operator.report({'ERROR'}, 'No "swingbones" list in the prc!')
        return
    for chain in prc_swing_bone_chains:
        matched_start_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(chain, 'start_bonename').value)
        matched_end_bone: bpy.types.Bone = raw_hash_to_blender_bone.get(struct_get(chain, 'end_bonename').value)
        if matched_start_bone is None or matched_end_bone is None:
            chain_name = struct_get_str(chain, 'name')
            start_bone_hash = struct_get_str(chain, 'start_bonename')
            end_bone_hash = struct_get_str(chain, 'end_bonename')
            operator.report({'WARNING'}, f'Could not match bones for the bone chain "{chain_name}", skipping this chain. {start_bone_hash=}, {end_bone_hash=}')
            continue

        if multi_child_bones:= {bone.name for bone in {matched_start_bone} | set(matched_start_bone.children_recursive) if len(bone.children) > 1}:
            chain_name = struct_get_str(chain, 'name')
            operator.report({'WARNING'}, f'The bone chain "{chain_name}" is not a linear hierarchy! Skipping this chain. (The following bones had more than one child: {multi_child_bones})')
            continue

        new_chain: SUB_PG_swing_bone_chain = ssd.swing_bone_chains.add()
        new_chain.name            = struct_get_str(chain, 'name')
        new_chain.start_bone_name = matched_start_bone.name
        new_chain.start_bone = matched_start_bone
        new_chain.end_bone = matched_end_bone
        new_chain.end_bone_name   = matched_end_bone.name
        new_chain.is_skirt        = struct_get_val(chain, 'isskirt')
        new_chain.rotate_order    = struct_get_val(chain, 'rotateorder')
        new_chain.curve_rotate_x  = struct_get_val(chain, 'curverotatex')
        unk_8 = struct_get_val(chain, 0x0f7316a113)
        if unk_8 is not None:
            new_chain.has_unk_8 = True
            new_chain.unk_8 = unk_8
        
        for prc_swing_bone_parameters in list(struct_get(chain, 'params')):
            new_swing_bone: SUB_PG_swing_bone           = new_chain.swing_bones.add()
            new_swing_bone.air_resistance       = struct_get_val(prc_swing_bone_parameters, 'airresistance')
            new_swing_bone.water_resistance     = struct_get_val(prc_swing_bone_parameters, 'waterresistance')
            #new_swing_bone.min_angle_z          = struct_get_val(prc_swing_bone_parameters, 'minanglez')
            #new_swing_bone.max_angle_z          = struct_get_val(prc_swing_bone_parameters, 'maxanglez')
            new_swing_bone.angle_z[0]           = radians(struct_get_val(prc_swing_bone_parameters, 'minanglez'))
            new_swing_bone.angle_z[1]           = radians(struct_get_val(prc_swing_bone_parameters, 'maxanglez'))
            #new_swing_bone.min_angle_y          = struct_get_val(prc_swing_bone_parameters, 'minangley')
            #new_swing_bone.max_angle_y          = struct_get_val(prc_swing_bone_parameters, 'maxangley')
            new_swing_bone.angle_y[0]           = radians(struct_get_val(prc_swing_bone_parameters, 'minangley'))
            new_swing_bone.angle_y[1]           = radians(struct_get_val(prc_swing_bone_parameters, 'maxangley'))
            #new_swing_bone.collision_size_tip   = struct_get_val(prc_swing_bone_parameters, 'collisionsizetip')
            #new_swing_bone.collision_size_root  = struct_get_val(prc_swing_bone_parameters, 'collisionsizeroot')
            new_swing_bone.collision_size[0]    = struct_get_val(prc_swing_bone_parameters, 'collisionsizeroot')
            new_swing_bone.collision_size[1]    = struct_get_val(prc_swing_bone_parameters, 'collisionsizetip')
            new_swing_bone.friction_rate        = struct_get_val(prc_swing_bone_parameters, 'frictionrate')
            new_swing_bone.goal_strength        = struct_get_val(prc_swing_bone_parameters, 'goalstrength')
            new_swing_bone.inertial_mass        = struct_get_val(prc_swing_bone_parameters, 'inertialmass')
            new_swing_bone.local_gravity        = struct_get_val(prc_swing_bone_parameters, 'localgravity')
            new_swing_bone.fall_speed_scale     = struct_get_val(prc_swing_bone_parameters, 'fallspeedscale')
            new_swing_bone.ground_hit           = struct_get_val(prc_swing_bone_parameters, 'groundhit')
            new_swing_bone.wind_affect          = struct_get_val(prc_swing_bone_parameters, 'windaffect')
            for prc_swing_bone_collision in list(struct_get(prc_swing_bone_parameters, 'collisions')):
                if str(prc_swing_bone_collision.value) == '':
                    operator.report({'INFO'}, f'A swing bone in chain {new_chain.name} has an empty collision, discarding the empty collision.')
                    continue
                col_type, col_index = get_collision_info(ssd, str(prc_swing_bone_collision.value))
                if col_type is None or col_index is None:
                    operator.report({'WARNING'}, f'A swing bone in chain {new_chain.name} refrences missing collision {str(prc_swing_bone_collision.value)}, discarding the missing collision.')
                    continue
                new_swing_bone_collision: SUB_PG_swing_bone_collision   = new_swing_bone.collisions.add()
                new_swing_bone_collision.collision_type = col_type
                new_swing_bone_collision.collision_index = col_index
                #new_swing_bone_collision.target_collision_name = str(prc_swing_bone_collision.value)
                #new_swing_bone_collision: SwingBoneCollision   = new_swing_bone.collisions.add()
                #new_swing_bone_collision.target_collision_name = str(prc_swing_bone_collision.value)
    swing_bone_chain: SUB_PG_swing_bone_chain
    swing_bone: SUB_PG_swing_bone
    for chain_index, swing_bone_chain in enumerate(ssd.swing_bone_chains):
        starting_blender_bone = arma_data.bones.get(swing_bone_chain.start_bone_name) 
        current_blender_bone = starting_blender_bone
        for bone_index, swing_bone in enumerate(swing_bone_chain.swing_bones):
            swing_bone.name = current_blender_bone.name
            current_blender_bone.sub_swing_blender_bone_data.swing_bone_chain_index = chain_index
            current_blender_bone.sub_swing_blender_bone_data.swing_bone_index = bone_index
            current_blender_bone = current_blender_bone.children[0]

def get_collision_info(ssd, collision_name):
    ssd: SUB_PG_sub_swing_data = ssd
    # Needs to be in same order as the enum
    collision_collections = [
        ssd.spheres,
        ssd.ovals,
        ssd.ellipsoids,
        ssd.capsules,
        ssd.planes,
    ]
    for collection in collision_collections:
        for collision_index, collision in enumerate(collection):
            if collision.name == collision_name:
                if isinstance(collision, SUB_PG_swing_sphere):
                    return 'SPHERE', collision_index
                elif isinstance(collision, SUB_PG_swing_oval):
                    return 'OVAL', collision_index
                elif isinstance(collision, SUB_PG_swing_ellipsoid):
                    return 'ELLIPSOID', collision_index
                elif isinstance(collision, SUB_PG_swing_capsule):
                    return 'CAPSULE', collision_index
                elif isinstance(collision, SUB_PG_swing_plane):
                    return 'PLANE', collision_index
                else:
                    raise RuntimeError
    return None, None

def is_uncracked_hash(s: str) -> bool:
    try:
        int(s, base=16)
    except:
        return False
    else:
        return True

def rename_uncracked_hashes(operator: Operator, context: Context):
    # forward declaration for typechecking.
    swing_bone_chain: SUB_PG_swing_bone_chain
    swing_bone: SUB_PG_swing_bone 
    collision: SUB_PG_swing_bone_collision 
    sphere: SUB_PG_swing_sphere
    oval: SUB_PG_swing_oval
    ellipsoid: SUB_PG_swing_ellipsoid
    capsule: SUB_PG_swing_capsule
    plane: SUB_PG_swing_plane
    connection: SUB_PG_swing_connection

    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data

    for swing_bone_chain in ssd.swing_bone_chains:
        if is_uncracked_hash(swing_bone_chain.name):
            swing_bone_chain.name = f'{swing_bone_chain.start_bone_name}_to_{swing_bone_chain.end_bone_name}'
        for swing_bone in swing_bone_chain.swing_bones:
            for collision in swing_bone.collisions:
                if True:
                    pass

def new_swing_collection(collection_name: str) -> Collection:
    collection = bpy.data.collections.new(collection_name)
    collection.color_tag = 'COLOR_05'
    return collection

def parent_swing_child_to_parent_obj_armature_deform(parent: bpy.types.Object, child: bpy.types.Object, type: str, index: int):
    child.parent = parent
    linked_sphere_data: SUB_PG_sub_swing_data_linked_mesh = child.data.sub_swing_data_linked_mesh
    linked_sphere_data.collision_collection_type = type
    linked_sphere_data.is_swing_mesh = True
    linked_sphere_data.collision_collection_index = index
    armature_modifier: bpy.types.ArmatureModifier = child.modifiers.new("Armature", "ARMATURE")
    armature_modifier.object = parent

def parent_swing_bone_collision(parent, child, chain_index, bone_index):
    child.parent = parent
    linked_sphere_data: SUB_PG_sub_swing_data_linked_mesh = child.data.sub_swing_data_linked_mesh
    linked_sphere_data.is_swing_mesh = True
    linked_sphere_data.is_swing_bone_shape = True
    linked_sphere_data.swing_chain_index = chain_index
    linked_sphere_data.swing_bone_index = bone_index
    armature_modifier: bpy.types.ArmatureModifier = child.modifiers.new("Armature", "ARMATURE")
    armature_modifier.object = parent

def create_swing_mesh_master_collection(parent_collection: Collection, arma_obj: Object):
    swing_master_collection: Collection = new_swing_collection(f'{arma_obj.name} Swing Objects')
    sub_swing_collection_props: SUB_PG_sub_swing_master_collection_props = swing_master_collection.sub_swing_collection_props
    sub_swing_collection_props.linked_object = arma_obj
    sub_swing_collection_props.swing_chains_collection = new_swing_collection("Swing Bone Chains")
    sub_swing_collection_props.collision_shapes_collection = new_swing_collection("Collision Shapes")
    sub_swing_collection_props.spheres_collection = new_swing_collection("Spheres")
    sub_swing_collection_props.ovals_collection = new_swing_collection("Ovals")
    sub_swing_collection_props.ellipsoids_collection = new_swing_collection("Ellipsoids")
    sub_swing_collection_props.capsules_collection = new_swing_collection("Capsules")
    sub_swing_collection_props.planes_collection = new_swing_collection("Planes")
    sub_swing_collection_props.connections_collection = new_swing_collection("Connections")

    parent_collection.children.link(swing_master_collection)
    swing_master_collection.children.link(sub_swing_collection_props.swing_chains_collection)
    swing_master_collection.children.link(sub_swing_collection_props.collision_shapes_collection)

    for collision_shape_collection in (
        sub_swing_collection_props.spheres_collection,
        sub_swing_collection_props.ovals_collection,
        sub_swing_collection_props.ellipsoids_collection,
        sub_swing_collection_props.capsules_collection,
        sub_swing_collection_props.planes_collection,
        sub_swing_collection_props.connections_collection
    ):
        sub_swing_collection_props.collision_shapes_collection.children.link(collision_shape_collection)
    
    return swing_master_collection
    

def get_swing_mesh_master_collection(context: Context, arma_obj: Object) -> Collection:
    # The object could be in several collections, need to check them all
    collections: set[Collection] = {context.scene.collection} | set(context.scene.collection.children_recursive)
    arma_collections = {c for c in collections if arma_obj.name in c.objects}
    master_collection = None
    for arma_collection in arma_collections:
        for sibling_collection in arma_collection.children:
            sub_swing_collection_props: SUB_PG_sub_swing_master_collection_props = sibling_collection.sub_swing_collection_props
            if sub_swing_collection_props.linked_object is not None:
                if sub_swing_collection_props.linked_object.name == arma_obj.name:
                    master_collection = sibling_collection
    if master_collection is not None:
        return master_collection
    else:
        # Just spawn the new collection in one of the several possible ones the armature is in.
        return create_swing_mesh_master_collection(arma_collections.pop(), arma_obj)

def setup_bone_meshes(operator: Operator, context: Context, master_collection: Collection):
    ssd: SUB_PG_sub_swing_data = context.object.data.sub_swing_data
    master_collection_props: SUB_PG_sub_swing_master_collection_props = master_collection.sub_swing_collection_props

    """
    swing_master_collection: Collection = new_swing_collection(f'{context.object.name} Swing Objects')
    swing_chains_collection: Collection = new_swing_collection('Swing Bone Chains')
    collision_shapes_collection: Collection = new_swing_collection('Collision Shapes')
    shape_collection_names = ('Spheres', 'Ovals', 'Ellipsoids', 'Capsules', 'Planes', 'Connections')
    shape_name_to_collection: dict[str, Collection] = {}
    for shape_collection_name in shape_collection_names:
        shape_collection: Collection = new_swing_collection(shape_collection_name)
        collision_shapes_collection.children.link(shape_collection)
        shape_name_to_collection[shape_collection_name] = shape_collection
    context.collection.children.link(swing_master_collection)
    swing_master_collection.children.link(swing_chains_collection)
    swing_master_collection.children.link(collision_shapes_collection)
    """
    
    swing_sphere: SUB_PG_swing_sphere
    for sphere_index, swing_sphere in enumerate(ssd.spheres):
        blender_bone: bpy.types.Bone = context.object.data.bones.get(swing_sphere.bone)
        if blender_bone is None:
                continue
        
        sphere_obj = create_meshes.make_sphere_object_2(swing_sphere.name, swing_sphere.radius, swing_sphere.offset, blender_bone)
        parent_swing_child_to_parent_obj_armature_deform(context.object, sphere_obj, 'SPHERE', sphere_index)
        swing_sphere.blender_object = sphere_obj
        """spheres_collection = shape_name_to_collection['Spheres']
        spheres_collection.objects.link(sphere_obj)"""
        master_collection_props.spheres_collection.objects.link(sphere_obj)

    swing_oval: SUB_PG_swing_oval
    for oval_index, swing_oval in enumerate(ssd.ovals):
        start_bone: bpy.types.Bone = context.object.data.bones.get(swing_oval.start_bone_name)
        end_bone: bpy.types.Bone = context.object.data.bones.get(swing_oval.end_bone_name)
        oval_obj = create_meshes.make_capsule_object(
            swing_oval.name, 
            swing_oval.radius, 
            swing_oval.radius, 
            swing_oval.start_offset, 
            swing_oval.end_offset,
            start_bone,
            end_bone)
        swing_oval.blender_object = oval_obj
        parent_swing_child_to_parent_obj_armature_deform(context.object, oval_obj, 'OVAL', oval_index)
        """ovals_collection = shape_name_to_collection['Ovals']
        ovals_collection.objects.link(oval_obj)"""
        master_collection_props.ovals_collection.objects.link(oval_obj)

    swing_ellipsoid: SUB_PG_swing_ellipsoid
    for ellipsoid_index, swing_ellipsoid in enumerate(ssd.ellipsoids):
        blender_bone: bpy.types.Bone = context.object.data.bones.get(swing_ellipsoid.bone_name)
        s = Vector(swing_ellipsoid.scale)
        r = Vector(swing_ellipsoid.rotation)
        o = Vector(swing_ellipsoid.offset)
        ellipsoid_obj: bpy.types.Object = create_meshes.make_ellipsoid_object(swing_ellipsoid.name, offset=o, rotation=r, scale=s, bone=blender_bone )
        parent_swing_child_to_parent_obj_armature_deform(context.object, ellipsoid_obj, 'ELLIPSOID', ellipsoid_index)

        swing_ellipsoid.blender_object = ellipsoid_obj
        """ellipsoids_collection = shape_name_to_collection['Ellipsoids']
        ellipsoids_collection.objects.link(ellipsoid_obj)"""
        master_collection_props.ellipsoids_collection.objects.link(ellipsoid_obj)

    swing_capsule: SUB_PG_swing_capsule
    for capsule_index, swing_capsule in enumerate(ssd.capsules):
        start_bone: bpy.types.Bone = context.object.data.bones.get(swing_capsule.start_bone_name)
        end_bone: bpy.types.Bone = context.object.data.bones.get(swing_capsule.end_bone_name)
        start_offset = Vector(swing_capsule.start_offset)
        end_offset = Vector(swing_capsule.end_offset)
        capsule_obj = create_meshes.make_capsule_object(
            swing_capsule.name, 
            start_radius=swing_capsule.start_radius, 
            end_radius=swing_capsule.end_radius, 
            start_offset=start_offset, 
            end_offset=end_offset,
            start_bone=start_bone,
            end_bone=end_bone)
        parent_swing_child_to_parent_obj_armature_deform(context.object, capsule_obj, 'CAPSULE', capsule_index)

        swing_capsule.blender_object = capsule_obj

        """capsules_collection = shape_name_to_collection['Capsules']
        capsules_collection.objects.link(capsule_obj)"""
        master_collection_props.capsules_collection.objects.link(capsule_obj)

    swing_plane: SUB_PG_swing_plane
    for plane_index, swing_plane in enumerate(ssd.planes):
        blender_bone: bpy.types.Bone = context.object.data.bones.get(swing_plane.bone_name)
        plane_obj = create_meshes.make_plane_object(
            swing_plane.name,
            blender_bone,
            swing_plane.nx,
            swing_plane.ny,
            swing_plane.nz,
            swing_plane.distance
        )
        parent_swing_child_to_parent_obj_armature_deform(context.object, plane_obj, 'PLANE', plane_index)

        swing_plane.blender_object = plane_obj

        """planes_collection = shape_name_to_collection['Planes']
        planes_collection.objects.link(plane_obj)"""
        master_collection_props.planes_collection.objects.link(plane_obj)
        
    swing_connection: SUB_PG_swing_connection
    for connection_index, swing_connection in enumerate(ssd.connections):
        start_bone: bpy.types.Bone = context.object.data.bones.get(swing_connection.start_bone_name)
        end_bone: bpy.types.Bone = context.object.data.bones.get(swing_connection.end_bone_name)
        connection_name = f'{start_bone.name} -> {end_bone.name}'
        connection_obj = create_meshes.make_connection_obj(
            connection_name,
            swing_connection.radius, 
            start_bone,
            end_bone)
        parent_swing_child_to_parent_obj_armature_deform(context.object, connection_obj, 'CONNECTION', connection_index)
        swing_connection.blender_object = connection_obj

        """connections_collection = shape_name_to_collection['Connections']
        connections_collection.objects.link(connection_obj)"""
        master_collection_props.connections_collection.objects.link(connection_obj)

    swing_bone_chain: SUB_PG_swing_bone_chain
    swing_bone: SUB_PG_swing_bone
    swing_bone_collision: SUB_PG_swing_bone_collision
    collision_name_to_collision = {}
    collision_lists =(ssd.spheres, ssd.ovals, ssd.ellipsoids, ssd.capsules,) #ssd.planes
    for collision_list in collision_lists:
        for collision in collision_list:
            collision_name_to_collision[collision.name] = collision
    for chain_index, swing_bone_chain in enumerate(ssd.swing_bone_chains): # type: list[SwingBoneChain]
        chain_collection = new_swing_collection(swing_bone_chain.name)
        chain_swing_bones_collection = new_swing_collection(f'{swing_bone_chain.name} swing bones')
        chain_collision_collection = new_swing_collection(f'{swing_bone_chain.name} collisions')
        """swing_chains_collection.children.link(chain_collection)"""
        master_collection_props.swing_chains_collection.children.link(chain_collection)
        chain_collection.children.link(chain_swing_bones_collection)
        chain_collection.children.link(chain_collision_collection)
        for bone_index, swing_bone in enumerate(swing_bone_chain.swing_bones):
            # Set Up Swing Bone Capsules
            blender_bone: bpy.types.Bone = context.object.data.bones.get(swing_bone.name)
            if blender_bone is None:
                continue
            if len(blender_bone.children) != 1:
                continue
            child_bone = blender_bone.children[0]
            if child_bone.name.endswith("_null"):
                skin_to_start_only = True
            else:
                skin_to_start_only = False
            cap = create_meshes.make_capsule_object(
                swing_bone.name,
                swing_bone.collision_size[0], 
                swing_bone.collision_size[1], 
                (0,0,0),
                (0,0,0),
                blender_bone,
                child_bone,
                skin_to_start_only)
            swing_bone.blender_object = cap
            parent_swing_bone_collision(context.object, cap, chain_index, bone_index)
            chain_swing_bones_collection.objects.link(cap)
            swing_bone_collision_collection = new_swing_collection(f'{swing_bone_chain.name} {swing_bone.name} collisions')
            chain_collision_collection.children.link(swing_bone_collision_collection)
            '''
            for swing_bone_collision in swing_bone.collisions:
                # Set Up A Collection for this bone's collisions, 'link' collision objects to it.
                collision = collision_name_to_collision.get(swing_bone_collision.target_collision_name)
                if collision is None:
                    operator.report({'WARNING'}, f"Could not find collision {swing_bone_collision.target_collision_name} for Swing bone {blender_bone.name}")
                    continue
                if collision.blender_object is None:
                    operator.report({'WARNING'}, f"Collision {swing_bone_collision.target_collision_name} for Swing bone {blender_bone.name} has missing blender object")
                    continue
                swing_bone_collision_collection.objects.link(collision.blender_object)
            '''

class SUB_OP_swing_export(Operator):
    bl_idname = 'sub.swing_export'
    bl_label = 'Export swing.prc'
    bl_description = 'Choose where to export swing.prc for the active armature'

    filter_glob: StringProperty(
        default='*.prc',
        options={'HIDDEN'},
    )

    filepath: StringProperty(subtype='FILE_PATH')

    @classmethod
    def poll(cls, context):
        arma: bpy.types.Object = context.object
        if not arma:
            return False
        if arma.type != 'ARMATURE':
            return False
        ssd: SUB_PG_sub_swing_data = arma.data.sub_swing_data
        return len(ssd.swing_bone_chains) > 0

    def invoke(self, context, _event):
        ssp = context.scene.sub_scene_properties
        # Use the last directory if available
        if ssp.last_swing_directory:
            import os
            self.filepath = os.path.join(ssp.last_swing_directory, 'swing.prc')
        else:
            self.filepath = 'swing.prc'
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        # Save the directory to the scene property
        import os
        ssp = context.scene.sub_scene_properties
        ssp.last_swing_directory = os.path.dirname(self.filepath)
        
        swing_prc_export(self, context, self.filepath)
        return {'FINISHED'}

'''
A 'list' in the prc is really a 'struct' with entries (hash, list).
swing.prc format
pyprc.param.struct([
    (pyprc.param.struct([
        (pyprc.hash('swingbones'), pyprc.param.list([

        ]))
    ])),
])
    swingbones: list_struct i.e. struct([(hash('swingbones'), list)])
        struct([ # One for each swing bone chain
            name: hash_struct aka struct([hash(hash_name), hash(hash_value)])
            start_bonename: hash_struct
            end_bonename: hash_struct
            params: list_struct i.e. struct([(hash('params'), list)])
                struct([ # One for each swing bone in this chain
                airresistance: float_struct i.e. struct([(hash('airresistance'))])
                ])
                ...
        ])
        ...
    list aka struct([hash(bone_name.lower() + col), list]) # the collisions for each swing bone re-listed
        hash40(collision_name)
        ...

'''
def new_prc_list(list_name):
    add_hash_label(list_name)
    return pyprc.param.struct([
        (pyprc.hash(list_name),pyprc.param.list([]))
    ])

def new_prc_struct():
    return pyprc.param.struct([])

def new_prc_hash(hash_name: str|int, hash_value: str|int):
    regex = r"^0x[\da-f]{10}$"
    if isinstance(hash_name, str):
        add_hash_label(hash_name)
        matches = re.match(regex, hash_name)
        if matches is not None:
            hash_name = int(hash_name, base=16)

    if isinstance(hash_value, str):
        add_hash_label(hash_value)
        matches = re.match(regex, hash_value)
        if matches is not None:
            hash_value = int(hash_value, base=16)

    return pyprc.param.struct([
        (pyprc.hash(hash_name), pyprc.param.hash(pyprc.hash(hash_value)))
    ])

def new_prc_float(float_name: str| int, float_value: float):
    return pyprc.param.struct([
        (pyprc.hash(float_name), pyprc.param.float(float_value))
    ])

def new_prc_byte(byte_name: str|int, byte_value: int):
    return pyprc.param.struct([
        (pyprc.hash(byte_name), pyprc.param.i8(byte_value))
    ])
def new_prc_int(int_name: str|int, int_value: int):
    return pyprc.param.struct([
        (pyprc.hash(int_name), pyprc.param.i32(int_value))
    ])
def extend_struct(struct, new_thing):
    struct.set_struct(list(struct) + list(new_thing))

class PrcStruct():
    _prc_struct = None
    def __init__(self, params: list = []):
        self._prc_struct = pyprc.param.struct([])
        for param in params:
            self += param
    def __iter__(self):
        return self._prc_struct.__iter__()
    def __iadd__(self, other):
        self.extend_struct(other)
        return self
    def __repr__(self):
        return self._prc_struct.__repr__()
    def get_param(self):
        return self._prc_struct
    def extend_struct(self, new_prc_param):
        self._prc_struct.set_struct(list(self._prc_struct) + list(new_prc_param))
        return self
    def save(self, path):
        self._prc_struct.save(path)

class PrcList(PrcStruct):
    def __init__(self, list_name: str):
        add_hash_label(list_name)
        self._prc_struct = pyprc.param.struct([
            (pyprc.hash(list_name), pyprc.param.list([]))
        ])
    def __iadd__(self, other):
        self.extend_list(other)
        return self
    def get_prc_list(self):
        return list(self._prc_struct)[0][1]
    def get_prc_list_as_py_list(self):
        return list(self.get_prc_list())
    def extend_list(self, new_prc_param: PrcStruct):
        self.get_prc_list().set_list(self.get_prc_list_as_py_list() + [new_prc_param.get_param()])
        return self

class PrcHash40():
    _prc_param = None
    def __init__(self, hash_value):
        if isinstance(hash_value, str):
            add_hash_label(hash_value)
            regex = r"0x[\da-f]{10}"
            matches = re.match(regex, hash_value)
            if matches is not None:
                hash_value = int(hash_value, base=16)

        self._prc_param = pyprc.param.hash(pyprc.hash(hash_value))
    def get_param(self):
        return self._prc_param
    def __repr__(self):
        return self._prc_param.__repr__()

from ..export_progress import export_progress


@export_progress
def swing_prc_export(operator: Operator, context: Context, filepath: str):
    # forward declaration for typechecking.
    swing_bone_chain: SUB_PG_swing_bone_chain
    swing_bone: SUB_PG_swing_bone 
    collision: SUB_PG_swing_bone_collision 
    sphere: SUB_PG_swing_sphere
    oval: SUB_PG_swing_oval
    ellipsoid: SUB_PG_swing_ellipsoid
    capsule: SUB_PG_swing_capsule
    plane: SUB_PG_swing_plane
    connection: SUB_PG_swing_connection

    arma_data: bpy.types.Armature = context.object.data
    ssd: SUB_PG_sub_swing_data = arma_data.sub_swing_data
    prc_root = PrcStruct()
    # Add 'swingbones' list
    swing_bone_chains_list = PrcList('swingbones')
    for swing_bone_chain in ssd.swing_bone_chains:
        '''
        swing_bone_chain_struct  = PrcStruct()
        swing_bone_chain_struct += new_prc_hash('name', swing_bone_chain.name)
        swing_bone_chain_struct += new_prc_hash('start_bonename', swing_bone_chain.start_bone_name.lower())
        swing_bone_chain_struct += new_prc_hash('end_bonename', swing_bone_chain.end_bone_name.lower())
        swing_bone_param_list    = PrcList('params')
        swing_bone_chain_struct += swing_bone_param_list
        swing_bone_chain_struct += new_prc_byte('isskirt', swing_bone_chain.is_skirt)
        swing_bone_chain_struct += new_prc_int('rotateorder', swing_bone_chain.rotate_order)
        swing_bone_chain_struct += new_prc_byte('curverotatex', swing_bone_chain.is_skirt)
        if swing_bone_chain.has_unk_8 == True:
            swing_bone_chain_struct += new_prc_byte(0x0f7316a113, swing_bone_chain.unk_8)
        '''
        swing_bone_param_list    = PrcList('params')
        swing_bone_chain_struct  = PrcStruct([
            new_prc_hash('name', swing_bone_chain.name),
            new_prc_hash('start_bonename', swing_bone_chain.start_bone_name.lower()),
            new_prc_hash('end_bonename', swing_bone_chain.end_bone_name.lower()),
            swing_bone_param_list,
            new_prc_byte('isskirt', swing_bone_chain.is_skirt),
            new_prc_int('rotateorder', swing_bone_chain.rotate_order),
            new_prc_byte('curverotatex', swing_bone_chain.is_skirt),
        ])
        if swing_bone_chain.has_unk_8 == True:
            swing_bone_chain_struct += new_prc_byte(0x0f7316a113, swing_bone_chain.unk_8)
        for swing_bone in swing_bone_chain.swing_bones:
            '''
            swing_bone_params_struct = PrcStruct()
            swing_bone_params_struct += new_prc_float('airresistance', swing_bone.air_resistance)
            swing_bone_params_struct += new_prc_float('waterresistance', swing_bone.water_resistance)
            swing_bone_params_struct += new_prc_float('minanglez', degrees(swing_bone.angle_z[0]))
            swing_bone_params_struct += new_prc_float('maxanglez', degrees(swing_bone.angle_z[1]))
            swing_bone_params_struct += new_prc_float('minangley', degrees(swing_bone.angle_y[0]))
            swing_bone_params_struct += new_prc_float('maxangley', degrees(swing_bone.angle_y[1]))
            swing_bone_params_struct += new_prc_float('collisionsizetip', swing_bone.collision_size[1])
            swing_bone_params_struct += new_prc_float('collisionsizeroot', swing_bone.collision_size[0])
            swing_bone_params_struct += new_prc_float('frictionrate', swing_bone.friction_rate)
            swing_bone_params_struct += new_prc_float('goalstrength', swing_bone.goal_strength)
            swing_bone_params_struct += new_prc_float(0x0cc10e5d3a, swing_bone.unk_11)
            swing_bone_params_struct += new_prc_float('localgravity', swing_bone.local_gravity)
            swing_bone_params_struct += new_prc_float('fallspeedscale', swing_bone.fall_speed_scale)
            swing_bone_params_struct += new_prc_byte('groundhit', swing_bone.ground_hit)
            swing_bone_params_struct += new_prc_float('windaffect', swing_bone.wind_affect)
            '''
            collision_list = PrcList('collisions')
            swing_bone_params_struct = PrcStruct([
                new_prc_float('airresistance', swing_bone.air_resistance),
                new_prc_float('waterresistance', swing_bone.water_resistance),
                new_prc_float('minanglez', degrees(swing_bone.angle_z[0])),
                new_prc_float('maxanglez', degrees(swing_bone.angle_z[1])),
                new_prc_float('minangley', degrees(swing_bone.angle_y[0])),
                new_prc_float('maxangley', degrees(swing_bone.angle_y[1])),
                new_prc_float('collisionsizetip', swing_bone.collision_size[1]),
                new_prc_float('collisionsizeroot', swing_bone.collision_size[0]),
                new_prc_float('frictionrate', swing_bone.friction_rate),
                new_prc_float('goalstrength', swing_bone.goal_strength),
                new_prc_float('inertialmass', swing_bone.inertial_mass),
                new_prc_float('localgravity', swing_bone.local_gravity),
                new_prc_float('fallspeedscale', swing_bone.fall_speed_scale),
                new_prc_byte('groundhit', swing_bone.ground_hit),
                new_prc_float('windaffect', swing_bone.wind_affect),
                collision_list,
            ])
            swing_bone_collision: SUB_PG_swing_bone_collision
            for swing_bone_collision in swing_bone.collisions:
                if swing_bone_collision.collision_type == 'SPHERE':
                    c = ssd.spheres[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'OVAL':
                    c = ssd.ovals[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'ELLIPSOID':
                    c = ssd.ellipsoids[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'CAPSULE':
                    c = ssd.capsules[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'PLANE':
                    c = ssd.planes[swing_bone_collision.collision_index]
                collision_list += PrcHash40(c.name)
            if len(swing_bone.collisions) == 0:
                collision_list += PrcHash40("")
            swing_bone_param_list += swing_bone_params_struct
        swing_bone_chains_list += swing_bone_chain_struct
    prc_root += swing_bone_chains_list

    # add spheres
    spheres_list = PrcList('spheres')
    for sphere in ssd.spheres:
        spheres_list += PrcStruct([
            new_prc_hash('name', sphere.name),
            new_prc_hash('bonename', sphere.bone.lower()),
            new_prc_float('cx', sphere.offset.y),
            new_prc_float('cy', -sphere.offset.x),
            new_prc_float('cz', sphere.offset.z),
            new_prc_float('radius', sphere.radius),
        ])
    prc_root += spheres_list
    
    # add ovals
    ovals_list = PrcList('ovals')
    for oval in ssd.ovals:
        ovals_list += PrcStruct([
            new_prc_hash('name', oval.name),
            new_prc_hash('start_bonename', oval.start_bone_name.lower()),
            new_prc_hash('end_bonename', oval.end_bone_name.lower()),
            new_prc_float('radius', oval.radius),
            new_prc_float('start_offset_x',  oval.start_offset.y),
            new_prc_float('start_offset_y', -oval.start_offset.x),
            new_prc_float('start_offset_z',  oval.start_offset.z),
            new_prc_float('end_offset_x',  oval.end_offset.y),
            new_prc_float('end_offset_y', -oval.end_offset.x),
            new_prc_float('end_offset_z',  oval.end_offset.z),
        ])
    prc_root += ovals_list

    # add ellipsoids
    ellipsoids_list = PrcList('ellipsoids')
    for ellipsoid in ssd.ellipsoids:
        ellipsoids_list += PrcStruct([
            new_prc_hash('name', ellipsoid.name),
            new_prc_hash('bonename', ellipsoid.bone_name.lower()),
            new_prc_float('cx',  ellipsoid.offset.y),
            new_prc_float('cy', -ellipsoid.offset.x),
            new_prc_float('cz',  ellipsoid.offset.z),
            new_prc_float('rx',  ellipsoid.rotation.y),
            new_prc_float('ry', -ellipsoid.rotation.x),
            new_prc_float('rz',  ellipsoid.rotation.z),
            new_prc_float('sx',  ellipsoid.scale.y),
            new_prc_float('sy',  ellipsoid.scale.x),
            new_prc_float('sz',  ellipsoid.scale.z),
        ])
    prc_root += ellipsoids_list

    # add capsules
    capsules_list = PrcList('capsules')
    for capsule in ssd.capsules:
        capsules_list += PrcStruct([
            new_prc_hash('name', capsule.name),
            new_prc_hash('start_bonename', capsule.start_bone_name.lower()),
            new_prc_hash('end_bonename', capsule.end_bone_name.lower()),
            new_prc_float('start_offset_x',  capsule.start_offset.y),
            new_prc_float('start_offset_y', -capsule.start_offset.x),
            new_prc_float('start_offset_z',  capsule.start_offset.z),
            new_prc_float('end_offset_x',  capsule.end_offset.y),
            new_prc_float('end_offset_y', -capsule.end_offset.x),
            new_prc_float('end_offset_z',  capsule.end_offset.z),
            new_prc_float('start_radius',  capsule.start_radius),
            new_prc_float('end_radius',  capsule.end_radius),
        ])
    prc_root += capsules_list

    # add planes
    planes_list = PrcList('planes')
    for plane in ssd.planes:
        planes_list += PrcStruct([
            new_prc_hash('name', plane.name),
            new_prc_hash('bonename', plane.bone_name.lower()),
            new_prc_float('nx',  plane.ny),
            new_prc_float('ny',  -plane.nx),
            new_prc_float('nz',  plane.nz),
            new_prc_float('distance', plane.distance),
        ])
    prc_root += planes_list

    # add connections
    connections_list = PrcList('connections')
    for connection in ssd.connections:
        connections_list += PrcStruct([
            new_prc_hash('start_bonename', connection.start_bone_name.lower()),
            new_prc_hash('end_bonename', connection.end_bone_name.lower()),
            new_prc_float('radius', connection.radius),
            new_prc_float('length', connection.length),
        ])
    prc_root += connections_list

    # add swing bone collisions lists
    for swing_bone_chain in ssd.swing_bone_chains:
        for swing_bone in swing_bone_chain.swing_bones:
            swing_bone_collision_list = PrcList(swing_bone.name.lower() + 'col')
            #for collision in swing_bone.collisions:
                #swing_bone_collision_list += PrcHash40(collision.target_collision_name)
            swing_bone_collision: SUB_PG_swing_bone_collision
            for swing_bone_collision in swing_bone.collisions:
                if swing_bone_collision.collision_type == 'SPHERE':
                    c = ssd.spheres[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'OVAL':
                    c = ssd.ovals[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'ELLIPSOID':
                    c = ssd.ellipsoids[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'CAPSULE':
                    c = ssd.capsules[swing_bone_collision.collision_index]
                elif swing_bone_collision.collision_type == 'PLANE':
                    c = ssd.planes[swing_bone_collision.collision_index]
                swing_bone_collision_list += PrcHash40(c.name)
            prc_root += swing_bone_collision_list

    # Save all hashes used in the export to ParamLabels.csv
    operator.report({'INFO'}, "Saving hashes to ParamLabels.csv...")
    
    # Save swing bone chain hashes
    for swing_bone_chain in ssd.swing_bone_chains:
        # Chain name
        add_hash_to_param_labels(swing_bone_chain.name, swing_bone_chain.name)
        # Start and end bone names (lowercase as used in PRC)
        add_hash_to_param_labels(swing_bone_chain.start_bone_name.lower(), swing_bone_chain.start_bone_name.lower())
        add_hash_to_param_labels(swing_bone_chain.end_bone_name.lower(), swing_bone_chain.end_bone_name.lower())
        
        # Individual bone names and collision list hashes
        for swing_bone in swing_bone_chain.swing_bones:
            bone_lower = swing_bone.name.lower()
            add_hash_to_param_labels(bone_lower, bone_lower)
            # Collision list hash (bone_name + "col")
            collision_hash = bone_lower + "col"
            add_hash_to_param_labels(collision_hash, collision_hash)
    
    # Save collision hashes
    for sphere in ssd.spheres:
        add_hash_to_param_labels(sphere.name, sphere.name)
        add_hash_to_param_labels(sphere.bone.lower(), sphere.bone.lower())
    
    for oval in ssd.ovals:
        add_hash_to_param_labels(oval.name, oval.name)
        add_hash_to_param_labels(oval.start_bone_name.lower(), oval.start_bone_name.lower())
        add_hash_to_param_labels(oval.end_bone_name.lower(), oval.end_bone_name.lower())
    
    for ellipsoid in ssd.ellipsoids:
        add_hash_to_param_labels(ellipsoid.name, ellipsoid.name)
        add_hash_to_param_labels(ellipsoid.bone_name.lower(), ellipsoid.bone_name.lower())
    
    for capsule in ssd.capsules:
        add_hash_to_param_labels(capsule.name, capsule.name)
        add_hash_to_param_labels(capsule.start_bone_name.lower(), capsule.start_bone_name.lower())
        add_hash_to_param_labels(capsule.end_bone_name.lower(), capsule.end_bone_name.lower())
    
    for plane in ssd.planes:
        add_hash_to_param_labels(plane.name, plane.name)
        add_hash_to_param_labels(plane.bone_name.lower(), plane.bone_name.lower())
    
    for connection in ssd.connections:
        add_hash_to_param_labels(connection.start_bone_name.lower(), connection.start_bone_name.lower())
        add_hash_to_param_labels(connection.end_bone_name.lower(), connection.end_bone_name.lower())
    
    # Save standard PRC structure hashes
    standard_hashes = [
        'swingbones', 'spheres', 'ovals', 'ellipsoids', 'capsules', 'planes', 'connections',
        'name', 'start_bonename', 'end_bonename', 'params', 'bonename', 'collisions',
        'isskirt', 'rotateorder', 'curverotatex', 'airresistance', 'waterresistance',
        'minanglez', 'maxanglez', 'minangley', 'maxangley', 'collisionsizetip', 'collisionsizeroot',
        'frictionrate', 'goalstrength', 'inertialmass', 'localgravity', 'fallspeedscale',
        'groundhit', 'windaffect', 'cx', 'cy', 'cz', 'radius', 'start_offset_x', 'start_offset_y',
        'start_offset_z', 'end_offset_x', 'end_offset_y', 'end_offset_z', 'rx', 'ry', 'rz',
        'sx', 'sy', 'sz', 'start_radius', 'end_radius', 'nx', 'ny', 'nz', 'distance', 'length'
    ]
    
    for hash_label in standard_hashes:
        add_hash_to_param_labels(hash_label, hash_label)

    prc_root.save(filepath)

class SUB_OT_swing_preset_add(bpy.types.Operator):
    """Save the physical property values of the current swing bone as a preset"""
    bl_idname = "sub.swing_preset_add"
    bl_label = "Add Swing Values Preset"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: bpy.props.StringProperty(
        name="Name",
        description="Name of the preset to add",
        default="New Preset"
    )
    
    @classmethod
    def poll(cls, context):
        if not context.object or context.object.type != 'ARMATURE':
            return False
        sub_swing_data = context.object.data.sub_swing_data
        if len(sub_swing_data.swing_bone_chains) == 0:
            return False
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        return len(active_chain.swing_bones) > 0
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        from . import preset_handler
        
        sub_swing_data = context.object.data.sub_swing_data
        
        # Create presets directory if it doesn't exist
        presets_dir = preset_handler.get_swing_presets_dir()
        os.makedirs(presets_dir, exist_ok=True)
        
        # Sanitize filename
        filename = self.preset_name.replace(" ", "_").lower()
        if not filename.endswith('.py'):
            filename += '.py'
            
        # Full path to the preset file
        filepath = os.path.join(presets_dir, filename)
        
        # Get the active swing bone
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        active_bone = active_chain.swing_bones[active_chain.active_swing_bone_index]
        
        # Generate the preset Python code - ONLY focus on the physical properties
        with open(filepath, 'w') as f:
            f.write('import bpy\n\n')
            f.write('# This preset contains physical property values for swing bones\n')
            f.write('# Apply this to any active swing bone to use these values\n\n')
            
            f.write('def apply_preset(swing_bone):\n')
            f.write(f'    swing_bone.air_resistance = {active_bone.air_resistance}\n')
            f.write(f'    swing_bone.water_resistance = {active_bone.water_resistance}\n')
            f.write(f'    swing_bone.angle_z = ({active_bone.angle_z[0]}, {active_bone.angle_z[1]})\n')
            f.write(f'    swing_bone.angle_y = ({active_bone.angle_y[0]}, {active_bone.angle_y[1]})\n')
            f.write(f'    swing_bone.collision_size = ({active_bone.collision_size[0]}, {active_bone.collision_size[1]})\n')
            f.write(f'    swing_bone.friction_rate = {active_bone.friction_rate}\n')
            f.write(f'    swing_bone.goal_strength = {active_bone.goal_strength}\n')
            f.write(f'    swing_bone.inertial_mass = {active_bone.inertial_mass}\n')
            f.write(f'    swing_bone.local_gravity = {active_bone.local_gravity}\n')
            f.write(f'    swing_bone.fall_speed_scale = {active_bone.fall_speed_scale}\n')
            f.write(f'    swing_bone.ground_hit = {active_bone.ground_hit}\n')
            f.write(f'    swing_bone.wind_affect = {active_bone.wind_affect}\n')
            
            # Add code that runs when the preset is loaded
            f.write('\n# Get the active swing bone when preset is loaded\n')
            f.write('swing_data = bpy.context.object.data.sub_swing_data\n')
            f.write('active_chain = swing_data.swing_bone_chains[swing_data.active_swing_bone_chain_index]\n')
            f.write('active_bone = active_chain.swing_bones[active_chain.active_swing_bone_index]\n')
            f.write('apply_preset(active_bone)\n')
        
        # Update the UI to show the new preset immediately
        from . import preset_handler
        preset_handler.update_enum_items(context)
        context.scene.sub_swing_preset = os.path.splitext(filename)[0]
        
        self.report({'INFO'}, f"Saved preset: {filename}")
        return {'FINISHED'}

class SUB_OT_swing_preset_remove(bpy.types.Operator):
    """Delete the selected preset"""
    bl_idname = "sub.swing_preset_remove"
    bl_label = "Remove Swing Values Preset"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.scene.sub_swing_preset and context.scene.sub_swing_preset != '--'
    
    def execute(self, context):
        from . import preset_handler
        preset = context.scene.sub_swing_preset
        
        if preset == '--':
            self.report({'ERROR'}, "No preset selected")
            return {'CANCELLED'}
        
        # Add .py extension if needed
        if not preset.endswith('.py'):
            preset += '.py'
        
        # Get the full path to the preset file
        presets_dir = preset_handler.get_swing_presets_dir()
        filepath = os.path.join(presets_dir, preset)
        
        # Delete the file
        if os.path.exists(filepath):
            os.remove(filepath)
            self.report({'INFO'}, f"Deleted preset: {preset}")
            
            # Reset the current selection
            context.scene.sub_swing_preset = '--'
            preset_handler.update_enum_items(context)
            
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Preset file not found: {filepath}")
            return {'CANCELLED'}

class SUB_OT_swing_preset_apply(bpy.types.Operator):
    """Apply physical property values from the selected preset to the current swing bone"""
    bl_idname = "sub.swing_preset_apply"
    bl_label = "Apply Swing Values Preset"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        if not context.object or context.object.type != 'ARMATURE':
            return False
        sub_swing_data = context.object.data.sub_swing_data
        if len(sub_swing_data.swing_bone_chains) == 0:
            return False
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        return (len(active_chain.swing_bones) > 0 and 
                context.scene.sub_swing_preset and 
                context.scene.sub_swing_preset != '--')
    
    def execute(self, context):
        from . import preset_handler
        
        preset = context.scene.sub_swing_preset
        if preset == '--':
            self.report({'ERROR'}, "No preset selected")
            return {'CANCELLED'}
        
        if preset_handler.set_preset(preset, context):
            self.report({'INFO'}, f"Applied preset: {preset}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Failed to apply preset: {preset}")
            return {'CANCELLED'}

def get_preset_names():
    """Helper function to get preset names for enum property"""
    from . import preset_handler
    
    # Get the presets directory
    presets_dir = preset_handler.get_swing_presets_dir()
    
    # Check if directory exists
    if not os.path.exists(presets_dir):
        return []
    
    # Return list of Python files without extension
    return [os.path.splitext(f)[0] for f in os.listdir(presets_dir) if f.endswith('.py')]


class SUB_OT_swing_chain_preset_add(bpy.types.Operator):
    """Save the physical property values of the current swing bone chain as a preset"""
    bl_idname = "sub.swing_chain_preset_add"
    bl_label = "Add Swing Chain Preset"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: bpy.props.StringProperty(
        name="Name",
        description="Name of the chain preset to add",
        default="New Chain Preset"
    )
    
    @classmethod
    def poll(cls, context):
        if not context.object or context.object.type != 'ARMATURE':
            return False
        sub_swing_data = context.object.data.sub_swing_data
        if len(sub_swing_data.swing_bone_chains) == 0:
            return False
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        return len(active_chain.swing_bones) > 0
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        from . import preset_handler
        
        sub_swing_data = context.object.data.sub_swing_data
        
        # Create chain presets directory if it doesn't exist
        presets_dir = preset_handler.get_swing_chain_presets_dir()
        os.makedirs(presets_dir, exist_ok=True)
        
        # Sanitize filename
        filename = self.preset_name.replace(" ", "_").lower()
        if not filename.endswith('.py'):
            filename += '.py'
            
        # Full path to the preset file
        filepath = os.path.join(presets_dir, filename)
        
        # Get the active swing bone chain
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        
        # Generate the preset Python code for the entire chain
        with open(filepath, 'w') as f:
            f.write('import bpy\n\n')
            f.write('# This preset contains chain settings and all bone values for a swing bone chain\n')
            f.write('# Apply this to any active swing bone chain to use these values\n\n')
            
            # Store chain info
            f.write('# Chain configuration\n')
            f.write('CHAIN_CONFIG = {\n')
            f.write(f'    "is_skirt": {active_chain.is_skirt},\n')
            f.write(f'    "rotate_order": {active_chain.rotate_order},\n')
            f.write(f'    "curve_rotate_x": {active_chain.curve_rotate_x},\n')
            f.write(f'    "has_unk_8": {active_chain.has_unk_8},\n')
            f.write(f'    "unk_8": {active_chain.unk_8}\n')
            f.write('}\n\n')
            
            # Store bone values for each bone in the chain
            f.write('# Bone values for each bone in the chain\n')
            f.write('BONE_VALUES = [\n')
            for i, bone in enumerate(active_chain.swing_bones):
                f.write('    {\n')
                f.write(f'        "air_resistance": {bone.air_resistance},\n')
                f.write(f'        "water_resistance": {bone.water_resistance},\n')
                f.write(f'        "angle_z": ({bone.angle_z[0]}, {bone.angle_z[1]}),\n')
                f.write(f'        "angle_y": ({bone.angle_y[0]}, {bone.angle_y[1]}),\n')
                f.write(f'        "collision_size": ({bone.collision_size[0]}, {bone.collision_size[1]}),\n')
                f.write(f'        "friction_rate": {bone.friction_rate},\n')
                f.write(f'        "goal_strength": {bone.goal_strength},\n')
                f.write(f'        "inertial_mass": {bone.inertial_mass},\n')
                f.write(f'        "local_gravity": {bone.local_gravity},\n')
                f.write(f'        "fall_speed_scale": {bone.fall_speed_scale},\n')
                f.write(f'        "ground_hit": {bone.ground_hit},\n')
                f.write(f'        "wind_affect": {bone.wind_affect}\n')
                f.write('    }')
                if i < len(active_chain.swing_bones) - 1:
                    f.write(',')
                f.write('\n')
            f.write(']\n\n')
            
            # Add function to apply chain configuration
            f.write('def apply_chain_config(chain):\n')
            f.write('    """Apply chain configuration to a swing bone chain"""\n')
            f.write('    chain.is_skirt = CHAIN_CONFIG["is_skirt"]\n')
            f.write('    chain.rotate_order = CHAIN_CONFIG["rotate_order"]\n')
            f.write('    chain.curve_rotate_x = CHAIN_CONFIG["curve_rotate_x"]\n')
            f.write('    chain.has_unk_8 = CHAIN_CONFIG["has_unk_8"]\n')
            f.write('    chain.unk_8 = CHAIN_CONFIG["unk_8"]\n\n')
            
            # Add function to apply bone values
            f.write('def apply_bone_values(bone, values):\n')
            f.write('    """Apply bone values to a swing bone"""\n')
            f.write('    bone.air_resistance = values["air_resistance"]\n')
            f.write('    bone.water_resistance = values["water_resistance"]\n')
            f.write('    bone.angle_z = values["angle_z"]\n')
            f.write('    bone.angle_y = values["angle_y"]\n')
            f.write('    bone.collision_size = values["collision_size"]\n')
            f.write('    bone.friction_rate = values["friction_rate"]\n')
            f.write('    bone.goal_strength = values["goal_strength"]\n')
            f.write('    bone.inertial_mass = values["inertial_mass"]\n')
            f.write('    bone.local_gravity = values["local_gravity"]\n')
            f.write('    bone.fall_speed_scale = values["fall_speed_scale"]\n')
            f.write('    bone.ground_hit = values["ground_hit"]\n')
            f.write('    bone.wind_affect = values["wind_affect"]\n\n')
            
            # Add function to apply preset with bone count checking
            f.write('def apply_chain_preset():\n')
            f.write('    """Apply the chain preset to the active swing bone chain"""\n')
            f.write('    swing_data = bpy.context.object.data.sub_swing_data\n')
            f.write('    active_chain = swing_data.swing_bone_chains[swing_data.active_swing_bone_chain_index]\n')
            f.write('    \n')
            f.write('    # Apply chain configuration\n')
            f.write('    apply_chain_config(active_chain)\n')
            f.write('    \n')
            f.write('    # Check bone count and handle mismatches\n')
            f.write('    preset_bone_count = len(BONE_VALUES)\n')
            f.write('    target_bone_count = len(active_chain.swing_bones)\n')
            f.write('    \n')
            f.write('    if preset_bone_count == target_bone_count:\n')
            f.write('        # Perfect match - apply directly\n')
            f.write('        for i, bone_values in enumerate(BONE_VALUES):\n')
            f.write('            apply_bone_values(active_chain.swing_bones[i], bone_values)\n')
            f.write('    else:\n')
            f.write('        # Bone count mismatch - trigger dialog\n')
            f.write('        bpy.ops.sub.swing_chain_preset_apply_mismatch(\n')
            f.write('            "INVOKE_DEFAULT",\n')
            f.write(f'            preset_name="{filename[:-3]}"\n')  # Remove .py extension
            f.write('        )\n\n')
            
            # Add code that runs when the preset is loaded
            f.write('# Apply the chain preset when this file is executed\n')
            f.write('apply_chain_preset()\n')
        
        # Update the UI to show the new preset immediately
        from . import preset_handler
        preset_handler.update_chain_enum_items(context)
        context.scene.sub_swing_chain_preset = os.path.splitext(filename)[0]
        
        self.report({'INFO'}, f"Saved chain preset: {filename}")
        return {'FINISHED'}


class SUB_OT_swing_chain_preset_remove(bpy.types.Operator):
    """Delete the selected chain preset"""
    bl_idname = "sub.swing_chain_preset_remove"
    bl_label = "Remove Swing Chain Preset"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.scene.sub_swing_chain_preset and context.scene.sub_swing_chain_preset != '--'
    
    def execute(self, context):
        from . import preset_handler
        preset = context.scene.sub_swing_chain_preset
        
        if preset == '--':
            self.report({'ERROR'}, "No chain preset selected")
            return {'CANCELLED'}
        
        # Add .py extension if needed
        if not preset.endswith('.py'):
            preset += '.py'
        
        # Get the full path to the preset file
        presets_dir = preset_handler.get_swing_chain_presets_dir()
        filepath = os.path.join(presets_dir, preset)
        
        # Delete the file
        if os.path.exists(filepath):
            os.remove(filepath)
            self.report({'INFO'}, f"Deleted chain preset: {preset}")
            
            # Reset the current selection
            context.scene.sub_swing_chain_preset = '--'
            preset_handler.update_chain_enum_items(context)
            
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Chain preset file not found: {filepath}")
            return {'CANCELLED'}


class SUB_OT_swing_chain_preset_apply(bpy.types.Operator):
    """Apply physical property values from the selected chain preset to the current swing bone chain"""
    bl_idname = "sub.swing_chain_preset_apply"
    bl_label = "Apply Swing Chain Preset"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        if not context.object or context.object.type != 'ARMATURE':
            return False
        sub_swing_data = context.object.data.sub_swing_data
        if len(sub_swing_data.swing_bone_chains) == 0:
            return False
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        return (len(active_chain.swing_bones) > 0 and 
                context.scene.sub_swing_chain_preset and 
                context.scene.sub_swing_chain_preset != '--')
    
    def execute(self, context):
        from . import preset_handler
        
        preset = context.scene.sub_swing_chain_preset
        if preset == '--':
            self.report({'ERROR'}, "No chain preset selected")
            return {'CANCELLED'}
        
        success = preset_handler.set_chain_preset(preset, context)
        if success:
            self.report({'INFO'}, f"Applied chain preset: {preset}")
            return {'FINISHED'}
        else:
            # Bone count mismatch - trigger mismatch dialog
            bpy.ops.sub.swing_chain_preset_apply_mismatch(
                "INVOKE_DEFAULT",
                preset_name=preset
            )
            return {'FINISHED'}


class SUB_OT_swing_chain_preset_apply_mismatch(bpy.types.Operator):
    """Handle bone count mismatch when applying chain presets with flexible bone mapping"""
    bl_idname = "sub.swing_chain_preset_apply_mismatch"
    bl_label = "Apply Chain Preset - Bone Mapping"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: bpy.props.StringProperty(name="Preset Name")
    
    # Store bone mapping data
    preset_bone_count: bpy.props.IntProperty(default=0)
    target_bone_count: bpy.props.IntProperty(default=0)
    
    # Collection of bone mappings
    bone_mappings: bpy.props.CollectionProperty(type=SUB_PG_bone_mapping)
    
    def setup_bone_mappings(self, context):
        """Setup bone mappings based on target bones"""
        from . import preset_handler
        
        preset_data = preset_handler.get_chain_preset_data(self.preset_name)
        if not preset_data or 'bone_values' not in preset_data:
            return
        
        # Get current chain
        sub_swing_data = context.object.data.sub_swing_data
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        
        # Clear existing mappings
        self.bone_mappings.clear()
        
        # Create mapping for each target bone
        for i, target_bone in enumerate(active_chain.swing_bones):
            mapping = self.bone_mappings.add()
            mapping.target_bone_name = target_bone.name
            
            # Set default mapping if possible (map to corresponding preset bone)
            if i < self.preset_bone_count:
                mapping.preset_bone_index = i
                mapping.preset_bone_selection = str(i)
            else:
                mapping.preset_bone_index = -1
                mapping.preset_bone_selection = 'NONE'
    
    def invoke(self, context, event):
        global _active_mismatch_operator
        _active_mismatch_operator = self
        
        from . import preset_handler
        
        # Load preset to check bone counts
        preset_path = os.path.join(preset_handler.get_swing_chain_presets_dir(), self.preset_name + '.py')
        if not os.path.exists(preset_path):
            self.report({'ERROR'}, f"Preset file not found: {preset_path}")
            return {'CANCELLED'}
        
        # Load preset data safely
        preset_data = preset_handler.get_chain_preset_data(self.preset_name)
        if not preset_data or 'bone_values' not in preset_data:
            self.report({'ERROR'}, f"Invalid preset or preset not found: {self.preset_name}")
            return {'CANCELLED'}
        
        # Get current chain
        sub_swing_data = context.object.data.sub_swing_data
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        
        self.preset_bone_count = len(preset_data['bone_values'])
        self.target_bone_count = len(active_chain.swing_bones)
        
        # Setup bone mappings
        self.setup_bone_mappings(context)
        
        return context.window_manager.invoke_props_dialog(self, width=400)
    
    def draw(self, context):
        layout = self.layout
        
        # Header information - more compact
        row = layout.row()
        row.label(text=f"Preset: {self.preset_name}", icon='PRESET')
        
        # Bone count information - single line
        row = layout.row()
        row.label(text=f"Preset: {self.preset_bone_count} bones", icon='BONE_DATA')
        row.label(text=f"Target: {self.target_bone_count} bones", icon='ARMATURE_DATA')
        
        layout.separator()
        
        # Create the mapping list interface - more compact
        layout.label(text="Bone Mapping:")
        
        # Create a scrollable area for many bones
        col = layout.column()
        
        # Each target bone gets its own compact row
        for i, mapping in enumerate(self.bone_mappings):
            row = col.row()
            
            # Use split to control spacing better
            split = row.split(factor=0.6)
            
            # Left side: Target bone info - compact
            left = split.row()
            left.label(text=f"T{i+1}:", icon='ARMATURE_DATA')
            left.label(text=mapping.target_bone_name[:15] + "..." if len(mapping.target_bone_name) > 15 else mapping.target_bone_name)
            
            # Right side: Dropdown - takes remaining space
            right = split.row()
            right.prop(mapping, "preset_bone_selection", text="")
        
        layout.separator()
        
        # Quick action buttons - more compact
        row = layout.row()
        row.operator("sub.swing_chain_preset_mapping_auto", text="Auto", icon='AUTO').preset_name = self.preset_name
        row.operator("sub.swing_chain_preset_mapping_clear", text="Clear", icon='X').preset_name = self.preset_name
    
    def execute(self, context):
        global _active_mismatch_operator
        
        from . import preset_handler
        
        # Load preset data safely
        preset_data = preset_handler.get_chain_preset_data(self.preset_name)
        if not preset_data:
            self.report({'ERROR'}, f"Failed to load preset: {self.preset_name}")
            _active_mismatch_operator = None
            return {'CANCELLED'}
        
        # Get current chain
        sub_swing_data = context.object.data.sub_swing_data
        active_chain = sub_swing_data.swing_bone_chains[sub_swing_data.active_swing_bone_chain_index]
        
        # Apply chain configuration
        if 'chain_config' in preset_data:
            chain_config = preset_data['chain_config']
            active_chain.is_skirt = chain_config["is_skirt"]
            active_chain.rotate_order = chain_config["rotate_order"]
            active_chain.curve_rotate_x = chain_config["curve_rotate_x"]
            active_chain.has_unk_8 = chain_config["has_unk_8"]
            active_chain.unk_8 = chain_config["unk_8"]
        
        # Apply bone values with custom mapping
        bone_values = preset_data['bone_values']
        target_bones = active_chain.swing_bones
        
        applied_count = 0
        for i, mapping in enumerate(self.bone_mappings):
            if i < len(target_bones) and mapping.preset_bone_index >= 0:
                preset_index = mapping.preset_bone_index
                if preset_index < len(bone_values):
                    preset_handler.apply_bone_values_to_bone(target_bones[i], bone_values[preset_index])
                    applied_count += 1
        
        # Clear the global reference
        _active_mismatch_operator = None
        
        self.report({'INFO'}, f"Applied chain preset: {self.preset_name} with {applied_count} bone mappings")
        return {'FINISHED'}
    
    def cancel(self, context):
        global _active_mismatch_operator
        _active_mismatch_operator = None
        return {'CANCELLED'}


class SUB_OT_swing_chain_preset_mapping_auto(bpy.types.Operator):
    """Automatically map preset bones to target bones in order"""
    bl_idname = "sub.swing_chain_preset_mapping_auto"
    bl_label = "Auto Map Bones"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: bpy.props.StringProperty(name="Preset Name")
    
    def execute(self, context):
        global _active_mismatch_operator
        
        # Access the active mismatch operator and auto-map bones
        if _active_mismatch_operator:
            min_count = min(_active_mismatch_operator.preset_bone_count, _active_mismatch_operator.target_bone_count)
            
            for i in range(len(_active_mismatch_operator.bone_mappings)):
                if i < min_count:
                    _active_mismatch_operator.bone_mappings[i].preset_bone_index = i
                    _active_mismatch_operator.bone_mappings[i].preset_bone_selection = str(i)
                else:
                    _active_mismatch_operator.bone_mappings[i].preset_bone_index = -1
                    _active_mismatch_operator.bone_mappings[i].preset_bone_selection = 'NONE'
            
            # Force a redraw of the dialog
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        
        self.report({'INFO'}, f"Auto-mapped {min_count if _active_mismatch_operator else 0} bones")
        return {'FINISHED'}


class SUB_OT_swing_chain_preset_mapping_clear(bpy.types.Operator):
    """Clear all bone mappings"""
    bl_idname = "sub.swing_chain_preset_mapping_clear"
    bl_label = "Clear All Mappings"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: bpy.props.StringProperty(name="Preset Name")
    
    def execute(self, context):
        global _active_mismatch_operator
        
        # Access the active mismatch operator and clear all mappings
        if _active_mismatch_operator:
            for mapping in _active_mismatch_operator.bone_mappings:
                mapping.preset_bone_index = -1
                mapping.preset_bone_selection = 'NONE'
            
            # Force a redraw of the dialog
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        
        self.report({'INFO'}, "Cleared all bone mappings")
        return {'FINISHED'}


