"""Seed listed model torrents from content-addressed Hugging Face and Ollama caches."""

import functools
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tqdm.auto import tqdm

from pirateface.client import api, torrent
from pirateface.config import API, SEEDS
from pirateface.seed import clients, package, user
from pirateface.seed.per_file.command import seed_model
from pirateface.seed.user import SeedError

SHA256 = re.compile(r"[0-9a-fA-F]{64}")
REPO_LINE = re.compile(r"^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)(?:\s|$)")


@dataclass
class Candidate:
    repo: str
    quant: str | None
    name: str
    v1: str
    single: bool
    data: bytes
    files: dict[str, Path]
    seeders: int
    leechers: int


def _cached_file(path):
    """Return (sha256, real file path) when the cache names this file by its SHA-256."""
    target = path.parent / path.readlink() if path.is_symlink() else path
    digest = target.name
    if SHA256.fullmatch(digest) and target.is_file():
        return digest.lower(), target.resolve()
    return None


def _huggingface_cache():
    from huggingface_hub import constants

    root = Path(constants.HF_HUB_CACHE)
    snapshots = {}
    blobs = {}
    if not root.is_dir():
        return snapshots, blobs, root
    for repo_dir in sorted(root.glob("models--*")):
        repo = repo_dir.name.removeprefix("models--").replace("--", "/", 1)
        blob_dir = repo_dir / "blobs"
        if blob_dir.is_dir():
            # A repo blob is named by its SHA-256. Since huggingface_hub 1.32 it is a link into a shared
            # store named by Xet hash, so the key is the link's own name, not its target's.
            for blob in blob_dir.iterdir():
                if SHA256.fullmatch(blob.name) and blob.is_file():
                    blobs.setdefault(blob.name.lower(), blob.resolve())
        for snapshot in sorted((repo_dir / "snapshots").glob("*")):
            snapshots.setdefault(repo, {})[snapshot.name] = snapshot
    return snapshots, blobs, root


def _ollama_root():
    if os.environ.get("OLLAMA_MODELS"):
        return Path(os.environ["OLLAMA_MODELS"]).expanduser()
    candidates = [Path.home() / ".ollama" / "models"]
    if sys.platform.startswith("linux"):
        candidates.insert(0, Path("/usr/share/ollama/.ollama/models"))
    return next(
        (path for path in candidates if (path / "blobs").is_dir() and (path / "manifests").is_dir()),
        next((path for path in candidates if path.is_dir()), candidates[0]),
    )


def _ollama_cache():
    root = _ollama_root()
    blobs = {}
    queries = set()
    blob_dir = root / "blobs"
    manifests = root / "manifests"
    if blob_dir.is_dir():
        for blob in blob_dir.iterdir():
            digest = blob.name.removeprefix("sha256-")
            if SHA256.fullmatch(digest) and blob.is_file() and not blob.is_symlink():
                blobs.setdefault(digest.lower(), blob.resolve())
    if manifests.is_dir():
        for manifest in manifests.rglob("*"):
            if not manifest.is_file() or manifest.is_symlink():
                continue
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            parts = list(manifest.relative_to(manifests).parts)
            if parts and ("." in parts[0] or ":" in parts[0]):
                parts.pop(0)
            if len(parts) >= 3 and parts[0] == "library":
                parts.pop(0)
            if len(parts) >= 2:
                model, tag = "/".join(parts[:-1]), parts[-1]
                queries.add(f"{model} {tag}")
                if tag.casefold() == "latest":
                    queries.add(model)
    return root, blobs, queries


def _repos_from_search(text):
    repos = set()
    for line in (text or "").splitlines():
        match = REPO_LINE.match(line.strip())
        if match:
            repos.add(match.group(1))
    return repos


def _search_ollama(queries):
    repos = set()
    for query in tqdm(sorted(queries, key=str.casefold), desc="Searching Ollama model names", unit="model"):
        result = api.search_models(query)
        if result is None:
            print("Ollama name search failed; continuing with matches found so far.", file=sys.stderr)
            break
        repos.update(_repos_from_search(result))
    return repos


def _source_for(path, digest, size, blobs, snapshots):
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        return None
    digest = digest.lower()
    source = blobs.get(digest)
    if source and source.is_file() and source.stat().st_size == size:
        return source
    for snapshot in snapshots:
        source = snapshot / PurePosixPath(path)
        if source.is_file() and source.stat().st_size == size:
            cached = _cached_file(source)
            if cached:
                if cached[0] == digest:
                    return cached[1]
                continue
            source = source.resolve()
            if _sha256(source) == digest:
                return source
    return None


@functools.cache
def _sha256(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _candidate(repo, quant, entry, data, blobs, snapshots):
    payload = torrent.files_in(data)
    if not payload:
        return None
    name, v1, _, single = package.info(data)
    v1 = v1.lower()
    if v1 == "0" * 40:
        return None
    listed_hash = entry.get("infohash")
    if listed_hash and str(listed_hash).lower() != v1:
        return None
    manifest = {
        item["path"]: item["sha256"].lower()
        for item in entry.get("files") or []
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha256"), str)
        and SHA256.fullmatch(item["sha256"])
    }
    revisions = snapshots.get(repo, {})
    revision = entry.get("revision")
    snapshot_paths = ([revisions[revision]] if revision in revisions else []) + [
        path for commit, path in revisions.items() if commit != revision
    ]
    files = {}
    for path, size, _ in payload:
        source = _source_for(path, manifest.get(path), size, blobs, snapshot_paths)
        if not source:
            return None
        files[path] = source
    return Candidate(
        repo=entry.get("repoId") or repo,
        quant=quant,
        name=name,
        v1=v1,
        single=single,
        data=data,
        files=files,
        seeders=int(entry.get("seeders") or 0),
        leechers=int(entry.get("leechers") or 0),
    )


def _listings(repos, blobs, snapshots):
    found = {}
    try:
        for repo in tqdm(sorted(repos, key=str.casefold), desc="Checking Pirate Face listings", unit="repo"):
            base = api.lookup(repo)
            for listed in (base.get("quants") or [base]) if isinstance(base, dict) else []:
                quant = listed.get("quant") if isinstance(listed, dict) else None
                entry = api.lookup(repo, quant) if quant else base
                data = api.torrent_file(repo, quant) if isinstance(entry, dict) else None
                if not data:
                    continue
                try:
                    candidate = _candidate(repo, quant, entry, data, blobs, snapshots)
                except torrent.Unsafe:
                    continue
                if candidate:
                    found.setdefault(candidate.v1, candidate)
    except api.Unreachable as error:
        raise SeedError(f"could not reach {API}") from error
    return sorted(found.values(), key=lambda item: (item.repo.casefold(), item.quant or ""))


def _seed(candidate, qbt, trackers, per_file):
    data = package.with_trackers(candidate.data, trackers)
    out = Path(SEEDS, "matches", candidate.v1)
    save_dir = out / "files"
    destination = save_dir if candidate.single else save_dir / candidate.name
    if qbt.has(candidate.v1):
        print(f"    Already in qBittorrent: {candidate.repo}")
        return
    try:
        package.clear_links(destination)
        for relative, source in candidate.files.items():
            package.link(source, destination / relative)
        status = qbt.add_paused_and_recheck(data, save_dir, candidate.v1)
    except Exception:
        package.clear_links(destination)
        raise
    if status == "incomplete":
        package.clear_links(destination)
        print("    qBittorrent's recheck was below 100%; removed the torrent and kept it off the cache files.")
        return
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{candidate.name}.torrent").write_bytes(data)
    total = sum(path.stat().st_size for path in candidate.files.values())
    print(f"    qBittorrent verified {total / 1e9:.2f} GB and is seeding {candidate.repo}.")
    if per_file:
        seed_model(data, save_dir, candidate.v1, verified=True)


def seed_from_cache(per_file=True):
    """Match cached hashes to existing Pirate Face torrents and seed verified matches in qBittorrent."""
    snapshots, hf_blobs, hf_root = _huggingface_cache()
    ollama_root, ollama_blobs, ollama_queries = _ollama_cache()
    blobs = hf_blobs | ollama_blobs
    repos = set(snapshots)
    repos.update(_search_ollama(ollama_queries))
    if not blobs and not any(snapshots.values()):
        raise SeedError(
            f"no content-addressed model files found in Hugging Face cache ({hf_root}) or Ollama store ({ollama_root})"
        )
    if not repos:
        print("No Pirate Face listings were found for the available cache names.")
        return
    print(f"==> Found {len(blobs)} SHA-256 named cache file(s); searching their model listings")
    candidates = _listings(repos, blobs, snapshots)
    if not candidates:
        print("No complete listed torrents matched the local cache files.")
        return
    print("==> Candidate listed torrents; qBittorrent will verify each before seeding")
    for candidate in candidates:
        quant = f" (quant {candidate.quant})" if candidate.quant else ""
        print(f"    {candidate.repo}{quant}: {candidate.seeders} seeds, {candidate.leechers} leechers")
    try:
        qbt = clients.QBittorrent()
    except clients.LoginRefused as error:
        raise SeedError(str(error)) from error
    except Exception as error:
        raise SeedError(f"qBittorrent is not reachable; configure its Web UI ({clients.HELP})") from error
    key = trackers = None
    for candidate in candidates:
        if not user.YES and not user.ask(
            f"==> Seed {candidate.repo} ({candidate.seeders} seeds)? [y/N] "
        ).lower().startswith("y"):
            continue
        if trackers in (None, api.TRACKERS):  # share a real answer; after the built-in fallback list, ask again
            key = key or user.load_key()
            trackers = api.trackers(key)
        try:
            _seed(candidate, qbt, trackers, per_file)
        except (OSError, package.LinkError, RuntimeError) as error:
            print(f"    Could not seed {candidate.repo}: {error}", file=sys.stderr)
