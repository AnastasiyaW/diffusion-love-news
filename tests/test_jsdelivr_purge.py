import importlib.util
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "purge_jsdelivr", ROOT / "scripts" / "purge_jsdelivr.py"
)
assert SPEC is not None and SPEC.loader is not None
purge_jsdelivr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(purge_jsdelivr)


class JsDelivrPurgeTest(unittest.TestCase):
    def test_selection_always_includes_indexes_and_deduplicates_items(self):
        expected_critical = {
            "news/_aliases.json",
            "news/_categories.json",
            "news/_index.json",
            "news/_meta.json",
            "news/_organizations.json",
            "news/_projects.json",
            "news/_subjects.json",
            "news/_tags.json",
            "news/_years.json",
            "news/all.json",
        }
        paths = purge_jsdelivr.select_purge_paths(
            ["news/items/n-3904aeff63.json", "news/items/n-3904aeff63.json", "news/subject-links/n-3904aeff63.json", "news/schema-subject-links-v1.json"]
        )

        self.assertEqual(set(purge_jsdelivr.CRITICAL_PATHS), expected_critical)
        self.assertEqual(set(paths), expected_critical | {"news/items/n-3904aeff63.json", "news/subject-links/n-3904aeff63.json", "news/schema-subject-links-v1.json"})

    def test_selection_rejects_non_public_or_traversal_paths(self):
        for value in (
            "README.md",
            "../news/all.json",
            "news/private.sqlite3",
            "news/private.json",
            "news/private/secret.json",
            "news/items/not-an-item.json",
            "news/items/n-123.json",
            "news/subject-links/not-a-packet.json",
            "news/subject-links/n-123.json",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    purge_jsdelivr.select_purge_paths([value])

    def test_response_requires_exact_path_and_every_provider(self):
        expected = "/gh/AnastasiyaW/diffusion-love-news@main/news/_meta.json"
        payload = {
            "status": "finished",
            "paths": {
                expected: {
                    "throttled": False,
                    "providers": {"CF": True, "FY": True},
                }
            },
        }

        self.assertEqual(
            purge_jsdelivr.validate_purge_response(payload, expected),
            ("CF", "FY"),
        )

        invalid_payloads = [
            {**payload, "status": "pending"},
            {"status": "finished", "paths": {}},
            {
                "status": "finished",
                "paths": {
                    "/gh/AnastasiyaW/diffusion-love-news@main/news/_tags.json": {
                        "throttled": False,
                        "providers": {"CF": True, "FY": True},
                    }
                },
            },
            {
                "status": "finished",
                "paths": {
                    expected: {
                        "throttled": True,
                        "providers": {"CF": True, "FY": True},
                    }
                },
            },
            {
                "status": "finished",
                "paths": {
                    expected: {
                        "throttled": False,
                        "providers": {"CF": True, "FY": False},
                    }
                },
            },
            {"status": "finished", "paths": {expected: {"throttled": False}}},
        ]
        for invalid in invalid_payloads:
            with self.subTest(payload=invalid):
                with self.assertRaises(purge_jsdelivr.PurgeError):
                    purge_jsdelivr.validate_purge_response(invalid, expected)

    def test_http_failure_propagates(self):
        with mock.patch.object(
            purge_jsdelivr, "urlopen", side_effect=URLError("offline")
        ):
            with self.assertRaises(URLError):
                purge_jsdelivr.purge_path("news/_meta.json")


if __name__ == "__main__":
    unittest.main()
