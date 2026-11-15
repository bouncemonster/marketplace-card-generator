"""Offline regression for unsupported product claims; never calls an image provider."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

MODULE_FILE = 'cardgen.py'
spec = importlib.util.spec_from_file_location("qa_subject", Path(__file__).resolve().parents[1] / MODULE_FILE)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

# Existing BROVI 100645945662 QA was raw hard but persisted as soft.
BROVI_VERDICT = {
    "product_match": True, "product_color_ok": True,
    "title_color_pink": True, "title_product_overlap": True,
    "headline_legible": True, "follows_design_brief": False,
    "badges_style_ok": True, "no_sku_leak": True,
    "text_readable_ru": True, "no_invented_facts": False,
    "font_style_ok": True, "garbled_words": [],
    "verdict": "hard", "hard_fails": ["follows_design_brief"],
}
SEEDREAM = "bytedance-seed/seedream-5-0-flash"


class FactualityTests(unittest.TestCase):
    def valid_verdict(self):
        return {**BROVI_VERDICT, "follows_design_brief": True,
                "no_invented_facts": True, "verdict": "pass", "hard_fails": []}

    def test_historical_brovi_claim_failure_is_hard_for_every_engine(self):
        for model in (SEEDREAM, "google/gemini-3.1-flash-image", "openai/gpt-image-2", None):
            with self.subTest(model=model):
                self.assertEqual(subject.classify_qa([], BROVI_VERDICT, model), "hard")

    def test_claim_failure_overrides_model_pass(self):
        v = {**self.valid_verdict(), "no_invented_facts": False}
        self.assertEqual(subject.classify_qa([], v, SEEDREAM), "hard")

    def test_seedream_style_only_failure_remains_soft(self):
        v = {**self.valid_verdict(), "title_product_overlap": False,
             "follows_design_brief": False, "verdict": "hard"}
        self.assertEqual(subject.classify_qa([], v, SEEDREAM), "soft")

    def test_valid_card_passes(self):
        self.assertEqual(subject.classify_qa([], self.valid_verdict(), SEEDREAM), "pass")

    def test_undersized_card_remains_hard(self):
        self.assertEqual(subject.classify_qa(["короткая сторона 1024px < 1080px"],
                                           self.valid_verdict(), SEEDREAM), "hard")

    def test_provider_error_remains_error(self):
        self.assertEqual(subject.classify_qa([], {"verdict": "error"}, SEEDREAM), "error")

    def test_repair_names_unsupported_claims_once(self):
        fix = getattr(subject, "build_regen_addendum", None) or subject.build_fix_addendum
        addendum = fix({**self.valid_verdict(), "no_invented_facts": False}, SEEDREAM)
        self.assertEqual(addendum.count("Выдуманные характеристики"), 1)
        self.assertIn("свойства и числа", addendum)

    def test_qa_receives_product_description_and_claim_criterion(self):
        prod = {"title": "БРОВИ JW-15 Сочный арбуз", "description": "<p>Объём 15 мл.</p>"}
        response = {"choices": [{"message": {"content": json.dumps(self.valid_verdict())}}]}
        with mock.patch.object(subject, "photo_data_url", return_value="data:image/jpeg;base64,b2ZmbGluZQ=="):
            if MODULE_FILE == "tilda_cards.py":
                with mock.patch.object(subject.urllib.request, "urlopen",
                                       return_value=io.BytesIO(json.dumps(response).encode())) as transport:
                    subject.vlm_qa_image(prod, "original.jpg", "card.jpg", "offline-test", "offline-qa")
                    body = json.loads(transport.call_args.args[0].data)
                    text = body["messages"][0]["content"][0]["text"]
            else:
                prof = {"language": "Русский", "models": {"qa": "offline-qa"}}
                with mock.patch.object(subject, "chat", return_value=response) as transport:
                    subject.vlm_qa(prod, "original.jpg", "card.jpg", "offline-test", prof, None)
                    text = transport.call_args.args[0][0]["content"][0]["text"]
        self.assertIn("Объём 15 мл.", text)
        self.assertNotIn("<p>", text)
        self.assertIn('"no_invented_facts"', text)
        self.assertIn("no_invented_facts=false", text)
        self.assertIn("дизайн-бриф сам по себе не подтверждает свойства", text)


if __name__ == "__main__":
    unittest.main()
