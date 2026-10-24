import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("translate_items", ROOT / "scripts" / "translate_items.py")
ti = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ti)

SOURCE = {
    "title": "FLUX.2 [klein] 9B ships a LoRA trainer",
    "summary": "Black Forest Labs released FLUX.2 klein 9B with `diffusers` support; weights at https://huggingface.co/black-forest-labs/FLUX.2-klein-9B today.",
}
GOOD_DE = {
    "title": "FLUX.2 [klein] 9B erhält einen LoRA-Trainer",
    "summary": "Black Forest Labs hat FLUX.2 klein 9B mit `diffusers`-Unterstützung veröffentlicht; Gewichte unter https://huggingface.co/black-forest-labs/FLUX.2-klein-9B heute.",
}


class CheckTranslation(unittest.TestCase):
    def test_a_faithful_translation_passes(self):
        self.assertEqual(ti.check_translation("de", SOURCE, GOOD_DE), GOOD_DE)

    def test_each_broken_rule_is_refused(self):
        cases = {
            "keys": {"title": GOOD_DE["title"]},
            "empty": {**GOOD_DE, "title": " "},
            "url": {**GOOD_DE, "summary": GOOD_DE["summary"].replace("FLUX.2-klein-9B heute", "FLUX2 heute")},
            "code": {**GOOD_DE, "summary": GOOD_DE["summary"].replace("`diffusers`", "diffusers")},
            "name": {**GOOD_DE, "title": "FLUX 2 [klein] 9B erhält einen LoRA-Trainer"},
            "untranslated": dict(SOURCE),
        }
        for name, answer in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                ti.check_translation("de", SOURCE, answer)

    def test_script_languages_must_be_in_their_script(self):
        with self.assertRaises(ValueError):
            ti.check_translation("ru", SOURCE, GOOD_DE)

    def test_hash_ignores_fields_that_are_not_translated(self):
        item = {"title": "A", "summary": "B", "research_notes": "", "evolution": {"summary": None}, "links": [1]}
        other = {**item, "links": [2]}
        self.assertEqual(ti.source_hash(ti.source_fields(item)), ti.source_hash(ti.source_fields(other)))
        self.assertNotEqual(ti.source_hash(ti.source_fields(item)),
                            ti.source_hash(ti.source_fields({**item, "summary": "C"})))

    def test_answer_in_a_code_fence_is_read(self):
        self.assertEqual(ti.parse_answer('```json\n{"title": "x"}\n```'), {"title": "x"})


class StoredTranslations(unittest.TestCase):
    def test_committed_translations_are_valid_and_bundles_current(self):
        if not ti.OUT.is_dir():
            self.skipTest("no translations yet")
        self.assertEqual(ti.cmd_check(None), 0)


if __name__ == "__main__":
    unittest.main()
