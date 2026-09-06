# G4 public subject links contract

Status: PREP-only additive public export. This producer does not mint review
authority or alter existing `news/items/` records (schemas 1.2--1.4).

## Packet

Each approved export is exactly `subject-links/<news_id>.json`, where
`news_id` is an existing `n-<10-lower-hex>` item ID. Its exact top-level shape
is:

```json
{
  "schema_version": "subject-links-v1",
  "news_id": "n-...",
  "subjects": [{"subject_id": "sgs-...", "subject_kind": "tool", "canonical_name": "...", "identity_url": "https://..."}],
  "about": "sgs-...",
  "mentions": ["sgs-..."],
  "relations": [{"from_subject_id": "sgs-...", "relation": "uses", "to_subject_id": "sgs-...", "evidence_urls": ["https://..."]}]
}
```

Subject IDs are exactly `sgs-` plus the first 32 lower-hex characters of the
SHA-256 of the exact `identity_url`. `subject_kind` is one of `company`,
`project`, `model`, `version`, `tool`, `service`, `dataset`, `method`, or
`technology`; `relation` is one of `version_of`, `derived_from`,
`compatible_with`, `uses`, `requires`, or `owned_by`. Subject IDs are unique;
`about` is exactly one listed subject; `mentions` are unique listed secondary
subjects; relation endpoints are listed subjects, are distinct, and relations
are unique by endpoints, relation, and evidence URLs. Identity URLs are unique
within the packet subject list; evidence URLs are unique within each relation.
The same public HTTPS URL may appear in different identity/evidence roles or
support different relations. Private/local/file/Telegram URLs and raw
source/provenance fields are forbidden. Packets contain only approved-export
facts; no tag, title, or slug inference is permitted.

## Derived aggregate

`news/_subjects.json` is exactly:

```json
{
  "schema_version": "subjects-index-v1",
  "packets": {"n-...": "subject-links/n-....json"},
  "subjects": {
    "sgs-...": {
      "subject_kind": "tool",
      "canonical_name": "...",
      "identity_url": "https://...",
      "first_known_event_at": "<existing source.posted_at>",
      "about_news_ids": ["n-..."],
      "mentioned_news_ids": ["n-..."],
      "relations": [{"from_subject_id": "sgs-...", "relation": "uses", "to_subject_id": "sgs-...", "evidence_urls": ["https://..."], "news_id": "n-..."}]
    }
  }
}
```

Packet paths are exact relative paths. History IDs are sorted ascending by the
existing item `source.posted_at`, then `news_id`; `first_known_event_at` is the
earliest packet occurrence (including a relation-only subject), never a launch
date. A subject's identity URL and kind must not conflict across packets;
canonical name selects the latest source-date then ID. Aggregate relations are
deduplicated by exact endpoints, relation, evidence URLs, and `news_id`, and
are stored only on their `from_subject_id` record. `version_of` and
`derived_from` are acyclic across packets.

With no packet files, the aggregate is the empty additive index. The producer
validates public export shape and invariants only; it creates no packets,
review decisions, tags, news content, service, queue, or deployment.
