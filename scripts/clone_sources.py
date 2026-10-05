#!/usr/bin/env python3
"""Clone and verify both data sources for the CompanyDocuments generator."""

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CLONE_LOG = LOG_DIR / "clone_sources.log"


SOURCES = {
    "northwind": {
        "url": "https://github.com/microsoft/sql-server-samples",
        "target": DATA_DIR / "northwind",
        "sparse": True,
        "sparse_path": "samples/databases/northwind-pubs",
        "expected_files": ["samples/databases/northwind-pubs/instnwnd.sql"],
    },
    "company-documents": {
        "url": "https://huggingface.co/datasets/AyoubChLin/CompanyDocuments",
        "target": DATA_DIR / "company-documents",
        "sparse": False,
        "expected_files": ["README.md", "invoices", "PurchaseOrders", "Shipping orders"],
    },
}


def run(cmd, cwd=None, check=True):
    print(f"[clone] {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc


def clone(source_name, cfg):
    target = cfg["target"]
    target.mkdir(parents=True, exist_ok=True)

    if cfg.get("sparse"):
        if (target / ".git").exists():
            run(["git", "fetch", "origin", "--depth=1"], cwd=target)
            run(["git", "reset", "--hard", "origin/master"], cwd=target)
            run(["git", "sparse-checkout", "set", cfg["sparse_path"]], cwd=target)
        else:
            run(["git", "init"], cwd=target)
            run(["git", "remote", "add", "origin", cfg["url"]], cwd=target)
            run(["git", "config", "core.sparseCheckout", "true"], cwd=target)
            run(["git", "sparse-checkout", "set", cfg["sparse_path"]], cwd=target)
            run(["git", "pull", "--depth=1", "origin", "master"], cwd=target)
    else:
        if target.exists() and any(target.iterdir()):
            run(["git", "fetch", "origin"], cwd=target)
            run(["git", "reset", "--hard", "origin/master"], cwd=target)
        else:
            run(["git", "clone", "--depth=1", cfg["url"], str(target)], cwd=REPO_ROOT)


def verify(source_name, cfg):
    target = cfg["target"]
    missing = []
    for rel in cfg.get("expected_files", []):
        if not (target / rel).exists():
            missing.append(rel)
    if missing:
        raise RuntimeError(f"[{source_name}] missing: {missing}")
    print(f"[verify] {source_name} OK at {target}")


def main():
    for name, cfg in SOURCES.items():
        target = cfg["target"]
        if target.exists() and any(target.iterdir()):
            print(f"[skip] {name} already exists at {target}")
            verify(name, cfg)
            continue
        print(f"[clone] Cloning {name} -> {target}")
        clone(name, cfg)
        verify(name, cfg)

    print("[done] All sources cloned and verified.")


if __name__ == "__main__":
    main()
