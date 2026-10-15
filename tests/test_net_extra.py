"""Network-failure, time-budget and bot-wall tests. Everything is monkeypatched; no network."""
import io
import json
import os
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anvil import ember, llm, recon  # noqa: E402

# Captured at import: discovery imports every test module before running any test, and
# tests/test_anvil.py swaps ember.scrape / llm.polish for fakes without restoring them.
_REAL_SCRAPE = ember.scrape
_REAL_POLISH = llm.polish

JD = ("We are hiring a revops engineer to build and automate HubSpot workflows. You will own the "
      "data hygiene program and help us scale the go-to-market stack across regions. " * 3)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_scrape(payload):
    return FakeResp(json.dumps({"success": True, "data": payload}).encode())


class EmberScrapeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, ember, "scrape", ember.scrape)
        ember.scrape = _REAL_SCRAPE
        self.env = mock.patch.dict(os.environ, {"EMBER_API_KEY": "emb_test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _urlopen(self, side_effect):
        return mock.patch("urllib.request.urlopen", side_effect=side_effect)

    def test_success_returns_data_dict(self):
        with self._urlopen(lambda req, timeout=None: _ok_scrape({"markdown": "hi"})):
            self.assertEqual(ember.scrape("https://x.test")["markdown"], "hi")

    def test_timeout_and_reset_become_ember_error(self):
        for exc in (socket.timeout("timed out"), TimeoutError("t"), ConnectionResetError("reset")):
            with self.subTest(exc=type(exc).__name__), self._urlopen(lambda req, timeout=None, e=exc: (_ for _ in ()).throw(e)):
                with self.assertRaises(ember.EmberError):
                    ember.scrape("https://x.test")

    def test_read_timeout_during_body_becomes_ember_error(self):
        class Boom:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                raise socket.timeout("read timed out")

        with self._urlopen(lambda req, timeout=None: Boom()):
            with self.assertRaises(ember.EmberError):
                ember.scrape("https://x.test")

    def test_non_json_body_becomes_ember_error(self):
        with self._urlopen(lambda req, timeout=None: FakeResp(b"<html>502</html>")):
            with self.assertRaises(ember.EmberError):
                ember.scrape("https://x.test")

    def test_missing_data_key_becomes_ember_error(self):
        with self._urlopen(lambda req, timeout=None: FakeResp(b'{"success": true}')):
            with self.assertRaises(ember.EmberError):
                ember.scrape("https://x.test")

    def test_timeout_is_passed_through(self):
        seen = {}

        def fake(req, timeout=None):
            seen["timeout"] = timeout
            return _ok_scrape({"markdown": "x"})

        with self._urlopen(fake):
            ember.scrape("https://x.test", timeout=7)
        self.assertEqual(seen["timeout"], 7)


class LoadLeadsTests(unittest.TestCase):
    def test_url_error_becomes_ember_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(ember.EmberError):
                ember.load_leads()

    def test_os_error_becomes_ember_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(ember.EmberError):
                ember.load_leads()

    def test_utf8_bom_is_stripped_over_http(self):
        body = "﻿canonical_id,verdict\nlever:a:1,PASS\n".encode("utf-8")

        class R(FakeResp):
            pass

        with mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: R(body)):
            rows = ember.load_leads()
        self.assertEqual(rows[0]["canonical_id"], "lever:a:1")

    def test_utf8_bom_is_stripped_from_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "leads.csv"
            p.write_bytes("﻿canonical_id,verdict\nx,PASS\n".encode("utf-8"))
            rows = ember.load_leads(str(p))
        self.assertEqual(list(rows[0].keys()), ["canonical_id", "verdict"])

    def test_token_not_sent_to_other_hosts(self):
        seen = {}

        def fake(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["timeout"] = timeout
            return FakeResp(b"canonical_id\n")

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_secret"}), \
                mock.patch.object(ember, "LEADS_URL", "https://evil.example.com/open-leads.csv"), \
                mock.patch("urllib.request.urlopen", side_effect=fake):
            ember.load_leads()
        self.assertIsNone(seen["auth"])
        self.assertEqual(seen["timeout"], 15)

    def test_token_sent_to_github_raw_host(self):
        seen = {}

        def fake(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return FakeResp(b"canonical_id\n")

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_secret"}), \
                mock.patch("urllib.request.urlopen", side_effect=fake):
            ember.load_leads()
        self.assertEqual(seen["auth"], "Bearer ghp_secret")


class VerdictTests(unittest.TestCase):
    def test_actionable_tolerates_case_and_whitespace(self):
        leads = [{"verdict": " pass "}, {"verdict": "highpay"}, {"verdict": "Park\n"},
                 {"verdict": "REJECT"}, {"verdict": None}]
        got = [r["verdict"] for r in ember.actionable(leads)]
        self.assertEqual(got, [" pass ", "highpay", "Park\n"])


class LlmTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, llm, "polish", llm.polish)
        llm.polish = _REAL_POLISH
        self.env = mock.patch.dict(os.environ, {"EMBER_LLM_KEYS": "", "GROQ_API_KEY": "",
                                                "GEMINI_API_KEY": ""}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_malformed_ember_llm_keys_ignored(self):
        for raw in ("5", "[1]", "garbage", "{", '"str"', "null", "[null, 3, {}]"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"EMBER_LLM_KEYS": raw}):
                self.assertEqual(llm.candidates(), [])

    def test_valid_entry_still_parsed(self):
        raw = json.dumps([{"provider": "groq", "key": "k1"}, "junk", {"provider": "", "key": "x"}])
        with mock.patch.dict(os.environ, {"EMBER_LLM_KEYS": raw}):
            cands = llm.candidates()
        self.assertEqual([c["provider"] for c in cands], ["groq"])

    def test_gemini_key_in_header_not_url(self):
        seen = {}

        def fake_post(url, body, headers, timeout=None):
            seen.update(url=url, headers=headers, timeout=timeout)
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"lines": ["a"]})}]}}]}

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIza-secret"}), \
                mock.patch.object(llm, "_post", side_effect=fake_post):
            out = llm.polish(["a"], [], [])
        self.assertEqual(out, ["a"])
        self.assertNotIn("AIza-secret", seen["url"])
        self.assertEqual(seen["headers"], {"x-goog-api-key": "AIza-secret"})
        self.assertLessEqual(seen["timeout"], 12)

    def test_provider_exception_does_not_break_polish(self):
        calls = []

        def fake_call(c, system, user, timeout=None):
            calls.append(c["provider"])
            if c["provider"] == "groq":
                raise RuntimeError("provider exploded")
            return json.dumps({"lines": ["b"]})

        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "x"}), \
                mock.patch.object(llm, "_call", side_effect=fake_call):
            self.assertEqual(llm.polish(["a"], [], []), ["b"])
        self.assertEqual(calls, ["groq", "google"])

    def test_at_most_two_providers_tried(self):
        calls = []

        def always_fail(c, system, user, timeout=None):
            calls.append(c["provider"])
            raise socket.timeout("slow")

        raw = json.dumps([{"provider": "openai", "key": "a"}, {"provider": "mistral", "key": "b"},
                          {"provider": "xai", "key": "c"}])
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "g", "EMBER_LLM_KEYS": raw}), \
                mock.patch.object(llm, "_call", side_effect=always_fail):
            self.assertIsNone(llm.polish(["a"], [], []))
        self.assertEqual(len(calls), 2)

    def test_all_failures_return_none_not_raise(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "g"}), \
                mock.patch.object(llm, "_call", side_effect=ValueError("bad")):
            self.assertIsNone(llm.polish(["a"], [], []))


class _Store:
    """In-memory stand-in for store.read_state/write_state so recon tests never touch disk."""

    def __init__(self):
        self.saved = {}

    def read(self, rel, default=None):
        return default

    def write(self, rel, data):
        self.saved[rel] = data


class ReconTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, ember, "scrape", ember.scrape)
        ember.scrape = _REAL_SCRAPE
        self.store = _Store()
        for patch in (mock.patch.object(recon.store, "read_state", side_effect=self.store.read),
                      mock.patch.object(recon.store, "write_state", side_effect=self.store.write)):
            patch.start()
            self.addCleanup(patch.stop)
        self.lead = {"canonical_id": "lever:acme:1", "company": "Acme Labs", "role": "RevOps",
                     "url": "https://jobs.lever.co/acme/1"}

    def test_bot_wall_jd_raises_and_is_not_cached(self):
        wall = {"title": "Just a moment...", "markdown": "Checking your browser before accessing " * 10}
        with mock.patch.object(recon.ember, "scrape", return_value=wall):
            with self.assertRaises(recon.ember.EmberError) as cm:
                recon.gather(self.lead, log=lambda *_: None)
        self.assertIn("jd_text", str(cm.exception))
        self.assertEqual(self.store.saved, {})

    def test_enable_javascript_wall_on_jd_rejected(self):
        wall = {"title": "acme", "markdown": "Enable JavaScript and cookies to continue. " * 20}
        with mock.patch.object(recon.ember, "scrape", return_value=wall):
            with self.assertRaises(recon.ember.EmberError):
                recon.gather(self.lead, log=lambda *_: None)
        self.assertEqual(self.store.saved, {})

    def test_real_jd_mentioning_cloudflare_is_accepted(self):
        page = {"title": "Solutions Engineer", "markdown": "Cloudflare is hiring. " + JD}
        with mock.patch.object(recon.ember, "scrape", return_value=page):
            rec = recon.gather(self.lead, company_url="", log=lambda *_: None)
        self.assertTrue(rec["jd"].startswith("Cloudflare is hiring"))

    def test_bot_wall_about_page_is_skipped(self):
        def fake(url, max_tokens=2500, timeout=15):
            if url.endswith("/about"):
                return {"title": "Attention Required!", "markdown": "x" * 500}
            if url.endswith("/about-us") or url.endswith("/company"):
                raise recon.ember.EmberError("404")
            if url.rstrip("/") == "https://acme.com":
                return {"title": "Acme Labs - revops platform", "markdown": "Acme Labs builds a platform. " * 5}
            return {"title": "job", "markdown": JD}

        with mock.patch.object(recon.ember, "scrape", side_effect=fake):
            rec = recon.gather(self.lead, company_url="https://acme.com", log=lambda *_: None)
        self.assertEqual(rec["company_pages"], ["https://acme.com"])

    def test_time_budget_skips_company_sites_and_caps_timeouts(self):
        calls = []

        def fake(url, max_tokens=2500, timeout=15):
            calls.append((url, timeout))
            return {"title": "job", "markdown": JD}

        with mock.patch.object(recon.ember, "scrape", side_effect=fake):
            rec = recon.gather(self.lead, company_url="https://acme.com", budget=3, log=lambda *_: None)
        self.assertEqual([u for u, _ in calls], ["https://jobs.lever.co/acme/1"])
        self.assertEqual(rec["company_pages"], [])
        self.assertLessEqual(calls[0][1], 15)

    def test_per_call_timeout_capped_at_15(self):
        calls = []

        def fake(url, max_tokens=2500, timeout=15):
            calls.append(timeout)
            if url == "https://jobs.lever.co/acme/1":
                return {"title": "job", "markdown": JD}
            raise recon.ember.EmberError("404")

        with mock.patch.object(recon.ember, "scrape", side_effect=fake):
            recon.gather(self.lead, company_url="https://acme.com", log=lambda *_: None)
        self.assertTrue(all(1.0 <= t <= 15.0 for t in calls), calls)

    def test_blank_company_name_does_not_crash(self):
        lead = dict(self.lead, company="   ")
        with mock.patch.object(recon.ember, "scrape", return_value={"title": "job", "markdown": JD}):
            rec = recon.gather(lead, jd_text=JD, company_url="https://acme.com", log=lambda *_: None)
        self.assertEqual(rec["company"], "   ")
        self.assertEqual(recon.product_summary(["Some text. " * 10], "   "), [])


if __name__ == "__main__":
    unittest.main()
