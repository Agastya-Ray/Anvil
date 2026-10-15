"""Thompson-sampling bandit over genes, per role cluster.

state/bandit-log.json
  genes:  {cluster: {gene_id: {alpha, beta}}}      alpha = wins + 1, beta = losses + 1
  hooks:  {cluster: {hook_id: {sent, positive, replies, deprecated}}}
  golden: {cluster: [recipe, ...]}                   CVs that got a callback or better
  events: [ {at, canonical_id, outcome, cluster} ]
state/applications.json
  [ {canonical_id, company, role, cluster, dna_hash, hook_id, genes_used, recipe, applied_at, outcome,
     credited, ...} ]
  credited: "positive" | "negative" | None. Each application is credited at most once; see apply_outcome.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import store

POSITIVE = {"callback": 2, "interview": 2, "offer": 2}  # alpha increment per gene
NEGATIVE = {"rejected": 1, "ghosted": 1}               # beta increment per gene
NO_REPLY = {"PENDING", "GHOSTED"}                      # outcomes that are not a reply from the company
DEPRECATE_AFTER = 8   # sends of one hook with zero replies
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
        hook_id, {"sent": 0, "positive": 0, "replies": 0, "deprecated": False})


def is_deprecated(log: dict, cluster: str, hook_id: str) -> bool:
    return log["hooks"].get(cluster, {}).get(hook_id, {}).get("deprecated", False)


def _reply_count(h: dict) -> int:
    # Logs written before `replies` existed: a positive is always a reply.
    return max(h.get("replies", 0), h.get("positive", 0))


def _refresh_deprecation(h: dict) -> None:
    """Deprecated means 8+ sends and no reply at all. Recomputed, so a reply lifts it again."""
    h["deprecated"] = h["sent"] >= DEPRECATE_AFTER and _reply_count(h) == 0


def _credit(app: dict) -> str | None:
    """Which credit this application already carries. Rows from before the flag existed are inferred."""
    if "credited" in app:
        return app["credited"]
    prev = str(app.get("outcome", "")).lower()
    if prev in POSITIVE:
        return "positive"
    if prev in NEGATIVE:
        return "negative"
    return None


def record_application(app: dict) -> None:
    apps = load_apps()
    prior = [a for a in apps if a["canonical_id"] == app["canonical_id"]]
    pending = [a for a in prior if a["outcome"] == "PENDING"]
    apps = [a for a in apps if not (a["canonical_id"] == app["canonical_id"] and a["outcome"] == "PENDING")]
    apps.append(app)
    save_apps(apps)
    log = load_log()
    touched = []
    if not prior:
        # Only a new canonical_id is a send.
        hook_state(log, app["cluster"], app["hook_id"])["sent"] += 1
        touched.append((app["cluster"], app["hook_id"]))
    elif pending and pending[-1]["hook_id"] != app["hook_id"]:
        # Re-tailored before any reply: the send moves from the old hook to the new one.
        old = pending[-1]
        oh = hook_state(log, old["cluster"], old["hook_id"])
        oh["sent"] = max(0, oh["sent"] - 1)
        hook_state(log, app["cluster"], app["hook_id"])["sent"] += 1
        touched += [(old["cluster"], old["hook_id"]), (app["cluster"], app["hook_id"])]
    for cl, hid in touched:
        _refresh_deprecation(hook_state(log, cl, hid))
    save_log(log)


def apply_outcome(canonical_id: str, outcome: str, note: str = "") -> dict:
    outcome = outcome.lower()
    if outcome not in POSITIVE and outcome not in NEGATIVE:
        raise SystemExit(f"unknown outcome {outcome!r}; use callback, interview, offer, rejected or ghosted")
    apps = load_apps()
    matches = [a for a in apps if a["canonical_id"] == canonical_id]
    if not matches:
        raise SystemExit(f"no application logged for {canonical_id}. Run `anvil tailor` for it first.")
    app = matches[-1]
    prev = str(app["outcome"]).upper()
    if prev == outcome.upper():
        return app  # same outcome twice: nothing changes
    log = load_log()
    cl = app["cluster"]
    genes = app["genes_used"]
    h = hook_state(log, cl, app["hook_id"])
    credit = _credit(app)

    if prev in NO_REPLY and (outcome in POSITIVE or outcome == "rejected"):
        h["replies"] = h.get("replies", 0) + 1

    if outcome in POSITIVE:
        if credit != "positive":
            if credit == "negative":
                # Late positive after a negative: take back that beta (never below 1).
                for g in genes:
                    a = arm(log, cl, g)
                    a["beta"] = max(1, a["beta"] - 1)
            for g in genes:
                arm(log, cl, g)["alpha"] += POSITIVE[outcome]
            h["positive"] += 1
            golden = log["golden"].setdefault(cl, [])
            if not any(r["dna_hash"] == app["dna_hash"] for r in golden):
                golden.append({"dna_hash": app["dna_hash"], "recipe": app["recipe"], "company": app["company"],
                               "role": app["role"], "outcome": outcome, "at": now()})
            else:
                for r in golden:
                    if r["dna_hash"] == app["dna_hash"]:
                        r["outcome"] = outcome
            credit = "positive"
    elif credit is None:
        # Negative on an uncredited application. A positive credit, if any, is kept.
        for g in genes:
            arm(log, cl, g)["beta"] += NEGATIVE[outcome]
        credit = "negative"

    app["credited"] = credit
    _refresh_deprecation(h)
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
