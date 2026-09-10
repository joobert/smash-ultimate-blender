from bpy.types import Panel

class SUB_PT_update_plugin(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Update Available!'

    @classmethod
    def poll(cls, context):
        # LOCAL_VERSION_AHEAD is a cached flag from the last check, so this stays
        # cheap enough for a poll and keeps the panel hidden on newer local builds.
        from .version_check import UPDATE_AVAILABLE, LOCAL_VERSION_AHEAD
        return bool(UPDATE_AVAILABLE) and not LOCAL_VERSION_AHEAD
    
    def draw(self, context):
        self.layout.use_property_decorate = False
        from ...__init__ import bl_info
        from .version_check import LATEST_COMMIT_SHA, LATEST_COMMIT_MESSAGE, LATEST_COMMIT_DATE, CURRENT_COMMIT_SHA, CURRENT_COMMIT_MESSAGE, UPDATE_STATUS, UPDATE_DOWNLOAD_PROGRESS

        layout = self.layout
        layout.use_property_split = False
        
        # Commit information
        layout.row().label(text="A new update is available on animation-workflow branch!")
        
        # Current and latest commit info
        current_version = bl_info['version']
        layout.row().label(text=f"Plugin version: v{current_version[0]}.{current_version[1]}.{current_version[2]}")
        
        layout.separator()
        
        # Current commit
        if CURRENT_COMMIT_MESSAGE:
            # Truncate long commit messages
            current_msg = CURRENT_COMMIT_MESSAGE[:50] + "..." if len(CURRENT_COMMIT_MESSAGE) > 50 else CURRENT_COMMIT_MESSAGE
            layout.row().label(text=f"Current: {current_msg}")
        elif CURRENT_COMMIT_SHA:
            layout.row().label(text=f"Current: {CURRENT_COMMIT_SHA[:8]}")
        else:
            layout.row().label(text="Current: Unknown")
            
        # Latest commit
        if LATEST_COMMIT_MESSAGE:
            # Truncate long commit messages  
            latest_msg = LATEST_COMMIT_MESSAGE[:50] + "..." if len(LATEST_COMMIT_MESSAGE) > 50 else LATEST_COMMIT_MESSAGE
            layout.row().label(text=f"Latest: {latest_msg}")
        elif LATEST_COMMIT_SHA:
            layout.row().label(text=f"Latest: {LATEST_COMMIT_SHA[:8]}")
            
        if LATEST_COMMIT_DATE:
            layout.row().label(text=f"Date: {LATEST_COMMIT_DATE[:10]}")  # Show just the date part

        layout.separator()
        layout.row().operator(
            "sub.view_update_changelog",
            text="View What's New",
            icon='TEXT',
        )
        
        layout.separator()
        
        # Status and buttons based on current update state
        if UPDATE_STATUS == "idle":
            layout.row().operator("sub.check_for_updates", text="Refresh Update Check")
            col = layout.column()
            col.scale_y = 1.0
            col.operator("sub.download_update", text="Download & Install Update", icon='IMPORT')
            
        elif UPDATE_STATUS == "checking":
            layout.row().label(text="Checking for updates...", icon='INFO')
            
        elif UPDATE_STATUS == "downloading":
            layout.row().label(text="Downloading update...", icon='IMPORT')
            # Progress bar
            from ..blender_compat import draw_progress
            progress_text = f"Progress: {UPDATE_DOWNLOAD_PROGRESS:.1%}"
            draw_progress(layout, UPDATE_DOWNLOAD_PROGRESS, text=progress_text)
            
        elif UPDATE_STATUS == "installing":
            layout.row().label(text="Installing update...", icon='FILE_REFRESH')
            layout.row().label(text="Please wait, Blender will restart automatically!")
            layout.row().label(text="Do not close Blender manually!", icon='ERROR')

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)

class SUB_PT_updater_settings(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ultimate'
    bl_label = 'Plugin Updater'
    bl_parent_id = "SUB_PT_update_plugin"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        # LOCAL_VERSION_AHEAD is a cached flag from the last check, so this stays
        # cheap enough for a poll and keeps the panel hidden on newer local builds.
        from .version_check import UPDATE_AVAILABLE, LOCAL_VERSION_AHEAD
        return bool(UPDATE_AVAILABLE) and not LOCAL_VERSION_AHEAD
    
    def draw(self, context):
        self.layout.use_property_decorate = False
        from .version_check import UPDATE_STATUS

        layout = self.layout
        layout.use_property_split = False
        
        # Manual controls
        layout.row().label(text="Manual Controls:")
        layout.row().operator("sub.check_for_updates", text="Check for Updates", icon='FILE_REFRESH')
        
        if UPDATE_STATUS not in ["downloading", "installing"]:
            layout.row().operator("sub.download_update", text="Force Download & Install", icon='IMPORT')
        
        # Information
        layout.separator()
        layout.row().label(text="Repository: CrusherD2/smash-ultimate-blender")
        layout.row().label(text="Branch: animation-workflow")
        layout.row().label(text="Updates monitor commits on this branch")

    def draw_header_preset(self, context):
        from ..ui_help import draw_panel_help
        draw_panel_help(self.layout, self)
