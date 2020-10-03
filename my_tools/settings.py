import bpy
from bpy.app.handlers import persistent
from .helpers import is_object_defaulted

def on_collection_updated(self, context):
    scn = context.scene
    job = scn.my_tools.export_jobs[self.job_index]
    index = job.collections.values().index(self)

    empty = not self.collection

    if empty and index < len(job.collections) - 1:
        # Remove it unless it's the last item
        job.collections.remove(index)
    elif not empty and index == len(job.collections) - 1:
        # Make sure there's always an empty item at the end
        coll = job.collections.add()
        coll.job_index = self.job_index

class MY_PG_export_collection(bpy.types.PropertyGroup):
    job_index: bpy.props.IntProperty()
    collection: bpy.props.PointerProperty(
        name="Collection",
        description="Collection to include",
        type=bpy.types.Collection,
        update=on_collection_updated,
    )
    export_viewport: bpy.props.BoolProperty(
        name="Export Viewport",
        description="Include collections and objects that are visible in viewport",
        default=False,
    )
    export_render: bpy.props.BoolProperty(
        name="Export Render",
        description="Include collections and objects that are visible in render",
        default=True,
    )

def on_action_updated(self, context):
    scn = context.scene
    job = scn.my_tools.export_jobs[self.job_index]
    index = job.actions.values().index(self)

    empty = not self.action and not self.use_pattern

    if empty and index < len(job.actions) - 1:
        # Remove it unless it's the last item
        job.actions.remove(index)
    elif not empty and index == len(job.actions) - 1:
        # Make sure there's always an empty item at the end
        action = job.actions.add()
        action.job_index = self.job_index

class MY_PG_export_action(bpy.types.PropertyGroup):
    job_index: bpy.props.IntProperty()
    action: bpy.props.StringProperty(
        name="Action",
        description="Action or actions to export",
        default="",
        update=on_action_updated,
    )
    use_pattern: bpy.props.BoolProperty(
        name="Use Pattern",
        description="Adds all actions that match a pattern (.?* allowed)",
        default=False,
        update=on_action_updated,
    )

def on_copy_property_updated(self, context):
    scn = context.scene
    job = scn.my_tools.export_jobs[self.job_index]
    index = job.copy_properties.values().index(self)

    empty = not self.source and not self.destination

    if empty and index < len(job.copy_properties) - 1:
        # Remove it unless it's the last item
        job.copy_properties.remove(index)
    elif not empty and index == len(job.copy_properties) - 1:
        # Make sure there's always an empty item at the end
        copy_property = job.copy_properties.add()
        copy_property.job_index = self.job_index

class MY_PG_copy_property(bpy.types.PropertyGroup):
    job_index: bpy.props.IntProperty()
    source: bpy.props.StringProperty(
        name="Source",
        description="""Path of the source property to bake.
e.g.: pose.bones["c_eye_target.x"]["eye_target"]""",
        default="",
        update=on_copy_property_updated,
    )
    destination: bpy.props.StringProperty(
        name="Destination",
        description="""Path of the destination property.
e.g.: ["eye_target"]""",
        default="",
        update=on_copy_property_updated,
    )

def on_what_updated(self, context):
    # Ensure collections are valid
    if not self.collections:
        job_index = context.scene.my_tools.export_jobs.values().index(self)
        collection = self.collections.add()
        collection.job_index = job_index
    if not self.actions:
        job_index = context.scene.my_tools.export_jobs.values().index(self)
        action = self.actions.add()
        action.job_index = job_index
    if not self.copy_properties:
        job_index = context.scene.my_tools.export_jobs.values().index(self)
        copy_property = self.copy_properties.add()
        copy_property.job_index = job_index

class MY_PG_export_job(bpy.types.PropertyGroup):
    show_expanded: bpy.props.BoolProperty(
        name="Show Expanded",
        description="Set export job expanded in the user interface",
        default=True,
    )
    name: bpy.props.StringProperty(
        name="Name",
        description="Export job name",
        default="Job",
    )
    rig: bpy.props.PointerProperty(
        name="Rig",
        description="Armature to operate on",
        type=bpy.types.Object,
        poll=lambda self, obj: obj and obj.type == 'ARMATURE',
    )
    what: bpy.props.EnumProperty(
        items=[
            ('SCENE', "Scene", "Scene objects.", 'SCENE_DATA', 0),
            ('RIG', "Rig", "Armature and meshes.", 'ARMATURE_DATA', 1),
            ('ANIMATION', "Animation", "Armature animation only.", 'ANIM', 2),
        ],
        name="Export Type",
        description="What to export",
        update=on_what_updated,
    )
    export_path: bpy.props.StringProperty(
        name="Export Path",
        description="""Export path relative to the current folder.
{basename} = Name of this .blend file without extension.
{object} = Name of the object being exported.
{action} = Name of the first action being exported, if exporting animation""",
        default="//export/{basename}.fbx",
        subtype='FILE_PATH',
    )
    export_collection: bpy.props.PointerProperty(
        name="Export Collection",
        description="Collection where to place export products",
        type=bpy.types.Collection,
    )
    selection_only: bpy.props.BoolProperty(
        name="Selection Only",
        description="Exports the current selection",
        default=True,
    )
    collections: bpy.props.CollectionProperty(
        type=MY_PG_export_collection,
    )
    material_name_prefix: bpy.props.StringProperty(
        name="Material Prefix",
        description="Ensures that exported material names begin with a prefix",
        default="MI_",
    )

    # Scene export options
    export_collision: bpy.props.BoolProperty(
        name="Export Collision",
        description="Exports collision objects that follow the UE4 naming pattern",
        default=True,
    )

    # Rig export options
    merge_basis_shape_keys: bpy.props.BoolProperty(
        name="Merge Basis Shape Keys",
        description="Blends 'Key' and 'b_' shapekeys into the basis shape",
        default=True,
    )
    mirror_shape_keys: bpy.props.BoolProperty(
        name="Mirror Shape Keys",
        description="""Creates mirrored versions of shape keys that have side suffixes.
Requires a mirror modifier""",
        default=True,
    )
    side_vgroup_name: bpy.props.StringProperty(
        name="Side Vertex Group Name",
        description="Name of the vertex groups that will be created on mirroring shape keys",
        default="_side.l",
    )
    apply_modifiers: bpy.props.BoolProperty(
        name="Apply Modifiers",
        description="Allows exporting of shape keys even if the meshes have generative modifiers",
        default=True,
    )
    join_meshes: bpy.props.BoolProperty(
        name="Join Meshes",
        description="Joins meshes before exporting",
        default=True,
    )
    split_masks: bpy.props.BoolProperty(
        name="Split Masks",
        description="""Splits mask modifiers into extra meshes that are exported separately.
Normals are preserved""",
        default=False,
    )
    to_collection: bpy.props.BoolProperty(
        name="To Collection",
        description="Produced meshes are put in a collection instead of being exported",
        default=False,
    )

    # Animation export options
    actions: bpy.props.CollectionProperty(
        type=MY_PG_export_action,
    )
    copy_properties: bpy.props.CollectionProperty(
        type=MY_PG_copy_property,
    )

def poll_insertee(self, obj):
    return (obj.type == 'CURVE'
        and '_bone_names' in obj
        and obj.find_armature())

class MY_PG_settings(bpy.types.PropertyGroup):
    # Simple export
    export_path: bpy.props.StringProperty(
        name="Export Path",
        description="""Export path relative to the current folder.
{basename} = Name of this .blend file without extension.
{num} = Increments for every file exported""",
        default="//export/{object}.fbx",
        subtype='FILE_PATH',
    )
    export_collision: bpy.props.BoolProperty(
        name="Export Collision",
        description="Exports collision objects that follow the UE4 naming pattern",
        default=True,
    )
    export_animation_only: bpy.props.BoolProperty(
        name="Animation Only",
        description="Skips exporting meshes",
        default=False,
    )

    # Export
    export_jobs: bpy.props.CollectionProperty(
        type=MY_PG_export_job,
    )

classes = (
    MY_PG_copy_property,
    MY_PG_export_action,
    MY_PG_export_collection,
    MY_PG_export_job,
    MY_PG_settings,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Settings used to live in WindowManager, however pointer properties break with global undo
    bpy.types.Scene.my_tools = bpy.props.PointerProperty(type=MY_PG_settings)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
