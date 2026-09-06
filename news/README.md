# `news/` — content feed for diffusion.love

Curated AI/ML project news in a uniform JSON format. One file per news item,
plus reverse-lookup indexes. Designed for any consumer to ingest without
scanning every item: faceted search, project pages, "more like this", RSS.

## Layout

```
news/
├── README.md             # this file
├── _meta.json            # schema_version + generated_at + counts
├── _index.json           # array of lightweight item projections (date desc)
├── _tags.json            # tag (string) → [news_id, ...]
├── _categories.json      # category → [news_id, ...]
├── _years.json           # year → [news_id, ...]
├── _projects.json        # family_slug → project history + explicit development threads
├── _organizations.json   # organization_slug → company news + owned projects
├── _subjects.json        # optional approved generic-subject reverse index
├── _aliases.json         # canonical_family ← [slug_aliases, ...]
├── all.json              # full bundle: { schema_version, total, records: [...] }
├── items/
    └── n-{10-char-hex}.json   # one file per news, schema v1.2-v1.4
└── subject-links/
    └── n-{10-char-hex}.json   # optional approved subject-links-v1 packet
```

## Item schema (v1.4)

Each item is **self-contained** — a consumer can render one file without
additional context. IDs are opaque hashes; nothing in the item reveals where
the news was originally surfaced.

| Field | Purpose |
|---|---|
| `schema_version` | `"1.2"`, `"1.3"`, or `"1.4"` — version pin |
| `id` | Opaque identifier, `n-{10-char-hex}` |
| `entry_type` | `project` / `news` / `paper` / `opinion` / `misc` |
| `category` | One of 11 fixed categories (`vlm`, `diffusion-image`, etc.) |
| `lang` | `"en"` |
| `title`, `summary`, `research_notes` | English content |
| `project` | Family / model / adaptation hierarchy (see below) |
| `organization` | Reviewed company/owner identity, or `null` |
| `development` | Project/company scope, named thread, reviewed predecessor IDs |
| `knowledge` | Knowledge Space article, immutable Markdown and Agent Brief URL |
| `tags` | Flat string array — search and tag-cloud friendly |
| `facets` | Structured object — faceted filter UI friendly |
| `links` | Typed link array — only project resources (code, weights, demo, paper, …) |
| `evolution` | Timeline: summary, latest_version, related_ids |
| `lifecycle_event` | Typed, dated project event used in derived timelines |
| `claims` | Evidence-bearing public claims with explicit verification status |
| `media` | `has_image`, `has_video` |
| `source` | `posted_at` (ISO date) + `excerpt` (English) — nothing more |
| `_meta` | Pipeline timestamps + model used |

### Project hierarchy (family / model / adaptation)

```
family       (e.g. FLUX)
  └─ model   (e.g. FLUX.2-dev)
       └─ adaptation (e.g. LoRA, ControlNet, fine-tune)
```

Schema fields:
- `project.family_slug` / `project.family_display`
- `project.model_slug` / `project.model_display`
- `project.adaptation` → `null` OR `{type, purpose, base_model_slug}`

Schema 1.4 keeps the complete legacy project shape. A company-only record uses
an anonymous project object and a non-null `organization`; an independent
project may use `organization: null`.

### Development graph

`development.scope` is exactly `project` or `organization`. `thread_slug`
groups a named line of work and `predecessor_ids` contains only reviewed,
strictly earlier news in that same subject/thread. Missing, forward,
cross-subject and cross-thread edges fail the build.

Legacy 1.2/1.3 items retain their chronological project history and are not
assigned semantic threads by title, tag or embedding similarity.

### Knowledge Space bridge

Every 1.4 item includes one `knowledge` object with:

- `article_url` — stable rendered page on `happyin.space`
- `source_url` — Markdown pinned to a full `knowledge-space` Git commit
- `agent_context_url` — the article's public `#agent-brief` section

The public feed still rejects every nested key named `prompt`; a reviewed
Agent Brief URL does not expose internal provider prompts or transcripts.

### Link `kind` enum

`code`, `weights`, `weights-collection`, `dataset`, `demo`, `paper`, `colab`,
`video`, `community`, `civitai`, `apple-store`, `play-store`, `reference`,
`attachment`, `vendor-page`, `blog`, `cookbook`, `project-page`, `other`.

### Adaptation `type` enum

`lora`, `controlnet`, `ip-adapter`, `dreambooth`, `textual-inversion`,
`fine-tune`, `checkpoint-merge`, `hypernetwork`.

## Cross-linking primitives

Each item exposes four orthogonal ways to find related items:

1. **By project family** — `project.family_slug`. Lookup: `_projects.json[slug].news_ids`.
2. **By tag** — `tags[]` overlap. Lookup: `_tags.json[tag]`.
3. **By explicit timeline** — `evolution.related_ids` is a curated list of
   sibling-family items.
4. **By reviewed development** — schema 1.4 `development.predecessor_ids`
   forms a checked continuation graph inside one project/company thread.

A consumer can build a project page by joining `_projects.json[family_slug]`
with each `items/{id}.json` in the `news_ids` array — no extra parsing needed.

## Optional generic subject links

`subject-links/<news_id>.json` is an additive, approved-export packet for one
existing public item. It is not embedded in or inferred from legacy item
schemas. `news/_subjects.json` indexes packet subjects, packet paths, ordered
about/mention histories, and outgoing typed relations. With no packets it is
the empty index, leaving existing item and discovery JSON byte-compatible.

The producer validates exact `sgs-*` identities derived from HTTPS identity
URLs, public HTTPS relation evidence, listed endpoints, and acyclic
`version_of`/`derived_from` ancestry. It never derives subjects or relations
from titles, tags, slugs, raw sources, private provenance, or Telegram links.
See [`subject-links/README.md`](./subject-links/README.md) and
[`schema-subject-links-v1.json`](./schema-subject-links-v1.json).

## Namespaced tags

Hierarchical filtering uses namespaced tags alongside flat tags:

- `family:flux` — all FLUX-related items (any model, any adaptation)
- `model:flux-2-dev` — specific model
- `adaptation:lora` — all LoRAs across the catalog
- `lora-for:flux-2-dev` — LoRAs targeting one model

Combined with `facets.projects` / `facets.models` / `facets.adaptations` arrays
for faceted UI sidebars.

## Indexes

All indexes are **derived data** — regenerable from `items/`. Sorted
deterministically (date desc, id desc) so file diffs stay clean.

### `_meta.json`
```json
{
  "schema_version": "1.4",
  "site": "diffusion.love",
  "generated_at": "2026-...",
  "counts": { "items": N, "tags": N, "categories": N, "years": N, "projects": N, "organizations": N }
}
```

### `_index.json` (master list)
Array of lightweight projections: `id, date, title, category, entry_type,
project (family_slug), version, tags (top 8), primary_link, has_image, has_video`.
Suitable for "latest news" listing UI without loading every item.

### `_tags.json`
```json
{ "flux": ["n-abc...", ...], "vlm": ["n-def...", ...], ... }
```

### `_projects.json`
```json
{
  "flux": {
    "family_slug": "flux",
    "family_display": "FLUX",
    "vendor": null,
    "origin_region": "us",
    "license": "open-non-commercial",
    "categories": ["diffusion-image"],
    "post_count": 14,
    "first_seen": "2024-08-...", "last_seen": "2026-...",
    "latest_version": "FLUX.2",
    "latest_evolution": "...narrative...",
    "news_ids": ["n-...", ...],
    "versions_timeline": [
      { "version": "2.0", "date": "2026-...", "news_id": "...", "title": "...", "primary_link": "..." }
    ],
    "features": ["edit", "controlnet"],
    "formats": ["github", "huggingface"],
    "platforms": ["comfyui"],
    "base_models": []
  }
}
```

Single index supports project page rendering, version timelines, vendor
listings, "all FLUX news" queries, related-project navigation.

For schema 1.4 projects, `organization` identifies the reviewed owner and
`development_threads[]` lists each explicit branch with stable news IDs. These
fields are omitted when no 1.4 item supplies them, keeping legacy aggregates
byte-stable apart from the new feed envelope.

### `_organizations.json`

```json
{
  "happyin": {
    "slug": "happyin",
    "display": "Happyin",
    "homepage": "https://happyin.app/",
    "project_slugs": ["happyin-space"],
    "news_ids": ["n-..."],
    "direct_news_ids": ["n-..."],
    "development_threads": []
  }
}
```

`news_ids` includes every reviewed item carrying the organization identity;
`direct_news_ids` contains only organization-scoped development. Project
ownership is joined only through explicit 1.4 records.

### Aggregation guarantees

For a project page, `family_display` is the sole non-null source display on the
latest `source.posted_at` date. Distinct values on any one date fail the build:
they are an ambiguous identity/model classification, never a rename resolved by
an opaque ID. `family_display_variants` retains display history across distinct
dates with contributing news IDs; the slug `aliases` field remains reserved for
canonical slug aliases.

Repeated project-link URLs must have identical non-null shared metadata
(`kind`, `host`, `lang`, and `status`) or the build fails. A project-link
`is_primary` means primary in at least one linked news item, and `verified_at`
is the latest public verification timestamp. When either varies by news item,
`occurrences` preserves the exact per-item values so aggregation loses no
context.

## How to fetch

The repo is public. Consume the JSON via:

- **jsDelivr CDN** (recommended, edge-cached; replace `FULL_SHA` with the
  producer commit resolved once per client bootstrap):
  ```
  https://cdn.jsdelivr.net/gh/AnastasiyaW/diffusion-love-news@FULL_SHA/news/_index.json
  https://cdn.jsdelivr.net/gh/AnastasiyaW/diffusion-love-news@FULL_SHA/news/items/n-{hex}.json
  ```
- **GitHub raw** (release verification, also pin a full SHA):
  ```
  https://raw.githubusercontent.com/AnastasiyaW/diffusion-love-news/FULL_SHA/news/...
  ```

Cache busting: replace `@main` with a commit hash (`@sha-12chars`) to pin a snapshot.
After every validated push to `main`, CI asks jsDelivr to purge changed public
JSON plus the eight generated discovery files. CDN propagation is still
eventual; the commit-addressed URL is the immutable release proof.

## Update cadence

Updates land as new git commits whenever the upstream pipeline ships fresh items.
Subscribe to commit feed: `https://github.com/AnastasiyaW/diffusion-love-news/commits.atom`

## License

Content is curated AI/ML project metadata; project facts and URLs are factual
references. See `LICENSE` for terms.
