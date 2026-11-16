# Pirate Face Hub

Downloads Hugging Face models over BitTorrent from [Pirate Face](https://pirateface.co) seeders. Your code stays the same.

## Install

```sh
pip install pirateface
```

Or without installing:

```sh
uv run --with pirateface python your_script.py
```

Anything that downloads through `huggingface_hub` (transformers, diffusers, `hf download`) now pulls from the swarm. Every file is checked against Hugging Face's hash. If a model isn't on Pirate Face or a peer stalls, that file comes from Hugging Face as usual.

Needs Python 3.12 or 3.13. With `uvx`, pass `-p 3.13`.

## Commands

```sh
pf download org/model      # download into the Hugging Face cache (--local-dir DIR for a folder)
pf seed org/model [FOLDER] # seed a model, submitting it first if Pirate Face doesn't list it
pf submit --repo org/model FOLDER  # package and submit a folder without the seed step (--dry-run to preview)
pf work                    # build torrents for queued model requests, streamed from Hugging Face
pf restore PATH            # upload .torrent files to fix a listing whose magnet stalls
```

More `pf seed` options:

```sh
pf seed --hf-cache          # every complete model in your Hugging Face cache, asking per model
pf seed --update-trackers   # add your seeding tracker to torrents you already seed
pf seed --update-per-file   # add per-file swarms for models you already seed in qBittorrent, then say which can't
pf seed --no-per-file org/model  # skip per-file swarms
pf seed --upgrade org/model # relist your v1-only torrent as hybrid (v1 + v2) so its files get per-file swarms
pf seed --upgrade --mine  # the same for every v1 torrent you submitted that has a complete copy in qBittorrent
pf seed --upgrade         # switch torrents Pirate Face replaced to their replacements
```

`pf seed --update-per-file` then lists the two kinds of model you can act on: v1-only ones
(`pf seed --upgrade` switches any their uploader made hybrid; if you uploaded one,
`pf seed --upgrade org/model`) and ones that aren't complete yet.

`pf work --max-size 100` takes only models up to 100 GiB. `pf work --once` takes one job and exits.

`pf restore` takes `.torrent` files or a client folder, such as qBittorrent's `BT_backup` or Transmission's `torrents/`.

`pf submit --direct --repo your-org/model FOLDER` publishes your own model that isn't on Hugging Face.

`pf seed`, `submit`, `work` and `restore` ask once for your key from [pirateface.co/settings](https://pirateface.co/settings). Run `pf <command> --help` for every flag.

## Settings

| Variable | |
|---|---|
| `PIRATEFACE_KEY` | Account key (otherwise asked once and saved) |
| `QBT_URL`, `QBT_USER`, `QBT_PASS` | qBittorrent Web UI (default `http://localhost:8080`) |
| `TR_HOST`, `TR_AUTH` | Transmission RPC (default `localhost:9091`), `user:pass` |
| `HF_TOKEN` | For gated repos |
| `PF_DISABLE` | Set to `1` to turn the download hook off |

## Development

```sh
uv run --group dev ruff check
```

## Contributing

- One change per PR, small enough to read in one sitting.
- Please don't add tests for now. We're still settling how we test, so check your change by hand and say how in the PR.
- `uv run --group dev ruff check` and `ruff format --check` should pass with nothing new.
- When you replace code, delete the old code. No disabled flags or leftover branches.
- Fix problems where they start rather than adding special cases around them.
- If Pirate Face can't be reached, say so and carry on. Never crash, and never write outside the model folder.
- Add your Pirate Face username to the PR so we can credit you.
