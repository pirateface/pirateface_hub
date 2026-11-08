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
pf download org/model [--revision REV] [--local-dir DIR]
pf seed org/model [FOLDER]
pf submit --repo org/model [--dry-run] FOLDER
```

## Share a model

```sh
pf seed org/model [FOLDER]
```

If Pirate Face lists the model, this adds its torrent to your torrent client, linked to the files you have
(from FOLDER, or the Hugging Face cache), and the client seeds. If not, it builds a hybrid (v1 + v2) torrent,
checks every file against Hugging Face, adds it to your client and submits it. Matching models are listed
right away. With no FOLDER and nothing in the cache, it downloads the model first. Your files are only read;
links to them go in `~/pirateface-seeds/<model>`.

```sh
pf seed --hf-cache         # every complete model in the Hugging Face cache, asking per model
pf seed --update-trackers  # add your account's seeding tracker to torrents you already seed
```

To submit one folder with `package.sh`'s flags:

```sh
pf submit --repo org/model [--revision COMMIT] [--name NAME] [--out DIR] [-y] ./model-folder
pf submit --direct --repo your-org/model [--license ID] [--web-seed https://host/path/] ./model-folder
```

`--direct` publishes your own model (not on Hugging Face) under your Hugging Face username or one of your
orgs; `--web-seed` is an https server with the files at `<url><name>/<path>`. `--dry-run` builds the torrent and
prints the infohashes and the exact request, without adding anything to a client or submitting. It needs no key.

It asks once for your key from [pirateface.co/account](https://pirateface.co/account) and saves it.
`-y` skips every prompt; agents should pass it only on the user's explicit OK.

| Variable | |
|---|---|
| `PIRATEFACE_KEY` | Account key (else asked and saved to `~/.config/pirateface/key`) |
| `PIRATEFACE_SEEDS` | Where the links and `.torrent` files go (default `~/pirateface-seeds`) |
| `HF_TOKEN` | For gated repos |
| `TR_HOST`, `TR_AUTH` | Transmission RPC (default `localhost:9091`), `user:pass` |
| `QBT_URL`, `QBT_USER`, `QBT_PASS` | qBittorrent Web UI (default `http://localhost:8080`) |

## Models only on Pirate Face

Some models are only on Pirate Face (published there directly, or removed from Hugging Face). They load
like any other model. When Hugging Face says a repo doesn't exist, the hook asks Pirate Face, answers
huggingface_hub from Pirate Face's file list, downloads over BitTorrent and checks every file's SHA-256.
For Pirate Face's own namespace, `pirateface/`, Pirate Face is asked first and Hugging Face never is.

## How it hooks in

The package installs `pirateface.pth`, which Python runs at startup. It does nothing until something
imports `huggingface_hub`, then fetches each file's bytes from the swarm. Hugging Face still answers
which commit and fingerprint to expect, and every file from the swarm is checked against it. If Pirate
Face has no torrent, or no peer sends the file list within 30 s, the file comes from Hugging Face as usual.
Cache layout, symlinks and locks stay with `huggingface_hub`.

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
| `PF_ONLY_STALL_TIMEOUT` | `600` | The same for models only on Pirate Face (no Hugging Face to fall back to) |

Flags are on for any value except empty or `0`.

Needs Python 3.12 or 3.13 (libtorrent has no wheels for 3.14 yet). `uvx` picks the newest Python it has,
so pass the version:

```sh
uvx -p 3.13 --with pirateface --from huggingface_hub hf download org/model
```

## Development

```sh
uv run --group dev ruff check
```
