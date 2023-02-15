import bpy

module_names = [
    'copybuffer_flatten',
    'deduplicate_materials',
]
from .. import import_or_reload_modules, register_submodules, unregister_submodules
modules = import_or_reload_modules(module_names, __name__)

def register(settings, prefs):
    global registered_modules
    registered_modules = register_submodules(modules, settings)

def unregister():
    unregister_submodules(registered_modules)
