"""Thompson-sampling bandit over genes, per role cluster.

state/bandit-log.json
  genes:  {cluster: {gene_id: {alpha, beta}}}      alpha = wins + 1, beta = losses + 1
  hooks:  {cluster: {hook_id: {sent, positive, deprecated}}}
  golden: {cluster: [recipe, ...]}                   CVs that got a callback or better
  events: [ {at, canonical_id, outcome, cluster} ]
state/applications.json
  [ {canonical_id, company, role, cluster, dna_hash, hook_id, genes_used, recipe, applied_at, outcome, ...} ]
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import store

POSITIVE = {"callback": 2, "interview": 2, "offer": 2}  # alpha increment per gene
NEGATIVE = {"rejected": 1, "ghosted": 1}               # beta increment per gene
DEPRECATE_AFTER = 8   # sends of one hook with zero positive replies
GHOST_DAYS = 14


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_log() -> dict:
    log = store.read_state("bandit-log.json") or {}
    for k in ("genes", "hooks", "golden"):
        log.setdefault(k, {})
    log.setdefault("events", [])
    return log


def save_log(log: dict) -> None:
    store.write_state("bandit-log.json", log)


def load_apps() -> list[dict]:
    return store.read_state("applications.json") or []


def save_apps(apps: list[dict]) -> None:
    store.write_state("applications.json", apps)


def arm(log: dict, cluster: str, gene_id: str) -> dict:
    return log["genes"].setdefault(cluster, {}).setdefault(gene_id, {"alpha": 1, "beta": 1})


def sample(log: dict, cluster: str, gene_id: str, rng: random.Random) -> float:
    a = arm(log, cluster, gene_id)
    return rng.betavariate(a["alpha"], a["beta"])


def mean(log: dict, cluster: str, gene_id: str) -> float:
    a = log["genes"].get(cluster, {}).get(gene_id, {"alpha": 1, "beta": 1})
    return a["alpha"] / (a["alpha"] + a["beta"])


def hook_state(log: dict, cluster: str, hook_id: str) -> dict:
    return log["hooks"].setdefault(cluster, {}).setdefault(
        hook_id, {"sent": 0, "positive": 0, "deprecated": False})


def is_deprecated(log: dict, cluster: str, hook_id: str) -> bool:
    return log["hooks"].get(cluster, {}).get(hook_id, {}).get("deprecated", False)


def record_application(app: dict) -> None:
    apps = load_apps()
    apps = [a for a in apps if not (a["canonical_id"] == app["canonical_id"] and a["outcome"] == "PENDING")]
    apps.append(app)
    save_apps(apps)
    log = load_log()
    hook_state(log, app["cluster"], app["hook_id"])["sent"] += 1
    _maybe_deprecate(log, app["cluster"], app["hook_id"])
    save_log(log)


def _maybe_deprecate(log: dict, cluster: str, hook_id: str) -> None:
    h = hook_state(log, cluster, hook_id)
    if h["sent"] >= DEPRECATE_AFTER and h["positive"] == 0:
        h["deprecated"] = True


def apply_outcome(canonical_id: str, outcome: str, note: str = "") -> dict:
    outcome = outcome.lower()
    if outcome not in POSITIVE and outcome not in NEGATIVE:
        raise SystemExit(f"unknown outcome {outcome!r}; use callback, interview, offer, rejected or ghosted")
    apps = load_apps()
    matches = [a for a in apps if a["canonical_id"] == canonical_id]
    if not matches:
        raise SystemExit(f"no application logged for {canonical_id}. Run `anvil tailor` for it first.")
    app = matches[-1]
    prev = app["outcome"]
    log = load_log()
    cl = app["cluster"]
    # Undo a previous negative if the company replies late (ghosted -> callback).
    if prev.lower() in NEGATIVE and outcome in POSITIVE:
        for g in app["genes_used"]:
            a = arm(log, cl, g)
            a["beta"] = max(1, a["beta"] - NEGATIVE[prev.lower()])
    already_positive = prev.lower() in POSITIVE
    if outcome in POSITIVE and not already_positive:
        for g in app["genes_used"]:
            arm(log, cl, g)["alpha"] += POSITIVE[outcome]
        h = hook_state(log, cl, app["hook_id"])
        h["positive"] += 1
        h["deprecated"] = False
        golden = log["golden"].setdefault(cl, [])
        if not any(r["dna_hash"] == app["dna_hash"] for r in golden):
            golden.append({"dna_hash": app["dna_hash"], "recipe": app["recipe"], "company": app["company"],
                           "role": app["role"], "outcome": outcome, "at": now()})
        else:
            for r in golden:
                if r["dna_hash"] == app["dna_hash"]:
                    r["outcome"] = outcome
    elif outcome in NEGATIVE and prev == "PENDING":
        for g in app["genes_used"]:
            arm(log, cl, g)["beta"] += NEGATIVE[outcome]
    app["outcome"] = outcome.upper()
    app["outcome_at"] = now()
    if note:
        app["note"] = note
    log["events"].append({"at": now(), "canonical_id": canonical_id, "outcome": outcome, "cluster": cl,
                          "dna_hash": app["dna_hash"]})
    save_log(log)
    save_apps(apps)
    return app


def ghost_sweep(days: int = GHOST_DAYS) -> list[str]:
    """Mark PENDING applications older than `days` as ghosted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    ghosted = []
    for a in load_apps():
        if a["outcome"] == "PENDING" and not a.get("dry_run"):
            try:
                at = datetime.fromisoformat(a["applied_at"])
            except ValueError:
                continue
            if at < cutoff:
                ghosted.append(a["canonical_id"])
    for cid in ghosted:
        apply_outcome(cid, "ghosted", note=f"auto: no reply in {days} days")
    return ghosted
