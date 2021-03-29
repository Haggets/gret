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
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.StringProperty(
        name="Source",
        description="Source mesh object that the meshes were originally fit to",
    )
    destination: bpy.props.StringProperty(
        name="Destination",
        description="Modified mesh object to retarget to",
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
        description="Increase vertex sampling stride to reduce accuracy and speed up calculation",
        default=1,
        min=1,
    )
    as_shapekey: bpy.props.BoolProperty(
        name="As Shapekey",
        description="Save the result as a shape key on the mesh",
        default=False,
    )
    use_object_transform: bpy.props.BoolProperty(
        name="Object Transform",
        description="Evaluate all meshes in global space",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.selected_objects

    def execute(self, context):
        objs = context.selected_objects
        src_obj = bpy.data.objects.get(self.source)
        dst_obj = bpy.data.objects.get(self.destination)
        if not src_obj or src_obj.type != 'MESH' or not dst_obj or dst_obj.type != 'MESH':
            # Don't error here so the user can call up the props dialog
            return {'FINISHED'}
        if len(src_obj.data.vertices) != len(dst_obj.data.vertices):
            self.report({'ERROR'}, "Source and destination meshes must have equal amount of vertices.")
            return {'CANCELLED'}

        rbf_kernel = rbf_kernels.get(self.function, rbf.linear)
        src_pts = rbf.get_mesh_points(src_obj, self.use_object_transform, self.stride)
        dst_pts = rbf.get_mesh_points(dst_obj, self.use_object_transform, self.stride)
        try:
            weights = rbf.get_weight_matrix(src_pts, dst_pts, rbf_kernel, self.radius)
        except np.linalg.LinAlgError:
            # Solving for C2 kernel may throw 'SVD did not converge' sometimes
            self.report({'ERROR'}, "Failed to retarget. Try a different function or change the radius.")
            return {'CANCELLED'}

        for obj in objs:
            if obj.type != 'MESH':
                continue
            mesh_pts = rbf.get_mesh_points(obj, self.use_object_transform)
            num_mesh_pts = mesh_pts.shape[0]

            dist = rbf.get_distance_matrix(mesh_pts, src_pts, rbf_kernel, self.radius)
            identity = np.ones((num_mesh_pts, 1))
            h = np.bmat([[dist, identity, mesh_pts]])
            new_mesh_pts = np.asarray(np.dot(h, weights))
            if self.use_object_transform:
                # Result back to local space
                new_mesh_pts = np.c_[new_mesh_pts, identity]
                new_mesh_pts = np.einsum('ij,aj->ai', obj.matrix_world.inverted(), new_mesh_pts)
                new_mesh_pts = new_mesh_pts[:, :-1]

            if self.as_shapekey:
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

    def draw(self, context):
        layout = self.layout
        layout.prop_search(self, 'source', bpy.data, 'meshes', text="From")
        layout.prop_search(self, 'destination', bpy.data, 'meshes', text="To")
        layout.prop(self, 'function')
        layout.prop(self, 'radius')
        layout.prop(self, 'stride')
        layout.prop(self, 'as_shapekey')

def draw_panel(self, context):
    layout = self.layout
    settings = context.scene.gret

    col = layout.column(align=True)
    row = col.row(align=False)
    row.label(text="Retarget Mesh:")
    sub = row.row(align=True)
    sub.prop(settings, 'retarget_use_object_transform', icon='ORIENTATION_GLOBAL', text="")
    sub.prop(settings, 'retarget_show_options', icon='SETTINGS', text="")

    if settings.retarget_show_options:
        sub = col.column(align=False)
        sub.prop(settings, 'retarget_function', text="")
        sub.prop(settings, 'retarget_radius')
        sub.prop(settings, 'retarget_stride')
        sub.separator()

    row = col.row(align=True)
    row.prop(settings, 'retarget_src', text="")
    row.label(text="", icon='FORWARD')
    row.prop(settings, 'retarget_dst', text="")

    row = col.row(align=True)
    op1 = row.operator('gret.retarget_mesh', icon='CHECKMARK', text="Apply")
    op2 = row.operator('gret.retarget_mesh', icon='SHAPEKEY_DATA', text="Save")
    if settings.retarget_src and settings.retarget_dst:
        op1.source = op2.source = settings.retarget_src.name
        op1.destination = op2.destination = settings.retarget_dst.name
        op1.function = op2.function = settings.retarget_function
        op1.radius = op2.radius = settings.retarget_radius
        op1.use_object_transform = op2.use_object_transform = settings.retarget_use_object_transform
        op1.as_shapekey = False
        op2.as_shapekey = True
    else:
        row.active = False

classes = (
    GRET_OT_retarget_mesh,
)

def register(settings):
    for cls in classes:
        bpy.utils.register_class(cls)

    # Add persistent settings for mesh retargeting
    settings.add_property('retarget_src', bpy.props.PointerProperty(
        name="Mesh Retarget Source",
        description="Source mesh that the meshes were originally fit to",
        type=bpy.types.Object,
        poll=lambda self, obj: obj and obj.type == 'MESH',
    ))
    settings.add_property('retarget_dst', bpy.props.PointerProperty(
        name="Mesh Retarget Destination",
        description="Modified source mesh to retarget to",
        type=bpy.types.Object,
        poll=lambda self, obj: obj and obj.type == 'MESH' and obj != self.retarget_src and (
            not self.retarget_src or len(obj.data.vertices) == len(self.retarget_src.data.vertices))
    ))
    retarget_props = GRET_OT_retarget_mesh.__annotations__
    settings.add_property('retarget_function', retarget_props['function'])
    settings.add_property('retarget_radius', retarget_props['radius'])
    settings.add_property('retarget_stride', retarget_props['stride'])
    settings.add_property('retarget_use_object_transform', retarget_props['use_object_transform'])
    settings.add_property('retarget_show_options', bpy.props.BoolProperty(
        name="Configure",
        description="Show retargeting options",
        default=False,
    ))

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
