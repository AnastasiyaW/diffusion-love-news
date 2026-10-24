"""Machine-translate the text of feed items into the site's other languages.

English in news/items/ is the source of truth. A translation lives next to the feed, one
file per item and language, and records the SHA-256 of the English text it was made from:

  news/translations/<lang>/<id>.json   the record (source hash, model, date, fields)
  news/translations/<lang>/_index.json  derived: id -> title, translated_at, fresh
  news/translations/<lang>.json         derived: id -> fields, the bundle the site loads

Only title, summary, research_notes and evolution.summary are translated. `plan` compares the
stored hash with the current English text, so only new and changed items are sent again.
A result is written only after it passes the checks below; anything else is reported by
`collect` and kept under var/translate/rejected/ for diagnosis, never published:
  - the same fields come back, none empty;
  - every URL and every `inline code` span is byte-identical;
  - every name/version token that mixes letters and digits (FLUX.2, U1.5-8B, SDXL) survives;
  - the text is actually in the target script/language and has a sane length.

  py -3 scripts/translate_items.py plan    --lang de [--lang fr ...]
  py -3 scripts/translate_items.py submit  --lang de [--limit N] [--only n-xxxxxxxxxx ...]
  py -3 scripts/translate_items.py status  <batch_id>
  py -3 scripts/translate_items.py collect <batch_id>
  py -3 scripts/translate_items.py bundle  [--lang de ...]
  py -3 scripts/translate_items.py check
  py -3 scripts/translate_items.py import-legacy <translations.json> --lang ru

submit/status/collect need ANTHROPIC_API_KEY and use the Message Batches API (asynchronous,
half price). Exit codes: 0 ok, 1 failures or invalid data, 2 not checked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEMS = ROOT / "news" / "items"
OUT = ROOT / "news" / "translations"
STATE = ROOT / "var" / "translate"
API = "https://api.anthropic.com/v1/messages/batches"
# Owner routing rule 2026-10-13: execution work runs on Claude Haiku 5.5.
MODEL = "claude-haiku-5-5"
MAX_TOKENS = 4096  # longest item is ~2.8k characters of English across all fields

# The site's language set (diffusion-love-web src/lib/i18n/locales.ts); English is the source.
LANGS = {
    "ru": "Russian",
    "zh-CN": "Simplified Chinese",
    "es": "Spanish",
    "de": "German",
    "fr": "French",
    "ko": "Korean",
}
FIELDS = ("title", "summary", "research_notes", "evolution_summary")
LEGACY = "legacy"

URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
INLINE_CODE_RE = re.compile(r"`[^`]+`")
# A token that mixes ASCII letters and digits is a model/version name: FLUX.2, U1.5-8B, SD3.5.
NAME_TOKEN_RE = re.compile(r"(?<![\w.\-])[A-Za-z0-9][A-Za-z0-9.\-_/+]*[A-Za-z0-9](?![\w\-])")
ID_RE = re.compile(r"^n-[0-9a-f]{10}$")

SYSTEM = """You translate the text fields of one AI/ML news item from English into {language}.
Readers are engineers and researchers. Produce a faithful, natural, precise translation in the
register of good technical journalism written natively in {language}: no simplification, no added
content, no commentary, no notes about the translation.

Keep exactly as they are, byte for byte:
- URLs, file paths, commands, flags, config keys, code and everything between backticks;
- product, model, library, company, organisation and person names, and model/version identifiers
  such as FLUX.2, SDXL, Qwen2.5-VL-7B, U1.5-8B-MoT (do not transliterate or translate them);
- numbers and units.
Established English technical terms that {language}-speaking engineers normally keep in English
(LoRA, checkpoint, benchmark, fine-tune, diffusion) may stay in English.

The input is a JSON object. Answer with one JSON object with exactly the same keys, each value
the translation of the input value. Return only that JSON object, nothing before or after it."""

SCRIPT_CHECK = {
    "ru": re.compile(r"[Ѐ-ӿ]"),
    "zh-CN": re.compile(r"[一-鿿]"),
    "ko": re.compile(r"[가-힯]"),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def source_fields(item: dict) -> dict[str, str]:
    """The translatable English text of one item; empty fields are left out."""
    evolution = item.get("evolution") or {}
    raw = {
        "title": item.get("title"),
        "summary": item.get("summary"),
        "research_notes": item.get("research_notes"),
        "evolution_summary": evolution.get("summary"),
    }
    return {k: v.strip() for k, v in raw.items() if isinstance(v, str) and v.strip()}


def source_hash(fields: dict[str, str]) -> str:
    return sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def items() -> dict[str, dict]:
    out = {}
    for path in sorted(ITEMS.glob("n-*.json")):
        item = load_json(path)
        if isinstance(item.get("id"), str) and ID_RE.match(item["id"]):
            out[item["id"]] = item
    return out


def record_path(lang: str, item_id: str) -> Path:
    return OUT / lang / f"{item_id}.json"


def records(lang: str) -> dict[str, dict]:
    folder = OUT / lang
    if not folder.is_dir():
        return {}
    return {p.stem: load_json(p) for p in sorted(folder.glob("n-*.json"))}


def plan(lang: str, corpus: dict[str, dict]) -> dict[str, list[str]]:
    have = records(lang)
    result: dict[str, list[str]] = {"missing": [], "stale": [], "fresh": [], "orphan": []}
    for item_id, item in corpus.items():
        rec = have.get(item_id)
        if rec is None:
            result["missing"].append(item_id)
        elif rec.get("source_sha256") != source_hash(source_fields(item)):
            result["stale"].append(item_id)
        else:
            result["fresh"].append(item_id)
    result["orphan"] = sorted(set(have) - set(corpus))
    return result


def name_tokens(text: str) -> list[str]:
    return [t for t in NAME_TOKEN_RE.findall(text)
            if re.search(r"[A-Za-z]", t) and re.search(r"\d", t)]


def check_translation(lang: str, source: dict[str, str], translated) -> dict[str, str]:
    """Return the translated fields, or raise ValueError naming the first broken rule."""
    if not isinstance(translated, dict):
        raise ValueError("answer is not a JSON object")
    if set(translated) != set(source):
        raise ValueError(f"keys {sorted(translated)} != {sorted(source)}")
    out: dict[str, str] = {}
    for key, src in source.items():
        tr = translated[key]
        if not isinstance(tr, str) or not tr.strip():
            raise ValueError(f"{key}: empty")
        tr = tr.strip()
        if sorted(URL_RE.findall(src)) != sorted(URL_RE.findall(tr)):
            raise ValueError(f"{key}: URLs changed")
        lost = Counter(INLINE_CODE_RE.findall(src))
        lost.subtract(INLINE_CODE_RE.findall(tr))
        if any(n > 0 for n in lost.values()):
            raise ValueError(f"{key}: inline code lost or changed")
        missing = [t for t in name_tokens(src) if t not in tr]
        if missing:
            raise ValueError(f"{key}: names changed {missing[:3]}")
        ratio = len(tr) / max(1, len(src))
        if not 0.2 <= ratio <= 3.5:
            raise ValueError(f"{key}: length ratio {ratio:.2f}")
        out[key] = tr
    prose = " ".join(out.values())
    pattern = SCRIPT_CHECK.get(lang)
    if pattern is not None:
        letters = [c for c in prose if c.isalpha()]
        if letters and sum(1 for c in letters if pattern.match(c)) / len(letters) < 0.25:
            raise ValueError(f"too little {lang} script")
    else:
        long_fields = [k for k, v in source.items() if len(v.split()) >= 6]
        if long_fields and all(out[k] == source[k] for k in long_fields):
            raise ValueError("text left untranslated")
    return out


def parse_answer(text: str):
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.S)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def bundle(lang: str, corpus: dict[str, dict]) -> int:
    """Rebuild the two derived files of one language from its records."""
    have = records(lang)
    full, index = {}, {}
    for item_id in sorted(have):
        if item_id not in corpus:
            continue
        rec = have[item_id]
        fields = {k: rec[k] for k in FIELDS if isinstance(rec.get(k), str) and rec[k]}
        full[item_id] = fields
        index[item_id] = {
            "title": fields.get("title", ""),
            "translated_at": rec.get("translated_at"),
            "fresh": rec.get("source_sha256") == source_hash(source_fields(corpus[item_id])),
        }
    write_json(OUT / f"{lang}.json", full)
    write_json(OUT / lang / "_index.json", index)
    return len(full)


def validate_record(lang: str, item_id: str, rec: dict) -> list[str]:
    errors = []
    if rec.get("id") != item_id or rec.get("lang") != lang:
        errors.append("id/lang mismatch")
    if rec.get("translation") != "machine":
        errors.append("translation must be 'machine'")
    sha = rec.get("source_sha256")
    if not (sha == LEGACY or (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha))):
        errors.append("source_sha256 missing")
    if not isinstance(rec.get("title"), str) or not rec["title"].strip():
        errors.append("title missing")
    unknown = set(rec) - {"id", "lang", "translation", "model", "translated_at", "source_sha256", *FIELDS}
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}")
    return errors


def api(method: str, url: str, body: dict | None = None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY is not set: not checked")
        sys.exit(2)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
                return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 4:
                time.sleep(10 * (attempt + 1))
                continue
            print(f"API {method} {url} -> {e.code}: {e.read()[:500]!r}")
            sys.exit(2)
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(10 * (attempt + 1))
                continue
            print(f"API unreachable: {e}")
            sys.exit(2)


def cmd_plan(args) -> int:
    corpus = items()
    for lang in args.lang:
        p = plan(lang, corpus)
        print(f"{lang}: missing {len(p['missing'])}, stale {len(p['stale'])}, "
              f"fresh {len(p['fresh'])}, orphan {len(p['orphan'])}")
    return 0


def cmd_submit(args) -> int:
    corpus = items()
    STATE.mkdir(parents=True, exist_ok=True)
    requests, manifest = [], {}
    for lang in args.lang:
        p = plan(lang, corpus)
        todo = args.only or (p["missing"] + p["stale"])
        if args.limit:
            todo = todo[: args.limit]
        for item_id in todo:
            if item_id not in corpus:
                print(f"unknown item {item_id}")
                return 1
            fields = source_fields(corpus[item_id])
            if not fields:
                continue
            cid = f"{lang}-{item_id}"
            manifest[cid] = {"lang": lang, "id": item_id, "sha": source_hash(fields)}
            requests.append({"custom_id": cid, "params": {
                "model": MODEL, "max_tokens": MAX_TOKENS,
                "system": SYSTEM.format(language=LANGS[lang]),
                "messages": [{"role": "user", "content": json.dumps(fields, ensure_ascii=False, indent=1)}],
            }})
    if not requests:
        print("nothing to translate")
        return 0
    batch = api("POST", API, {"requests": requests})
    (STATE / f"{batch['id']}.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"submitted {batch['id']}: {len(requests)} requests")
    return 0


def cmd_status(args) -> int:
    b = api("GET", f"{API}/{args.batch}")
    print(json.dumps({k: b.get(k) for k in ("id", "processing_status", "request_counts", "ended_at")}))
    return 0


def cmd_collect(args) -> int:
    manifest = json.loads((STATE / f"{args.batch}.json").read_text(encoding="utf-8"))
    b = api("GET", f"{API}/{args.batch}")
    if b["processing_status"] != "ended":
        print(f"batch {args.batch} is {b['processing_status']}: not collected")
        return 2
    raw = api("GET", b["results_url"])
    corpus = items()
    ok, failed, langs = 0, [], set()
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        job = manifest.get(r["custom_id"])
        if job is None:
            continue
        lang, item_id = job["lang"], job["id"]
        text = None
        try:
            res = r["result"]
            if res["type"] != "succeeded":
                raise ValueError(f"request {res['type']}")
            msg = res["message"]
            if msg.get("stop_reason") != "end_turn":
                raise ValueError(f"stop_reason {msg.get('stop_reason')}")
            text = "".join(c.get("text", "") for c in msg["content"])
            if item_id not in corpus:
                raise ValueError("item no longer in the feed")
            source = source_fields(corpus[item_id])
            if source_hash(source) != job["sha"]:
                raise ValueError("English source changed since submit")
            try:
                answer = parse_answer(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"answer is not JSON: {e}") from None
            fields = check_translation(lang, source, answer)
            write_json(record_path(lang, item_id), {
                "id": item_id, "lang": lang, "translation": "machine", "model": msg.get("model", MODEL),
                "translated_at": dt.date.today().isoformat(), "source_sha256": job["sha"], **fields,
            })
            langs.add(lang)
            ok += 1
        except (ValueError, KeyError) as e:
            failed.append({"lang": lang, "id": item_id, "reason": str(e)})
            if text is not None:
                rejected = STATE / "rejected" / lang / f"{item_id}.txt"
                rejected.parent.mkdir(parents=True, exist_ok=True)
                rejected.write_text(text, encoding="utf-8")
    for lang in sorted(langs):
        bundle(lang, corpus)
    report = STATE / f"{args.batch}.result.json"
    report.write_text(json.dumps({"written": ok, "failed": failed}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"written {ok}, failed {len(failed)} (details: {report})")
    for f in failed[:20]:
        print(f"  FAIL {f['lang']} {f['id']}: {f['reason']}")
    return 1 if failed else 0


def cmd_bundle(args) -> int:
    corpus = items()
    for lang in args.lang or sorted(LANGS):
        if (OUT / lang).is_dir():
            print(f"{lang}: {bundle(lang, corpus)} items")
    return 0


def cmd_check(args) -> int:
    """Records valid, derived files current. Runs offline; for CI."""
    corpus = items()
    errors = []
    for lang_dir in sorted(p for p in OUT.iterdir() if p.is_dir()) if OUT.is_dir() else []:
        lang = lang_dir.name
        if lang not in LANGS:
            errors.append(f"{lang}: not a site language")
            continue
        for item_id, rec in records(lang).items():
            errors += [f"{lang}/{item_id}: {e}" for e in validate_record(lang, item_id, rec)]
        expected = {k: {f: v[f] for f in FIELDS if isinstance(v.get(f), str) and v[f]}
                    for k, v in records(lang).items() if k in corpus}
        bundle_path = OUT / f"{lang}.json"
        if not bundle_path.is_file() or load_json(bundle_path) != expected:
            errors.append(f"{lang}.json is not current: run `bundle --lang {lang}`")
        index_path = lang_dir / "_index.json"
        if not index_path.is_file() or set(load_json(index_path)) != set(expected):
            errors.append(f"{lang}/_index.json is not current: run `bundle --lang {lang}`")
    for e in errors[:50]:
        print(f"  {e}")
    print(f"translations: {'invalid' if errors else 'valid'} ({len(errors)} errors)")
    return 1 if errors else 0


def cmd_import_legacy(args) -> int:
    """Bring in the interim overlay the site kept (id -> title/summary/research_notes).

    Its source text is unknown, so each record is marked stale (source_sha256 "legacy"): it is
    served until `submit` replaces it with a checked translation of the current English.
    """
    corpus = items()
    data = load_json(Path(args.path))
    lang = args.lang[0]
    added = 0
    for item_id, fields in data.items():
        if item_id not in corpus or record_path(lang, item_id).exists():
            continue
        clean = {k: v.strip() for k, v in fields.items() if k in FIELDS and isinstance(v, str) and v.strip()}
        if "title" not in clean:
            continue
        write_json(record_path(lang, item_id), {
            "id": item_id, "lang": lang, "translation": "machine", "model": "legacy-web-overlay",
            "translated_at": None, "source_sha256": LEGACY, **clean,
        })
        added += 1
    bundle(lang, corpus)
    print(f"{lang}: imported {added} legacy records")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "submit"):
        s = sub.add_parser(name)
        s.add_argument("--lang", action="append", choices=sorted(LANGS), required=True)
        if name == "submit":
            s.add_argument("--limit", type=int)
            s.add_argument("--only", nargs="*")
    for name in ("status", "collect"):
        sub.add_parser(name).add_argument("batch")
    sub.add_parser("bundle").add_argument("--lang", action="append", choices=sorted(LANGS))
    sub.add_parser("check")
    s = sub.add_parser("import-legacy")
    s.add_argument("path")
    s.add_argument("--lang", action="append", choices=sorted(LANGS), required=True)
    args = ap.parse_args()
    return {"plan": cmd_plan, "submit": cmd_submit, "status": cmd_status, "collect": cmd_collect,
            "bundle": cmd_bundle, "check": cmd_check, "import-legacy": cmd_import_legacy}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
