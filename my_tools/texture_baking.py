from itertools import chain
from mathutils import Matrix
import bpy
import time
from .helpers import (
    beep,
    fail_if_invalid_export_path,
    get_export_path,
    get_nice_export_report,
    load_selection,
    log,
    logger,
    save_selection,
    select_only,
    show_only,
)

class SolidPixels:
    """Mimics a pixels array, always returning the same value for all pixels."""
    def __init__(self, size, value=0.0):
        self.size = size
        self.value = value
    def __len__(self):
        return self.size * self.size * 4
    def __getitem__(self, key):
        if isinstance(key, slice):
            return [self.value] * len(range(*key.indices(len(self))))
        return self.value

def remap_materials(objs, src_mat, dst_mat):
    for obj in objs:
        for mat_idx, mat in enumerate(obj.data.materials):
            if mat == src_mat:
                obj.data.materials[mat_idx] = dst_mat

def bake_ao(scn, nodes, links):
    scn.cycles.samples = 128
    bpy.ops.object.bake(type='AO')

def bake_bevel(scn, nodes, links):
    geometry_node = nodes.new(type='ShaderNodeNewGeometry')
    bevel_node = nodes.new(type='ShaderNodeBevel')
    bevel_node.samples = 1
    bevel_node.inputs['Radius'].default_value = 0.1
    cross_node = nodes.new(type='ShaderNodeVectorMath')
    cross_node.operation = 'CROSS_PRODUCT'
    length_node = nodes.new(type='ShaderNodeVectorMath')
    length_node.operation = 'LENGTH'
    emission_node = nodes.new(type='ShaderNodeEmission')
    output_node = nodes.new(type='ShaderNodeOutputMaterial')
    links.new(output_node.inputs['Surface'], emission_node.outputs['Emission'])
    links.new(emission_node.inputs['Color'], length_node.outputs['Value'])
    links.new(length_node.inputs['Vector'], cross_node.outputs['Vector'])
    links.new(cross_node.inputs[0], geometry_node.outputs['Normal'])
    links.new(cross_node.inputs[1], bevel_node.outputs['Normal'])

    scn.cycles.samples = 64
    bpy.ops.object.bake(type='EMIT')

bakers = {
    'AO': bake_ao,
    'BEVEL': bake_bevel,
}

bake_items = [
    ('NONE', "None", "Nothing."),
    ('AO', "AO", "Ambient occlusion."),
    ('BEVEL', "Bevel", "Bevel mask, similar to curvature."),
]

class MY_OT_bake(bpy.types.Operator):
    #tooltip
    """Bake and export the texture.
All faces from all objects assigned to this material are assumed to contribute"""

    bl_idname = 'my_tools.bake'
    bl_label = "Bake"
    bl_options = {'REGISTER'}

    def new_image(self, name, size):
        image = bpy.data.images.new(name=name, width=size, height=size)
        self.new_images.append(image)

        image.alpha_mode = 'NONE'
        return image

    def new_bake_material(self, image):
        mat = bpy.data.materials.new(name=image.name)
        self.new_materials.append(mat)

        mat.use_nodes = True
        mat.node_tree.nodes.clear()
        image_node = mat.node_tree.nodes.new(type='ShaderNodeTexImage')
        image_node.image = image
        return mat

    @classmethod
    def poll(cls, context):
        return context.object and context.object.active_material and context.mode == 'OBJECT'

    def _execute(self, context):
        # External baking is broken in Blender
        # See https://developer.blender.org/T57143 and https://developer.blender.org/D4162

        mat = context.object.active_material
        bake = mat.texture_bake
        size = bake.size

        # Collect all the objects that share this material
        objs = [o for o in context.scene.objects if mat.name in o.data.materials]
        show_only(context, objs)
        select_only(context, objs)

        log(f"Baking {mat.name} with {len(objs)} contributing objects")
        logger.log_indent += 1

        # Explode objects
        for obj_idx, obj in enumerate(objs):
            self.saved_transforms[obj] = obj.matrix_world.copy()
            obj.matrix_world = Matrix.Translation((100.0 * obj_idx, 0.0, 0.0))

        # Setup common to all bakers
        # Note that dilation happens before the bake results from multiple objects are merged
        # Margin should be kept at a minimum to prevent bakes from overlapping
        context.scene.render.engine = 'CYCLES'
        context.scene.render.bake.margin = size // 128

        bake_pixels = [SolidPixels(size, k) for k in (0.0, 0.0, 0.0, 1.0)]
        bake_srcs = [bake.r, bake.g, bake.b]
        for bake_src in bake_srcs:
            if bake_src != 'NONE':
                # Avoid doing extra work and bake only once for all channels with the same source
                channel_idxs = [idx for idx, src in enumerate(bake_srcs) if src == bake_src]
                channel_names = ""
                for channel_idx in channel_idxs:
                    bake_srcs[channel_idx] = 'NONE'
                    channel_names += ("R", "G", "B")[channel_idx]
                log(f"Baking {bake_src} for channel {channel_names}")
                bake_img = self.new_image(f"_{mat.name}_{bake_src}", size)
                bake_mat = self.new_bake_material(bake_img)

                remap_materials(objs, mat, bake_mat)
                bakers[bake_src](context.scene, bake_mat.node_tree.nodes, bake_mat.node_tree.links)
                remap_materials(objs, bake_mat, mat)

                # Store the result
                pixels = bake_img.pixels[:]
                for channel_idx in channel_idxs:
                    bake_pixels[channel_idx] = pixels

        # Composite and write file to disk
        path_fields = {
            'material': mat.name,
        }
        filepath = get_export_path(bake.export_path, path_fields)
        filename = bpy.path.basename(filepath)

        log(f"Exporting {filename}")
        pack_img = self.new_image(f"_{mat.name}", size)
        pack_img.pixels[:] = chain.from_iterable(
            zip(*(pixels[channel_idx::4] for channel_idx, pixels in enumerate(bake_pixels))))
        pack_img.filepath_raw = filepath
        pack_img.file_format = 'PNG'
        pack_img.save()
        self.exported_files.append(filepath)

        logger.log_indent -= 1

    def execute(self, context):
        bake = context.object.active_material.texture_bake

        try:
            fail_if_invalid_export_path(bake.export_path, ['material'])
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        saved_selection = save_selection()
        saved_render_engine = context.scene.render.engine
        saved_render_bake_margin = context.scene.render.bake.margin  # Don't mistake for bake_margin
        saved_cycles_samples = context.scene.cycles.samples
        saved_use_global_undo = context.preferences.edit.use_global_undo
        context.preferences.edit.use_global_undo = False
        self.exported_files = []
        self.new_materials = []
        self.new_images = []
        self.saved_transforms = {}
        logger.start_logging()

        try:
            start_time = time.time()
            self._execute(context)
            # Finished without errors
            elapsed = time.time() - start_time
            self.report({'INFO'}, get_nice_export_report(self.exported_files, elapsed))
            beep(pitch=3, num=1)
        finally:
            # Clean up
            while self.new_materials:
                bpy.data.materials.remove(self.new_materials.pop())
            while self.new_images:
                bpy.data.images.remove(self.new_images.pop())
            for obj, matrix_world in self.saved_transforms.items():
                obj.matrix_world = matrix_world
            del self.saved_transforms

            load_selection(saved_selection)
            context.scene.render.engine = saved_render_engine
            context.scene.render.bake.margin = saved_render_bake_margin
            context.scene.cycles.samples = saved_cycles_samples
            context.preferences.edit.use_global_undo = saved_use_global_undo
            logger.end_logging()

        return {'FINISHED'}

class MY_OT_quick_unwrap(bpy.types.Operator):
    #tooltip
    """Smart unwrap and pack UVs for all objects that have this material assigned"""

    bl_idname = 'my_tools.quick_unwrap'
    bl_label = "Quick Unwrap"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and context.mode == 'OBJECT'

    def execute(self, context):
        objs = context.selected_objects

        # for obj in objs:

class MY_PT_material_tools(bpy.types.Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'
    bl_label = "Texture Bake"

    @classmethod
    def poll(cls, context):
        return context.object and context.object.active_material

    def draw(self, context):
        layout = self.layout
        mat = context.object.active_material
        bake = mat.texture_bake

        row = layout.row(align=True)
        row.prop(bake, 'r', icon='COLOR_RED', text="")
        row.prop(bake, 'g', icon='COLOR_GREEN', text="")
        row.prop(bake, 'b', icon='COLOR_BLUE', text="")
        row.prop(bake, 'size', text="")
        col = layout.column(align=True)
        col.prop(bake, 'export_path', text="")
        row = col.row(align=True)
        row.operator('my_tools.quick_unwrap', icon='UV')
        op = row.operator('my_tools.bake', icon='RENDER_STILL')

class MY_PG_texture_bake(bpy.types.PropertyGroup):
    size: bpy.props.IntProperty(
        name="Texture Size",
        description="Size of the exported texture",
        default=256,
        min=8,
    )
    r: bpy.props.EnumProperty(
        name="Texture R Source",
        description="Mask to bake into the texture's red channel",
        items=bake_items,
    )
    g: bpy.props.EnumProperty(
        name="Texture G Source",
        description="Mask to bake into the texture's green channel",
        items=bake_items,
    )
    b: bpy.props.EnumProperty(
        name="Texture B Source",
        description="Mask to bake into the texture's blue channel",
        items=bake_items,
    )
    export_path: bpy.props.StringProperty(
        name="Export Path",
        description="""Export path for the baked texture.
{file} = Name of this .blend file without extension.
{material} = Name of the material being baked.""",
        default="//export/T_{material}.png",
        subtype='FILE_PATH',
    )

classes = (
    MY_OT_bake,
    MY_OT_quick_unwrap,
    MY_PG_texture_bake,
    MY_PT_material_tools,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Material.texture_bake = bpy.props.PointerProperty(type=MY_PG_texture_bake)

def unregister():
    del bpy.types.Material.texture_bake

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
