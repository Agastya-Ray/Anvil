"""File locations and JSON helpers. Everything lives under ANVIL_HOME (default: cwd)."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("ANVIL_HOME", os.getcwd())).resolve()


def vault() -> Path:
    return home() / "vault"


def state() -> Path:
    p = home() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir() -> Path:
    p = home() / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load(path: Path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save(path: Path, data) -> None:
    """Atomic write so a crash never leaves half a state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def skeleton() -> dict:
    data = load(vault() / "skeleton.json")
    if not data:
        raise SystemExit("vault/skeleton.json missing. It holds the locked facts.")
    return data


def safe_id(canonical_id: str) -> str:
    """canonical_id is <ats>:<company>:<job_id>; colons are not allowed in Windows filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", canonical_id).strip("_")


def file_company(company: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", company).strip("_")
    return s or "Company"
