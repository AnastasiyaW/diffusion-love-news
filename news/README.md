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
├── _projects.json        # family_slug → { display, versions timeline, news_ids, ... }
├── _aliases.json         # canonical_family ← [slug_aliases, ...]
├── all.json              # full bundle: { schema_version, total, records: [...] }
└── items/
    └── n-{10-char-hex}.json   # one file per news, full schema v1.2
```

## Item schema (v1.2)

Each item is **self-contained** — a consumer can render one file without
additional context. IDs are opaque hashes; nothing in the item reveals where
the news was originally surfaced.

| Field | Purpose |
|---|---|
| `schema_version` | `"1.2"` — version pin |
| `id` | Opaque identifier, `n-{10-char-hex}` |
| `entry_type` | `project` / `news` / `paper` / `opinion` / `misc` |
| `category` | One of 11 fixed categories (`vlm`, `diffusion-image`, etc.) |
| `lang` | `"en"` |
| `title`, `summary`, `research_notes` | English content |
| `project` | Family / model / adaptation hierarchy (see below) |
| `tags` | Flat string array — search and tag-cloud friendly |
| `facets` | Structured object — faceted filter UI friendly |
| `links` | Typed link array — only project resources (code, weights, demo, paper, …) |
| `evolution` | Timeline: summary, latest_version, related_ids |
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

### Link `kind` enum

`code`, `weights`, `weights-collection`, `dataset`, `demo`, `paper`, `colab`,
`video`, `community`, `civitai`, `apple-store`, `play-store`, `reference`,
`attachment`, `vendor-page`, `blog`, `cookbook`, `project-page`, `other`.

### Adaptation `type` enum

`lora`, `controlnet`, `ip-adapter`, `dreambooth`, `textual-inversion`,
`fine-tune`, `checkpoint-merge`, `hypernetwork`.

## Cross-linking primitives

Each item exposes three orthogonal ways to find related items:

1. **By project family** — `project.family_slug`. Lookup: `_projects.json[slug].news_ids`.
2. **By tag** — `tags[]` overlap. Lookup: `_tags.json[tag]`.
3. **By explicit timeline** — `evolution.related_ids` is a curated list of
   sibling-family items.

A consumer can build a project page by joining `_projects.json[family_slug]`
with each `items/{id}.json` in the `news_ids` array — no extra parsing needed.

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
  "schema_version": "1.2",
  "site": "diffusion.love",
  "generated_at": "2026-...",
  "counts": { "items": N, "tags": N, "categories": N, "years": N, "projects": N }
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

## How to fetch

The repo is public. Consume the JSON via:

- **jsDelivr CDN** (recommended, edge-cached):
  ```
  https://cdn.jsdelivr.net/gh/AnastasiyaW/diffusion-love-news@main/news/_index.json
  https://cdn.jsdelivr.net/gh/AnastasiyaW/diffusion-love-news@main/news/items/n-{hex}.json
  ```
- **GitHub raw** (no CDN, lower rate limits):
  ```
  https://raw.githubusercontent.com/AnastasiyaW/diffusion-love-news/main/news/...
  ```

Cache busting: replace `@main` with a commit hash (`@sha-12chars`) to pin a snapshot.

## Update cadence

Updates land as new git commits whenever the upstream pipeline ships fresh items.
Subscribe to commit feed: `https://github.com/AnastasiyaW/diffusion-love-news/commits.atom`

## License

Content is curated AI/ML project metadata; project facts and URLs are factual
references. See `LICENSE` for terms.
