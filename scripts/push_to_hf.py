#!/usr/bin/env python3
"""Upload an exported dataset folder (default: dataset/v2) to a Hugging Face dataset repo.

Independent of the pipeline: run it after `export`, on any machine that has the folder.

    ./venv/bin/python scripts/push_to_hf.py --repo AyoubChLin/CompanyDocuments-v2
    ./venv/bin/python scripts/push_to_hf.py --repo <user>/<name> --public          # create it public
    ./venv/bin/python scripts/push_to_hf.py --repo <user>/<name> --dry-run         # list what would be sent
    ./venv/bin/python scripts/push_to_hf.py --repo <user>/<name> --exclude "scans/*"

Token: HF_TOKEN (environment or .env), else the one saved by `hf auth login`. It needs write access.
Repo: --repo, else HF_REPO_ID. A missing repo is created (private unless --public).

Files go up in commits of --batch files, so a 60k-file export doesn't hit the per-commit limits.
Resumable: files already on the Hub with the same size are skipped, so an interrupted push just
reruns. The card, stats and metadata.csv files are compared by content and sent last. Remote files that no longer
exist locally are kept; pass --delete-stale to remove them.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DELETE_BATCH = 10_000
ALWAYS_SEND = ["README.md", "stats.json", "*.csv"]          # card, stats, metadata.csv: change between runs


def log(msg: str) -> None:
    print(f"[push] {msg}", flush=True)


def load_dotenv(path: Path) -> None:
    """KEY=VALUE lines from .env, without overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def local_files(root: Path, exclude: list[str]) -> dict[str, Path]:
    files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or rel.startswith(".") or "/." in rel:
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
            continue
        files[rel] = path
    return files


def remote_sizes(api, repo: str, revision: str | None) -> dict[str, int]:
    from huggingface_hub.errors import RepositoryNotFoundError, RevisionNotFoundError
    try:
        tree = api.list_repo_tree(repo, recursive=True, repo_type="dataset", revision=revision)
        return {f.path: f.size for f in tree if getattr(f, "size", None) is not None}
    except (RepositoryNotFoundError, RevisionNotFoundError):
        return {}


def unchanged_on_hub(api, repo: str, revision: str | None, files: dict[str, Path], paths: list[str]) -> set[str]:
    """Of `paths` (small files that are always candidates), those whose content already matches the Hub:
    git blob hash for regular files, sha256 for LFS files. Skipping them saves a commit."""
    same = set()
    for i in range(0, len(paths), 500):
        for info in api.get_paths_info(repo, paths[i:i + 500], expand=True, repo_type="dataset", revision=revision):
            data = files[info.path].read_bytes()
            if info.lfs:
                match = info.lfs.sha256 == hashlib.sha256(data).hexdigest()
            else:
                match = info.blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            if match:
                same.add(info.path)
    return same


def commit(api, args, ops, message: str, label: str) -> bool:
    """create_commit with backoff on network errors. The Hub allows 128 commits per repo per hour: when that
    limit is hit, stop at once (waiting minutes won't help); a rerun resumes."""
    for attempt in range(4):
        try:
            api.create_commit(args.repo, ops, repo_type="dataset", revision=args.revision, commit_message=message)
            return True
        except Exception as exc:
            if "rate limit for repository commits" in str(exc) or attempt == 3:
                log(f"{label} failed: {str(exc).strip().splitlines()[-1][:300]}")
                log("rerun later to resume (files already on the Hub are skipped)")
                return False
            wait = 30 * 2 ** attempt
            log(f"{label}: {exc}; retrying in {wait}s")
            time.sleep(wait)
    return False


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("HF_REPO_ID"), help="<user or org>/<name> (default: HF_REPO_ID)")
    ap.add_argument("--folder", default="dataset/v2", help="exported dataset folder (default: dataset/v2)")
    ap.add_argument("--revision", help="branch to push to (default: main)")
    ap.add_argument("--public", action="store_true", help="create the repo public (default: private)")
    ap.add_argument("--exclude", nargs="*", default=[], help='glob patterns to skip, e.g. "scans/*"')
    ap.add_argument("--batch", type=int, default=1000, help="files uploaded per commit (default: 1000)")
    ap.add_argument("--delete-stale", action="store_true", help="delete remote files missing locally")
    ap.add_argument("--dry-run", action="store_true", help="show what would be uploaded, change nothing")
    args = ap.parse_args()

    if not args.repo or "/" not in args.repo:
        ap.error("--repo <user>/<name> is required (or set HF_REPO_ID in .env)")
    folder = Path(args.folder)
    folder = folder if folder.is_absolute() else REPO_ROOT / folder
    if not (folder / "README.md").exists() or not (folder / "stats.json").exists():
        log(f"{folder} doesn't look like an export (no README.md or stats.json). Run the export stage first.")
        return 1

    try:
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi, get_token
    except ImportError:
        log("huggingface_hub is missing: ./venv/bin/pip install -r requirements.txt")
        return 1

    token = os.environ.get("HF_TOKEN") or get_token()
    if not token:
        log("no token: set HF_TOKEN in .env or run `hf auth login` (needs a write token)")
        return 1
    api = HfApi(token=token)
    log(f"logged in as {api.whoami()['name']}")

    files = local_files(folder, args.exclude)
    remote = remote_sizes(api, args.repo, args.revision)
    always = [rel for rel in files if any(fnmatch.fnmatch(rel, pat) for pat in ALWAYS_SEND)]
    same = unchanged_on_hub(api, args.repo, args.revision, files, [rel for rel in always if rel in remote])
    todo = [rel for rel, path in files.items()
            if rel not in same and (rel in always or remote.get(rel) != path.stat().st_size)]
    stale = sorted(set(remote) - set(files) - {".gitattributes"}) if args.delete_stale else []
    size = sum(files[rel].stat().st_size for rel in todo)
    log(f"{len(files):,} local files, {len(remote):,} on the Hub: "
        f"{len(todo):,} to upload ({human(size)}), {len(files) - len(todo):,} already there"
        + (f", {len(stale):,} to delete" if stale else ""))

    if args.dry_run:
        for rel in todo[:20]:
            print(f"  + {rel}")
        if len(todo) > 20:
            print(f"  ... and {len(todo) - 20:,} more")
        for rel in stale[:20]:
            print(f"  - {rel}")
        return 0
    if not todo and not stale:
        log("nothing to do")
        return 0

    if not remote:
        api.create_repo(args.repo, repo_type="dataset", private=not args.public, exist_ok=True)
        log(f"repo ready: https://huggingface.co/datasets/{args.repo}")

    # Card and CSVs last: until the data files are up, the dataset viewer would point at missing PDFs.
    todo.sort(key=lambda rel: any(fnmatch.fnmatch(rel, pat) for pat in ALWAYS_SEND))
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    for n, batch in enumerate(batches, 1):
        ops = [CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(files[rel])) for rel in batch]
        if not commit(api, args, ops, f"Upload {folder.name} ({n}/{len(batches)})", f"batch {n}/{len(batches)}"):
            return 1
        log(f"batch {n}/{len(batches)}: {len(batch):,} files")

    # deletes carry no data: big batches keep the commit count (128 per hour) low
    for i in range(0, len(stale), DELETE_BATCH):
        ops = [CommitOperationDelete(path_in_repo=rel) for rel in stale[i:i + DELETE_BATCH]]
        if not commit(api, args, ops, "Remove files no longer in the export", "delete"):
            return 1
        log(f"deleted {min(i + DELETE_BATCH, len(stale)):,}/{len(stale):,} stale files")

    log(f"done: https://huggingface.co/datasets/{args.repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
