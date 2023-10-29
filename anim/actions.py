from functools import partial
import bpy
import os

from .. import prefs
from ..log import log, logd
from ..rig.helpers import clear_pose

class GRET_OT_action_set(bpy.types.Operator):
    """Edit this action. Ctrl-Click to rename"""

    bl_idname = 'gret.action_set'
    bl_label = "Set Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty(options={'HIDDEN'})
    new_name: bpy.props.StringProperty(name="New name", default="")
    play: bpy.props.BoolProperty(options={'HIDDEN'}, default=False)

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.animation_data

    def execute(self, context):
        obj = context.active_object
        if not self.name:
            obj.animation_data.action = None
            return {'FINISHED'}

        action = bpy.data.actions.get(self.name, None)
        if not action:
            return {'CANCELLED'}

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
            sync_frame_range()

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
    """Add a new action"""

    bl_idname = 'gret.action_add'
    bl_label = "Add Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty(default="New action")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object

        if not obj.animation_data:
            obj.animation_data_create()
        new_action = bpy.data.actions.new(self.name)
        new_action.use_fake_user = True
        clear_pose(obj)
        obj.animation_data.action = new_action

        return {'FINISHED'}

class GRET_OT_action_remove(bpy.types.Operator):
    """Delete the action"""

    bl_idname = 'gret.action_remove'
    bl_label = "Remove Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.animation_data

    def execute(self, context):
        obj = context.active_object
        action = bpy.data.actions.get(self.name, None)
        if not action:
            return {'CANCELLED'}

        bpy.data.actions.remove(action)

        return {'FINISHED'}

class GRET_OT_action_duplicate(bpy.types.Operator):
    """Duplicate this action"""

    bl_idname = 'gret.action_duplicate'
    bl_label = "Duplicate Action"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        action = bpy.data.actions.get(self.name, None)
        if not action:
            return {'CANCELLED'}

        new_action = action.copy()
        new_action.use_fake_user = True

        return {'FINISHED'}

class GRET_OT_pose_set(bpy.types.Operator):
    """Go to the frame for this pose. Ctrl-click to rename"""

    bl_idname = 'gret.pose_set'
    bl_label = "Set Pose"
    bl_options = {'INTERNAL', 'UNDO'}

    name: bpy.props.StringProperty(options={'HIDDEN'})
    new_name: bpy.props.StringProperty(name="New name", default="")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.animation_data and obj.animation_data.action

    def execute(self, context):
        obj = context.active_object
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
    """Creates a pose marker for every frame in the action"""

    bl_idname = 'gret.pose_make'
    bl_label = "Make Poses"
    bl_options = {'INTERNAL', 'UNDO'}

    create_custom_properties: bpy.props.BoolProperty(
        name="For Pose Blender",
        description="Create a custom property for each pose, required for pose blending",
        default=False,
    )

    key_custom_properties: bpy.props.BoolProperty(
        name="For Exporting",
        description="""Key pose weight custom property for each frame.
Not required for pose blending but required for exporting""",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.animation_data and obj.animation_data.action

    def execute(self, context):
        obj = context.active_object
        action = obj.animation_data.action
        start_frame, last_frame = int(action.curve_frame_range[0]), int(action.curve_frame_range[1] + 1)

        unused_markers = action.pose_markers[:]
        for frame in range(start_frame, last_frame):
            marker = next((m for m in action.pose_markers if m.frame == frame), None)
            if marker:
                # There is a marker for this frame, don't remove it
                unused_markers.remove(marker)
            else:
                # Create a marker for this frame
                # Docs read that new() takes a frame argument, this doesn't seem to be the case
                new_marker = action.pose_markers.new(name=f"Pose {frame:03d}")
                new_marker.frame = frame

        for marker in unused_markers:
            log(f"Removed unused pose marker {marker.name}")
            action.pose_markers.remove(marker)

        if self.create_custom_properties and obj.override_library:
            self.report({'WARNING'}, "Can't create custom properties from an override data-block.")
        elif self.create_custom_properties:
            for marker in action.pose_markers:
                if marker.name not in obj:
                    obj[marker.name] = 0.0

                obj.property_overridable_library_set(f'["{marker.name}"]', True)
                obj.id_properties_ui(marker.name).update(default=0.0, description="Pose weight",
                    min=0.0, max=1.0, soft_min=0.0, soft_max=1.0)

            if self.key_custom_properties:
                group = action.groups.get(action.name) or action.groups.new(name=action.name)
                data_path_to_fc = {fc.data_path: fc for fc in action.fcurves}

                for marker in action.pose_markers:
                    data_path = f'["{marker.name}"]'
                    fc = data_path_to_fc.get(data_path)
                    if fc:
                        action.fcurves.remove(fc)
                    data_path_to_fc[data_path] = fc = action.fcurves.new(data_path)
                    fc.group = group

                    if marker.frame > start_frame:
                        fc.keyframe_points.insert(marker.frame - 1, 0.0).interpolation = 'LINEAR'
                    fc.keyframe_points.insert(marker.frame, 1.0).interpolation = 'LINEAR'
                    if marker.frame < last_frame:
                        fc.keyframe_points.insert(marker.frame + 1, 0.0).interpolation = 'LINEAR'

        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, 'create_custom_properties')
        row = col.row(align=True)
        row.prop(self, 'key_custom_properties')
        row.enabled = self.create_custom_properties

def get_actions_for_rig(rig):
    for action in bpy.data.actions:
        if action.library:
            # Never show linked actions
            continue
        yield action

def draw_panel(self, context):
    obj = context.active_object
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

                sub = row.column(align=True)
                sub.ui_units_x = 1.0
                if selected and context.screen.is_animation_playing:
                    op = sub.operator('screen.animation_cancel', icon='PAUSE', text="", emboss=False)
                    op.restore_frame = False
                else:
                    icon = 'PLAY' if selected else 'TRIA_RIGHT'
                    op = sub.operator('gret.action_set', icon=icon, text="", emboss=False)
                    op.name = action.name
                    op.play = True

                op = row.operator('gret.action_set', text=action.name)
                op.name = action.name
                op.play = False
                row.operator('gret.action_duplicate', icon='DUPLICATE', text="").name = action.name
                row.operator('gret.action_remove', icon='X', text="").name = action.name

                if prefs.animation__show_action_frame_range and selected:
                    row = col.row(align=True)
                    sub = row.column(align=True)
                    sub.ui_units_x = 0.95  # Eyeballed to make it line up, beats split() madness
                    sub.separator()  # Whitespace
                    row.prop(active_action, 'use_frame_range', text="Range")
                    sub = row.row(align=True)
                    sub.prop(active_action, 'frame_start', text="")
                    sub.prop(active_action, 'frame_end', text="")
                    sub.prop(active_action, 'use_cyclic', icon='CON_FOLLOWPATH', text="")
                    sub.enabled = active_action.use_frame_range
                    col.separator()

        if active_action:
            box = layout.box()
            row = box.row(align=True)
            row.label(text="Pose Markers", icon='BOOKMARKS')
            row.prop(settings, 'poses_sorted', icon='SORTALPHA', text="")
            row.operator('gret.pose_make', icon='PMARKER_ACT', text="")

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

def sync_frame_range():
    if not prefs.animation__sync_action_frame_range:
        return

    context = bpy.context
    obj = context.active_object
    if obj and obj.animation_data and obj.animation_data.action:
        action = obj.animation_data.action
        if action.use_frame_range:
            context.scene.frame_preview_start = int(action.frame_start)
            context.scene.frame_preview_end = int(action.frame_end)
        else:
            context.scene.frame_preview_start = int(action.curve_frame_range[0])
            context.scene.frame_preview_end = int(action.curve_frame_range[1])
        context.scene.use_preview_range = True

owner = object()
def subscribe_all():
    subscribe = partial(bpy.msgbus.subscribe_rna, owner=owner, args=())
    subscribe(key=(bpy.types.Action, 'use_frame_range'), notify=sync_frame_range)
    subscribe(key=(bpy.types.Action, 'frame_start'), notify=sync_frame_range)
    subscribe(key=(bpy.types.Action, 'frame_end'), notify=sync_frame_range)

def unsubscribe_all():
    bpy.msgbus.clear_by_owner(owner)

def on_prefs_updated():
    if prefs.animation__sync_action_frame_range:
        unsubscribe_all()
        subscribe_all()
        sync_frame_range()
    else:
        unsubscribe_all()

def register(settings, prefs):
    for cls in classes:
        bpy.utils.register_class(cls)

    settings.add_property('poses_sorted', bpy.props.BoolProperty(
        name="Sort Poses",
        description="Displays pose markers sorted alphabetically",
        default=False,
        options=set(),
    ))

    if prefs.animation__sync_action_frame_range:
        subscribe_all()

def unregister():
    unsubscribe_all()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
