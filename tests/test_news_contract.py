import copy
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_news", ROOT / "scripts" / "build_news.py"
)
assert SPEC is not None and SPEC.loader is not None
build_news = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_news)


class NewsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_item = json.loads(
            (ROOT / "news" / "items" / "n-0124eabefd.json").read_text(encoding="utf-8")
        )

    def item(self, item_id, posted_at, family_display):
        item = copy.deepcopy(self.base_item)
        item["id"] = item_id
        item["source"]["posted_at"] = posted_at
        item["project"]["family_slug"] = "aggregation-test"
        item["project"]["family_display"] = family_display
        return item

    def link(self, kind, is_primary, verified_at):
        return {
            "kind": kind,
            "url": "https://example.test/project",
            "host": "example.test",
            "lang": "en",
            "is_primary": is_primary,
            "status": "live",
            "verified_at": verified_at,
        }

    def test_project_uses_latest_display_and_preserves_history(self):
        older = self.item("n-1111111111", "2026-01-01", "Older Name")
        newer = self.item("n-2222222222", "2026-01-02", "Current Name")

        project = build_news.aggregate_project("aggregation-test", [newer, older])

        self.assertEqual(project["family_display"], "Current Name")
        self.assertEqual(
            project["family_display_variants"],
            [
                {
                    "family_display": "Older Name",
                    "first_seen": "2026-01-01",
                    "last_seen": "2026-01-01",
                    "news_ids": ["n-1111111111"],
                },
                {
                    "family_display": "Current Name",
                    "first_seen": "2026-01-02",
                    "last_seen": "2026-01-02",
                    "news_ids": ["n-2222222222"],
                },
            ],
        )

    def test_project_rejects_incompatible_display_on_any_same_date(self):
        left = self.item("n-3333333333", "2026-01-03", "One Name")
        right = self.item("n-4444444444", "2026-01-03", "Other Name")
        newest = self.item("n-5555555555", "2026-01-04", "Current Name")

        with self.assertRaisesRegex(build_news.ContractError, "conflicting family displays on date"):
            build_news.aggregate_project("aggregation-test", [newest, left, right])

    def test_link_rejects_incompatible_shared_metadata(self):
        left = self.item("n-4444444444", "2026-01-01", "Link Test")
        right = self.item("n-5555555555", "2026-01-02", "Link Test")
        left["links"] = [self.link("code", False, None)]
        right["links"] = [self.link("paper", True, "2026-01-02T00:00:00Z")]

        with self.assertRaisesRegex(build_news.ContractError, "conflicting non-null 'kind'"):
            build_news.aggregate_links([left, right])

    def test_link_preserves_contextual_primary_and_verification_metadata(self):
        older = self.item("n-6666666666", "2026-01-01", "Link Test")
        newer = self.item("n-7777777777", "2026-01-02", "Link Test")
        older["links"] = [self.link("other", False, None)]
        newer["links"] = [self.link("other", True, "2026-01-02T00:00:00Z")]

        link = build_news.aggregate_links([newer, older])[0]

        self.assertTrue(link["is_primary"])
        self.assertEqual(link["verified_at"], "2026-01-02T00:00:00Z")
        self.assertEqual(link["news_ids"], ["n-6666666666", "n-7777777777"])
        self.assertEqual(
            link["occurrences"],
            [
                {"news_id": "n-6666666666", "is_primary": False, "verified_at": None},
                {
                    "news_id": "n-7777777777",
                    "is_primary": True,
                    "verified_at": "2026-01-02T00:00:00Z",
                },
            ],
        )

    def test_schema_validated_must_be_literal_true(self):
        item = copy.deepcopy(self.base_item)
        item["_meta"]["schema_validated"] = False

        self.assertIn(
            "_meta.schema_validated: expected literal true",
            build_news.validate_item(item, item["id"] + ".json"),
        )


if __name__ == "__main__":
    unittest.main()
