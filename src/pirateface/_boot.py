"""Runs at every Python startup via pirateface.pth, so it must be cheap and never raise.
Patches huggingface_hub lazily, when something imports it."""

import sys

from pirateface.config import flag

TARGET = "huggingface_hub.file_download"


class _PatchOnImport:
    def find_spec(self, name, path=None, target=None):
        if name != TARGET:
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
            try:
                from pirateface import hook

                hook.install(module)
            except Exception as e:
                print(f"[pirateface] hook disabled: {e}", file=sys.stderr)

        spec.loader.exec_module = exec_and_patch
        return spec


if not flag("PF_DISABLE"):
    if TARGET in sys.modules:
        from pirateface import hook

        hook.install(sys.modules[TARGET])
    else:
        sys.meta_path.insert(0, _PatchOnImport())
