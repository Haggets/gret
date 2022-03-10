import bpy

module_names = [
    'anim_export',
    'rig_export',
    'scene_export',
    'export',  # Depends on all others
]
from .. import import_or_reload_modules
modules = import_or_reload_modules(module_names, __name__)

def register(settings):
    for module in modules:
        if hasattr(module, 'register'):
            module.register(settings)

def unregister():
    for module in reversed(modules):
        if hasattr(module, 'unregister'):
            module.unregister()
