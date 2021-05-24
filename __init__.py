bl_info = {
    'name': "gret",
    'author': "greisane",
    'description': "",
    'version': (0, 0, 1),
    'blender': (2, 92, 0),
    'location': "3D View > Tools",
    'category': "Object"
}

from bpy.app.handlers import persistent
import bpy
import importlib
import sys

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
        module = sys.modules.get(module_name)
        if module:
            module = importlib.reload(module)
        else:
            module = globals()[module_name] = importlib.import_module(module_name)
        modules.append(module)
    return modules

module_names = [
    'log',
    'helpers',
    'math',
    'rbf',
    # 'stringcase',  # Third party, no need to register or reload
    'file',
    'material',
    'mesh',
    'rig',
    'uv',
    'anim',  # Depends on rig
    'jobs',  # Depends on mesh, rig
]
modules = import_or_reload_modules(module_names, __name__)

class GretAddonPreferences(bpy.types.AddonPreferences):
    # This must match the addon name, use '__package__'
    # when defining this in a submodule of a python package.
    bl_idname = __name__

    jobs_panel_enable: bpy.props.BoolProperty(
        name="Jobs Panel",
        description="Show the export jobs panel",
        default=False,
    )
    texture_bake_uv_layer_name: bpy.props.StringProperty(
        name="Default UV Layer",
        description="Name of the default UV layer for texture bakes",
        default="UVMap",
    )
    tileset_uv_layer_name: bpy.props.StringProperty(
        name="Default UV Layer",
        description="Name of the default UV layer for tileset paint",
        default="UVMap",
    )
    auto_hide_arp_enable: bpy.props.BoolProperty(
        name="Auto Hide ARP",
        description="Automatically hide the ARP armature collection on saving",
        default=False,
    )
    backwards_compat: bpy.props.BoolProperty(
        name="Backwards Compatibility",
        description="Restore settings from when the addon was called my_tools",
        default=False,
    )
    debug: bpy.props.BoolProperty(
        name="Debug Mode",
        description="Enables verbose output",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        col.prop(self, 'jobs_panel_enable')
        col.prop(self, 'backwards_compat')
        col.prop(self, 'auto_hide_arp_enable')
        col.prop(self, 'debug')
        col.separator()

        col.label(text='Texture Bake:')
        col.prop(self, 'texture_bake_uv_layer_name')
        col.separator()

        col.label(text='Tile Paint:')
        col.prop(self, 'tileset_uv_layer_name')
        col.separator()

class GRET_PG_settings(bpy.types.PropertyGroup):
    @classmethod
    def add_property(cls, name, annotation):
        if not hasattr(cls, '__annotations__'):
            cls.__annotations__ = {}
        cls.__annotations__[name] = annotation

# Backwards compatibility if enabled in preferences
@persistent
def load_pre(dummy):
    bpy.types.Scene.my_tools = bpy.props.PointerProperty(type=GRET_PG_settings)

@persistent
def load_post(dummy):
    if not hasattr(bpy.types.Scene, 'my_tools'):
        return
    from .helpers import is_defaulted, save_properties, load_properties
    for scene in bpy.data.scenes:
        if 'my_tools' in scene and not is_defaulted(scene.my_tools):
            print("Found old gret settings in file, restoring")
            load_properties(scene.gret, save_properties(scene.my_tools))
            # Fix up holes in old job settings
            for job in scene.gret.export_jobs:
                for index in range(len(job.collections) - 2, -1, -1):
                    item = job.collections[index]
                    if not item.collection:
                        job.collections.remove(index)
                for index in range(len(job.actions) - 2, -1, -1):
                    item = job.actions[index]
                    if not item.action and not item.use_pattern:
                        job.actions.remove(index)
                for index in range(len(job.copy_properties) - 2, -1, -1):
                    item = job.copy_properties[index]
                    if not item.source and not item.destination:
                        job.copy_properties.remove(index)
                for index in range(len(job.remap_materials) - 2, -1, -1):
                    item = job.remap_materials[index]
                    if not item.source and not item.destination:
                        job.remap_materials.remove(index)
            del scene['my_tools']
    del bpy.types.Scene.my_tools

classes = (
    GRET_PG_settings,
    GretAddonPreferences,
)

def register():
    # Register prefs first so that modules can access them through gret.prefs
    bpy.utils.register_class(GretAddonPreferences)

    # Each module adds its own settings to the main group via add_property()
    for module in modules:
        if hasattr(module, 'register'):
            module.register(GRET_PG_settings)
    bpy.utils.register_class(GRET_PG_settings)

    bpy.types.Scene.gret = bpy.props.PointerProperty(type=GRET_PG_settings)

    if prefs.backwards_compat:
        bpy.app.handlers.load_pre.append(load_pre)
        bpy.app.handlers.load_post.append(load_post)

def unregister():
    if prefs.backwards_compat:
        bpy.app.handlers.load_pre.remove(load_pre)
        bpy.app.handlers.load_post.remove(load_post)

    del bpy.types.Scene.gret

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    for module in reversed(modules):
        if hasattr(module, 'unregister'):
            module.unregister()

if __name__ == '__main__':
    register()
