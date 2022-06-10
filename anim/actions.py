import bpy
import os

from ..rig.helpers import clear_pose, try_key

class GRET_OT_action_set(bpy.types.Operator):
    #tooltip
    """Edit this action. Ctrl-click to rename"""

    bl_idname = 'gret.action_set'
    bl_label = "Set Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty(options={'HIDDEN'})
    new_name: bpy.props.StringProperty(name="New name", default="")
    play: bpy.props.BoolProperty(options={'HIDDEN'}, default=False)

    @classmethod
    def poll(cls, context):
        return context.object and context.object.animation_data

    def execute(self, context):
        obj = context.object
        if not self.name:
            obj.animation_data.action = None
            return {'FINISHED'}

        action = bpy.data.actions.get(self.name, None)
        if action:
            # Always save it, just in case
            action.use_fake_user = True

            if self.new_name:
                # Rename
                action.name = self.new_name
            elif not self.play and obj.animation_data.action == action:
                # Action was already active, stop editing
                obj.animation_data.action = None
            else:
                clear_pose(obj)
                obj.animation_data.action = action

                # Set preview range. Use start and end markers if they exist
                # Use new Blender 3.1 Frame Range feature
                if action.use_frame_range:
                    context.scene.frame_preview_start = int(action.frame_start)
                    context.scene.frame_preview_end = int(action.frame_end)
                else:
                    context.scene.frame_preview_start = int(action.curve_frame_range[0])
                    context.scene.frame_preview_end = int(action.curve_frame_range[1])

                context.scene.use_preview_range = True

                if self.play:
                    context.scene.frame_current = int(action.curve_frame_range[0])
                    bpy.ops.screen.animation_cancel(restore_frame=False)
                    bpy.ops.screen.animation_play()

        return {'FINISHED'}

    def invoke(self, context, event):
        if event.ctrl:
            # Rename
            self.new_name = self.name
            return context.window_manager.invoke_props_dialog(self)
        else:
            self.new_name = ""
            return self.execute(context)

class GRET_OT_action_add(bpy.types.Operator):
    #tooltip
    """Add a new action"""

    bl_idname = 'gret.action_add'
    bl_label = "Add Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty(default="New action")

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object

        if not obj.animation_data:
            obj.animation_data_create()
        new_action = bpy.data.actions.new(self.name)
        new_action.use_fake_user = True
        clear_pose(obj)
        obj.animation_data.action = new_action

        return {'FINISHED'}

class GRET_OT_action_remove(bpy.types.Operator):
    #tooltip
    """Delete the action"""

    bl_idname = 'gret.action_remove'
    bl_label = "Remove Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.object and context.object.animation_data

    def execute(self, context):
        obj = context.object
        action = bpy.data.actions.get(self.name, None)
        if not action:
            return {'CANCELLED'}

        bpy.data.actions.remove(action)

        return {'FINISHED'}

class GRET_OT_action_duplicate(bpy.types.Operator):
    #tooltip
    """Duplicate this action"""

    bl_idname = 'gret.action_duplicate'
    bl_label = "Duplicate Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object
        action = bpy.data.actions.get(self.name, None)
        if not action:
            return {'CANCELLED'}

        new_action = action.copy()
        new_action.use_fake_user = True

        return {'FINISHED'}

class GRET_OT_pose_set(bpy.types.Operator):
    #tooltip
    """Go to the frame for this pose. Ctrl-click to rename"""

    bl_idname = 'gret.pose_set'
    bl_label = "Set Pose"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty(options={'HIDDEN'})
    new_name: bpy.props.StringProperty(name="New name", default="")

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj and obj.animation_data and obj.animation_data.action

    def execute(self, context):
        obj = context.object
        if not self.name:
            return {'CANCELLED'}

        action = obj.animation_data.action
        marker = action.pose_markers.get(self.name, None)
        if marker:
            if self.new_name:
                # Rename
                if self.new_name in action.pose_markers:
                    # Blender allows it, but don't permit conflicting pose names
                    return {'CANCELLED'}
                marker.name = self.new_name
            else:
                context.scene.frame_set(frame=marker.frame)

        return {'FINISHED'}

    def invoke(self, context, event):
        if event.ctrl:
            # Rename
            self.new_name = self.name
            return context.window_manager.invoke_props_dialog(self)
        else:
            self.new_name = ""
            return self.execute(context)

class GRET_OT_pose_make(bpy.types.Operator):
    #tooltip
    """Creates a pose marker for every frame in the action"""

    bl_idname = 'gret.pose_make'
    bl_label = "Make Poses"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj and obj.animation_data and obj.animation_data.action

    def execute(self, context):
        obj = context.object

        action = obj.animation_data.action
        unused_markers = action.pose_markers[:]
        first_frame, last_frame = int(action.frame_range[0]), int(action.frame_range[1] + 1)
        for frame in range(first_frame, last_frame):
            marker = next((m for m in action.pose_markers if m.frame == frame), None)
            if marker:
                # There is a marker for this frame, don't remove it
                unused_markers.remove(marker)
            else:
                # Create a marker for this frame
                new_marker = action.pose_markers.new(name=f"Pose {frame:03d}")
                # Docs read that new() takes a frame kwarg, this doesn't seem to be the case
                new_marker.frame = frame
        for marker in unused_markers:
            print(f"Removed unused pose marker '{marker.name}'")
            action.pose_markers.remove(marker)

        return {'FINISHED'}

def get_actions_for_rig(rig):
    for action in bpy.data.actions:
        if action.library:
            # Never show linked actions
            continue
        yield action

def draw_panel(self, context):
    obj = context.object
    layout = self.layout
    settings = context.scene.gret

    if obj and obj.type == 'ARMATURE':
        box = layout.box()
        row = box.row(align=True)
        row.label(text="Available Actions", icon='ACTION')
        row.operator('gret.action_add', icon='ADD', text="")

        rig_actions = list(get_actions_for_rig(obj))
        active_action = obj.animation_data.action if obj.animation_data else None
        if rig_actions:
            col = box.column(align=True)
            for action in rig_actions:
                selected = action == active_action
                row = col.row(align=True)
                if selected and context.screen.is_animation_playing:
                    op = row.operator('screen.animation_cancel', icon='PAUSE', text="", emboss=False)
                    op.restore_frame = False
                else:
                    icon = 'PLAY' if selected else 'TRIA_RIGHT'
                    op = row.operator('gret.action_set', icon=icon, text="", emboss=False)
                    op.name = action.name
                    op.play = True
                op = row.operator('gret.action_set', text=action.name)
                op.name = action.name
                op.play = False
                row.operator('gret.action_duplicate', icon='DUPLICATE', text="").name = action.name
                row.operator('gret.action_remove', icon='X', text="").name = action.name

        if active_action:
            box = layout.box()
            row = box.row(align=True)
            row.label(text="Pose Markers", icon='BOOKMARKS')
            row.prop(settings, 'poses_sorted', icon='SORTALPHA', text="")
            row.operator('gret.pose_make', icon='ADD', text="")

            if active_action.pose_markers:
                col = box.column(align=True)
                if settings.poses_sorted:
                    markers = sorted(active_action.pose_markers, key=lambda p: p.name)
                else:
                    markers = active_action.pose_markers
                for marker in markers:
                    selected = marker.frame == context.scene.frame_current
                    row = col.row(align=True)
                    row.label(text="", icon='PMARKER_ACT' if selected else 'PMARKER_SEL')
                    op = row.operator('gret.pose_set', text=marker.name)
                    op.name = marker.name

classes = (
    GRET_OT_action_add,
    GRET_OT_action_duplicate,
    GRET_OT_action_remove,
    GRET_OT_action_set,
    GRET_OT_pose_make,
    GRET_OT_pose_set,
)

def register(settings):
    for cls in classes:
        bpy.utils.register_class(cls)

    settings.add_property('poses_sorted', bpy.props.BoolProperty(
        name="Sort Poses",
        description="Displays pose markers sorted alphabetically",
        default=False,
    ))

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
