bl_info = {
    'name': "gret",
    'author': "greisane",
    'description': "",
    'version': (0, 3, 0),
    'blender': (3, 1, 0),
    'location': "3D View > Tools",
    'category': "Object"
}

from bpy.app.handlers import persistent
from collections import defaultdict
import bpy
import importlib
import sys

from .log import log, logd, logger
# logger.categories.add("DEBUG")

# Names here will be accessible as imports from other modules
class AddonPreferencesWrapper:
    def __getattr__(self, attr):
        return getattr(bpy.context.preferences.addons[__package__].preferences, attr)
prefs = AddonPreferencesWrapper()

def import_or_reload_modules(module_names, package_name):
    ensure_starts_with = lambda s, prefix: s if s.startswith(prefix) else prefix + s
    module_names = [ensure_starts_with(name, f'{package_name}.') for name in module_names]
    modules = []
    for module_name in module_names:
        logd(f"Importing module {module_name}")
        module = sys.modules.get(module_name)
        if module:
            module = importlib.reload(module)
        else:
            module = globals()[module_name] = importlib.import_module(module_name)
        modules.append(module)
    return modules

def register_submodules(modules, settings, draw_funcs=[]):
    registered_modules = []
    for module in modules:
        if hasattr(module, 'register'):
            logd(f"Registering module {module.__name__}")
            # Explicitly check for False to avoid having to return True every time
            if module.register(settings, prefs) != False:
                registered_modules.append(module)
                if hasattr(module, 'draw_panel'):
                    draw_funcs.append(module.draw_panel)
    return registered_modules

def unregister_submodules(modules, draw_funcs=[]):
    for module in reversed(modules):
        if hasattr(module, 'unregister'):
            logd(f"Unregistering module {module.__name__}")
            module.unregister()
    draw_funcs.clear()
    modules.clear()

module_names = [
    'helpers',
    'math',
    'drawing',
    'operator',
    'patcher',
    'rbf',
    # Submodules
    'file',
    'material',
    'mesh',
    'rig',
    'uv',  # Depends on material
    'anim',  # Depends on rig
    'jobs',  # Depends on mesh, rig
]
modules = import_or_reload_modules(module_names, __name__)
registered_modules = []

def prefs_updated(self, context):
    for module in registered_modules:
        for submodule in getattr(module, "registered_modules", []):
            if hasattr(submodule, "on_prefs_updated"):
                submodule.on_prefs_updated()

needs_restart = False
def registered_updated(self, context):
    global needs_restart
    needs_restart = True

def debug_updated(self, context):
    if prefs.debug:
        logger.categories.add("DEBUG")
    else:
        logger.categories.discard("DEBUG")

class GretAddonPreferences(bpy.types.AddonPreferences):
    # This must match the addon name, use '__package__'
    # when defining this in a submodule of a python package.
    bl_idname = __name__

    jobs__panel_enable: bpy.props.BoolProperty(
        name="Enable Panel",
        description="Show the export jobs panel",
        default=False,
    )
    use_beeps: bpy.props.BoolProperty(
        name="Beep",
        description="Play a beep sound after an export job or texture bake finishes",
        default=False,
    )
    texture_bake__enable: bpy.props.BoolProperty(
        name="Enable",
        description="Enable this feature",
        default=True,
        update=registered_updated,
    )
    texture_bake__uv_layer_name: bpy.props.StringProperty(
        name="UV Layer",
        description="Name of the default UV layer for texture bakes",
        default="UVMap",
    )
    uv_paint__layer_name: bpy.props.StringProperty(
        name="UV Layer",
        description="Default UV layer to paint to. Leave empty to use the active UV layer",
        default="",
    )
    uv_paint__picker_show_info: bpy.props.BoolProperty(
        name="Show UV Picker Info",
        description="Display information when hovering the UV picker",
        default=True,
    )
    uv_paint__picker_copy_color: bpy.props.BoolProperty(
        name="Clicking UV Picker Copies Color",
        description="Copy image color from the UV picker to the clipboard on click",
        default=False,
    )
    actions__register_pose_blender: bpy.props.BoolProperty(
        name="Enable Pose Blender",
        description="""Allows blending poses together, similar to the UE4 AnimGraph node.
NEEDS UPDATING TO 3.0""",
        default=False,
        update=registered_updated,
    )
    actions__show_frame_range: bpy.props.BoolProperty(
        name="Show Frame Range",
        description="Show custom frame range controls in the action panel",
        default=True,
    )
    actions__sync_frame_range: bpy.props.BoolProperty(
        name="Sync Frame Range",
        description="Keep preview range in sync with the action's custom frame range",
        default=True,
        update=prefs_updated,
    )
    rig__register_autoname_bone_chain: bpy.props.BoolProperty(
        name="Register \"Auto-Name Bone Chain\"",
        description="Automatically renames a chain of bones starting at the selected bone",
        default=True,
        update=registered_updated,
    )
    debug: bpy.props.BoolProperty(
        name="Debug Mode",
        description="Enables verbose output",
        default=False,
        update=debug_updated,
    )
    categories = None

    def draw(self, context):
        layout = self.layout

        if not self.categories:
            # Cache grouped props by category (the part left of the double underscore "__")
            from .helpers import titlecase
            d = defaultdict(list)
            for prop_name in self.__annotations__:
                cpos = prop_name.find("__")
                category_name = titlecase(prop_name[:cpos]) if cpos > 0 else "Miscellaneous"
                d[category_name].append(prop_name)
            self.categories = [(k, sorted(d[k])) for k in sorted(d.keys())]

        if needs_restart:
            alert_row = layout.row()
            alert_row.alert = True
            alert_row.operator("gret.save_userpref_and_quit_blender", icon='ERROR',
                text="Blender restart is required")

        # Display properties in two columns of boxes side to side
        # Avoiding use_property_split because the indent is too big
        split = layout.split(factor=0.5)
        boxes = split.column(align=True)
        boxes2 = split.column(align=True)
        for category_name, prop_names in self.categories:
            box = boxes.box()
            # box = box.column(align=True)
            box.label(text=category_name + ":", icon='DOT')
            split = box.split(factor=0.05)
            split.separator()
            col = split.column(align=True)
            for prop_name in prop_names:
                col.prop(self, prop_name)
            col.separator()
            boxes, boxes2 = boxes2, boxes

class GRET_PG_settings(bpy.types.PropertyGroup):
    @classmethod
    def add_property(cls, name, annotation):
        if not hasattr(cls, '__annotations__'):
            cls.__annotations__ = {}
        cls.__annotations__[name] = annotation

class GRET_OT_save_userpref_and_quit_blender(bpy.types.Operator):
    #tooltip
    """Make the current preferences default then quit blender"""

    bl_idname = 'gret.save_userpref_and_quit_blender'
    bl_label = "Save Preferences and Quit"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        bpy.ops.wm.save_userpref()
        bpy.ops.wm.quit_blender()

        return {'FINISHED'}

@persistent
def load_post(_):
    prefs_updated(bpy.context.preferences.addons[__package__].preferences, bpy.context)

def register():
    # Register prefs first so that modules can access them through gret.prefs
    bpy.utils.register_class(GretAddonPreferences)
    if prefs.debug:
        logger.categories.add("DEBUG")
    else:
        logger.categories.discard("DEBUG")

    # Each module adds its own settings to the main group via add_property()
    global registered_modules
    registered_modules = register_submodules(modules, GRET_PG_settings)

    bpy.utils.register_class(GRET_PG_settings)
    bpy.utils.register_class(GRET_OT_save_userpref_and_quit_blender)

    bpy.types.Scene.gret = bpy.props.PointerProperty(type=GRET_PG_settings)
    bpy.app.handlers.load_post.append(load_post)

def unregister():
    bpy.app.handlers.load_post.remove(load_post)
    del bpy.types.Scene.gret

    bpy.utils.unregister_class(GRET_OT_save_userpref_and_quit_blender)
    bpy.utils.unregister_class(GRET_PG_settings)

    unregister_submodules(registered_modules)

    bpy.utils.unregister_class(GretAddonPreferences)

if __name__ == '__main__':
    register()
