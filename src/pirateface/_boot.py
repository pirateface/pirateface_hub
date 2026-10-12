"""Runs at every Python startup via pirateface.pth, so it must be cheap and never raise.
Patches huggingface_hub lazily, when something imports it."""

import sys

from pirateface.config import flag

# module -> patches to apply to it, as (pirateface module, function)
TARGETS = {
    "huggingface_hub.file_download": [("huggingface", "install"), ("fallback", "install")],
    "huggingface_hub._snapshot_download": [("fallback", "install_snapshot")],
}


def _install(name, module):
    for patch_module, func in TARGETS[name]:
        try:
            patches = __import__(f"pirateface.{patch_module}", fromlist=[func])
            getattr(patches, func)(module)
        except Exception as e:
            print(f"[pirateface] {patch_module}.{func} disabled: {e}", file=sys.stderr)


class _PatchOnImport:
    def find_spec(self, name, path=None, target=None):
        if name not in TARGETS:
            return None
        sys.meta_path.remove(self)
        try:
            import importlib.util

            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return None
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            _install(name, module)

        spec.loader.exec_module = exec_and_patch
        return spec


if not flag("PF_DISABLE"):
    sys.meta_path.insert(0, _PatchOnImport())
    for name in TARGETS:
        if name in sys.modules:
            _install(name, sys.modules[name])
