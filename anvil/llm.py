"""Optional LLM polish (light synonym swaps only). Same env vars as Ember:
GROQ_API_KEY, GEMINI_API_KEY, EMBER_LLM_KEYS (JSON array of {provider, key, model?, baseUrl?}).

Every output is re-checked in code by guards.assert_no_new_facts; anything that
fails is thrown away and the verbatim gene is used instead.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "xai": ("https://api.x.ai/v1", "grok-4.5"),
}
GEMINI_DEFAULT = os.environ.get("ANVIL_GEMINI_MODEL", "gemini-2.5-flash")
MAX_OUT_TOKENS = 300
CALL_TIMEOUT_S = 12      # per provider call; polish runs inside a 60 s serverless function
MAX_PROVIDER_TRIES = 2


def candidates() -> list[dict]:
    """Usable LLM providers in preference order. Malformed EMBER_LLM_KEYS is ignored, never raised."""
    out = []
    groq = os.environ.get("GROQ_API_KEY", "").strip()
    if groq:
        out.append({"provider": "groq", "key": groq, "model": os.environ.get("ANVIL_GROQ_MODEL")})
    gemini = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini:
        out.append({"provider": "google", "key": gemini, "model": GEMINI_DEFAULT})
    raw = os.environ.get("EMBER_LLM_KEYS", "")
    if raw.strip():
        try:
            entries = json.loads(raw)
        except Exception:  # noqa: BLE001 - bad config must not break tailoring
            entries = []
        if isinstance(entries, list):
            for e in entries:
                if not isinstance(e, dict):
                    continue
                prov, key = e.get("provider"), e.get("key")
                if not (isinstance(prov, str) and prov.strip() and isinstance(key, str) and key.strip()):
                    continue
                model = e.get("model") if isinstance(e.get("model"), str) else None
                base = e.get("baseUrl") if isinstance(e.get("baseUrl"), str) else None
                out.append({"provider": prov.strip(), "key": key.strip(), "model": model, "baseUrl": base})
    return out


def _post(url: str, body: dict, headers: dict, timeout: float = CALL_TIMEOUT_S) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _call(c: dict, system: str, user: str, timeout: float = CALL_TIMEOUT_S) -> str:
    p = c["provider"]
    if p == "google":
        model = c.get("model") or GEMINI_DEFAULT
        # Key goes in a header, never in the URL (URLs end up in logs and error messages).
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        cfg = {"temperature": 0.2, "maxOutputTokens": MAX_OUT_TOKENS, "responseMimeType": "application/json"}
        if "2.5" in model:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        data = _post(url, {"systemInstruction": {"parts": [{"text": system}]},
                           "contents": [{"role": "user", "parts": [{"text": user}]}],
                           "generationConfig": cfg}, {"x-goog-api-key": c["key"]}, timeout=timeout)
        return data["candidates"][0]["content"]["parts"][0]["text"]
    if p == "anthropic":
        data = _post("https://api.anthropic.com/v1/messages",
                     {"model": c.get("model") or "claude-3-5-haiku-latest", "max_tokens": MAX_OUT_TOKENS,
                      "system": system, "messages": [{"role": "user", "content": user}]},
                     {"x-api-key": c["key"], "anthropic-version": "2023-06-01"}, timeout=timeout)
        return data["content"][0]["text"]
    base, model = OPENAI_COMPAT.get(p, (c.get("baseUrl"), c.get("model")))
    base = c.get("baseUrl") or base
    if not base:
        raise RuntimeError(f"no base URL for provider {p}")
    data = _post(base.rstrip("/") + "/chat/completions",
                 {"model": c.get("model") or model, "temperature": 0.2, "max_tokens": MAX_OUT_TOKENS,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                 {"authorization": f"Bearer {c['key']}"}, timeout=timeout)
    return data["choices"][0]["message"]["content"]


SYSTEM = (
    "You lightly edit resume lines so their wording matches a job description. "
    "Allowed: swap a word or short phrase for a synonym the job description uses, from the APPROVED TERMS list "
    "or plain English synonyms. Forbidden: adding any number, name, tool, client, company, metric or claim; "
    "removing numbers; using em dashes; words like spearheaded, orchestrated, championed, leveraged, delved. "
    "Keep each line's meaning, length and tone. If no safe swap exists, return the line unchanged. "
    'Reply with JSON only: {"lines": ["...", "..."]} in the same order.'
)


def polish(lines: list[str], jd_terms: list[str], approved: list[str]) -> list[str] | None:
    """Returns rewritten lines (same length) or None if no key / every tried provider failed.
    Never raises: a polish failure must not break tailoring."""
    if not lines:
        return lines
    user = json.dumps({"job_terms": jd_terms[:25], "approved_terms": approved[:20], "lines": lines},
                      ensure_ascii=False)
    try:
        tries = candidates()[:MAX_PROVIDER_TRIES]
    except Exception:  # noqa: BLE001
        return None
    for c in tries:
        try:
            txt = _call(c, SYSTEM, user, timeout=CALL_TIMEOUT_S)
            m = re.search(r"\{.*\}", txt, re.S)
            data = json.loads(m.group(0) if m else txt)
            out = data.get("lines") if isinstance(data, dict) else data
            if isinstance(out, list) and len(out) == len(lines) and all(isinstance(x, str) for x in out):
                return [x.strip() for x in out]
        except Exception:  # noqa: BLE001 - try the next provider
            continue
    return None
