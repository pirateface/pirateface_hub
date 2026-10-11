"""Runs at every Python startup (via pirateface.pth). Must be cheap and never raise.

Importing huggingface_hub here would slow down every Python process, so we wait
until something else imports huggingface_hub.file_download, then patch it.
"""

import os
import sys

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
            except Exception as e:  # never break the user's import
                print(f"[pirateface] hook disabled: {e}", file=sys.stderr)

        spec.loader.exec_module = exec_and_patch
        return spec


if os.environ.get("PF_DISABLE") != "1":
    if TARGET in sys.modules:
        from pirateface import hook

        hook.install(sys.modules[TARGET])
    else:
        sys.meta_path.insert(0, _PatchOnImport())
