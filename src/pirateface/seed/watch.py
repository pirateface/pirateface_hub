"""Poll ComfyUI and Ollama model stores and submit newly discovered models."""

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pirateface.client import api
from pirateface.seed import submit, user
from pirateface.seed.user import SeedError

COMFY_WEIGHT = re.compile(r"\.(safetensors|gguf|onnx|bin|pt|pth|ckpt|mlx|npz)$", re.IGNORECASE)
OLLAMA_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SLUG_CHAR = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Candidate:
    backend: str
    root: Path
    relative: str
    files: dict[str, Path]
    signature: str
    size_bytes: int
    modified: float

    @property
    def identity(self):
        return f"{self.backend}:{self.root}:{self.relative}"

    def state_key(self, owner, license):
        return hashlib.sha256(f"{self.identity}\0{self.signature}\0{owner}\0{license}".encode()).hexdigest()


def _candidate(backend, root, relative, files):
    """Create a stable snapshot signature for one model's files."""
    stats = []
    size = 0
    modified = 0.0
    for rel, source in sorted(files.items()):
        info = source.stat()
        stats.append((rel, info.st_size, info.st_mtime_ns))
        size += info.st_size
        modified = max(modified, info.st_mtime)
    signature = hashlib.sha256(json.dumps(stats, separators=(",", ":")).encode()).hexdigest()
    return Candidate(backend, root, relative, files, signature, size, modified)


def _hidden(relative):
    return any(part.startswith(".") for part in Path(relative).parts)


def discover_comfyui(models_dir):
    """Find supported ComfyUI weight files. Each file becomes its own model torrent."""
    root = Path(models_dir).expanduser().resolve()
    if not root.is_dir():
        raise SeedError(f"ComfyUI models folder does not exist: {root}")
    found = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (base / d).is_symlink())
        for name in sorted(names):
            source = base / name
            if source.is_symlink() or not COMFY_WEIGHT.search(name):
                continue
            relative = source.relative_to(root).as_posix()
            if _hidden(relative):
                continue
            try:
                if not source.is_file() or source.stat().st_size == 0:
                    continue
                found.append(_candidate("comfyui", root, relative, {relative: source}))
            except OSError:
                continue
    return found


def discover_ollama(models_dir):
    """Find complete Ollama manifests and the exact blobs they reference."""
    root = Path(models_dir).expanduser().resolve()
    manifests = root / "manifests"
    blobs = root / "blobs"
    if not manifests.is_dir() or not blobs.is_dir():
        raise SeedError(f"Ollama models folder must contain manifests/ and blobs/: {root}")
    found = []
    for manifest in sorted(manifests.rglob("*")):
        if not manifest.is_file() or manifest.is_symlink():
            continue
        relative = manifest.relative_to(root).as_posix()
        if _hidden(relative):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        descriptors = [data.get("config"), *(data.get("layers") or [])]
        files = {relative: manifest}
        complete = True
        digest_count = 0
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                continue
            digest = descriptor.get("digest")
            if not isinstance(digest, str) or not OLLAMA_DIGEST.fullmatch(digest):
                continue
            digest_count += 1
            blob_rel = "blobs/" + digest.replace(":", "-")
            blob = root / blob_rel
            if not blob.is_file() or blob.is_symlink():
                complete = False
                break
            files[blob_rel] = blob
        if not complete or not digest_count:
            continue
        try:
            found.append(_candidate("ollama", root, relative, files))
        except OSError:
            continue
    return found


def repo_name(candidate):
    """Make a portable, collision-resistant model name under the supplied owner."""
    raw = f"{candidate.backend}-{candidate.relative.replace('/', '-')}"
    slug = SLUG_CHAR.sub("-", raw).strip("._-").lower()
    slug = slug[:64].rstrip("._-") or candidate.backend
    unique = hashlib.sha256(f"{candidate.identity}\0{candidate.signature}".encode()).hexdigest()[:8]
    return f"{slug}-{unique}"


def _read_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise SeedError(f"could not read watcher state {path}: {e}") from e
    processed = data.get("processed") if isinstance(data, dict) else None
    if not isinstance(processed, dict):
        raise SeedError(f"invalid watcher state file: {path}")
    return processed


def _write_state(path, processed):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"version": 1, "processed": processed}, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def _record(path, processed, candidate, repo, owner, license, status):
    processed[candidate.state_key(owner, license)] = {"repo": repo, "status": status, "source": candidate.identity}
    _write_state(path, processed)


def _size(size):
    return f"{size / 1_000_000_000:.2f} GB" if size >= 1_000_000_000 else f"{size / 1_000_000:.1f} MB"


def _scan(args):
    found = []
    if args.comfyui:
        found.extend(discover_comfyui(args.comfyui))
    if args.ollama:
        found.extend(discover_ollama(args.ollama))
    return sorted(found, key=lambda c: (c.backend, c.relative.casefold()))


def _validate(args):
    if not args.comfyui and not args.ollama:
        raise SeedError("choose at least one source with --comfyui or --ollama")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.owner):
        raise SeedError("--owner must be one Hugging Face username or organization, without a slash")
    if not args.license.strip():
        raise SeedError("--license must be a Pirate Face license ID")
    if args.interval <= 0:
        raise SeedError("--interval must be greater than zero")
    if args.settle < 0:
        raise SeedError("--settle cannot be negative")
    if args.dry_run and not args.once:
        raise SeedError("--dry-run requires --once")
    if args.dry_run and args.yes:
        raise SeedError("--dry-run and --yes cannot be used together")


def _share(candidate, args, state_path, processed, key):
    model = repo_name(candidate)
    repo = f"{args.owner}/{model}"
    size = _size(candidate.size_bytes)
    print(f"==> {candidate.backend}: {candidate.relative} ({size}) -> {repo}")
    if args.dry_run:
        print(f"    license {args.license}; dry run only, nothing built or submitted")
        return key

    listed = api.lookup(repo)
    if listed is None:
        raise SeedError(f"could not reach Pirate Face while checking {repo}")
    if listed is not api.NOT_LISTED:
        print(f"    already listed; skipping {repo}")
        _record(state_path, processed, candidate, repo, args.owner, args.license, "already-listed")
        return key

    if not args.yes and not user.ask(f"    Share these files under {args.license}? [y/N] ").lower().startswith("y"):
        _record(state_path, processed, candidate, repo, args.owner, args.license, "declined")
        return key

    if key is None:
        key = user.load_key()
    out = submit.SEEDS / "watch" / args.owner / model
    submit.submit_new(
        repo,
        candidate.root,
        None,
        key,
        name=model,
        out=out,
        direct=True,
        license=args.license,
        command="pf watch",
        files=candidate.files,
    )
    _record(state_path, processed, candidate, repo, args.owner, args.license, "submitted")
    return key


def run(args):
    """Run a one-shot scan or keep polling the selected model stores."""
    try:
        _validate(args)
        user.YES = args.yes
        state_path = Path(args.state).expanduser() if args.state else submit.SEEDS / ".watch-state.json"
        processed = _read_state(state_path) if not args.dry_run else {}
        key = None
        while True:
            candidates = _scan(args)
            now = time.time()
            ready = [c for c in candidates if now - c.modified >= args.settle]
            waiting = [c for c in candidates if now - c.modified < args.settle]
            if not candidates:
                print("No supported model files found.")
            elif args.dry_run:
                for candidate in candidates:
                    suffix = "ready" if candidate in ready else "settling"
                    _share(candidate, args, state_path, processed, key)
                    if suffix == "settling":
                        print(f"    waiting for files to settle ({args.settle:g}s)")
            else:
                for candidate in ready:
                    if candidate.state_key(args.owner, args.license) in processed:
                        continue
                    key = _share(candidate, args, state_path, processed, key)
                if args.once and waiting:
                    print(f"{len(waiting)} model(s) still settling; rerun later or pass --settle 0.")
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped watching.")
        return 0
    except (SeedError, submit.package.LinkError) as e:
        sys.exit(f"error: {e}")
    except OSError as e:
        sys.exit(f"error: {e}")
