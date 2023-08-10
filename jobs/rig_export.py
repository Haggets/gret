from collections import namedtuple, defaultdict
from itertools import chain
from math import pi
import bpy
import os
import re
import shlex
import time

from .. import prefs
from .helpers import clone_obj
from ..helpers import (
    beep,
    fail_if_invalid_export_path,
    get_context,
    get_export_path,
    get_modifier,
    get_name_safe,
    get_nice_export_report,
    get_bid_filepath,
    gret_operator_exists,
    load_properties,
    load_selection,
    save_properties,
    save_selection,
    split_sequence,
    TempModifier,
    viewport_reveal_all,
)
from ..mesh.helpers import (
    apply_modifiers,
    apply_shape_keys_with_vertex_groups,
    delete_faces_with_no_material,
    edit_face_map_elements,
    encode_shape_keys,
    get_operator_target_vertex_groups,
    merge_islands,
    merge_shape_keys_pattern,
    mirror_shape_keys,
    remove_shape_keys,
    unsubdivide_preserve_uvs,
)
from ..mesh.vertex_color_mapping import get_first_mapping
from ..log import logger, log, logd
from ..rig.helpers import (
    copy_drivers,
    export_autorig,
    export_autorig_universal,
    export_fbx,
    is_object_arp,
    is_object_arp_humanoid,
)

def sanitize_mesh(obj):
    # Ensure basis is selected
    obj.active_shape_key_index = 0
    obj.show_only_shape_key = False

    # After messing with shape keys, basis may be left in an undesirable state
    # Not sure why, and data.update() doesn't seem to fix it
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.object.mode_set(mode='OBJECT')

    # Delete drivers made invalid by deleted modifiers and so on
    if obj.animation_data:
        for fc in obj.animation_data.drivers[:]:
            if not fc.driver.is_valid:
                logd(f"Removing invalid driver {fc.data_path}")
                obj.animation_data.drivers.remove(fc)

    # Prefer no shape keys at all if only basis is left
    if obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) == 1:
        obj.shape_key_clear()

def _rig_export(self, context, job, rig):
    rig_filepath = get_bid_filepath(rig)
    rig_basename = os.path.splitext(bpy.path.basename(rig_filepath))[0]
    rig.data.pose_position = 'REST'

    # Find and clone objects to be exported
    # Original objects that aren't exported will be hidden for render, only for driver purposes
    export_objs, job_cls = job.get_export_objects(context)

    ExportItem = namedtuple('ExportItem', 'original obj job_collection subd_level')
    items = []
    groups = defaultdict(list)  # Filepath to list of ExportItems
    for obj in context.scene.objects:
        obj.hide_render = True
    for obj, job_cl in zip(export_objs, job_cls):
        obj.hide_render = False
        new_obj = clone_obj(context, obj, parent=rig, convert_to_mesh=False)
        if new_obj:
            self.new_objs.add(new_obj)
            self.new_meshes.add(new_obj.data)
            items.append(ExportItem(obj, new_obj, job_cl, job_cl.subdivision_levels))

    # Process individual meshes
    for item in items:
        log(f"Processing {item.original.name}")
        obj = item.obj
        job_cl = item.job_collection
        ctx = get_context(obj)
        logger.indent += 1

        # Simplify now if specified in job collection. Subdivision is handled after merging
        if item.subd_level < 0:
            unsubdivide_preserve_uvs(obj, -item.subd_level)
            log(f"Unsubdivided {-item.subd_level} times")
            item.subd_level = 0

        # Ensure mesh has custom normals so that they won't be recalculated on masking
        bpy.ops.mesh.customdata_custom_splitnormals_add(ctx)
        obj.data.use_auto_smooth = True
        obj.data.auto_smooth_angle = pi

        # Remove vertex group filtering from shapekeys before merging
        apply_shape_keys_with_vertex_groups(obj)

        if job.merge_basis_shape_keys:
            for shape_key_pattern in shlex.split(job.basis_shape_key_pattern):
                merge_shape_keys_pattern(obj, shape_key_pattern)

        # Don't export muted shape keys
        if obj.data.shape_keys and obj.data.shape_keys.key_blocks:
            for sk in obj.data.shape_keys.key_blocks:
                if sk.mute:
                    obj.shape_key_remove(sk)

        if job.mirror_shape_keys:
            # Create shape keys with L/R vertex group masking, to be applied after mirroring
            mirror_shape_keys(obj, job.side_vgroup_name)

        apply_modifiers(obj, should_apply_modifier=job.should_apply_modifier)

        if job.mirror_shape_keys:
            apply_shape_keys_with_vertex_groups(obj)

        # Remap materials, any objects or faces with no material won't be exported
        all_none = lambda iterable: all(not el for el in iterable)

        remapped_to_none = False
        for remap in job.remap_materials:
            if remap.source:
                for mat_idx, mat in enumerate(obj.data.materials):
                    if mat and mat == remap.source:
                        log(f"Remapped material {mat.name} to {get_name_safe(remap.destination)}")
                        obj.data.materials[mat_idx] = remap.destination
                        remapped_to_none = remapped_to_none or not remap.destination
            elif remap.destination and all_none(obj.data.materials):
                log(f"Added material {get_name_safe(remap.destination)}")
                obj.data.materials.append(remap.destination)

        if all_none(obj.data.materials):
            log(f"Object has no materials and won't be exported")
            logger.indent -= 1
            continue

        if remapped_to_none:
            delete_faces_with_no_material(obj)
            if not obj.data.polygons:
                log(f"Object has no faces and won't be exported")
                logger.indent -= 1
                continue

        # Holes in the material list tend to mess everything up on joining objects
        # Note this is not the same as bpy.ops.object.material_slot_remove_unused
        for mat_idx in range(len(obj.data.materials) - 1, -1, -1):
            if not obj.data.materials[mat_idx]:
                logd(f"Popped empty material #{mat_idx}")
                obj.data.materials.pop(index=mat_idx)

        # If set, ensure prefix for exported materials
        if job.material_name_prefix:
            for mat_slot in obj.material_slots:
                mat = mat_slot.material
                if mat and not mat.name.startswith(job.material_name_prefix):
                    self.saved_material_names[mat] = mat.name
                    mat.name = job.material_name_prefix + mat.name

        # Bake and clear vertex color mappings before merging
        if get_first_mapping(obj):
            if not obj.data.vertex_colors:
                log("Baking vertex color mappings")
                bpy.ops.gret.vertex_color_mapping_refresh(ctx, invert=job.invert_vertex_color_mappings)
            bpy.ops.gret.vertex_color_mapping_clear(ctx)

        if job.ensure_vertex_color and not obj.data.vertex_colors:
            log("Created default vertex color layer")
            vcol = obj.data.vertex_colors.new()
            for loop in vcol.data:
                loop.color = job.default_vertex_color

        # Ensure vertex color layers share a single name so they merge correctly
        default_vcol_name = "Col"
        if len(obj.data.vertex_colors) > 1:
            log(f"More than one vertex color layer, is this intended?",
                ", ".join(vcol.name for vcol in obj.data.vertex_colors))
        elif obj.data.vertex_colors.active and obj.data.vertex_colors.active.name != default_vcol_name:
            logd(f"Renamed vertex color layer {obj.data.vertex_colors.active.name} to {default_name}")
            obj.data.vertex_colors.active.name = default_name

        # Ensure proper mesh state
        sanitize_mesh(obj)
        if gret_operator_exists('vertex_group_remove_unused'):
            bpy.ops.gret.vertex_group_remove_unused(ctx)
        obj.data.transform(obj.matrix_basis, shape_keys=True)
        obj.matrix_basis.identity()

        # Put the objects in a group
        path_fields = {
            'job': job.name,
            'scene': context.scene.name,
            'rigfile': rig_basename,
            'rig': rig.name.removesuffix('_rig'),
            'object': item.original.name,
            'collection': job_cl.get_collection(context).name,
        }
        filepath = None if job.to_collection else get_export_path(job.rig_export_path, path_fields)
        groups[filepath].append(item)
        logger.indent -= 1
    del items  # These objects might become invalid soon

    def merge_items(items):
        if len(items) <= 1:
            return items[0]

        # Pick the object that all others will be merged into
        # First choice should be the character's body, otherwise pick the densest mesh
        merged_item = next((it for it in items if it.original.name.lower() == "body"), None)
        if merged_item is None:
            merged_item = max(items, key=lambda it: len(it.obj.data.vertices))

        # TODO this sucks
        for obj in (it.obj for it in items if it is not merged_item):
            self.new_objs.discard(obj)
            self.new_meshes.discard(obj.data)
        obj = merged_item.obj
        objs = [item.obj for item in items]
        ctx = get_context(active_obj=obj, selected_objs=objs)
        bpy.ops.object.join(ctx)
        del objs

        log(f"Merged {', '.join(it.original.name for it in items if it is not merged_item)} "
            f"into {merged_item.original.name}")

        num_verts_merged = merge_islands(obj, mode=job.weld_mode, threshold=job.weld_distance)
        if num_verts_merged > 0:
            log(f"Welded {num_verts_merged} vertices")

        items[:] = [merged_item]
        return merged_item

    # Process groups. Meshes in each group are merged together
    for filepath, group_items in sorted(groups.items()):
        log(f"Processing {bpy.path.basename(filepath) if filepath else 'unnamed group'}")
        logger.indent += 1

        items = group_items[:]
        while len(items) > 1:
            # Merge items with the same requested subdiv level. Repeat until there's one item left
            max_subd_level = max(it.subd_level for it in items)
            items_to_merge, items = split_sequence(items, lambda it: it.subd_level == max_subd_level)
            item = merge_items(items_to_merge)
            items.append(item)

            if item.subd_level > 0:
                ctx = get_context(item.obj)
                # Meshes can deform unpredictably if weights weren't normalized before subdivision
                # bpy.ops.object.vertex_group_normalize_all(ctx,
                    # group_select_mode='BONE_DEFORM', lock_active=False)
                with TempModifier(item.obj, type='SUBSURF') as subd_mod:
                    subd_mod.levels = item.subd_level
                    subd_mod.use_creases = True
                    subd_mod.use_custom_normals = True
                log(f"Subdivided {item.original.name} {item.subd_level} times")
                item.subd_level = 0

        # Joining objects loses drivers, restore them
        for item in group_items:
            copy_drivers(item.original.data.shape_keys, items[0].obj.data.shape_keys)

        group_items[:] = items
        logger.indent -= 1

    # Post-process
    for item in chain.from_iterable(groups.values()):
        log(f"Post-processing mesh {item.original.name}")
        obj = item.obj
        ctx = get_context(obj)
        logger.indent += 1

        if job.subdivide_faces and obj.face_maps and obj.data.face_maps:
            for face_map_name in shlex.split(job.subdivide_face_map_names):
                if face_map_name in obj.face_maps:
                    num_selected = edit_face_map_elements(obj, face_map_name)
                    if num_selected:
                        log(f"Subdividing face map {face_map_name} ({num_selected} faces)")
                        bpy.ops.gret.cut_faces_smooth(ctx)
                    bpy.ops.object.editmode_toggle()

        if job.encode_shape_keys:
            encode_shape_keys(obj, "*_UV")
        else:
            remove_shape_keys(obj, "*_UV")

        # Assign to rig. Parenting is not strictly necessary, only ARP wants it that way
        rig_mod = get_modifier(obj, type='ARMATURE', name="rig")
        rig_mod.object = rig
        rig_mod.show_expanded = False
        obj.parent = rig

        if prefs.jobs__limit_vertex_weights > 0:
            log(f"Limiting vertex weights to {prefs.jobs__limit_vertex_weights}")
            bpy.ops.object.vertex_group_limit_total(ctx, group_select_mode='BONE_DEFORM',
                limit=prefs.jobs__limit_vertex_weights)

        # Ensure proper mesh state
        sanitize_mesh(obj)
        logger.indent -= 1

    if job.to_collection:
        # Keep new objects in the target collection
        objs = [item.obj for item in chain.from_iterable(groups.values())]
        old_objs = {}

        if job.clean_collection:
            log(f"Cleaning target collection")
            for obj in job.export_collection.objects:
                old_objs[obj.name] = obj
                obj.name += "_"
                obj.data.name += "_"
                job.export_collection.objects.unlink(obj)

        for obj in objs:
            if len(objs) == 1:
                # If producing a single object, rename it to match the collection
                obj.name = job.export_collection.name
                obj.data.name = job.export_collection.name

            old_obj = old_objs.get(obj.name)
            if old_obj:
                old_data = old_obj.data
                logd(f"Remap object {old_obj.name} -> {obj.name} ({old_obj.users} users)")
                logd(f"Remap data {old_data.name} -> {obj.data.name} ({old_data.users} users)")
                old_obj.data.user_remap(obj.data)
                old_obj.user_remap(obj)
                old_obj.data = old_data  # Revert remap for the old object

            job.export_collection.objects.link(obj)
            context.scene.collection.objects.unlink(obj)
            apply_shape_keys_with_vertex_groups(obj)

            # Remove superfluous data
            if hasattr(obj.data, 'shape_key_storage'):
                obj.data.shape_key_storage.clear()
            clean_vertex_groups = False
            if clean_vertex_groups:
                for vg_index in reversed(get_operator_target_vertex_groups(obj, 'BONE_NOT_DEFORM')):
                    obj.vertex_groups.remove(obj.vertex_groups[vg_index])

            # Auto-smooth has a noticeable impact in performance while animating,
            # disable unless the user explicitly enabled it back in the previous build result
            obj.data.use_auto_smooth = False
            if old_obj and old_obj.type == 'MESH':
                obj.data.use_auto_smooth = old_obj.data.use_auto_smooth

            # Don't delete this
            self.new_objs.discard(obj)
            self.new_meshes.discard(obj.data)

        for old_obj in old_objs.values():
            old_data = obj.data
            bpy.data.objects.remove(old_obj)
            if old_data.users == 0 and isinstance(old_data, bpy.types.Mesh):
                bpy.data.meshes.remove(old_data)
    else:
        # Finally export
        for filepath, items in groups.items():
            filename = bpy.path.basename(filepath)
            objs = [item.obj for item in items]

            if is_object_arp_humanoid(rig):
                log(f"Exporting {filename} via Auto-Rig export")
                exporter = export_autorig
            elif is_object_arp(rig):
                log(f"Exporting {filename} via Auto-Rig export (universal)")
                exporter = export_autorig_universal
            else:
                log(f"Exporting {filename}")
                exporter = export_fbx
            logger.indent += 1

            logd(f"{len(objs)} object{'s' if len(objs) > 1 else ''} in group")
            materials_used = set(chain.from_iterable(obj.data.materials for obj in objs
                if obj.type == 'MESH'))
            log(f"Materials used: {', '.join(sorted(mat.name for mat in materials_used))}")

            options = {
                'minimize_bones': job.minimize_bones,
                'remove_bones': shlex.split(job.remove_bone_names) if job.remove_bones else [],
            }
            result = exporter(filepath, context, rig, objects=objs, options=options)

            if result == {'FINISHED'}:
                self.exported_files.append(filepath)
            else:
                log("Failed to export!")
            logger.indent -= 1

def rig_export(self, context, job):
    assert job.what == 'RIG'
    rig = job.rig

    # Validate job settings
    if not rig or rig.type != 'ARMATURE':
        self.report({'ERROR'}, "No armature selected.")
        return {'CANCELLED'}
    if job.to_collection and not job.export_collection:
        self.report({'ERROR'}, "No collection selected to export to.")
        return {'CANCELLED'}

    rig_in_scene = rig.name in context.view_layer.objects
    if not rig_in_scene:
        # Workaround for ARP hating it when rigs are present in multiple scenes...
        context.scene.collection.objects.link(rig)
    context.view_layer.objects.active = rig

    # Check addon availability and export path
    try:
        if not job.to_collection:
            field_names = ['job', 'scene', 'rigfile', 'rig', 'object', 'collection']
            fail_if_invalid_export_path(job.rig_export_path, field_names)
    except Exception as e:
        self.report({'ERROR'}, str(e))
        return {'CANCELLED'}

    # Save state of various things and keep track of new objects that should be deleted afterwards
    saved_selection = save_selection()
    viewport_reveal_all()
    assert rig.visible_get()
    saved_pose_position = rig.data.pose_position
    if hasattr(rig, 'pose_blender'):
        # ARP chokes when pose blender is enabled, regular export should be fine
        saved_pose_blender_enable = rig.pose_blender.enabled
        rig.pose_blender.enabled = False
    saved_bone_deform = {b: b.use_deform for b in rig.data.bones}
    self.exported_files = []
    self.new_objs = set()
    self.new_meshes = set()
    self.saved_material_names = {}
    logger.start_logging()
    log(f"Beginning rig export job '{job.name}'")

    try:
        start_time = time.time()
        _rig_export(self, context, job, rig)
        # Finished without errors
        elapsed = time.time() - start_time
        self.report({'INFO'}, get_nice_export_report(self.exported_files, elapsed))
        log("Job complete")
        if prefs.jobs__beep_on_finish:
            beep(pitch=0)
    finally:
        # Restore state and clean up
        while self.new_objs:
            bpy.data.objects.remove(self.new_objs.pop())
        while self.new_meshes:
            bpy.data.meshes.remove(self.new_meshes.pop())
        for mat, name in self.saved_material_names.items():
            mat.name = name
        del self.saved_material_names
        for bone, use_deform in saved_bone_deform.items():
            bone.use_deform = use_deform

        rig.data.pose_position = saved_pose_position
        if hasattr(rig, 'pose_blender'):
            rig.pose_blender.enabled = saved_pose_blender_enable
        load_selection(saved_selection)
        job.log = logger.end_logging()

    if not rig_in_scene:
        context.scene.collection.objects.unlink(rig)

    if job.to_collection:
        # Scene has new objects
        bpy.ops.ed.undo_push()

    return {'FINISHED'}
