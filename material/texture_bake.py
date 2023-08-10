from collections import namedtuple
from itertools import chain
from math import radians
from mathutils import Matrix
import bpy
import time

from .. import prefs
from ..log import log, logd, logger
from ..helpers import (
    beep,
    fail_if_invalid_export_path,
    get_context,
    get_export_path,
    get_nice_export_report,
    load_selection,
    override_viewports,
    restore_viewports,
    save_selection,
    select_only,
    show_only,
)
from .helpers import SolidPixels, Node

# TODO
# - AO floor
# - Allow Quick Unwrap from object mode
# - Report progress, see io_scene_obj/export_obj.py and bpy_extras.wm_utils.progress_report

def reverse_morton3(x):
    x &= 0x09249249
    x = (x ^ (x >> 2)) & 0x030c30c3
    x = (x ^ (x >> 4)) & 0x0300f00f
    x = (x ^ (x >> 8)) & 0xff0000ff
    x = (x ^ (x >> 16)) & 0x000003ff
    return x

def zagzig(x):
    return (x >> 1) ^ -(x & 1)

def xyz_from_index(i):
    x = zagzig(reverse_morton3(i >> 0))
    y = zagzig(reverse_morton3(i >> 1))
    z = zagzig(reverse_morton3(i >> 2))
    return x, y, z

def get_bake_objects(context, material, out_objects, out_meshes):
    for obj in context.scene.objects[:]:
        if obj.type != 'MESH' or obj.hide_render:
            continue  # Not a mesh or filtered by visibility
        if not material.name in obj.material_slots:
            continue  # Object doesn't contribute
        if not obj.data.polygons:
            continue  # Empty meshes cause bake to fail

        # Only apply render modifiers
        saved_modifier_show_viewport = []
        for mod in obj.modifiers:
            saved_modifier_show_viewport.append(mod.show_viewport)
            mod.show_viewport = mod.show_render

        dg = context.evaluated_depsgraph_get()
        new_data = bpy.data.meshes.new_from_object(obj.evaluated_get(dg),
            preserve_all_data_layers=True, depsgraph=dg)
        new_obj = bpy.data.objects.new(obj.name + "_", new_data)
        new_data.transform(obj.matrix_world)
        bpy.ops.object.origin_set(get_context(new_obj), type='ORIGIN_GEOMETRY', center='MEDIAN')

        # Move object materials to mesh
        for mat_idx, mat_slot in enumerate(obj.material_slots):
            if mat_slot.link == 'OBJECT':
                new_data.materials[mat_idx] = mat_slot.material
                new_obj.material_slots[mat_idx].link = 'DATA'

        # Restore modifiers
        for mod, show_viewport in zip(obj.modifiers, saved_modifier_show_viewport):
            mod.show_viewport = show_viewport

        out_objects.append(new_obj)
        assert isinstance(new_data, bpy.types.Mesh)
        assert new_data.users == 1
        out_meshes.append(new_data)

        # New objects are moved to the scene collection, ensuring they're visible
        context.scene.collection.objects.link(new_obj)
        new_obj.hide_set(False)
        new_obj.hide_viewport = False
        new_obj.hide_render = False
        new_obj.hide_select = False
    return out_objects

def remap_materials(objs, src_mat, dst_mat):
    for obj in objs:
        for mat_idx, mat in enumerate(obj.data.materials):
            obj.data.materials[mat_idx] = dst_mat if mat == src_mat else None

nodes_ao = (Node('OutputMaterial')
.link('Surface', None,
    Node('Emission')
    .link('Color', 0,
        Node('AmbientOcclusion', samples=16, only_local=True)
        .set('Distance', "scale*1.0")
    )
))

nodes_bevel = (Node('OutputMaterial')
.link('Surface', None,
    Node('Emission')
    .link('Color', 0,
        Node('Math', operation='SMOOTH_MIN')
        .set(1, 0.6)  # Value2
        .set(2, 2.0)  # Distance
        .link(0, None,
            Node('VectorMath', operation='LENGTH')
            .link('Vector', None,
                Node('VectorMath', operation='CROSS_PRODUCT')
                .link(0, 'Normal',
                    Node('NewGeometry')
                )
                .link(1, 'Normal',
                    Node('Bevel', samples=4)
                    .set('Radius', "scale*0.05")
                )
            )
        )
    )
))

nodes_curvature_cavity = (Node('Math', operation='SUBTRACT', use_clamp=True)
.set(0, 1.0)
.link(1, 'AO',
    Node('AmbientOcclusion', samples=16, only_local=True)
    .set('Distance', "scale*0.025")
    .link('Normal', None,
        Node('Bevel', samples=8)
        .set('Radius', "scale*0.1")
    )
))
nodes_curvature_edge = (Node('Math', operation='SMOOTH_MIN', use_clamp=True)
.set(1, 0.5)  # Value2
.set(2, 1.0)  # Distance
.link(0, None,
    Node('Math', operation='SUBTRACT', use_clamp=True)
    .set(0, 1.0)  # One minus AO
    .link(1, 'AO',
        Node('AmbientOcclusion', samples=16, inside=True, only_local=True)
        .set('Distance', "scale*0.05")
        .link('Normal', None,
            Node('Bevel', samples=8)
            .set('Radius', "scale*0.05")
        )
    )
))
nodes_curvature = (Node('OutputMaterial')
.link('Surface', None,
    Node('Emission')
    .link('Color', 0,
        Node('Math', operation='SUBTRACT', use_clamp=True)
        .link(1, None,
            Node('Math', operation='MULTIPLY', use_clamp=True)
            .set(1, 4.0)  # Value2
            .link(0, None,
                Node('Math', operation='MAXIMUM', use_clamp=True)
                .link(0, None,
                    Node('Math', operation='SUBTRACT', use_clamp=True)
                    .link(0, None, nodes_curvature_cavity)
                    .link(1, None, nodes_curvature_edge)
                )
                .link(1, None,
                    Node('Math', operation='MULTIPLY', use_clamp=True)
                    .link(0, None, nodes_curvature_cavity)
                    .link(1, None, nodes_curvature_edge)
                )
            )
        )
        .link(0, None,
            Node('Math', operation='ADD', use_clamp=True)
            .set(0, 0.5)  # Value1
            .link(1, None, nodes_curvature_edge)
        )
    )
))

def _bake(type):
    # Fixes a bug where bake fails because it polls for context.object being visible
    # Why the hell is 'object' not in sync with 'active_object'?
    ctx = bpy.context.copy()
    ctx['object'] = ctx['active_object']
    bpy.ops.object.bake(ctx, type=type)

def bake_ao(scene, node_tree, values):
    # scene.cycles.samples = 128
    # bpy.ops.object.bake(type='AO')
    # Ambient occlusion node seems to produce less artifacts
    nodes_ao.build(node_tree, values)
    scene.cycles.samples = 16
    _bake(type='EMIT')

def bake_bevel(scene, node_tree, values):
    nodes_bevel.build(node_tree, values)
    scene.cycles.samples = 16
    _bake(type='EMIT')

def bake_curvature(scene, node_tree, values):
    nodes_curvature.build(node_tree, values)
    scene.cycles.samples = 16
    _bake(type='EMIT')

bake_funcs = {
    'AO': bake_ao,
    'BEVEL': bake_bevel,
    'CURVATURE': bake_curvature,
}

bake_zeros = {
    'AO': 1.0,
    'BEVEL': 0.0,
    'CURVATURE': 0.5,
}

node_trees = {
    'AO': nodes_ao,
    'BEVEL': nodes_bevel,
    'CURVATURE': nodes_curvature,
}

bake_items = [
    ('NONE', "None", "Nothing"),
    ('AO', "AO", "Ambient occlusion"),
    ('BEVEL', "Bevel", "Bevel mask, similar to curvature"),
    ('CURVATURE', "Curvature", "Curvature, centered on gray"),
]

class GRET_OT_quick_unwrap(bpy.types.Operator):
    """Smart unwrap and pack UVs for all objects that have the active material assigned"""

    bl_idname = 'gret.quick_unwrap'
    bl_label = "Quick Unwrap"
    bl_options = {'INTERNAL', 'UNDO'}

    index: bpy.props.IntProperty(options={'HIDDEN'})

    angle_limit: bpy.props.FloatProperty(
        name="Angle Limit",
        description="Lower for more projection groups, higher for less distortion",
        subtype='ANGLE',
        default=radians(66.0),
        min=radians(0.0),
        max=radians(89.0),
    )
    area_weight: bpy.props.FloatProperty(
        name="Area Weight",
        description="Weight projection vectors by faces with larger areas",
        subtype='FACTOR',
        default=0.0,
        min=0.0,
        max=1.0,
    )
    align_with_world: bpy.props.BoolProperty(
        name="Align With World",
        description="Rotate UV islands to flow in the direction of gravity. Requires TexTools addon",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.object and context.object.active_material and context.mode == 'EDIT_MESH'

    def execute(self, context):
        mat = context.object.active_material
        bake = mat.texture_bakes[self.index]
        saved_area_ui_type = context.area.ui_type
        saved_use_uv_select_sync = context.scene.tool_settings.use_uv_select_sync
        saved_selection = save_selection()
        saved_active_uv_layers = {}  # Object to UV layer
        margin = 1.0 / 128 * 2

        try:
            # Select all faces of all objects that share the material
            bpy.ops.object.editmode_toggle()
            context.scene.tool_settings.use_uv_select_sync = True
            objs = [o for o in context.scene.objects if o.type == 'MESH' and mat.name in o.data.materials]
            select_only(context, objs)
            bpy.ops.object.editmode_toggle()
            bpy.ops.mesh.reveal()
            bpy.ops.mesh.select_mode(type='FACE')
            bpy.ops.object.editmode_toggle()
            for obj in objs:
                saved_active_uv_layers[obj] = obj.data.uv_layers.active
                uv = obj.data.uv_layers.get(bake.uv_layer_name)
                if not uv:
                    uv = obj.data.uv_layers.new(name=bake.uv_layer_name)
                uv.active = True
                for face in obj.data.polygons:
                    face.select = obj.data.materials[face.material_index] == mat
            bpy.ops.object.editmode_toggle()

            # Unwrap
            bpy.ops.uv.smart_project(
                angle_limit=self.angle_limit,
                island_margin=margin,
                area_weight=self.area_weight,
                correct_aspect=True,
                scale_to_bounds=False)

            # If set and TexTools is available, rotate islands
            if self.align_with_world:
                try:
                    context.area.ui_type = 'UV'
                    context.scene.tool_settings.use_uv_select_sync = False
                    bpy.ops.uv.textools_island_align_world(steps=2)
                except AttributeError:
                    pass

            # If available, pack using an addon
            try:
                context.scene.uvp2_props.margin = margin
                context.scene.uvp2_props.rot_enable = not self.align_with_world
                bpy.ops.uvpackmaster2.uv_pack()
            except AttributeError:
                pass
        finally:
            for obj, uv_layer in saved_active_uv_layers.items():
                obj.data.uv_layers.active = uv_layer
            load_selection(saved_selection)
            context.scene.tool_settings.use_uv_select_sync = saved_use_uv_select_sync
            context.area.ui_type = saved_area_ui_type
            # Exiting edit mode here causes uvpackmaster2 to break, it's doing some weird modal stuff
            # bpy.ops.object.mode_set(mode='OBJECT')

        return {'FINISHED'}

class GRET_OT_texture_bake(bpy.types.Operator):
    """Bake and export the texture.
All faces from all objects assigned to the active material are assumed to contribute"""

    bl_idname = 'gret.texture_bake'
    bl_label = "Bake Textures"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty(options={'HIDDEN'})

    def new_image(self, name, size):
        image = bpy.data.images.new(name=name, width=size, height=size)
        self.new_images.append(image)

        image.colorspace_settings.name = 'Linear'
        image.alpha_mode = 'NONE'
        return image

    def new_bake_material(self, image):
        mat = bpy.data.materials.new(name=f"_bake{image.name}")
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
        texture_bake = mat.texture_bakes[self.index]
        size = texture_bake.size

        # Collect all the objects that share this material
        objs = get_bake_objects(context, mat, self.new_objs, self.new_meshes)
        for obj in objs:
            if texture_bake.uv_layer_name not in obj.data.uv_layers:
                self.report({'ERROR'}, f"{obj.name} has no UV layer named '{texture_bake.uv_layer_name}'")
                return {'CANCELLED'}

        log(f"Baking {mat.name} with {len(objs)} contributing objects")
        logger.indent += 1
        show_only(context, objs)
        select_only(context, objs)

        # Explode objects. Not strictly necessary anymore since AO node has only_local flag
        explode_dist = max(max(obj.dimensions) for obj in objs) + 10.0
        for obj_idx, obj in enumerate(objs):
            # Spread out in every direction
            if False:
                x, y, z = xyz_from_index(obj_idx)
                explode_loc = (x * explode_dist, y * explode_dist, z * explode_dist)
                logd(f"Moving {obj.name} to {explode_loc}")
                obj.matrix_world = Matrix.Translation(explode_loc)
            obj.data.uv_layers[texture_bake.uv_layer_name].active = True

        # Setup common to all bakers
        # Note that dilation happens before the bake results from multiple objects are merged
        # Margin should be kept at a minimum to prevent bakes from overlapping
        context.scene.render.engine = 'CYCLES'
        context.scene.render.bake.margin = size // 128
        context.scene.render.bake.use_selected_to_active = False
        context.scene.render.bake.use_clear = False

        bake_pixels = [SolidPixels(size, k) for k in (0.0, 0.0, 0.0, 1.0)]
        BakeInfo = namedtuple('BakeInfo', ['type', 'scale'])
        BakeInfo.__bool__ = lambda self: self.type != 'NONE'
        bakes = [
            BakeInfo(texture_bake.r, texture_bake.r_scale),
            BakeInfo(texture_bake.g, texture_bake.g_scale),
            BakeInfo(texture_bake.b, texture_bake.b_scale),
        ]
        zero_color = [bake_zeros.get(bake.type, 0.0) for bake in bakes] + [0.0]
        for bake in bakes:
            if not bake:
                continue

            # Avoid doing extra work and bake only once for all channels with the same baker
            target_channel_idxs = []
            for channel_idx, other_bake in enumerate(bakes):
                if bake == other_bake:
                    bakes[channel_idx] = None
                    target_channel_idxs.append(channel_idx)

            channel_names = ''.join(("R", "G", "B")[idx] for idx in target_channel_idxs)
            log(f"Baking {bake.type} for channel {channel_names}")
            bake_img = self.new_image(f"_{mat.name}_{bake.type}", size)
            bake_img.generated_color = zero_color
            bake_mat = self.new_bake_material(bake_img)

            # Switch to the bake material temporarily and bake
            saved_materials = {obj: obj.data.materials[:] for obj in objs}
            remap_materials(objs, mat, bake_mat)

            bake_funcs[bake.type](context.scene, bake_mat.node_tree, {'scale': bake.scale})

            for obj, saved_mats in saved_materials.items():
                for mat_idx, saved_mat in enumerate(saved_mats):
                    obj.data.materials[mat_idx] = saved_mat

            # Store the result
            pixels = bake_img.pixels[:]
            for channel_idx in target_channel_idxs:
                bake_pixels[channel_idx] = pixels

        # Composite and write file to disk
        path_fields = {
            'material': mat.name,
        }
        filepath = get_export_path(texture_bake.export_path, path_fields)
        filename = bpy.path.basename(filepath)

        log(f"Exporting {filename}")
        pack_img = self.new_image(f"_{mat.name}", size)
        pack_img.pixels[:] = chain.from_iterable(
            zip(*(pixels[channel_idx::4] for channel_idx, pixels in enumerate(bake_pixels))))
        pack_img.filepath_raw = filepath
        pack_img.file_format = 'PNG'  # TODO detect format from extension
        pack_img.save()
        self.exported_files.append(filepath)

        logger.indent -= 1

    def execute(self, context):
        texture_bake = context.object.active_material.texture_bakes[self.index]

        try:
            fail_if_invalid_export_path(texture_bake.export_path, ['material'])
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        saved_selection = save_selection()
        saved_render_engine = context.scene.render.engine
        saved_render_bake_margin = context.scene.render.bake.margin  # Don't mistake for bake_margin
        saved_render_use_selected_to_active = context.scene.render.bake.use_selected_to_active
        saved_cycles_samples = context.scene.cycles.samples
        self.exported_files = []
        self.new_objs = []
        self.new_meshes = []
        self.new_materials = []
        self.new_images = []
        logger.start_logging()

        try:
            start_time = time.time()
            self._execute(context)
            # Finished without errors
            elapsed = time.time() - start_time
            self.report({'INFO'}, get_nice_export_report(self.exported_files, elapsed))
            if prefs.texture_bake__beep_on_finish:
                beep(pitch=3, num=1)
        finally:
            # Clean up
            while self.new_objs:
                bpy.data.objects.remove(self.new_objs.pop())
            while self.new_meshes:
                bpy.data.meshes.remove(self.new_meshes.pop())
            while self.new_materials:
                bpy.data.materials.remove(self.new_materials.pop())
            while self.new_images:
                bpy.data.images.remove(self.new_images.pop())

            load_selection(saved_selection)
            context.scene.render.engine = saved_render_engine
            context.scene.render.bake.margin = saved_render_bake_margin
            context.scene.render.bake.use_selected_to_active = saved_render_use_selected_to_active
            context.scene.cycles.samples = saved_cycles_samples
            logger.end_logging()

        return {'FINISHED'}

class GRET_OT_texture_bake_preview(bpy.types.Operator):
    """Preview this baker in the viewport. Click anywhere to stop previewing"""
    # This is a modal operator because it would be far too messy to revert the changes otherwise

    bl_idname = 'gret.texture_bake_preview'
    bl_label = "Preview Bake"
    bl_options = {'INTERNAL'}

    baker: bpy.props.EnumProperty(
        name="Source",
        description="Mask type to preview",
        items=bake_items,
    )
    scale: bpy.props.FloatProperty(
        name="Scale",
        description="Baker-specific scaling factor",
        default=1.0,
        min=0.0,
    )

    @classmethod
    def poll(cls, context):
        return context.object and context.object.active_material

    def modal(self, context, event):
        if event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'ESC', 'RET', 'SPACE'}:
            # Revert screen changes
            restore_viewports()

            # Revert scene changes
            context.scene.render.engine = self.saved_render_engine
            context.scene.cycles.preview_samples = self.saved_cycles_samples

            # Clean up
            while self.new_objs:
                bpy.data.objects.remove(self.new_objs.pop())
            while self.new_meshes:
                bpy.data.meshes.remove(self.new_meshes.pop())
            bpy.data.materials.remove(self.preview_mat)
            del self.preview_mat

            load_selection(self.saved_selection)
            del self.saved_selection

            return {'CANCELLED'}

        elif event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'MIDDLEMOUSE', 'WHEELDOWNMOUSE',
            'WHEELUPMOUSE', 'LEFT_CTRL', 'LEFT_SHIFT', 'LEFT_ALT'}:
            # Only allow navigation keys. Kind of sucks, see https://developer.blender.org/T37427
            return {'PASS_THROUGH'}

        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        scn = context.scene
        mat = context.object.active_material
        node_tree = node_trees.get(self.baker)
        if node_tree is None:
            self.report({'ERROR'}, "Select a baker type.")
            return {'CANCELLED'}

        # Collect all the objects that share this material
        self.new_objs = []
        self.new_meshes = []
        objs = get_bake_objects(context, mat, self.new_objs, self.new_meshes)

        logger.start_logging(timestamps=False)
        log(f"Previewing {self.baker} baker with {len(objs)} objects")
        logger.indent += 1

        self.saved_selection = save_selection()
        show_only(context, objs)

        self.preview_mat = preview_mat = bpy.data.materials.new(name=f"_preview_{self.baker}")
        preview_mat.use_nodes = True
        preview_mat.node_tree.nodes.clear()
        node_tree.build(preview_mat.node_tree, {'scale': self.scale})
        remap_materials(objs, mat, preview_mat)

        self.saved_render_engine, scn.render.engine = scn.render.engine, 'CYCLES'
        self.saved_cycles_samples, scn.cycles.preview_samples = scn.cycles.preview_samples, 8

        # Set all 3D views to rendered shading
        override_viewports(header_text=f"Previewing {self.baker} baker", type='RENDERED')

        logger.end_logging()

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

class GRET_OT_texture_bake_add(bpy.types.Operator):
    """Add vertex color mapping"""

    bl_idname = 'gret.texture_bake_add'
    bl_label = "Add Texture Bake"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.active_material

    def execute(self, context):
        mat = context.active_object.active_material
        bake = mat.texture_bakes.add()
        bake.uv_layer_name = prefs.texture_bake__uv_layer_name

        return {'FINISHED'}

class GRET_OT_texture_bake_clear(bpy.types.Operator):
    """Clear vertex color mapping"""

    bl_idname = 'gret.texture_bake_clear'
    bl_label = "Clear Texture Bake"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.active_material

    def execute(self, context):
        mat = context.active_object.active_material
        mat.texture_bakes.clear()

        return {'FINISHED'}

class GRET_PT_texture_bake(bpy.types.Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'
    bl_label = "Texture Bake"

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.active_material

    def draw(self, context):
        layout = self.layout
        mat = context.object.active_material

        if not mat.texture_bakes:
            layout.operator('gret.texture_bake_add', icon='ADD')
        else:
            layout.operator('gret.texture_bake_clear', icon='X')

        def draw_bake_layout(layout, bake, src_property, icon):
            scale_property = src_property + '_scale'
            row = layout.row(align=True)
            row.prop(bake, src_property, icon=icon, text="")
            sub = row.split(align=True)
            sub.prop(bake, scale_property, text="")
            sub.scale_x = 0.4
            op = row.operator('gret.texture_bake_preview', icon='HIDE_OFF', text="")
            op.baker = getattr(bake, src_property)
            op.scale = getattr(bake, scale_property)

        for bake_idx, bake in enumerate(mat.texture_bakes):
            box = layout
            col = box.column(align=True)

            # Disabled, might rework or remove in the future
            # op = row.operator('gret.quick_unwrap', icon='MOD_UVPROJECT')
            # op.index = bake_idx

            draw_bake_layout(col, bake, 'r', 'COLOR_RED')
            draw_bake_layout(col, bake, 'g', 'COLOR_GREEN')
            draw_bake_layout(col, bake, 'b', 'COLOR_BLUE')

            col.separator()

            row = col.row(align=True)
            row.prop(bake, 'uv_layer_name', icon='UV', text="")
            row.prop(bake, 'size')
            col.prop(bake, 'export_path', text="")
            op = col.operator('gret.texture_bake', icon='INDIRECT_ONLY_ON', text="Bake")
            op.index = bake_idx

class GRET_PG_texture_bake(bpy.types.PropertyGroup):
    uv_layer_name: bpy.props.StringProperty(
        name="UV Layer",
        description="Target UV layer name. Defaults can be changed in addon preferences",
        default="UVMap",
    )
    size: bpy.props.IntProperty(
        name="Texture Size",
        description="Size of the exported texture",
        default=256,
        min=8,
    )
    r: bpy.props.EnumProperty(
        name="Texture R Baker",
        description="Mask to bake into the texture's red channel",
        items=bake_items,
        default='AO',
    )
    r_scale: bpy.props.FloatProperty(
        name="Texture R Baker Scale",
        description="Baker-specific scaling factor",
        default=1.0,
        min=0.0,
    )
    g: bpy.props.EnumProperty(
        name="Texture G Baker",
        description="Mask to bake into the texture's green channel",
        items=bake_items,
        default='CURVATURE',  # Curvature in green for RGB565
    )
    g_scale: bpy.props.FloatProperty(
        name="Texture G Baker Scale",
        description="Baker-specific scaling factor",
        default=1.0,
        min=0.0,
    )
    b: bpy.props.EnumProperty(
        name="Texture B Baker",
        description="Mask to bake into the texture's blue channel",
        items=bake_items,
        default='BEVEL',
    )
    b_scale: bpy.props.FloatProperty(
        name="Texture B Baker Scale",
        description="Baker-specific scaling factor",
        default=1.0,
        min=0.0,
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
    # GRET_OT_quick_unwrap,
    GRET_OT_texture_bake,
    GRET_OT_texture_bake_add,
    GRET_OT_texture_bake_clear,
    GRET_OT_texture_bake_preview,
    GRET_PG_texture_bake,
    GRET_PT_texture_bake,
)

def register(settings, prefs):
    if not prefs.texture_bake__enable:
        return False

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Material.texture_bakes = bpy.props.CollectionProperty(
        type=GRET_PG_texture_bake,
    )

def unregister():
    del bpy.types.Material.texture_bakes

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
