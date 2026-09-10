import bpy
from bpy.types import Operator
from bpy.props import BoolProperty

from ..anim.fcurve_compat import action_frame_range_safe, action_matches_armature, get_fcurves
from ..blender_compat import assign_action

# Global variable to track the last checked object
_last_checked_object = None

# Global variable to track the last action to detect changes
_last_action = None

_sequence_operator = None
_addon_keymaps = []


def get_armature_actions(armature):
    """Return this armature's playable actions in stable name order."""
    if armature is None or armature.type != 'ARMATURE':
        return []
    actions = [
        action for action in bpy.data.actions
        if "SAP" not in action.name
        and "_old" not in action.name
        and action_matches_armature(action, armature)
    ]
    return sorted(actions, key=lambda action: action.name.casefold())


def apply_armature_action(context, armature, action, *, jump_to_start=True, autoplay=True):
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.use_nla = False
    assign_action(armature.animation_data, action)

    global _last_action
    _last_action = action
    reset_unanimated_bones_to_rest(armature, action)
    start, end = action_frame_range_safe(action)
    context.scene.frame_start = start
    context.scene.frame_end = end
    if jump_to_start:
        context.scene.frame_set(start)
    screen = context.screen
    if autoplay and screen is not None and not screen.is_animation_playing:
        bpy.ops.screen.animation_play()
    return start, end


def cycle_armature_action(context, direction):
    armature = context.object
    if armature is None or armature.type != 'ARMATURE':
        return None
    actions = get_armature_actions(armature)
    if not actions:
        return None
    current = armature.animation_data.action if armature.animation_data else None
    if current in actions:
        index = actions.index(current)
        next_index = (index + direction) % len(actions)
    else:
        next_index = 0 if direction > 0 else len(actions) - 1
    action = actions[next_index]
    apply_armature_action(context, armature, action)
    return action

def reset_unanimated_bones_to_rest(armature, action):
    """
    Reset bones that are not animated in the given action to their rest positions.
    
    Args:
        armature: The armature object
        action: The action to check for animated bones
    """
    try:
        # Store original mode to restore later
        original_mode = bpy.context.mode
        
        # Switch to pose mode if not already
        if original_mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        
        if not action or not get_fcurves(action):
            # If no action or no fcurves, reset all bones to rest (except _RET bones)
            for bone in armature.pose.bones:
                # Skip bones with "_RET" suffix - don't reset them to rest pose
                if bone.name.endswith("_RET"):
                    continue
                    
                bone.location = (0, 0, 0)
                bone.rotation_euler = (0, 0, 0)
                bone.rotation_quaternion = (1, 0, 0, 0)
                bone.rotation_axis_angle = (0, 0, 1, 0)
                bone.scale = (1, 1, 1)
            
            # Update the view layer to apply the transform resets
            bpy.context.view_layer.update()
            return
        
        # Get all bone names that are animated in this action
        animated_bones = set()
        for fcurve in get_fcurves(action):
            if fcurve.data_path.startswith('pose.bones["'):
                # Extract bone name from data path like 'pose.bones["BoneName"].location'
                try:
                    bone_name = fcurve.data_path.split('"')[1]
                    animated_bones.add(bone_name)
                except (IndexError, ValueError):
                    continue
        
        # Reset bones that are not animated to their rest positions
        for bone in armature.pose.bones:
            if bone.name not in animated_bones:
                # Skip bones with "_RET" suffix - don't reset them to rest pose
                if bone.name.endswith("_RET"):
                    continue
                    
                # Reset location to rest position
                bone.location = (0, 0, 0)
                
                # Reset rotation based on rotation mode
                if bone.rotation_mode == 'QUATERNION':
                    bone.rotation_quaternion = (1, 0, 0, 0)  # Identity quaternion
                elif bone.rotation_mode == 'AXIS_ANGLE':
                    bone.rotation_axis_angle = (0, 0, 1, 0)  # No rotation
                else:
                    bone.rotation_euler = (0, 0, 0)  # No rotation
                
                # Reset scale to rest position
                bone.scale = (1, 1, 1)
        
        # Update the view layer to apply the transform resets
        bpy.context.view_layer.update()
        
    except Exception as e:
        # Silently handle any errors to avoid breaking the animation scroll
        print(f"Error in reset_unanimated_bones_to_rest: {e}")

def handle_action_change(context):
    """
    Handle action changes from the UI dropdown menu.
    This function is called by the timer to check if the action has changed.
    """
    global _last_action
    
    try:
        # Check if we have an active object with animation data
        if (context.active_object and 
            context.active_object.type == 'ARMATURE' and 
            context.active_object.animation_data):
            
            current_action = context.active_object.animation_data.action
            
            # If the action has changed, reset unanimated bones
            if current_action != _last_action:
                _last_action = current_action
                
                if current_action:
                    # Reset unanimated bones to rest positions
                    reset_unanimated_bones_to_rest(context.active_object, current_action)
                else:
                    # If no action is selected, reset all bones to rest
                    reset_unanimated_bones_to_rest(context.active_object, None)
                    
    except Exception as e:
        # Silently handle any errors to avoid breaking the system
        print(f"Error in handle_action_change: {e}")

# Timer function to check if we should auto-start/stop animation scroll
def auto_start_animation_scroll_timer():
    """
    Timer function that ensures animation scroll is always active when appropriate conditions are met.
    Automatically starts when you select an armature with multiple animations and stops when you don't.
    Also handles action changes from the UI dropdown menu.
    """
    global _last_checked_object
    
    try:
        context = bpy.context
        current_object = context.active_object
        
        # Handle action changes from UI dropdown
        handle_action_change(context)
        
        # Only check if the active object has changed
        if current_object != _last_checked_object:
            _last_checked_object = current_object
            
            # Check if we have the right conditions
            if (current_object and 
                current_object.type == 'ARMATURE' and 
                current_object.animation_data):
                
                # Get available actions (filter out SAP and _old)
                actions = [action for action in bpy.data.actions 
                          if not ("SAP" in action.name or "_old" in action.name)]
                
                # Only auto-start if we have multiple animations and modal isn't already running
                if len(actions) >= 2 and not SUB_OP_animation_scroll_modal._running:
                    # Start the modal operator
                    bpy.ops.sub.animation_scroll_modal('INVOKE_DEFAULT')
                    
            # Stop the modal if conditions are no longer met
            elif SUB_OP_animation_scroll_modal._running:
                # Set flag for modal to stop itself
                SUB_OP_animation_scroll_modal._should_auto_stop = True
                
    except Exception as e:
        # Silently handle any errors to avoid breaking Blender
        pass
    
    # Return interval for next check (1 second)
    return 1.0

# Handler to automatically start animation scroll when conditions are met
@bpy.app.handlers.persistent
def auto_start_animation_scroll_handler(scene):
    """
    Handler that ensures animation scroll is always active when appropriate.
    """
    # We'll use the timer instead of this handler for better performance
    pass

class SUB_OP_animation_scroll_modal(Operator):
    """Modal operator that automatically enables animation scrolling when working with armatures"""
    bl_idname = "sub.animation_scroll_modal"
    bl_label = "Animation Scroll Modal"
    bl_description = "Automatically scroll through animations using the mouse wheel when hovering over animation areas"
    bl_options = {'REGISTER'}

    _running = False
    _handler = None
    _should_auto_stop = False

    @classmethod
    def poll(cls, context):
        # This is called by the auto-start timer to check if conditions are appropriate
        has_object = context.active_object is not None
        is_armature = has_object and context.active_object.type == 'ARMATURE'
        has_anim_data = is_armature and context.active_object.animation_data is not None
        
        return (context.active_object and 
                context.active_object.type == 'ARMATURE' and
                context.active_object.animation_data)

    def modal(self, context, event):
        # Check if we should auto-stop
        if SUB_OP_animation_scroll_modal._should_auto_stop:
            SUB_OP_animation_scroll_modal._should_auto_stop = False
            return self.cancel(context)
        
        # Handle wheel events
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            # Check if mouse is over animation areas (Action Editor, Dopesheet, etc.)
            if self.is_mouse_over_animation_area(context, event):
                if event.value == 'PRESS':
                    self.cycle_animation(context, event.type == 'WHEELUPMOUSE')
                    return {'RUNNING_MODAL'}
        
        return {'PASS_THROUGH'}

    def is_mouse_over_animation_area(self, context, event):
        """Check if mouse is over the action name area in the dopesheet editor"""
        mouse_x = event.mouse_x
        mouse_y = event.mouse_y
        
        # Get the window and its screen
        window = context.window
        screen = window.screen
        
        for area in screen.areas:
            # Check if mouse is within this area
            if (area.x <= mouse_x <= area.x + area.width and 
                area.y <= mouse_y <= area.y + area.height):
                
                # Only work in the Dopesheet Editor (where the action name text box is)
                if area.type == 'DOPESHEET_EDITOR':
                    for space in area.spaces:
                        if hasattr(space, 'mode'):
                            # Only in Action Editor mode (where action names are shown)
                            if space.mode in {'ACTION', 'SHAPEKEY'}:
                                return True
        
        return False

    def cycle_animation(self, context, scroll_up):
        """Cycle through available animations"""
        action = cycle_armature_action(context, -1 if scroll_up else 1)
        if action is None:
            self.report({'INFO'}, "No compatible armature actions found")
            return
        self.report(
            {'INFO'},
            f"Switched to animation: {action.name} "
            f"(frames {context.scene.frame_start}-{context.scene.frame_end})",
        )

    def execute(self, context):
        # Start the modal operator (only called by auto-start timer)
        context.window_manager.modal_handler_add(self)
        SUB_OP_animation_scroll_modal._running = True
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        SUB_OP_animation_scroll_modal._running = False
        return {'CANCELLED'}

class SUB_OT_next_armature_action(Operator):
    bl_idname = "sub.next_armature_action"
    bl_label = "Next Armature Action"
    bl_description = "Play the next armature action (wraps after the last action)"

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'ARMATURE'

    def execute(self, context):
        action = cycle_armature_action(context, 1)
        if action is None:
            return {'CANCELLED'}
        self.report({'INFO'}, f"Playing {action.name}")
        return {'FINISHED'}


class SUB_OT_previous_armature_action(Operator):
    bl_idname = "sub.previous_armature_action"
    bl_label = "Previous Armature Action"
    bl_description = "Play the previous armature action (wraps before the first action)"

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'ARMATURE'

    def execute(self, context):
        action = cycle_armature_action(context, -1)
        if action is None:
            return {'CANCELLED'}
        self.report({'INFO'}, f"Playing {action.name}")
        return {'FINISHED'}


def _sequence_frame_change(scene, _depsgraph=None):
    operator = _sequence_operator
    if operator is not None:
        operator.on_frame_change(scene)


class SUB_OT_animation_sequence(Operator):
    bl_idname = "sub.animation_sequence"
    bl_label = "Play Animation Sequence"
    bl_description = "Play every compatible action once, starting with the selected action"

    _running = False
    _cancel_requested = False

    @classmethod
    def poll(cls, context):
        return (
            not cls._running
            and context.object is not None
            and context.object.type == 'ARMATURE'
        )

    def execute(self, context):
        global _sequence_operator
        armature = context.object
        actions = get_armature_actions(armature)
        if not actions:
            self.report({'WARNING'}, "No compatible armature actions found")
            return {'CANCELLED'}

        current = armature.animation_data.action if armature.animation_data else None
        start_index = actions.index(current) if current in actions else 0
        self._actions = actions[start_index:] + actions[:start_index]
        self._index = 0
        self._armature = armature
        self._finished = False
        self._changing = False
        SUB_OT_animation_sequence._cancel_requested = False
        SUB_OT_animation_sequence._running = True
        _sequence_operator = self

        if _sequence_frame_change not in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.append(_sequence_frame_change)
        apply_armature_action(context, armature, self._actions[0], jump_to_start=True, autoplay=True)
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, f"Sequence started with {self._actions[0].name}")
        return {'RUNNING_MODAL'}

    def on_frame_change(self, scene):
        if self._changing or self._finished or scene.frame_current < scene.frame_end:
            return
        self._index += 1
        if self._index >= len(self._actions):
            self._finished = True
            return
        self._changing = True
        try:
            context = bpy.context
            apply_armature_action(
                context,
                self._armature,
                self._actions[self._index],
                jump_to_start=True,
                autoplay=False,
            )
        finally:
            self._changing = False

    def modal(self, context, event):
        if event.type == 'ESC' or SUB_OT_animation_sequence._cancel_requested:
            return self._stop(context, cancelled=True)
        if event.type == 'TIMER' and self._finished:
            return self._stop(context, cancelled=False)
        if (
            event.type == 'TIMER'
            and context.screen is not None
            and not context.screen.is_animation_playing
        ):
            return self._stop(context, cancelled=True)
        return {'PASS_THROUGH'}

    def _stop(self, context, *, cancelled):
        global _sequence_operator
        if context.screen is not None and context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        if getattr(self, '_timer', None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if _sequence_frame_change in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.remove(_sequence_frame_change)
        _sequence_operator = None
        SUB_OT_animation_sequence._running = False
        SUB_OT_animation_sequence._cancel_requested = False
        if not cancelled:
            self.report({'INFO'}, f"Finished {len(self._actions)} animation(s)")
            return {'FINISHED'}
        self.report({'INFO'}, "Animation sequence stopped")
        return {'CANCELLED'}

    def cancel(self, context):
        return self._stop(context, cancelled=True)


class SUB_OT_toggle_animation_sequence(Operator):
    bl_description = 'Start or stop playing loaded animations in sequence'
    bl_idname = "sub.toggle_animation_sequence"
    bl_label = "Toggle Animation Sequence"

    def execute(self, _context):
        if SUB_OT_animation_sequence._running:
            SUB_OT_animation_sequence._cancel_requested = True
        else:
            bpy.ops.sub.animation_sequence('INVOKE_DEFAULT')
        return {'FINISHED'}


# --- HEADER BUTTON DRAW FUNCTION ---
def draw_animation_scroll_button(self, context):
    # Only show in Action Editor mode
    area = context.area
    if area and hasattr(area.spaces.active, 'mode') and area.spaces.active.mode == 'ACTION':
        is_running = SUB_OP_animation_scroll_modal._running
        icon = 'PLAY' if not is_running else 'PAUSE'
        label = 'Start Animation Scroll' if not is_running else 'Stop Animation Scroll'
        row = self.layout.row(align=True)
        row.operator('sub.toggle_animation_scroll_modal', text=label, icon=icon, depress=is_running)
        sequence_running = SUB_OT_animation_sequence._running
        row.operator(
            'sub.toggle_animation_sequence',
            text='Stop Sequence' if sequence_running else 'Start Sequence',
            icon='PAUSE' if sequence_running else 'PLAY',
            depress=sequence_running,
        )
        self.layout.operator('sub.gif_or_photo', text='GIF or Photo', icon='RENDER_ANIMATION')
        try:
            from .face_picker import armature_has_face_picker_menu
            if armature_has_face_picker_menu(context):
                self.layout.operator(
                    'sub.face_picker_popup',
                    text='Easy Facial Animation',
                    icon='IMAGE_DATA',
                )
        except Exception:
            pass

# --- TOGGLE OPERATOR ---
class SUB_OP_toggle_animation_scroll_modal(Operator):
    bl_idname = 'sub.toggle_animation_scroll_modal'
    bl_label = 'Toggle Animation Scroll Modal'
    bl_description = 'Start or stop the animation scroll modal operator'

    def execute(self, context):
        if SUB_OP_animation_scroll_modal._running:
            # Set the flag so the modal will stop itself
            SUB_OP_animation_scroll_modal._should_auto_stop = True
            self.report({'INFO'}, 'Animation scroll modal stopping...')
        else:
            bpy.ops.sub.animation_scroll_modal('INVOKE_DEFAULT')
            self.report({'INFO'}, 'Animation scroll modal started.')
        return {'FINISHED'}

def register():
    bpy.utils.register_class(SUB_OP_animation_scroll_modal)
    bpy.utils.register_class(SUB_OP_toggle_animation_scroll_modal)
    bpy.utils.register_class(SUB_OT_next_armature_action)
    bpy.utils.register_class(SUB_OT_previous_armature_action)
    bpy.utils.register_class(SUB_OT_animation_sequence)
    bpy.utils.register_class(SUB_OT_toggle_animation_sequence)
    # Add button to Action Editor/Dope Sheet header
    bpy.types.DOPESHEET_HT_header.append(draw_animation_scroll_button)

    key_config = bpy.context.window_manager.keyconfigs.addon
    if key_config is not None:
        keymap = key_config.keymaps.new(name='3D View', space_type='VIEW_3D')
        item = keymap.keymap_items.new(
            SUB_OT_next_armature_action.bl_idname,
            'DOWN_ARROW',
            'PRESS',
            ctrl=True,
            repeat=True,
        )
        _addon_keymaps.append((keymap, item))
        item = keymap.keymap_items.new(
            SUB_OT_previous_armature_action.bl_idname,
            'UP_ARROW',
            'PRESS',
            ctrl=True,
            repeat=True,
        )
        _addon_keymaps.append((keymap, item))

def unregister():
    global _sequence_operator
    SUB_OT_animation_sequence._cancel_requested = True
    if _sequence_operator is not None and getattr(_sequence_operator, '_timer', None) is not None:
        try:
            bpy.context.window_manager.event_timer_remove(_sequence_operator._timer)
        except Exception:
            pass
        _sequence_operator._timer = None
    if _sequence_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_sequence_frame_change)
    _sequence_operator = None
    SUB_OT_animation_sequence._running = False
    for keymap, item in _addon_keymaps:
        keymap.keymap_items.remove(item)
    _addon_keymaps.clear()
    # Remove button from header
    bpy.types.DOPESHEET_HT_header.remove(draw_animation_scroll_button)
    bpy.utils.unregister_class(SUB_OT_toggle_animation_sequence)
    bpy.utils.unregister_class(SUB_OT_animation_sequence)
    bpy.utils.unregister_class(SUB_OT_previous_armature_action)
    bpy.utils.unregister_class(SUB_OT_next_armature_action)
    bpy.utils.unregister_class(SUB_OP_toggle_animation_scroll_modal)
    bpy.utils.unregister_class(SUB_OP_animation_scroll_modal)
    # Reset class variables
    SUB_OP_animation_scroll_modal._running = False
    SUB_OP_animation_scroll_modal._should_auto_stop = False
