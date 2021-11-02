from itertools import dropwhile, chain
from math import pi
import bmesh
import bpy

from .helpers import new_vgroup, new_modifier, edit_mesh_elements, bmesh_vertex_group_bleed
from ..helpers import get_context, link_properties, load_selection, save_selection

class GRET_OT_graft(bpy.types.Operator):
    #tooltip
    """Connects boundaries of selected objects to the active object"""

    bl_idname = 'gret.graft'
    bl_label = "Graft"
    bl_options = {'REGISTER', 'UNDO'}

    expand: bpy.props.IntProperty(
        name="Expand",
        description="Expand the target area on the active mesh",
        default=0,
        min=0,
    )
    cuts: bpy.props.IntProperty(
        name="Number of Cuts",
        description="Number of cuts",
        default=0,
        min=0,
    )
    transfer_normals: bpy.props.BoolProperty(
        name="Transfer Normals",
        description="Transfer custom normals",
        default=True,
    )
    normal_blend_distance: bpy.props.FloatProperty(
        name="Normal Blend Distance",
        description="Blur boundary normals up to this distance",
        subtype='DISTANCE',
        default=0.0,
        min=0.0,
    )
    normal_blend_power: bpy.props.FloatProperty(
        name="Normal Blend Power",
        description="Adjust the strength of boundary normal blending",
        default=1.0,
        min=1.0,
    )
    transfer_vertex_groups: bpy.props.BoolProperty(
        name="Transfer Vertex Groups",
        description="Transfer vertex groups",
        default=True,
    )
    transfer_uv: bpy.props.BoolProperty(
        name="Transfer UVs",
        description="Transfer UV layers",
        default=False,
    )
    create_mask: bpy.props.BoolProperty(
        name="Create Mask",
        description="Create mask modifiers on the active object to hide the affected faces",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return (len(context.selected_objects) > 1
            and context.active_object
            and context.active_object.type == 'MESH'
            and context.mode == 'OBJECT')

    def _execute(self, context):
        orig_dst_obj = context.active_object
        objs = [o for o in context.selected_objects if o.type == 'MESH' and o != orig_dst_obj]

        # Get an evaluated version of the destination object
        # Can't use to_mesh because we will need to enter edit mode on it
        dg = context.evaluated_depsgraph_get()
        eval_obj = orig_dst_obj.evaluated_get(dg)
        dst_mesh = bpy.data.meshes.new_from_object(eval_obj)
        dst_obj = bpy.data.objects.new(eval_obj.name, dst_mesh)
        dst_obj.matrix_world = eval_obj.matrix_world
        context.scene.collection.objects.link(dst_obj)

        for obj in objs:
            # Initial setup
            obj_to_world = obj.matrix_world.copy()
            world_to_obj = obj.matrix_world.inverted()
            dst_to_obj = world_to_obj @ dst_obj.matrix_world
            obj_to_dst = dst_to_obj.inverted()

            boundary_vg = obj.vertex_groups.new(name="__boundary")
            bm = bmesh.new()
            bm.from_mesh(obj.data)

            # The source edge loop is currently the mesh boundary. Not doing any validation
            edges1 = [e for e in bm.edges if e.is_boundary]
            for edge in edges1:
                boundary_vg.add([edge.verts[0].index, edge.verts[1].index], 1.0, 'REPLACE')

            if not edges1:
                bm.free()
                bpy.data.objects.remove(dst_obj)
                bpy.data.meshes.remove(dst_mesh)
                self.report({'ERROR'}, f"The object must have an open boundary.")
                return

            # Push the boundary into the destination mesh and get the boolean intersection
            # Use fast since exact solver demands the object is manifold. Might need to close holes
            saved_active_modifiers = []
            for mod in obj.modifiers:
                if mod.show_viewport:
                    mod.show_viewport = False
                    saved_active_modifiers.append(mod)
            wrap_mod = obj.modifiers.new(type='SHRINKWRAP', name="")
            wrap_mod.wrap_method = 'TARGET_PROJECT' # 'NEAREST_SURFACEPOINT'
            wrap_mod.wrap_mode = 'INSIDE'
            wrap_mod.target = dst_obj
            wrap_mod.vertex_group = boundary_vg.name
            wrap_mod.offset = 0.01
            bool_mod = obj.modifiers.new(type='BOOLEAN', name="")
            bool_mod.operation = 'INTERSECT'
            bool_mod.solver = 'FAST'
            bool_mod.object = dst_obj
            dg = context.evaluated_depsgraph_get()
            bool_bm = bmesh.new()
            bool_bm.from_object(obj, dg)
            obj.modifiers.remove(bool_mod)
            obj.modifiers.remove(wrap_mod)

            # Because the result of the boolean operation mostly matches the destination geometry,
            # all that's needed is finding those same faces in the original mesh
            intersecting_face_indices = []
            for face in bool_bm.faces:
                p = obj_to_dst @ face.calc_center_median()
                result, closest_point, normal, face_idx = dst_obj.closest_point_on_mesh(p)
                if result:
                    if (dst_mesh.polygons[face_idx].center - p).length_squared <= 0.05:
                        intersecting_face_indices.append(face_idx)

            while saved_active_modifiers:
                saved_active_modifiers.pop().show_viewport = True
            bool_bm.free()

            if not intersecting_face_indices:
                bm.free()
                bpy.data.objects.remove(dst_obj)
                bpy.data.meshes.remove(dst_mesh)
                self.report({'ERROR'}, f"No intersection found between the objects.")
                return

            # The target edge loop is the boundary of the intersection. Recreate it in working bmesh
            edit_mesh_elements(dst_obj, 'FACE', intersecting_face_indices)
            for _ in range(self.expand):
                bpy.ops.mesh.select_more()
            bpy.ops.object.editmode_toggle()
            intersecting_vert_indices = [v.index for v in dst_mesh.vertices if v.select]
            bpy.ops.object.editmode_toggle()
            bpy.ops.mesh.region_to_loop()
            bpy.ops.object.editmode_toggle()
            idx_to_bmvert = {v.index: bm.verts.new(dst_to_obj @ v.co)
                for v in dst_mesh.vertices if v.select}
            bm.verts.index_update()
            edges2 = [bm.edges.new((idx_to_bmvert[e.vertices[0]], idx_to_bmvert[e.vertices[1]]))
                for e in dst_mesh.edges if e.select]
            bm.edges.index_update()
            fm_layer = bm.faces.layers.face_map.verify()

            try:
                ret = bmesh.ops.bridge_loops(bm, edges=edges1+edges2, use_pairs=False,
                    use_cyclic=False, use_merge=False, merge_factor=0.5, twist_offset=0)
                new_faces = ret['faces']
                if self.cuts:
                    ret = bmesh.ops.subdivide_edges(bm, edges=ret['edges'], smooth=1.0,
                        smooth_falloff='LINEAR', cuts=self.cuts)
                    new_faces = list(dropwhile(lambda el: not isinstance(el, bmesh.types.BMFace),
                        ret['geom']))
            except RuntimeError:
                bm.free()
                bpy.data.objects.remove(dst_obj)
                bpy.data.meshes.remove(dst_mesh)
                self.report({'ERROR'}, f"Couldn't bridge edge loops.")
                return

            face_map = obj.face_maps.get('graft') or obj.face_maps.new(name='graft')
            for face in new_faces:
                face.smooth = True
                face[fm_layer] = face_map.index

            # Begin transferring data from the destination mesh
            deform_layer = bm.verts.layers.deform.verify()
            for edge in bm.edges:
                if edge.is_boundary:
                    for vert in edge.verts:
                        vert[deform_layer][boundary_vg.index] = 1.0
            if self.transfer_normals:
                bmesh_vertex_group_bleed(bm, boundary_vg.index,
                    distance=self.normal_blend_distance,
                    power=self.normal_blend_power)

            # Apply the result
            bm.to_mesh(obj.data)
            bm.free()

            ctx = get_context(obj)
            if self.transfer_normals:
                mod = new_modifier(obj, type='DATA_TRANSFER', at_top=True)
                mod.object = dst_obj
                mod.vertex_group = boundary_vg.name
                mod.use_object_transform = True
                mod.use_loop_data = True
                mod.data_types_loops = {'CUSTOM_NORMAL'}
                mod.loop_mapping = 'POLYINTERP_NEAREST'
                obj.data.use_auto_smooth = True
                obj.data.auto_smooth_angle = pi
                bpy.ops.mesh.customdata_custom_splitnormals_clear(ctx)
                bpy.ops.object.modifier_apply(ctx, modifier=mod.name)

            if self.transfer_vertex_groups or self.transfer_uv:
                mod = new_modifier(obj, type='DATA_TRANSFER', at_top=True)
                mod.object = dst_obj
                mod.use_object_transform = True
                if self.transfer_vertex_groups:
                    mod.use_vert_data = True
                    mod.data_types_verts = {'VGROUP_WEIGHTS'}
                    mod.vert_mapping = 'EDGEINTERP_NEAREST'
                if self.transfer_uv:
                    mod.use_loop_data = True
                    mod.data_types_loops = {'UV'}  # Automatically turns on use_poly_data
                    mod.loop_mapping = 'POLYINTERP_NEAREST'
                bpy.ops.object.datalayout_transfer(ctx, modifier=mod.name)
                bpy.ops.object.modifier_apply(ctx, modifier=mod.name)

            # If requested, create a mask modifier that will hide the intersection's inner verts
            if self.create_mask:
                mask_vg = new_vgroup(orig_dst_obj, f"_mask_{obj.name}")
                intersecting_verts = (dst_mesh.vertices[i] for i in intersecting_vert_indices)
                mask_vg.add([v.index for v in intersecting_verts if not v.select], 1.0, 'REPLACE')
                mask_mod = new_modifier(orig_dst_obj, type='MASK', name=mask_vg.name, at_top=True)
                mask_mod.vertex_group = mask_vg.name
                mask_mod.invert_vertex_group = True
                mod_dp = f'modifiers["{mask_mod.name}"]'
                # Can't create a hide_viewport driver for reasons
                link_properties(obj, 'hide_render', orig_dst_obj, mod_dp + '.show_render', invert=True)

        obj.vertex_groups.remove(boundary_vg)
        bpy.data.objects.remove(dst_obj)
        bpy.data.meshes.remove(dst_mesh)
        return {'FINISHED'}

    def execute(self, context):
        saved_selection = save_selection()

        try:
            self._execute(context)
        finally:
            # Clean up
            load_selection(saved_selection)

        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout

        layout.prop(self, 'expand')
        layout.prop(self, 'cuts')
        layout.prop(self, 'create_mask')

        layout.separator()
        layout.label(text="Transfer:")
        split = layout.split(factor=0.35)
        col = split.column()
        col.prop(self, 'transfer_normals', text="Normals")
        col.prop(self, 'transfer_vertex_groups', text="Vertex Groups")
        col.prop(self, 'transfer_uv', text="UVs")
        col = split.column()

        sub = col.split()
        sub.enabled = self.transfer_normals
        row = sub.row(align=True)
        row.prop(self, 'normal_blend_distance', text="Dist.")
        row.prop(self, 'normal_blend_power', text="Power")

def draw_panel(self, context):
    layout = self.layout

    col = layout.column(align=True)
    col.operator('gret.graft', icon='AUTOMERGE_ON')

def register(settings):
    bpy.utils.register_class(GRET_OT_graft)

def unregister():
    bpy.utils.unregister_class(GRET_OT_graft)
