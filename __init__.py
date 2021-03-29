bl_info = {
    'name': "gret",
    'author': "greisane",
    'description': "",
    'version': (1, 0),
    'blender': (2, 92, 0),
    'location': "3D View > Tools",
    'category': "Object"
}

import bpy
import importlib
import sys

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
    'helpers',
    # 'stringcase',  # Third party, no need to register or reload
    'file',
    'material',
    'mesh',
    'rig',
    'anim',  # Depends on rig
    'jobs',  # Depends on mesh, rig
]
modules = import_or_reload_modules(module_names, __name__)

class GRET_PG_settings(bpy.types.PropertyGroup):
    @classmethod
    def add_property(cls, name, annotation):
        if not hasattr(cls, '__annotations__'):
            cls.__annotations__ = {}
        cls.__annotations__[name] = annotation

def register():
    # On registering, each module can add its own settings to the main group via add_property()
    for module in modules:
        if hasattr(module, 'register'):
            module.register(GRET_PG_settings)

    # Settings used to live in WindowManager, however pointer properties break with global undo
    bpy.utils.register_class(GRET_PG_settings)
    bpy.types.Scene.gret = bpy.props.PointerProperty(type=GRET_PG_settings)

def unregister():
    del bpy.types.Scene.gret
    bpy.utils.unregister_class(GRET_PG_settings)

    for module in reversed(modules):
        if hasattr(module, 'unregister'):
            module.unregister()

if __name__ == '__main__':
    register()