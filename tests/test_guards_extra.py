"""Regression tests for the guardrail fixes: client scope, Unicode, banned words,
dashes, number magnitudes, and ingest-time blocking."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anvil import guards, ingest  # noqa: E402

SKELETON = json.loads((ROOT / "vault" / "skeleton.json").read_text(encoding="utf-8"))


def rules(report) -> list[str]:
    return [v.rule for v in report.violations]


class ClientScopeTests(unittest.TestCase):
    def check(self, text):
        return guards.check_client_scope(text, SKELETON)

    def test_pricing_in_same_bullet_as_petronas_fails(self):
        self.assertFalse(self.check("Petronas – trade data import/export; pricing configuration").ok)

    def test_split_separator_does_not_hide_forbidden_term(self):
        # The old splitter cut at ";" and "."; the whole bullet must be judged.
        self.assertFalse(self.check("Petronas – import/export; prices set up for clients").ok)
        self.assertFalse(self.check("Petronas import/export. Pricing rules were configured").ok)

    def test_stems_pric_tariff_ratecard_configur(self):
        for t in ["Petronas tariff tables", "Petronas rate card updates", "Petronas price list",
                  "Petronas configuration of the module", "Petronas configure tool",
                  "Petronas Pricing"]:
            self.assertFalse(self.check(t).ok, t)

    def test_import_export_only_passes(self):
        self.assertTrue(self.check("Petronas – trade data import/export").ok)
        # Allow-list is not enforced here: other Petronas work terms still pass.
        self.assertTrue(self.check("Petronas – client exposure across implementation and pre-sales").ok)

    def test_zero_width_and_homoglyph_cannot_hide_pricing(self):
        self.assertFalse(self.check("Petronas – Pri​cing of trades").ok)
        self.assertFalse(self.check("Petronas – рricing of trades").ok)  # Cyrillic р
        self.assertFalse(self.check("Petronas – pri­cing").ok)           # soft hyphen

    def test_pricing_without_petronas_is_not_a_client_scope_violation(self):
        self.assertTrue(self.check("CTRM domain: pricing indexes and raw prices").ok)


class UnicodeTests(unittest.TestCase):
    def test_format_chars_stripped_before_style_check(self):
        self.assertTrue(any(v.rule == "banned_word" for v in guards.check_style("lever­aging data", SKELETON).violations))
        self.assertTrue(any(v.rule == "banned_word" for v in guards.check_style("lever​aged data", SKELETON).violations))

    def test_cyrillic_lookalikes_caught(self):
        self.assertFalse(guards.check_style("сutting edge tools", SKELETON).ok)  # Cyrillic с
        self.assertFalse(guards.check_style("rоbust pipeline", SKELETON).ok)  # Cyrillic о

    def test_fullwidth_dash_normalised(self):
        self.assertFalse(guards.check_style("Trade － reporting", SKELETON).ok)


class BannedWordTests(unittest.TestCase):
    def banned(self, text):
        return [v for v in guards.check_style(text, SKELETON).violations if v.rule == "banned_word"]

    def test_word_stems_caught(self):
        for w in ["leverages", "leveraging", "leveraged", "spearheading", "spearheaded",
                  "orchestrating", "orchestrated", "robustly", "robustness", "delving",
                  "championed", "seamlessly", "cutting edge", "cutting-edge"]:
            self.assertTrue(self.banned(f"I was {w} the work."), w)

    def test_hyphen_and_space_equivalent(self):
        self.assertTrue(self.banned("A cutting edge approach"))
        self.assertTrue(self.banned("A cutting-edge approach"))

    def test_ordinary_words_pass(self):
        self.assertEqual(self.banned("Managed functional testing and trade creation."), [])


class DashTests(unittest.TestCase):
    def chars(self, text):
        return [v for v in guards.check_style(text, SKELETON).violations if v.rule == "banned_char"]

    def test_banned_dash_variants(self):
        for ch in ["―", "‒", "⸺", "－", "&mdash;", "--", "—"]:
            self.assertTrue(self.chars(f"Trade{ch}reporting"), repr(ch))

    def test_spaced_hyphen_between_words_banned(self):
        self.assertTrue(self.chars("Trade - reporting"))

    def test_en_dash_and_compound_hyphen_allowed(self):
        self.assertEqual(self.chars("Apr 2026 – Present"), [])
        self.assertEqual(self.chars("Power BI-based dashboards"), [])


class NumberTests(unittest.TestCase):
    def facts(self, src, out):
        return guards.assert_no_new_facts(src, out)

    def test_magnitude_change_fails(self):
        self.assertIn("new_number", rules(self.facts("Saved 2.5 million", "Saved 2.5 billion")))

    def test_suffix_added_fails(self):
        self.assertIn("new_number", rules(self.facts("Reviewed 50 files", "Reviewed 50K files")))

    def test_number_word_for_digits_fails(self):
        self.assertIn("new_number", rules(self.facts("7 of 17 done", "seven of seventeen done")))

    def test_same_form_passes(self):
        self.assertTrue(self.facts("2.5 million and 7 of 17", "2.5 million and 7 of 17").ok)
        self.assertTrue(self.facts("saved 2.5m", "saved 2.5 million").ok)
        self.assertTrue(self.facts("Reviewed 50 files", "Reviewed 50 files").ok)

    def test_number_words_in_source_pass(self):
        self.assertTrue(self.facts("seven of seventeen", "seven of seventeen").ok)

    def test_check_document_rejects_word_not_in_cv(self):
        known = guards.numbers("7 of 17 done")
        r = guards.check_document(["seven of seventeen"], SKELETON, set(), known)
        self.assertIn("new_number", rules(r))


class IngestTests(unittest.TestCase):
    def build(self, cv_text, overrides):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "10-test.md"
            p.write_text(cv_text, encoding="utf-8")
            return ingest.build_pool([p], SKELETON, overrides)

    CV = """SWASTIK KUMAR RAY
Product Analyst
Summary
Analyst with testing focus on trade data.
Experience
Product Analyst – Fendahl (Fusion CTRM), Nagpur | Apr 2026 – Present
- Ran functional testing on trade creation.
Projects
Cutting-edge Demo | 2025
- Built a small trade tool.
"""

    def test_project_title_is_checked_at_ingest(self):
        pool = self.build(self.CV, {})
        proj = [g for g in pool["genes"].values() if g["kind"] == "project"]
        self.assertEqual(len(proj), 1)
        self.assertTrue(proj[0]["blocked"])
        self.assertTrue(any("cutting" in r for r in proj[0]["block_reasons"]))

    def test_overrides_cannot_unblock_style_or_scope_failures(self):
        probe = self.build(self.CV, {})
        pid = next(g["id"] for g in probe["genes"].values() if g["kind"] == "project")
        pool = self.build(self.CV, {"unblock": [pid]})
        g = pool["genes"][pid]
        self.assertTrue(g["blocked"])
        self.assertTrue(any("unblock refused" in r for r in g["block_reasons"]))

    def test_override_block_still_applies_to_clean_gene(self):
        probe = self.build(self.CV, {})
        bid = next(g["id"] for g in probe["genes"].values() if g["kind"] == "bullet")
        self.assertFalse(probe["genes"][bid]["blocked"])
        pool = self.build(self.CV, {"block": [bid]})
        self.assertTrue(pool["genes"][bid]["blocked"])


if __name__ == "__main__":
    unittest.main()
