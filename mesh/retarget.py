import bmesh
import bpy
import numpy as np

import gret.rbf as rbf

rbf_kernels = {
    'LINEAR': rbf.linear,
    'GAUSSIAN': rbf.gaussian,
    'PLATE': rbf.thin_plate,
    'BIHARMONIC': rbf.multi_quadratic_biharmonic,
    'INV_BIHARMONIC': rbf.inv_multi_quadratic_biharmonic,
    'C2': rbf.beckert_wendland_c2_basis,
}

class GRET_OT_retarget_mesh(bpy.types.Operator):
    #tooltip
    """Retarget meshes fit on a source mesh to a modified version of the source mesh.
The meshes are expected to share topology and vertex order"""
    # Note: If vertex order gets messed up, try using an addon like Transfer Vert Order to fix it

    bl_idname = 'gret.retarget_mesh'
    bl_label = "Retarget Mesh"
    bl_options = {'INTERNAL', 'UNDO'}

    source: bpy.props.StringProperty(
        name="Source",
        description="Source mesh object that the meshes were originally fit to",
    )
    destination: bpy.props.StringProperty(
        name="Destination",
        description="Modified mesh object to retarget to",
    )
    use_shape_key: bpy.props.BoolProperty(
        name="Use Shape Key",
        description="Destination is the name of a shape key in the source mesh",
        default=False,
    )
    function: bpy.props.EnumProperty(
        items=[
            ('LINEAR', "Linear", "Linear function"),
            ('GAUSSIAN', "Gaussian", "Gaussian function"),
            ('PLATE', "Thin Plate", "Thin plate function"),
            ('BIHARMONIC', "Biharmonic", "Multi quadratic biharmonic"),
            ('INV_BIHARMONIC', "Inverse Biharmonic", "Inverse multi quadratic biharmonic"),
            ('C2', "C2", "Beckert-Wendland C2 basis"),
        ],
        name="Function",
        description="Radial basis function kernel",
        default='BIHARMONIC',  # Least prone to explode and not too slidy
    )
    radius: bpy.props.FloatProperty(
        name="Radius",
        description="Smoothing parameter for the radial basis function",
        subtype='DISTANCE',
        default=0.5,
        min=0.0,
    )
    stride: bpy.props.IntProperty(
        name="Stride",
        description="Increase vertex sampling stride to speed up calculation (reduces accuracy)",
        default=1,
        min=1,
    )
    as_shape_key: bpy.props.BoolProperty(
        name="As Shape Key",
        description="Save the result as a shape key on the mesh",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.selected_objects

    def execute(self, context):
        objs = context.selected_objects
        src_obj = bpy.data.objects.get(self.source)
        dst_is_shape_key = self.destination.startswith('s_')
        dst_obj = bpy.data.objects.get(self.destination[2:]) if not dst_is_shape_key else src_obj
        dst_shape_key = self.destination[2:] if dst_is_shape_key else None
        assert src_obj and dst_obj and src_obj.type == 'MESH' and dst_obj.type == 'MESH'

        if len(src_obj.data.vertices) != len(dst_obj.data.vertices):
            self.report({'ERROR'}, "Source and destination meshes must have equal amount of vertices.")
            return {'CANCELLED'}
        if (len(src_obj.data.vertices) // self.stride) > 5000:
            # Should stride be automatically determined?
            self.report({'ERROR'}, "With too many vertices, retargeting may take a long time or crash.\n"
                "Increase stride then try again.")
            return {'CANCELLED'}

        rbf_kernel = rbf_kernels.get(self.function, rbf.linear)
        src_pts = rbf.get_mesh_points(src_obj.data, stride=self.stride)
        dst_pts = rbf.get_mesh_points(dst_obj.data, shape_key=dst_shape_key, stride=self.stride)
        try:
            weights = rbf.get_weight_matrix(src_pts, dst_pts, rbf_kernel, self.radius)
        except np.linalg.LinAlgError:
            # Solving for C2 kernel may throw 'SVD did not converge' sometimes
            self.report({'ERROR'}, "Failed to retarget. Try a different function or change the radius.")
            return {'CANCELLED'}

        for obj in objs:
            if obj.type != 'MESH' or obj == src_obj or obj == dst_obj:
                continue
            dst_to_obj = obj.matrix_world.inverted() @ dst_obj.matrix_world
            obj_to_dst = dst_to_obj.inverted()
            mesh_pts = rbf.get_mesh_points(obj.data, matrix=obj_to_dst)
            num_mesh_pts = mesh_pts.shape[0]

            dist = rbf.get_distance_matrix(mesh_pts, src_pts, rbf_kernel, self.radius)
            identity = np.ones((num_mesh_pts, 1))
            h = np.bmat([[dist, identity, mesh_pts]])
            new_mesh_pts = np.asarray(np.dot(h, weights))
            # Result back to local space
            new_mesh_pts = np.c_[new_mesh_pts, identity]
            new_mesh_pts = np.einsum('ij,aj->ai', dst_to_obj, new_mesh_pts)
            new_mesh_pts = new_mesh_pts[:, :-1]

            if self.as_shape_key:
                # Result to new shape key
                if not obj.data.shape_keys or not obj.data.shape_keys.key_blocks:
                    obj.shape_key_add(name="Basis")
                shape_key = obj.shape_key_add(name=f"Retarget_{dst_obj.name}")
                shape_key.data.foreach_set('co', new_mesh_pts.ravel())
                shape_key.value = 1.0
            elif obj.data.shape_keys and obj.data.shape_keys.key_blocks:
                # There are shape keys, so replace the basis
                # Using bmesh propagates the change, where just setting the coordinates won't
                bm = bmesh.new()
                bm.from_mesh(obj.data)
                for vert, new_pt in zip(bm.verts, new_mesh_pts):
                    vert.co[:] = new_pt
                bm.to_mesh(obj.data)
                bm.free()
            else:
                # Set new coordinates directly
                obj.data.vertices.foreach_set('co', new_mesh_pts.ravel())
            obj.data.update()

        return {'FINISHED'}

def draw_panel(self, context):
    layout = self.layout
    settings = context.scene.gret
    obj = context.object

    col = layout.column(align=True)
    col.label(text="Retarget Mesh:")
    col.prop(settings, 'retarget_function', text="")
    row = col.row(align=True)
    row.prop(settings, 'retarget_radius')
    row.prop(settings, 'retarget_stride')
    col.separator()

    row = col.row(align=True)
    row.prop(settings, 'retarget_src', text="")
    row.label(text="", icon='FORWARD')
    row.prop(settings, 'retarget_dst', text="")

    row = col.row(align=True)
    op1 = row.operator('gret.retarget_mesh', icon='CHECKMARK', text="Retarget")
    op2 = row.operator('gret.retarget_mesh', icon='SHAPEKEY_DATA', text="As Shape Key")
    if settings.retarget_src and settings.retarget_dst != 'NONE':
        op1.source = op2.source = settings.retarget_src.name
        op1.destination = op2.destination = settings.retarget_dst
        op1.function = op2.function = settings.retarget_function
        op1.radius = op2.radius = settings.retarget_radius
        op1.stride = op2.stride = settings.retarget_stride
        op1.as_shape_key = False
        op2.as_shape_key = True
    else:
        row.enabled = False

classes = (
    GRET_OT_retarget_mesh,
)

def retarget_src_update(self, context):
    # On changing the source object, reset the destination object
    context.scene.gret.retarget_dst = 'NONE'

items = []
def retarget_dst_items(self, context):
    # Return shape keys of the source object and mesh objects with the same amount of vertices
    settings = context.scene.gret
    src_obj = settings.retarget_src

    items.clear()
    items.append(('NONE', "", ""))
    if src_obj:
        for o in context.scene.objects:
            if o.type == 'MESH' and o != src_obj and len(o.data.vertices) == len(src_obj.data.vertices):
                items.append(('o_' + o.name, o.name, f"Object '{o.name}'", 'OBJECT_DATA', len(items)))
        if src_obj.data.shape_keys:
            for sk in src_obj.data.shape_keys.key_blocks:
                items.append(('s_' + sk.name, sk.name, f"Shape Key '{sk.name}'", 'SHAPEKEY_DATA', len(items)))
    return items

def register(settings):
    for cls in classes:
        bpy.utils.register_class(cls)

    settings.add_property('retarget_src', bpy.props.PointerProperty(
        name="Mesh Retarget Source",
        description="Base mesh that the meshes are fit to",
        type=bpy.types.Object,
        poll=lambda self, obj: obj and obj.type == 'MESH',
        update=retarget_src_update,
    ))
    settings.add_property('retarget_dst', bpy.props.EnumProperty(
        name="Mesh Retarget Destination",
        description="Mesh or shape key to retarget to",
        items=retarget_dst_items,
    ))
    retarget_props = GRET_OT_retarget_mesh.__annotations__
    settings.add_property('retarget_function', retarget_props['function'])
    settings.add_property('retarget_radius', retarget_props['radius'])
    settings.add_property('retarget_stride', retarget_props['stride'])

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)