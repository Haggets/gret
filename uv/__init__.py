import bpy

module_names = [
    'align_each',
    'helpers',
    'relax_loops',
    'uv_paint',
    'uv_sheet',
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
