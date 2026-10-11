# Pirate Face Hub

**Plug and play torrent downloads for Hugging Face models.** Add one package. Change nothing else.

```sh
uv run --with pirateface python your_script.py
```

or install it once and forget about it:

```sh
pip install pirateface
```

That's the whole setup. No code changes, no config, no new API. Your script still calls transformers or
`huggingface_hub` like it always did, and the files now come from [Pirate Face](https://pirateface.co)
seeders over BitTorrent:

```python
from transformers import AutoModelForCausalLM

model = AutoModelForCausalLM.from_pretrained("org/model")  # unchanged, now from the swarm
```

The same goes for anything that downloads through `huggingface_hub`: diffusers, sentence-transformers,
`hf_hub_download`, `snapshot_download` and the `hf download` CLI.

Every file is checked against Hugging Face's own fingerprint (SHA-256 for large files, git SHA-1 for the
rest) before it goes into the normal Hugging Face cache. If Pirate Face doesn't have the model, a peer is
missing, or a check fails, that file comes from Hugging Face as usual. Uninstall it and your code
runs exactly as before.

There's also a small CLI:

```sh
pf download org/model [--revision REV]
```

## How it hooks in

The package installs `pirateface.pth`, which Python runs at startup. It does nothing until something
imports `huggingface_hub.file_download`, then replaces only the step that fetches a file's bytes.
Metadata, cache layout, symlinks and locks stay with `huggingface_hub`.

## Settings

| Variable | Default | |
|---|---|---|
| `PF_DISABLE` | off | Turn the hook off |
| `PF_QUIET` | off | Print nothing |
| `PF_NO_RESCUE` | off | Don't ask Pirate Face to add models it doesn't have yet |
| `PF_DEBUG` | off | Print libtorrent's log |
| `PF_CACHE` | `~/.cache/pirateface` | Where torrent data and file lists are kept |
| `PF_API` | `https://pirateface.co` | Pirate Face server |
| `PF_LISTEN` | any free port | libtorrent listen interfaces |
| `PF_METADATA_TIMEOUT` | `30` | Seconds to wait for a peer to send the file list |
| `PF_STALL_TIMEOUT` | `60` | Seconds without progress before falling back to Hugging Face |

Flags are on for any value except empty or `0`.

Needs Python 3.12 or 3.13 (libtorrent has no wheels for 3.14 yet).

## Development

```sh
uv run --group dev ruff check
```
