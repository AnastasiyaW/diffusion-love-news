import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_news", ROOT / "scripts" / "build_news.py")
assert SPEC is not None and SPEC.loader is not None
build_news = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_news)


def subject_id(url: str) -> str:
    return "sgs-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


class SubjectLinksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads((ROOT / "news" / "items" / "n-0124eabefd.json").read_text(encoding="utf-8"))

    def record(self, news_id: str, posted_at: str) -> dict:
        item = copy.deepcopy(self.base)
        item["id"] = news_id
        item["source"]["posted_at"] = posted_at
        item["facets"]["year"] = int(posted_at[:4])
        return item

    def packet(self, news_id: str, *, base_name: str = "Base Tool", relation: str = "uses", reverse: bool = False) -> dict:
        base_url = "https://example.test/base"
        tool_url = "https://example.test/tool"
        base = subject_id(base_url)
        tool = subject_id(tool_url)
        return {
            "schema_version": "subject-links-v1",
            "news_id": news_id,
            "subjects": [
                {"subject_id": base, "subject_kind": "tool", "canonical_name": base_name, "identity_url": base_url},
                {"subject_id": tool, "subject_kind": "version", "canonical_name": "Tool 2", "identity_url": tool_url},
            ],
            "about": base if news_id.endswith("1") else tool,
            "mentions": [] if news_id.endswith("1") else [base],
            "relations": [{
                "from_subject_id": base if reverse else tool,
                "relation": relation,
                "to_subject_id": tool if reverse else base,
                "evidence_urls": ["https://example.test/evidence"],
            }],
        }

    def test_legacy_records_emit_an_empty_additive_index(self):
        records = [self.record("n-1111111111", "2026-01-01")]
        self.assertEqual(build_news.build_subject_index(records, []), {
            "schema_version": "subjects-index-v1", "packets": {}, "subjects": {},
        })
        self.assertEqual(json.loads(build_news.build_outputs(records)["_subjects.json"]), {
            "schema_version": "subjects-index-v1", "packets": {}, "subjects": {},
        })

    def test_valid_packets_build_deterministic_subject_histories_and_outgoing_relations(self):
        records = [self.record("n-1111111111", "2026-01-01"), self.record("n-2222222222", "2026-01-02")]
        first = self.packet("n-1111111111")
        second = self.packet("n-2222222222", base_name="Base Tool Renamed")
        index = build_news.build_subject_index(records, [second, first])
        base = subject_id("https://example.test/base")
        tool = subject_id("https://example.test/tool")
        self.assertEqual(index["packets"], {
            "n-1111111111": "subject-links/n-1111111111.json",
            "n-2222222222": "subject-links/n-2222222222.json",
        })
        self.assertEqual(index["subjects"][base]["canonical_name"], "Base Tool Renamed")
        self.assertEqual(index["subjects"][base]["first_known_event_at"], "2026-01-01")
        self.assertEqual(index["subjects"][base]["about_news_ids"], ["n-1111111111"])
        self.assertEqual(index["subjects"][base]["mentioned_news_ids"], ["n-2222222222"])
        self.assertEqual(index["subjects"][base]["relations"], [])
        self.assertEqual([row["news_id"] for row in index["subjects"][tool]["relations"]], ["n-1111111111", "n-2222222222"])

    def test_forged_subject_identity_and_private_evidence_fail_closed(self):
        packet = self.packet("n-1111111111")
        packet["subjects"][0]["subject_id"] = "sgs-" + "0" * 32
        packet["relations"][0]["evidence_urls"] = ["https://api.telegram.org/private-source"]
        errors = build_news.validate_subject_packet(packet, "n-1111111111.json", {"n-1111111111"})
        self.assertTrue(any("must derive" in error for error in errors))
        self.assertTrue(any("public HTTPS" in error for error in errors))

    def test_unknown_packet_endpoint_fails_closed(self):
        packet = self.packet("n-1111111111")
        packet["relations"][0]["to_subject_id"] = "sgs-" + "0" * 32
        errors = build_news.validate_subject_packet(packet, "n-1111111111.json", {"n-1111111111"})
        self.assertIn("relations[0].to_subject_id: must name a packet subject", errors)

    def test_cross_packet_version_cycles_fail_closed(self):
        records = [self.record("n-1111111111", "2026-01-01"), self.record("n-2222222222", "2026-01-02")]
        with tempfile.TemporaryDirectory() as temp:
            news_dir = Path(temp)
            packet_dir = news_dir / "subject-links"
            packet_dir.mkdir()
            (packet_dir / "n-1111111111.json").write_text(json.dumps(self.packet("n-1111111111", relation="version_of")), encoding="utf-8")
            (packet_dir / "n-2222222222.json").write_text(json.dumps(self.packet("n-2222222222", relation="version_of", reverse=True)), encoding="utf-8")
            with self.assertRaisesRegex(build_news.ContractError, "version_of/derived_from cycle"):
                build_news.load_subject_packets(news_dir, records)

    def test_cross_packet_subject_kind_conflict_fails_closed(self):
        records = [self.record("n-1111111111", "2026-01-01"), self.record("n-2222222222", "2026-01-02")]
        first = self.packet("n-1111111111")
        second = self.packet("n-2222222222")
        second["subjects"][0]["subject_kind"] = "project"
        with tempfile.TemporaryDirectory() as temp:
            packet_dir = Path(temp) / "subject-links"
            packet_dir.mkdir()
            (packet_dir / "n-1111111111.json").write_text(json.dumps(first), encoding="utf-8")
            (packet_dir / "n-2222222222.json").write_text(json.dumps(second), encoding="utf-8")
            with self.assertRaisesRegex(build_news.ContractError, "conflicts with prior identity URL or kind"):
                build_news.load_subject_packets(Path(temp), records)


if __name__ == "__main__":
    unittest.main()
