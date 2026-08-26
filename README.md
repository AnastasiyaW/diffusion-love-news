# diffusion-love-news

Public content feed for [diffusion.love](https://diffusion.love).

Curated AI/ML project news in a uniform JSON format. One file per news item,
plus reverse-lookup indexes. Designed to be consumed by the site, by knowledge
bases, by RSS aggregators, by anything that wants structured project news
without scraping.

## What's here

Everything lives under [`news/`](./news/). Read [`news/README.md`](./news/README.md)
for the full schema, layout, and consumption patterns.

Quick summary:
- `news/items/n-{hex}.json` — one self-contained item per file (schema v1.2-v1.4)
- `news/_index.json`, `news/_tags.json`, `news/_projects.json`,
  `news/_organizations.json`, etc — derived
  reverse-lookup indexes
- `news/all.json` — full bundle for batch consumers

## How to use

Fetch via jsDelivr CDN (edge-cached, fast):

```
https://cdn.jsdelivr.net/gh/AnastasiyaW/diffusion-love-news@main/news/_index.json
https://cdn.jsdelivr.net/gh/AnastasiyaW/diffusion-love-news@main/news/items/{id}.json
```

Or directly from GitHub raw:

```
https://raw.githubusercontent.com/AnastasiyaW/diffusion-love-news/main/news/...
```

## Schema highlights

Each item exposes:
- English-only content (`title`, `summary`, `research_notes`, `source.excerpt`)
- Project hierarchy: family → model → adaptation (LoRA / ControlNet / fine-tune / etc.)
- Typed links to project resources (code, weights, demos, papers — but NEVER
  social media or source feeds)
- Flat + namespaced tags for hierarchical filtering (`family:flux`,
  `model:flux-2`, `adaptation:lora`, `lora-for:flux-2`)
- Faceted metadata for UI sidebars
- Opaque hash IDs (`n-{10hex}`) — no upstream-feed identifier in IDs

## License

See [`LICENSE`](./LICENSE).
