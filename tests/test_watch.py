import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pirateface.seed import package, watch


class DiscoverComfyUITests(unittest.TestCase):
    def test_finds_weight_files_and_keeps_their_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "checkpoints").mkdir()
            (root / "loras").mkdir()
            (root / "checkpoints" / "main.safetensors").write_bytes(b"checkpoint")
            (root / "loras" / "style.gguf").write_bytes(b"lora")
            (root / "readme.md").write_text("not a weight")
            hidden = root / ".hidden"
            hidden.mkdir()
            (hidden / "secret.safetensors").write_bytes(b"hidden")

            found = watch.discover_comfyui(root)

            self.assertEqual([candidate.relative for candidate in found], ["checkpoints/main.safetensors", "loras/style.gguf"])
            self.assertEqual(found[0].files, {"checkpoints/main.safetensors": root / "checkpoints" / "main.safetensors"})

    def test_rejects_missing_models_folder(self):
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(watch.SeedError, "does not exist"):
            watch.discover_comfyui(Path(temporary) / "missing")


class DiscoverOllamaTests(unittest.TestCase):
    def test_finds_a_manifest_and_only_its_referenced_blobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifests" / "registry.ollama.ai" / "library" / "tiny" / "latest"
            manifest.parent.mkdir(parents=True)
            (root / "blobs").mkdir()
            digests = [hashlib.sha256(value).hexdigest() for value in (b"config", b"weights", b"template")]
            for digest, value in zip(digests, (b"config", b"weights", b"template"), strict=True):
                (root / "blobs" / f"sha256-{digest}").write_bytes(value)
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "config": {"digest": f"sha256:{digests[0]}"},
                        "layers": [
                            {"digest": f"sha256:{digests[1]}"},
                            {"digest": f"sha256:{digests[2]}"},
                        ],
                    }
                )
            )
            (root / "blobs" / ("sha256-" + "f" * 64)).write_bytes(b"unreferenced")

            found = watch.discover_ollama(root)

            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].relative, "manifests/registry.ollama.ai/library/tiny/latest")
            self.assertEqual(
                set(found[0].files),
                {
                    "manifests/registry.ollama.ai/library/tiny/latest",
                    *(f"blobs/sha256-{digest}" for digest in digests),
                },
            )

    def test_skips_manifest_when_a_referenced_blob_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifests" / "registry.ollama.ai" / "library" / "tiny" / "latest"
            manifest.parent.mkdir(parents=True)
            (root / "blobs").mkdir()
            digest = "a" * 64
            manifest.write_text(json.dumps({"config": {"digest": f"sha256:{digest}"}, "layers": []}))

            self.assertEqual(watch.discover_ollama(root), [])

    def test_rejects_invalid_ollama_store(self):
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(watch.SeedError, "must contain"):
            watch.discover_ollama(temporary)


class StagingTests(unittest.TestCase):
    def test_stages_only_the_selected_files_under_safe_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.safetensors"
            source.write_bytes(b"weights")
            output = root / "torrent"

            included, skipped = package.stage_files({"checkpoints/model.safetensors": source}, output)

            self.assertEqual(included, ["checkpoints/model.safetensors"])
            self.assertEqual(skipped, [])
            self.assertEqual((output / "checkpoints" / "model.safetensors").read_bytes(), b"weights")

    def test_builds_a_torrent_from_an_explicit_model_file_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.safetensors"
            source.write_bytes(b"weights")

            paths, sums, data, v1, v2 = watch.submit.build_torrent(
                root,
                root / "seed",
                "comfy-model",
                ["udp://tracker.example:6969/announce"],
                files={"checkpoints/model.safetensors": source},
            )

            self.assertEqual(paths, ["checkpoints/model.safetensors"])
            self.assertEqual(sums, {"checkpoints/model.safetensors": hashlib.sha256(b"weights").hexdigest()})
            self.assertEqual(package.info(data)[:3], ("comfy-model", v1, v2))

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.safetensors"
            source.write_bytes(b"weights")
            with self.assertRaisesRegex(package.LinkError, "invalid torrent path"):
                package.stage_files({"../outside.safetensors": source}, root / "torrent")


class WatchCommandTests(unittest.TestCase):
    def test_dry_run_lists_candidates_without_submitting_or_writing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            models = root / "models"
            models.mkdir()
            (models / "model.safetensors").write_bytes(b"weights")
            state = root / "state.json"
            args = SimpleNamespace(
                comfyui=str(models), ollama=None, owner="model-maker", license="apache-2.0",
                interval=1, settle=0, dry_run=True, yes=False, state=str(state), once=True,
            )
            output = io.StringIO()
            with (
                redirect_stdout(output),
                patch.object(watch.api, "lookup", side_effect=AssertionError("dry run must not call the API")),
                patch.object(watch.submit, "submit_new") as submit_new,
            ):
                self.assertEqual(watch.run(args), 0)

            self.assertIn("comfyui: model.safetensors", output.getvalue())
            self.assertIn("dry run only", output.getvalue())
            submit_new.assert_not_called()
            self.assertFalse(state.exists())

    def test_submit_mode_uses_the_candidate_file_list_and_records_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            models = root / "models"
            models.mkdir()
            source = models / "model.safetensors"
            source.write_bytes(b"weights")
            state = root / "state.json"
            args = SimpleNamespace(
                comfyui=str(models), ollama=None, owner="model-maker", license="apache-2.0",
                interval=1, settle=0, dry_run=False, yes=True, state=str(state), once=True,
            )
            with (
                patch.object(watch.api, "lookup", return_value=watch.api.NOT_LISTED),
                patch.object(watch.user, "load_key", return_value="pfct_test"),
                patch.object(watch.submit, "submit_new") as submit_new,
            ):
                self.assertEqual(watch.run(args), 0)

            submit_new.assert_called_once()
            call = submit_new.call_args
            self.assertEqual(call.args[0].split("/", 1)[0], "model-maker")
            self.assertTrue(call.kwargs["direct"])
            self.assertEqual(call.kwargs["license"], "apache-2.0")
            self.assertEqual(call.kwargs["files"], {"model.safetensors": source})
            self.assertEqual(json.loads(state.read_text())["processed"].popitem()[1]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
