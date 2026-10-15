"""File locations and JSON helpers. Everything lives under ANVIL_HOME (default: cwd)."""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]


def home() -> Path:
    """ANVIL_HOME, else the repo the package sits in (works on Vercel), else cwd."""
    if os.environ.get("ANVIL_HOME"):
        return Path(os.environ["ANVIL_HOME"]).resolve()
    if (PKG_ROOT / "vault" / "skeleton.json").exists():
        return PKG_ROOT
    return Path(os.getcwd()).resolve()


def vault() -> Path:
    return home() / "vault"


def state() -> Path:
    p = home() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir() -> Path:
    p = Path(os.environ["ANVIL_OUT"]) if os.environ.get("ANVIL_OUT") else home() / "out"
    if os.environ.get("VERCEL") and not os.environ.get("ANVIL_OUT"):
        p = Path("/tmp/anvil-out")  # serverless: only /tmp is writable
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


# ---------------------------------------------------------------- shared state
# Learning state (bandit-log, applications, recon cache) must be shared by the CLI,
# the hosted API and any AI calling it. Backends:
#   local  (default)   files under state/
#   github             JSON files on a branch of a GitHub repo (like Ember's leads-data)
#                      env: ANVIL_STATE=github, GITHUB_TOKEN, ANVIL_STATE_REPO (owner/name),
#                           ANVIL_STATE_BRANCH (default anvil-state)


class StateError(Exception):
    pass


def backend() -> str:
    b = os.environ.get("ANVIL_STATE", "").strip().lower()
    if b:
        return b
    if os.environ.get("VERCEL"):
        return "github"
    return "local"


class _GitHub:
    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN", "").strip()
        self.repo = os.environ.get("ANVIL_STATE_REPO", "Agastya-Ray/Anvil").strip()
        self.branch = os.environ.get("ANVIL_STATE_BRANCH", "anvil-state").strip()
        if not self.token:
            raise StateError("ANVIL_STATE=github needs GITHUB_TOKEN (contents read/write on " + self.repo + ")")
        self.shas: dict[str, str] = {}

    def _req(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        url = f"https://api.github.com/repos/{self.repo}/{path}"
        accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None, headers={
            "authorization": f"Bearer {self.token}", "accept": accept,
            "user-agent": "anvil", "x-github-api-version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode() or "null"
                return r.status, json.loads(text)
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(text or "null")
            except ValueError:
                return e.code, text[:200]
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise StateError(f"GitHub unreachable: {e}") from e

    def get(self, rel: str):
        code, data = self._req("GET", f"contents/state/{rel}?ref={self.branch}")
        if code == 404:
            self.shas.pop(rel, None)
            return None
        if code != 200:
            raise StateError(f"GitHub read {rel}: HTTP {code} {str(data)[:200]}")
        self.shas[rel] = data["sha"]
        if data.get("encoding") == "none" or (not data.get("content") and data.get("size")):
            # Files over 1 MB come back without content; ask for the raw file instead.
            code, raw = self._req("GET", f"contents/state/{rel}?ref={self.branch}", raw=True)
            if code != 200:
                raise StateError(f"GitHub read {rel}: HTTP {code}")
            return raw
        return json.loads(base64.b64decode(data["content"]).decode("utf-8"))

    def _ensure_branch(self):
        code, _ = self._req("GET", f"branches/{self.branch}")
        if code == 200:
            return
        code, repo = self._req("GET", "")
        if code != 200:
            raise StateError(f"cannot read repo {self.repo}: HTTP {code}")
        code, ref = self._req("GET", f"git/ref/heads/{repo['default_branch']}")
        if code != 200:
            raise StateError(f"cannot read default branch of {self.repo}: HTTP {code}")
        code, out = self._req("POST", "git/refs", {"ref": f"refs/heads/{self.branch}", "sha": ref["object"]["sha"]})
        if code not in (201, 422):
            raise StateError(f"cannot create branch {self.branch}: HTTP {code} {str(out)[:200]}")

    def put(self, rel: str, data) -> None:
        content = base64.b64encode((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()).decode()
        for attempt in range(3):
            if rel not in self.shas:
                self.get(rel)
            body = {"message": f"anvil: update {rel}", "content": content, "branch": self.branch}
            if rel in self.shas:
                body["sha"] = self.shas[rel]
            code, out = self._req("PUT", f"contents/state/{rel}", body)
            if code in (200, 201):
                self.shas[rel] = out["content"]["sha"]
                return
            if code == 404 and attempt == 0:
                self._ensure_branch()
                continue
            if code in (409, 422) and "sha" in body:
                # Someone else wrote since we read it. Overwriting would lose their update.
                self.shas.pop(rel, None)
                raise StateError(f"{rel} was updated by another request at the same moment; try again")
            raise StateError(f"GitHub write {rel}: HTTP {code} {str(out)[:200]}")
        raise StateError(f"GitHub write {rel}: kept conflicting")


_gh: _GitHub | None = None


def read_state(rel: str, default=None):
    global _gh
    if backend() == "github":
        _gh = _gh or _GitHub()
        v = _gh.get(rel)
        return default if v is None else v
    return load(state() / rel, default)


def write_state(rel: str, data) -> None:
    global _gh
    if backend() == "github":
        _gh = _gh or _GitHub()
        _gh.put(rel, data)
        return
    save(state() / rel, data)
