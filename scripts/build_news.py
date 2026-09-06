#!/usr/bin/env python3
"""Validate canonical news items and deterministically rebuild public indexes."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEWS_DIR = ROOT / "news"
CURRENT_SCHEMA_VERSION = "1.4"
SUPPORTED_ITEM_VERSIONS = {"1.2", "1.3", "1.4"}
SITE = "diffusion.love"
GENERATED_FILES = (
    "_meta.json",
    "_index.json",
    "_tags.json",
    "_categories.json",
    "_years.json",
    "_projects.json",
    "_organizations.json",
    "_subjects.json",
    "_aliases.json",
    "all.json",
)

ID_RE = re.compile(r"^n-[0-9a-f]{10}$")
SUBJECT_ID_RE = re.compile(r"^sgs-[0-9a-f]{32}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_PUBLIC_KEYS = {
    "access_token",
    "api_key",
    "api_token",
    "authorization",
    "auth_token",
    "client_secret",
    "cookie",
    "cookies",
    "credentials",
    "password",
    "private_notes",
    "prompt",
    "raw_prompt",
    "secret",
    "session_path",
    "telegram_session",
    "token",
    "transcript",
}
BASE_REQUIRED_FIELDS = (
    "schema_version",
    "id",
    "entry_type",
    "category",
    "lang",
    "title",
    "summary",
    "research_notes",
    "project",
    "tags",
    "facets",
    "links",
    "evolution",
    "media",
    "source",
    "_meta",
)
V13_TOP_LEVEL_FIELDS = {"lifecycle_event", "claims"}
V14_TOP_LEVEL_FIELDS = {"organization", "development", "knowledge"}
PROJECT_REQUIRED_FIELDS = (
    "name",
    "family_slug",
    "family_display",
    "model_slug",
    "model_display",
    "version",
    "released",
    "vendor",
    "vendor_slug",
    "origin_region",
    "license",
    "base_model",
    "adaptation",
)
V13_PROJECT_FIELDS = {
    "aliases",
    "summary",
    "kind",
    "status",
    "homepage",
    "repository",
    "domains",
}
ADAPTATION_FIELDS = ("type", "purpose", "base_model_slug")
ADAPTATION_TYPES = {
    "lora",
    "controlnet",
    "ip-adapter",
    "dreambooth",
    "textual-inversion",
    "fine-tune",
    "checkpoint-merge",
    "hypernetwork",
}
FACET_FIELDS = (
    "category",
    "entry_type",
    "projects",
    "versions",
    "features",
    "formats",
    "platforms",
    "vendors",
    "base_models",
    "origin",
    "license",
    "year",
    "models",
    "adaptations",
)
RESOURCE_LINK_FIELDS = (
    "kind",
    "url",
    "host",
    "lang",
    "is_primary",
    "status",
    "verified_at",
)
LINK_SHARED_METADATA_FIELDS = ("kind", "host", "lang", "status", "label", "title")
ORGANIZATION_FIELDS = ("slug", "display", "homepage")
DEVELOPMENT_FIELDS = (
    "scope",
    "thread_slug",
    "thread_display",
    "predecessor_ids",
)
KNOWLEDGE_FIELDS = (
    "title",
    "article_url",
    "source_url",
    "agent_context_url",
)
SUBJECT_KINDS = {
    "company", "project", "model", "version", "tool", "service", "dataset", "method", "technology",
}
SUBJECT_RELATIONS = {
    "version_of", "derived_from", "compatible_with", "uses", "requires", "owned_by",
}
SUBJECT_PACKET_FIELDS = ("schema_version", "news_id", "subjects", "about", "mentions", "relations")
SUBJECT_FIELDS = ("subject_id", "subject_kind", "canonical_name", "identity_url")
SUBJECT_RELATION_FIELDS = ("from_subject_id", "relation", "to_subject_id", "evidence_urls")


class ContractError(ValueError):
    """Raised when canonical data cannot be built safely."""


class DuplicateKeyError(ValueError):
    """Raised when JSON text contains an ambiguous duplicate object key."""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def load_json_text(text: str) -> Any:
    return json.loads(text, object_pairs_hook=reject_duplicate_keys)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def is_http_url(value: Any, *, https_only: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    schemes = {"https"} if https_only else {"http", "https"}
    return parsed.scheme in schemes and bool(parsed.netloc)


def is_public_https_url(value: Any) -> bool:
    if not is_http_url(value, https_only=True):
        return False
    parsed = urlparse(value)
    host = parsed.hostname
    if not host or host.lower() == "localhost" or host.lower().endswith(".local"):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if host.lower() in {"t.me", "telegram.me", "telegram.org"} or host.lower().endswith((".telegram.me", ".telegram.org")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def validate_exact_keys(
    errors: list[str],
    path: str,
    value: dict[str, Any],
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    for key in missing:
        errors.append(f"{path}.{key}: required field is missing")
    for key in unknown:
        errors.append(f"{path}.{key}: unexpected field")


def validate_string_list(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: expected an array")
        return
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        errors.append(f"{path}: every entry must be a non-empty string")
        return
    if len(value) != len(set(value)):
        errors.append(f"{path}: duplicate entries are not allowed")


def validate_nullable_string(errors: list[str], path: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        errors.append(f"{path}: expected string or null")


def validate_timestamp(errors: list[str], path: str, value: Any) -> None:
    if value is not None and (
        not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value)
    ):
        errors.append(f"{path}: expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ or null")


def find_private_keys(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            child = f"{path}.{key}"
            if key.lower() in FORBIDDEN_PUBLIC_KEYS:
                findings.append(child)
            findings.extend(find_private_keys(nested, child))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(find_private_keys(nested, f"{path}[{index}]"))
    return findings


def validate_project(errors: list[str], item: dict[str, Any]) -> None:
    project = item.get("project")
    if not isinstance(project, dict):
        errors.append("project: expected object")
        return
    validate_exact_keys(
        errors,
        "project",
        project,
        PROJECT_REQUIRED_FIELDS,
        V13_PROJECT_FIELDS,
    )

    for key in PROJECT_REQUIRED_FIELDS:
        if key == "adaptation":
            adaptation = project.get(key)
            if adaptation is not None and not isinstance(adaptation, dict):
                errors.append("project.adaptation: expected object or null")
            elif isinstance(adaptation, dict):
                validate_exact_keys(
                    errors, "project.adaptation", adaptation, ADAPTATION_FIELDS
                )
                if adaptation.get("type") not in ADAPTATION_TYPES:
                    errors.append(
                        "project.adaptation.type: expected one of "
                        f"{sorted(ADAPTATION_TYPES)}"
                    )
                validate_nullable_string(
                    errors, "project.adaptation.purpose", adaptation.get("purpose")
                )
                base_model_slug = adaptation.get("base_model_slug")
                if not isinstance(base_model_slug, str) or not SLUG_RE.fullmatch(
                    base_model_slug
                ):
                    errors.append(
                        "project.adaptation.base_model_slug: expected lowercase "
                        "kebab-case slug"
                    )
        else:
            validate_nullable_string(errors, f"project.{key}", project.get(key))

    identity = ("name", "family_slug", "family_display")
    anonymous = all(project.get(key) is None for key in identity)
    if anonymous:
        if any(project.get(key) is not None for key in PROJECT_REQUIRED_FIELDS[:-1]):
            errors.append("project: anonymous records must keep all identity fields null")
    else:
        for key in identity:
            if not isinstance(project.get(key), str) or not project[key].strip():
                errors.append(f"project.{key}: expected non-empty string")

    slug = project.get("family_slug")
    if isinstance(slug, str) and not SLUG_RE.fullmatch(slug):
        errors.append("project.family_slug: expected lowercase kebab-case slug")

    aliases = project.get("aliases")
    if aliases is not None:
        validate_string_list(errors, "project.aliases", aliases)
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and not SLUG_RE.fullmatch(alias):
                    errors.append(f"project.aliases: invalid slug {alias!r}")
                if alias == slug:
                    errors.append("project.aliases: canonical slug cannot be its own alias")

    summary = project.get("summary")
    if summary is not None and (not isinstance(summary, str) or not summary.strip()):
        errors.append("project.summary: expected non-empty string")
    for key in ("kind", "status"):
        value = project.get(key)
        if value is not None and (
            not isinstance(value, str) or not SLUG_RE.fullmatch(value)
        ):
            errors.append(f"project.{key}: expected lowercase kebab-case slug")
    for key in ("homepage", "repository"):
        value = project.get(key)
        if value is not None and not is_http_url(value, https_only=True):
            errors.append(f"project.{key}: expected HTTPS URL")

    domains = project.get("domains")
    if domains is not None:
        if not isinstance(domains, list):
            errors.append("project.domains: expected array")
        else:
            seen_slugs: set[str] = set()
            seen_urls: set[str] = set()
            for index, domain in enumerate(domains):
                prefix = f"project.domains[{index}]"
                if not isinstance(domain, dict):
                    errors.append(f"{prefix}: expected object")
                    continue
                validate_exact_keys(errors, prefix, domain, ("slug", "label", "url"))
                domain_slug = domain.get("slug")
                if not isinstance(domain_slug, str) or not SLUG_RE.fullmatch(domain_slug):
                    errors.append(f"{prefix}.slug: expected lowercase kebab-case slug")
                elif domain_slug in seen_slugs:
                    errors.append(f"{prefix}.slug: duplicate domain slug")
                else:
                    seen_slugs.add(domain_slug)
                label = domain.get("label")
                if not isinstance(label, str) or not label.strip():
                    errors.append(f"{prefix}.label: expected non-empty string")
                url = domain.get("url")
                if not is_http_url(url, https_only=True):
                    errors.append(f"{prefix}.url: expected HTTPS URL")
                elif url in seen_urls:
                    errors.append(f"{prefix}.url: duplicate domain URL")
                else:
                    seen_urls.add(url)


def validate_organization(errors: list[str], item: dict[str, Any]) -> None:
    organization = item.get("organization")
    if organization is None:
        return
    if not isinstance(organization, dict):
        errors.append("organization: expected object or null")
        return
    validate_exact_keys(errors, "organization", organization, ORGANIZATION_FIELDS)
    slug = organization.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        errors.append("organization.slug: expected lowercase kebab-case slug")
    display = organization.get("display")
    if not isinstance(display, str) or not display.strip():
        errors.append("organization.display: expected non-empty string")
    homepage = organization.get("homepage")
    if homepage is not None and not is_http_url(homepage, https_only=True):
        errors.append("organization.homepage: expected HTTPS URL or null")


def validate_development(errors: list[str], item: dict[str, Any]) -> None:
    development = item.get("development")
    if not isinstance(development, dict):
        errors.append("development: expected object")
        return
    validate_exact_keys(errors, "development", development, DEVELOPMENT_FIELDS)
    if development.get("scope") not in {"project", "organization"}:
        errors.append("development.scope: expected 'project' or 'organization'")
    for key in ("thread_slug",):
        value = development.get(key)
        if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
            errors.append(f"development.{key}: expected lowercase kebab-case slug")
    display = development.get("thread_display")
    if not isinstance(display, str) or not display.strip():
        errors.append("development.thread_display: expected non-empty string")
    predecessors = development.get("predecessor_ids")
    validate_string_list(errors, "development.predecessor_ids", predecessors)
    if isinstance(predecessors, list):
        for predecessor_id in predecessors:
            if isinstance(predecessor_id, str) and not ID_RE.fullmatch(predecessor_id):
                errors.append(
                    "development.predecessor_ids: expected n-{10 lowercase hex characters}"
                )


def is_commit_addressed_knowledge_source(value: Any) -> bool:
    if not is_http_url(value, https_only=True):
        return False
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc == "raw.githubusercontent.com":
        if len(parts) < 6:
            return False
        owner, repository, revision, root = parts[:4]
        markdown_path = parts[4:]
    elif parsed.netloc == "github.com":
        if len(parts) < 7 or parts[2] != "raw":
            return False
        owner, repository, _, revision, root = parts[:5]
        markdown_path = parts[5:]
    else:
        return False
    return (
        owner == "AnastasiyaW"
        and repository == "knowledge-space"
        and FULL_GIT_SHA_RE.fullmatch(revision) is not None
        and root == "docs"
        and bool(markdown_path)
        and markdown_path[-1].endswith(".md")
        and not parsed.query
        and not parsed.fragment
    )


def validate_knowledge(errors: list[str], item: dict[str, Any]) -> None:
    knowledge = item.get("knowledge")
    if not isinstance(knowledge, dict):
        errors.append("knowledge: expected object")
        return
    validate_exact_keys(errors, "knowledge", knowledge, KNOWLEDGE_FIELDS)
    title = knowledge.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("knowledge.title: expected non-empty string")
    article_url = knowledge.get("article_url")
    article = urlparse(article_url) if isinstance(article_url, str) else None
    if (
        article is None
        or article.scheme != "https"
        or article.netloc != "happyin.space"
        or not article.path.strip("/")
        or article.query
        or article.fragment
    ):
        errors.append("knowledge.article_url: expected canonical happyin.space article URL")
    source_url = knowledge.get("source_url")
    if not is_commit_addressed_knowledge_source(source_url):
        errors.append(
            "knowledge.source_url: expected commit-addressed Knowledge Space Markdown URL"
        )
    agent_context_url = knowledge.get("agent_context_url")
    agent_context = (
        urlparse(agent_context_url) if isinstance(agent_context_url, str) else None
    )
    if (
        agent_context is None
        or article is None
        or agent_context.scheme != article.scheme
        or agent_context.netloc != article.netloc
        or agent_context.path != article.path
        or agent_context.query
        or agent_context.fragment != "agent-brief"
    ):
        errors.append(
            "knowledge.agent_context_url: expected the article's #agent-brief URL"
        )


def validate_facets(errors: list[str], item: dict[str, Any]) -> None:
    facets = item.get("facets")
    if not isinstance(facets, dict):
        errors.append("facets: expected object")
        return
    validate_exact_keys(errors, "facets", facets, FACET_FIELDS)
    for key in (
        "projects",
        "versions",
        "features",
        "formats",
        "platforms",
        "vendors",
        "base_models",
        "models",
        "adaptations",
    ):
        validate_string_list(errors, f"facets.{key}", facets.get(key))
    for key in ("category", "entry_type"):
        if not isinstance(facets.get(key), str):
            errors.append(f"facets.{key}: expected string")
        elif facets[key] != item.get(key):
            errors.append(f"facets.{key}: must match root {key}")
    for key in ("origin", "license"):
        validate_nullable_string(errors, f"facets.{key}", facets.get(key))
    year = facets.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        errors.append("facets.year: expected integer")


def validate_links(errors: list[str], item: dict[str, Any]) -> None:
    links = item.get("links")
    if not isinstance(links, list) or not links:
        errors.append("links: expected non-empty array")
        return
    primary_count = 0
    seen_urls: set[str] = set()
    for index, link in enumerate(links):
        prefix = f"links[{index}]"
        if not isinstance(link, dict):
            errors.append(f"{prefix}: expected object")
            continue
        validate_exact_keys(errors, prefix, link, RESOURCE_LINK_FIELDS)
        for key in ("kind", "host", "lang", "status"):
            if not isinstance(link.get(key), str) or not link[key].strip():
                errors.append(f"{prefix}.{key}: expected non-empty string")
        url = link.get("url")
        if not is_http_url(url):
            errors.append(f"{prefix}.url: expected HTTP(S) URL")
        elif url in seen_urls:
            errors.append(f"{prefix}.url: duplicate URL")
        else:
            seen_urls.add(url)
        if not isinstance(link.get("is_primary"), bool):
            errors.append(f"{prefix}.is_primary: expected boolean")
        elif link["is_primary"]:
            primary_count += 1
        validate_timestamp(errors, f"{prefix}.verified_at", link.get("verified_at"))
    if primary_count != 1:
        errors.append(f"links: expected exactly one primary link, found {primary_count}")


def validate_evolution(errors: list[str], item: dict[str, Any]) -> None:
    evolution = item.get("evolution")
    if not isinstance(evolution, dict):
        errors.append("evolution: expected object")
        return
    validate_exact_keys(
        errors,
        "evolution",
        evolution,
        ("summary", "latest_version", "related_ids", "is_latest_in_family"),
    )
    validate_nullable_string(errors, "evolution.summary", evolution.get("summary"))
    validate_nullable_string(
        errors, "evolution.latest_version", evolution.get("latest_version")
    )
    validate_string_list(errors, "evolution.related_ids", evolution.get("related_ids"))
    for related_id in evolution.get("related_ids", []):
        if isinstance(related_id, str) and not ID_RE.fullmatch(related_id):
            errors.append(f"evolution.related_ids: invalid news id {related_id!r}")
    if not isinstance(evolution.get("is_latest_in_family"), bool):
        errors.append("evolution.is_latest_in_family: expected boolean")


def validate_media(errors: list[str], item: dict[str, Any]) -> None:
    media = item.get("media")
    if not isinstance(media, dict):
        errors.append("media: expected object")
        return
    validate_exact_keys(errors, "media", media, ("has_image", "has_video"))
    for key in ("has_image", "has_video"):
        if not isinstance(media.get(key), bool):
            errors.append(f"media.{key}: expected boolean")


def validate_source(errors: list[str], item: dict[str, Any]) -> None:
    source = item.get("source")
    if not isinstance(source, dict):
        errors.append("source: expected object")
        return
    validate_exact_keys(errors, "source", source, ("posted_at", "excerpt"))
    if not is_date(source.get("posted_at")):
        errors.append("source.posted_at: expected valid YYYY-MM-DD")
    if not isinstance(source.get("excerpt"), str):
        errors.append("source.excerpt: expected string")


def validate_meta(errors: list[str], item: dict[str, Any]) -> None:
    meta = item.get("_meta")
    if not isinstance(meta, dict):
        errors.append("_meta: expected object")
        return
    validate_exact_keys(
        errors,
        "_meta",
        meta,
        (
            "extracted_at",
            "classified_at",
            "enriched_at",
            "verified_at",
            "models_used",
            "schema_validated",
        ),
    )
    for key in ("extracted_at", "classified_at", "enriched_at", "verified_at"):
        validate_timestamp(errors, f"_meta.{key}", meta.get(key))
    models = meta.get("models_used")
    if not isinstance(models, dict):
        errors.append("_meta.models_used: expected object")
    elif any(value is not None and not isinstance(value, str) for value in models.values()):
        errors.append("_meta.models_used: values must be strings or null")
    if meta.get("schema_validated") is not True:
        errors.append("_meta.schema_validated: expected literal true")


def validate_lifecycle(errors: list[str], item: dict[str, Any]) -> None:
    event = item.get("lifecycle_event")
    if event is None:
        return
    if not isinstance(event, dict):
        errors.append("lifecycle_event: expected object")
        return
    validate_exact_keys(
        errors,
        "lifecycle_event",
        event,
        ("kind", "date", "title"),
        ("version", "summary"),
    )
    kind = event.get("kind")
    if not isinstance(kind, str) or not SLUG_RE.fullmatch(kind):
        errors.append("lifecycle_event.kind: expected lowercase kebab-case slug")
    if not is_date(event.get("date")):
        errors.append("lifecycle_event.date: expected valid YYYY-MM-DD")
    if not isinstance(event.get("title"), str) or not event["title"].strip():
        errors.append("lifecycle_event.title: expected non-empty string")
    for key in ("version", "summary"):
        if key in event:
            validate_nullable_string(errors, f"lifecycle_event.{key}", event[key])


def validate_claims(errors: list[str], item: dict[str, Any]) -> None:
    claims = item.get("claims")
    if claims is None:
        return
    if not isinstance(claims, list) or not claims:
        errors.append("claims: expected non-empty array")
        return
    seen_ids: set[str] = set()
    for index, claim in enumerate(claims):
        prefix = f"claims[{index}]"
        if not isinstance(claim, dict):
            errors.append(f"{prefix}: expected object")
            continue
        validate_exact_keys(
            errors,
            prefix,
            claim,
            ("id", "statement", "kind", "status", "evidence"),
        )
        for key in ("id", "statement", "kind"):
            if not isinstance(claim.get(key), str) or not claim[key].strip():
                errors.append(f"{prefix}.{key}: expected non-empty string")
        claim_id = claim.get("id")
        if isinstance(claim_id, str):
            if claim_id in seen_ids:
                errors.append(f"{prefix}.id: duplicate claim id")
            seen_ids.add(claim_id)
        if claim.get("status") not in {"verified", "reported", "inferred"}:
            errors.append(f"{prefix}.status: expected verified, reported, or inferred")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{prefix}.evidence: expected non-empty array")
            continue
        for evidence_index, source in enumerate(evidence):
            source_path = f"{prefix}.evidence[{evidence_index}]"
            if not isinstance(source, dict):
                errors.append(f"{source_path}: expected object")
                continue
            validate_exact_keys(
                errors,
                source_path,
                source,
                ("url",),
                ("title", "publisher", "published_at"),
            )
            if not is_http_url(source.get("url"), https_only=True):
                errors.append(f"{source_path}.url: expected HTTPS URL")
            for key in ("title", "publisher"):
                if key in source:
                    validate_nullable_string(errors, f"{source_path}.{key}", source[key])
            if "published_at" in source:
                published_at = source["published_at"]
                if published_at is not None and not is_date(published_at):
                    errors.append(f"{source_path}.published_at: expected YYYY-MM-DD or null")


def validate_item(item: Any, filename: str = "<memory>") -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return ["item: expected object"]
    validate_exact_keys(
        errors,
        "$",
        item,
        BASE_REQUIRED_FIELDS,
        V13_TOP_LEVEL_FIELDS | V14_TOP_LEVEL_FIELDS,
    )

    version = item.get("schema_version")
    if version not in SUPPORTED_ITEM_VERSIONS:
        errors.append(
            f"schema_version: expected one of {sorted(SUPPORTED_ITEM_VERSIONS)}, got {version!r}"
        )
    item_id = item.get("id")
    if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
        errors.append("id: expected n-{10 lowercase hex characters}")
    if filename != "<memory>" and isinstance(item_id, str):
        if Path(filename).stem != item_id:
            errors.append(f"id: {item_id!r} does not match filename {filename!r}")

    for key in ("entry_type", "category"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            errors.append(f"{key}: expected non-empty string")
    for key in ("title", "summary", "research_notes"):
        if not isinstance(item.get(key), str):
            errors.append(f"{key}: expected string")
    if item.get("lang") != "en":
        errors.append("lang: public canonical items must use 'en'")
    validate_string_list(errors, "tags", item.get("tags"))

    validate_project(errors, item)
    validate_facets(errors, item)
    validate_links(errors, item)
    validate_evolution(errors, item)
    validate_media(errors, item)
    validate_source(errors, item)
    validate_meta(errors, item)
    validate_lifecycle(errors, item)
    validate_claims(errors, item)

    if version == "1.4":
        for key in sorted(V14_TOP_LEVEL_FIELDS):
            if key not in item:
                errors.append(f"$.{key}: required for schema 1.4")
        if "lifecycle_event" not in item:
            errors.append("$.lifecycle_event: required for schema 1.4")
        validate_organization(errors, item)
        validate_development(errors, item)
        validate_knowledge(errors, item)

        project = item.get("project") if isinstance(item.get("project"), dict) else {}
        organization = item.get("organization")
        family_slug = project.get("family_slug")
        if family_slug is None and organization is None:
            errors.append("schema 1.4 requires a project or organization identity")
        development = item.get("development")
        if isinstance(development, dict):
            if development.get("scope") == "project" and family_slug is None:
                errors.append("development.scope project requires project.family_slug")
            if development.get("scope") == "organization" and organization is None:
                errors.append("development.scope organization requires organization")
        if isinstance(organization, dict) and family_slug is not None:
            if project.get("vendor_slug") != organization.get("slug"):
                errors.append(
                    "project.vendor_slug must match organization.slug for schema 1.4"
                )
            if project.get("vendor") != organization.get("display"):
                errors.append(
                    "project.vendor must match organization.display for schema 1.4"
                )

    source = item.get("source")
    facets = item.get("facets")
    if isinstance(source, dict) and is_date(source.get("posted_at")) and isinstance(facets, dict):
        if facets.get("year") != int(source["posted_at"][:4]):
            errors.append("facets.year: must match source.posted_at")

    if version == "1.2":
        present_top = V13_TOP_LEVEL_FIELDS.intersection(item)
        project = item.get("project") if isinstance(item.get("project"), dict) else {}
        present_project = set(project).intersection(V13_PROJECT_FIELDS)
        if present_top or present_project:
            fields = sorted(present_top | {f"project.{name}" for name in present_project})
            errors.append(f"schema_version: v1.3 fields require '1.3': {', '.join(fields)}")

    if version in {"1.2", "1.3"}:
        present_v14 = sorted(V14_TOP_LEVEL_FIELDS.intersection(item))
        if present_v14:
            errors.append(
                f"schema_version: v1.4 fields require '1.4': {', '.join(present_v14)}"
            )

    for private_path in find_private_keys(item):
        errors.append(f"{private_path}: private/raw field is forbidden in the public feed")
    return errors


def development_subject(item: dict[str, Any]) -> tuple[str, str] | None:
    development = item.get("development")
    if not isinstance(development, dict):
        return None
    if development.get("scope") == "project":
        slug = item.get("project", {}).get("family_slug")
    elif development.get("scope") == "organization":
        organization = item.get("organization")
        slug = organization.get("slug") if isinstance(organization, dict) else None
    else:
        return None
    return (development["scope"], slug) if isinstance(slug, str) else None


def validate_development_graph(records: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    by_id = {item["id"]: item for item in records if isinstance(item.get("id"), str)}
    for item in records:
        if item.get("schema_version") != "1.4":
            continue
        development = item.get("development")
        if not isinstance(development, dict):
            continue
        subject = development_subject(item)
        for predecessor_id in development.get("predecessor_ids", []):
            predecessor = by_id.get(predecessor_id)
            if predecessor is None:
                errors.append(
                    f"{item['id']}: development.predecessor_ids references missing "
                    f"{predecessor_id}"
                )
                continue
            predecessor_development = predecessor.get("development")
            if not isinstance(predecessor_development, dict):
                errors.append(
                    f"{item['id']}: predecessor {predecessor_id} has no schema 1.4 development"
                )
                continue
            if development_subject(predecessor) != subject:
                errors.append(
                    f"{item['id']}: predecessor {predecessor_id} belongs to a different "
                    "development subject"
                )
                continue
            if predecessor_development.get("thread_slug") != development.get(
                "thread_slug"
            ):
                errors.append(
                    f"{item['id']}: predecessor {predecessor_id} belongs to a different "
                    "development thread"
                )
                continue
            if chronological_key(predecessor) >= chronological_key(item):
                errors.append(
                    f"{item['id']}: predecessor {predecessor_id} must be strictly earlier"
                )
    return errors


def load_items(news_dir: Path) -> list[dict[str, Any]]:
    item_dir = news_dir / "items"
    if not item_dir.is_dir():
        raise ContractError(f"items directory does not exist: {item_dir}")
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for path in sorted(item_dir.glob("*.json"), key=lambda candidate: candidate.name):
        try:
            item = load_json_text(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, DuplicateKeyError) as exc:
            errors.append(f"{path.name}: invalid JSON: {exc}")
            continue
        for error in validate_item(item, path.name):
            errors.append(f"{path.name}: {error}")
        item_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(item_id, str):
            if item_id in seen_ids:
                errors.append(f"{path.name}: duplicate id {item_id}")
            seen_ids.add(item_id)
        if isinstance(item, dict):
            records.append(item)

    if not records:
        errors.append("items: at least one canonical item is required")
    for item in records:
        evolution = item.get("evolution")
        if not isinstance(evolution, dict):
            continue
        for related_id in evolution.get("related_ids", []):
            if related_id not in seen_ids:
                errors.append(
                    f"{item.get('id', '<unknown>')}: evolution.related_ids references missing {related_id}"
                )
    errors.extend(validate_development_graph(records))
    if errors:
        raise ContractError("\n".join(errors))
    return records


def _subject_id(identity_url: str) -> str:
    return "sgs-" + hashlib.sha256(identity_url.encode("utf-8")).hexdigest()[:32]


def _subject_text(errors: list[str], path: str, value: Any, maximum: int = 2_000) -> bool:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        errors.append(f"{path}: expected trimmed non-empty string")
        return False
    return True


def validate_subject_packet(packet: Any, filename: str, item_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(packet, dict):
        return ["packet: expected object"]
    validate_exact_keys(errors, "$", packet, SUBJECT_PACKET_FIELDS)
    if packet.get("schema_version") != "subject-links-v1":
        errors.append("schema_version: expected 'subject-links-v1'")
    news_id = packet.get("news_id")
    if not isinstance(news_id, str) or not ID_RE.fullmatch(news_id):
        errors.append("news_id: expected existing n-{10 lowercase hex characters}")
    elif Path(filename).stem != news_id:
        errors.append(f"news_id: {news_id!r} does not match filename {filename!r}")
    elif news_id not in item_ids:
        errors.append(f"news_id: references missing canonical item {news_id}")

    subjects = packet.get("subjects")
    subjects_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(subjects, list) or not subjects:
        errors.append("subjects: expected non-empty array")
    elif any(not isinstance(subject, dict) for subject in subjects):
        errors.append("subjects: every entry must be an object")
    else:
        for index, subject in enumerate(subjects):
            assert isinstance(subject, dict)
            prefix = f"subjects[{index}]"
            validate_exact_keys(errors, prefix, subject, SUBJECT_FIELDS)
            subject_id = subject.get("subject_id")
            identity_url = subject.get("identity_url")
            if not isinstance(subject_id, str) or not SUBJECT_ID_RE.fullmatch(subject_id):
                errors.append(f"{prefix}.subject_id: expected sgs-{{32 lowercase hex characters}}")
            if subject.get("subject_kind") not in SUBJECT_KINDS:
                errors.append(f"{prefix}.subject_kind: unsupported subject kind")
            _subject_text(errors, f"{prefix}.canonical_name", subject.get("canonical_name"), 500)
            if not is_public_https_url(identity_url):
                errors.append(f"{prefix}.identity_url: expected public HTTPS URL")
            elif subject_id != _subject_id(identity_url):
                errors.append(f"{prefix}.subject_id: must derive from exact identity_url")
            if isinstance(subject_id, str):
                if subject_id in subjects_by_id:
                    errors.append(f"{prefix}.subject_id: duplicate subject")
                subjects_by_id[subject_id] = subject

    about = packet.get("about")
    if not isinstance(about, str) or about not in subjects_by_id:
        errors.append("about: must name exactly one packet subject")
    mentions = packet.get("mentions")
    if not isinstance(mentions, list) or any(not isinstance(value, str) for value in mentions):
        errors.append("mentions: expected array of subject IDs")
    elif len(mentions) != len(set(mentions)) or any(value not in subjects_by_id for value in mentions):
        errors.append("mentions: must be unique packet subjects")
    elif about in mentions:
        errors.append("mentions: primary about subject cannot be a secondary mention")

    relations = packet.get("relations")
    seen_relations: set[tuple[str, str, str, tuple[str, ...]]] = set()
    if not isinstance(relations, list) or any(not isinstance(value, dict) for value in relations):
        errors.append("relations: expected array of relation objects")
    else:
        for index, relation in enumerate(relations):
            assert isinstance(relation, dict)
            prefix = f"relations[{index}]"
            validate_exact_keys(errors, prefix, relation, SUBJECT_RELATION_FIELDS)
            source = relation.get("from_subject_id")
            target = relation.get("to_subject_id")
            if not isinstance(source, str) or source not in subjects_by_id:
                errors.append(f"{prefix}.from_subject_id: must name a packet subject")
            if not isinstance(target, str) or target not in subjects_by_id:
                errors.append(f"{prefix}.to_subject_id: must name a packet subject")
            if source == target and isinstance(source, str):
                errors.append(f"{prefix}: self relations are forbidden")
            if relation.get("relation") not in SUBJECT_RELATIONS:
                errors.append(f"{prefix}.relation: unsupported relation")
            urls = relation.get("evidence_urls")
            if not isinstance(urls, list) or not urls or any(not is_public_https_url(url) for url in urls):
                errors.append(f"{prefix}.evidence_urls: expected non-empty public HTTPS URLs")
                continue
            if len(urls) != len(set(urls)):
                errors.append(f"{prefix}.evidence_urls: duplicate URLs are forbidden")
            key = (source, str(relation.get("relation")), target, tuple(sorted(urls)))
            if key in seen_relations:
                errors.append(f"{prefix}: duplicate relation")
            seen_relations.add(key)
    for private_path in find_private_keys(packet):
        errors.append(f"{private_path}: private/raw field is forbidden in the public feed")
    return errors


def _acyclic_subject_relations(packets: list[dict[str, Any]]) -> list[str]:
    edges: dict[str, set[str]] = defaultdict(set)
    for packet in packets:
        for relation in packet["relations"]:
            if relation["relation"] in {"version_of", "derived_from"}:
                edges[relation["from_subject_id"]].add(relation["to_subject_id"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(subject_id: str) -> bool:
        if subject_id in visiting:
            return True
        if subject_id in visited:
            return False
        visiting.add(subject_id)
        if any(visit(target) for target in edges[subject_id]):
            return True
        visiting.remove(subject_id)
        visited.add(subject_id)
        return False

    return ["subject links: version_of/derived_from cycle"] if any(visit(subject_id) for subject_id in edges) else []


def load_subject_packets(news_dir: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    packet_dir = news_dir / "subject-links"
    if not packet_dir.exists():
        return []
    if not packet_dir.is_dir():
        raise ContractError(f"subject-links is not a directory: {packet_dir}")
    item_ids = {record["id"] for record in records}
    packets: list[dict[str, Any]] = []
    errors: list[str] = []
    identity_by_subject: dict[str, tuple[str, str]] = {}
    for path in sorted(packet_dir.glob("*.json"), key=lambda candidate: candidate.name):
        try:
            packet = load_json_text(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, DuplicateKeyError) as exc:
            errors.append(f"{path.name}: invalid JSON: {exc}")
            continue
        packet_errors = validate_subject_packet(packet, path.name, item_ids)
        for error in packet_errors:
            errors.append(f"{path.name}: {error}")
        if packet_errors or not isinstance(packet, dict):
            continue
        for subject in packet.get("subjects", []):
            if not isinstance(subject, dict):
                continue
            subject_id = subject.get("subject_id")
            identity = (subject.get("identity_url"), subject.get("subject_kind"))
            if isinstance(subject_id, str) and all(isinstance(value, str) for value in identity):
                prior = identity_by_subject.setdefault(subject_id, identity)
                if prior != identity:
                    errors.append(f"{path.name}: {subject_id} conflicts with prior identity URL or kind")
        packets.append(packet)
    errors.extend(_acyclic_subject_relations(packets))
    if errors:
        raise ContractError("\n".join(errors))
    return packets


def build_subject_index(records: list[dict[str, Any]], packets: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {record["id"]: record for record in records}
    packets_by_id = {packet["news_id"]: packet for packet in packets}
    subjects: dict[str, dict[str, Any]] = {}
    names: dict[str, tuple[str, str, str]] = {}
    for news_id, packet in sorted(packets_by_id.items()):
        event_at = posted_at(by_id[news_id])
        for subject in packet["subjects"]:
            subject_id = subject["subject_id"]
            entry = subjects.setdefault(subject_id, {
                "subject_kind": subject["subject_kind"], "canonical_name": subject["canonical_name"],
                "identity_url": subject["identity_url"], "first_known_event_at": event_at,
                "about_news_ids": [], "mentioned_news_ids": [], "relations": [],
            })
            if (entry["subject_kind"], entry["identity_url"]) != (subject["subject_kind"], subject["identity_url"]):
                raise ContractError(f"{subject_id}: conflicting subject identity")
            candidate = (event_at, news_id, subject["canonical_name"])
            if candidate >= names.get(subject_id, ("", "", "")):
                names[subject_id] = candidate
                entry["canonical_name"] = subject["canonical_name"]
            entry["first_known_event_at"] = min(entry["first_known_event_at"], event_at)
        subjects[packet["about"]]["about_news_ids"].append(news_id)
        for subject_id in packet["mentions"]:
            subjects[subject_id]["mentioned_news_ids"].append(news_id)
        for relation in packet["relations"]:
            subjects[relation["from_subject_id"]]["relations"].append({
                "from_subject_id": relation["from_subject_id"], "relation": relation["relation"],
                "to_subject_id": relation["to_subject_id"], "evidence_urls": sorted(relation["evidence_urls"]), "news_id": news_id,
            })
    for subject_id, entry in subjects.items():
        key = lambda news_id: (posted_at(by_id[news_id]), news_id)
        entry["about_news_ids"] = sorted(entry["about_news_ids"], key=key)
        entry["mentioned_news_ids"] = sorted(entry["mentioned_news_ids"], key=key)
        seen: set[tuple[str, str, str, tuple[str, ...], str]] = set()
        unique = []
        for relation in sorted(entry["relations"], key=lambda value: (posted_at(by_id[value["news_id"]]), value["news_id"], value["from_subject_id"], value["relation"], value["to_subject_id"], value["evidence_urls"])):
            marker = (relation["from_subject_id"], relation["relation"], relation["to_subject_id"], tuple(relation["evidence_urls"]), relation["news_id"])
            if marker not in seen:
                seen.add(marker)
                unique.append(relation)
        entry["relations"] = unique
    return {
        "schema_version": "subjects-index-v1",
        "packets": {news_id: f"subject-links/{news_id}.json" for news_id in sorted(packets_by_id)},
        "subjects": {subject_id: subjects[subject_id] for subject_id in sorted(subjects)},
    }


def posted_at(item: dict[str, Any]) -> str:
    return item["source"]["posted_at"]


def chronological_key(item: dict[str, Any]) -> tuple[str, str]:
    return posted_at(item), item["id"]


def primary_link(item: dict[str, Any]) -> str:
    for link in item["links"]:
        if link["is_primary"]:
            return link["url"]
    raise ContractError(f"{item['id']}: primary link disappeared after validation")


def projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "date": posted_at(item),
        "title": item["title"],
        "category": item["category"],
        "entry_type": item["entry_type"],
        "project": item["project"]["family_slug"],
        "version": item["project"].get("version"),
        "tags": item["tags"][:8],
        "primary_link": primary_link(item),
        "has_image": item["media"]["has_image"],
        "has_video": item["media"]["has_video"],
    }


def first_non_null(items: Iterable[dict[str, Any]], path: tuple[str, ...]) -> Any:
    for item in items:
        value: Any = item
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def aggregate_family_display(
    slug: str, items: list[dict[str, Any]]
) -> tuple[str | None, list[dict[str, Any]]]:
    """Select the latest display and retain all dated public display variants."""
    variants: dict[str, dict[str, Any]] = {}
    displays_by_date: dict[str, set[str]] = defaultdict(set)
    for item in sorted(items, key=chronological_key):
        display = item["project"].get("family_display")
        if display is None:
            continue
        displays_by_date[posted_at(item)].add(display)
        entry = variants.setdefault(
            display,
            {
                "family_display": display,
                "first_seen": posted_at(item),
                "last_seen": posted_at(item),
                "news_ids": [],
            },
        )
        entry["last_seen"] = posted_at(item)
        entry["news_ids"].append(item["id"])

    if not displays_by_date:
        return None, []
    errors = [
        f"project {slug!r} has conflicting family displays on date "
        f"{display_date!r}: {sorted(displays)!r}"
        for display_date, displays in sorted(displays_by_date.items())
        if len(displays) > 1
    ]
    if errors:
        raise ContractError("\n".join(errors))
    latest_date = max(displays_by_date)
    return next(iter(displays_by_date[latest_date])), list(variants.values())


def validate_project_display_aggregation(
    grouped_projects: dict[str, list[dict[str, Any]]]
) -> None:
    errors: list[str] = []
    for slug in sorted(grouped_projects):
        try:
            aggregate_family_display(slug, grouped_projects[slug])
        except ContractError as exc:
            errors.append(str(exc))
    if errors:
        raise ContractError("\n".join(errors))


def consistent_project_value(items: list[dict[str, Any]], key: str) -> Any:
    values = {
        json.dumps(item["project"][key], ensure_ascii=False, sort_keys=True): item["project"][key]
        for item in items
        if item["project"].get(key) is not None
    }
    if len(values) > 1:
        slug = items[0]["project"]["family_slug"]
        raise ContractError(f"project {slug!r} has conflicting non-null {key!r} values")
    return next(iter(values.values()), None)


def consistent_organization(items: list[dict[str, Any]], subject: str) -> Any:
    values = {
        json.dumps(item["organization"], ensure_ascii=False, sort_keys=True): item[
            "organization"
        ]
        for item in items
        if isinstance(item.get("organization"), dict)
    }
    if len(values) > 1:
        raise ContractError(
            f"{subject} has conflicting non-null organization metadata"
        )
    return next(iter(values.values()), None)


def aggregate_development_threads(
    items: list[dict[str, Any]], scope: str
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(items, key=chronological_key):
        development = item.get("development")
        if isinstance(development, dict) and development.get("scope") == scope:
            grouped[development["thread_slug"]].append(item)

    result: list[dict[str, Any]] = []
    for slug in sorted(grouped):
        thread_items = grouped[slug]
        displays = {item["development"]["thread_display"] for item in thread_items}
        if len(displays) != 1:
            raise ContractError(
                f"development thread {slug!r} has conflicting display names"
            )
        result.append(
            {
                "slug": slug,
                "display": next(iter(displays)),
                "first_seen": posted_at(thread_items[0]),
                "last_seen": posted_at(thread_items[-1]),
                "latest_news_id": thread_items[-1]["id"],
                "news_ids": [item["id"] for item in thread_items],
            }
        )
    return result


def aggregate_domains(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_slug: dict[str, dict[str, str]] = {}
    for item in items:
        for domain in item["project"].get("domains", []):
            existing = by_slug.get(domain["slug"])
            if existing is not None and existing != domain:
                raise ContractError(
                    f"project {item['project']['family_slug']!r} has conflicting domain "
                    f"metadata for {domain['slug']!r}"
                )
            by_slug[domain["slug"]] = domain
    return [by_slug[slug] for slug in sorted(by_slug)]


def aggregate_links(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for item in sorted(items, key=chronological_key):
        for link in item["links"]:
            by_url[link["url"]].append((item["id"], link))

    result: list[dict[str, Any]] = []
    for url in sorted(by_url):
        occurrences = by_url[url]
        shared: dict[str, Any] = {}
        for field in LINK_SHARED_METADATA_FIELDS:
            values = {
                json.dumps(link[field], ensure_ascii=False, sort_keys=True): link[field]
                for _, link in occurrences
                if link.get(field) is not None
            }
            if len(values) > 1:
                raise ContractError(
                    f"link {url!r} has conflicting non-null {field!r} metadata"
                )
            if values:
                shared[field] = next(iter(values.values()))

        verified_at = [
            link["verified_at"]
            for _, link in occurrences
            if link["verified_at"] is not None
        ]
        aggregate = {
            **shared,
            "url": url,
            "is_primary": any(link["is_primary"] for _, link in occurrences),
            "verified_at": max(verified_at) if verified_at else None,
            "news_ids": [news_id for news_id, _ in occurrences],
        }
        context = [
            {
                "news_id": news_id,
                "is_primary": link["is_primary"],
                "verified_at": link["verified_at"],
            }
            for news_id, link in occurrences
        ]
        if len({(value["is_primary"], value["verified_at"]) for value in context}) > 1:
            aggregate["occurrences"] = context
        result.append(aggregate)
    result.sort(key=lambda link: (not link["is_primary"], link["kind"], link["url"]))
    return result


def aggregate_project(slug: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    chronological = sorted(items, key=chronological_key)
    newest = chronological[-1]
    family_display, family_display_variants = aggregate_family_display(slug, chronological)
    aliases = sorted(
        {
            alias
            for item in chronological
            for alias in item["project"].get("aliases", [])
        }
    )
    timeline: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    for item in chronological:
        event = item.get("lifecycle_event")
        if event is None:
            kind = "news"
            event_date = posted_at(item)
            title = item["title"]
            summary = item["summary"] or None
            version = item["project"].get("version")
        else:
            kind = event["kind"]
            event_date = event["date"]
            title = event["title"]
            summary = event.get("summary")
            version = event.get("version")
        timeline.append(
            {
                "kind": kind,
                "date": event_date,
                "title": title,
                "summary": summary,
                "version": version,
                "news_id": item["id"],
                "primary_link": primary_link(item),
            }
        )
        for claim in item.get("claims", []):
            claims.append({**claim, "news_id": item["id"]})

    result = {
        "family_slug": slug,
        "family_display": family_display,
        "family_display_variants": family_display_variants,
        "vendor": first_non_null(chronological, ("project", "vendor")),
        "vendor_slug": first_non_null(chronological, ("project", "vendor_slug")),
        "origin_region": first_non_null(
            chronological, ("project", "origin_region")
        ),
        "license": first_non_null(chronological, ("project", "license")),
        "base_model": first_non_null(chronological, ("project", "base_model")),
        "categories": sorted({item["category"] for item in chronological}),
        "post_count": len(chronological),
        "first_seen": posted_at(chronological[0]),
        "last_seen": posted_at(newest),
        "latest_version": newest["evolution"].get("latest_version"),
        "latest_evolution": newest["evolution"].get("summary"),
        "news_ids": [item["id"] for item in chronological],
        "versions_timeline": [
            {
                "version": item["project"].get("version"),
                "date": posted_at(item),
                "news_id": item["id"],
                "title": item["title"],
                "primary_link": primary_link(item),
            }
            for item in chronological
        ],
        "features": sorted(
            {
                value
                for item in chronological
                for value in item["facets"].get("features", [])
            }
        ),
        "formats": sorted(
            {
                value
                for item in chronological
                for value in item["facets"].get("formats", [])
            }
        ),
        "platforms": sorted(
            {
                value
                for item in chronological
                for value in item["facets"].get("platforms", [])
            }
        ),
        "base_models": sorted(
            {
                value
                for item in chronological
                for value in item["facets"].get("base_models", [])
            }
        ),
        "kind": consistent_project_value(chronological, "kind"),
        "status": consistent_project_value(chronological, "status"),
        "homepage": consistent_project_value(chronological, "homepage"),
        "repository": consistent_project_value(chronological, "repository"),
        "summary": consistent_project_value(chronological, "summary"),
        "aliases": aliases,
        "domains": aggregate_domains(chronological),
        "links": aggregate_links(chronological),
        "timeline": timeline,
        "related_news": [projection(item) for item in reversed(chronological)],
        "claims": claims,
    }
    organization = consistent_organization(chronological, f"project {slug!r}")
    development_threads = aggregate_development_threads(chronological, "project")
    if organization is not None:
        result["organization"] = organization
    if development_threads:
        result["development_threads"] = development_threads
    return result


def aggregate_organization(
    slug: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    chronological = sorted(items, key=chronological_key)
    newest = chronological[-1]
    identity = consistent_organization(chronological, f"organization {slug!r}")
    if not isinstance(identity, dict):
        raise ContractError(f"organization {slug!r} has no public identity")
    direct = [
        item
        for item in chronological
        if item.get("development", {}).get("scope") == "organization"
    ]
    return {
        "slug": slug,
        "display": identity["display"],
        "homepage": identity["homepage"],
        "project_slugs": sorted(
            {
                item["project"]["family_slug"]
                for item in chronological
                if item["project"].get("family_slug") is not None
            }
        ),
        "post_count": len(chronological),
        "direct_post_count": len(direct),
        "first_seen": posted_at(chronological[0]),
        "last_seen": posted_at(newest),
        "news_ids": [item["id"] for item in chronological],
        "direct_news_ids": [item["id"] for item in direct],
        "development_threads": aggregate_development_threads(direct, "organization"),
        "related_news": [projection(item) for item in reversed(chronological)],
    }


def aggregate_organizations(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        organization = item.get("organization")
        if isinstance(organization, dict):
            grouped[organization["slug"]].append(item)
    return {
        slug: aggregate_organization(slug, grouped[slug]) for slug in sorted(grouped)
    }


def deterministic_timestamp(records: list[dict[str, Any]]) -> str:
    timestamps = [
        value
        for item in records
        for key in ("extracted_at", "classified_at", "enriched_at", "verified_at")
        if isinstance((value := item["_meta"].get(key)), str) and value
    ]
    if timestamps:
        return max(timestamps)
    return max(posted_at(item) for item in records) + "T00:00:00Z"


def build_outputs(records: list[dict[str, Any]], packets: list[dict[str, Any]] | None = None) -> dict[str, bytes]:
    ordered = sorted(records, key=chronological_key, reverse=True)
    generated_at = deterministic_timestamp(records)
    projections = [projection(item) for item in ordered]

    tags: dict[str, list[str]] = defaultdict(list)
    categories: dict[str, list[str]] = defaultdict(list)
    years: dict[str, list[str]] = defaultdict(list)
    for item in sorted(records, key=lambda record: record["id"], reverse=True):
        for tag in item["tags"]:
            tags[tag].append(item["id"])
        categories[item["category"]].append(item["id"])
        years[posted_at(item)[:4]].append(item["id"])

    grouped_projects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        family_slug = item["project"].get("family_slug")
        if family_slug is not None:
            grouped_projects[family_slug].append(item)
    validate_project_display_aggregation(grouped_projects)
    projects = {
        slug: aggregate_project(slug, grouped_projects[slug])
        for slug in sorted(grouped_projects)
    }
    organizations = aggregate_organizations(records)
    subject_index = build_subject_index(records, [] if packets is None else packets)

    aliases: dict[str, list[str]] = {}
    alias_owners: dict[str, str] = {}
    canonical_slugs = set(projects)
    for slug, entry in projects.items():
        if entry["aliases"]:
            aliases[slug] = entry["aliases"]
        for alias in entry["aliases"]:
            if alias in canonical_slugs:
                raise ContractError(
                    f"project alias {alias!r} collides with a canonical family slug"
                )
            owner = alias_owners.setdefault(alias, slug)
            if owner != slug:
                raise ContractError(
                    f"project alias {alias!r} is claimed by both {owner!r} and {slug!r}"
                )

    counts = {
        "items": len(ordered),
        "tags": len(tags),
        "categories": len(categories),
        "years": len(years),
        "projects": len(projects),
        "organizations": len(organizations),
    }
    values: dict[str, Any] = {
        "_meta.json": {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "site": SITE,
            "generated_at": generated_at,
            "counts": counts,
        },
        "_index.json": projections,
        "_tags.json": {key: tags[key] for key in sorted(tags)},
        "_categories.json": {key: categories[key] for key in sorted(categories)},
        "_years.json": {key: years[key] for key in sorted(years, reverse=True)},
        "_projects.json": projects,
        "_organizations.json": organizations,
        "_subjects.json": subject_index,
        "_aliases.json": aliases,
        "all.json": {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "generated_at": generated_at,
            "site": SITE,
            "total": len(ordered),
            "records": ordered,
        },
    }
    return {name: json_bytes(values[name]) for name in GENERATED_FILES}


def check_schema_document(news_dir: Path) -> None:
    schema_path = news_dir / "schema-v1.4.json"
    try:
        schema = load_json_text(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ContractError(f"formal schema is unreadable: {schema_path}: {exc}") from exc
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ContractError("schema-v1.4.json must declare JSON Schema draft 2020-12")
    if schema.get("$id") != "https://diffusion.love/news/schema-v1.4.json":
        raise ContractError("schema-v1.4.json has an unexpected canonical $id")
    subject_schema_path = news_dir / "schema-subject-links-v1.json"
    try:
        subject_schema = load_json_text(subject_schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ContractError(f"subject link schema is unreadable: {subject_schema_path}: {exc}") from exc
    if subject_schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ContractError("schema-subject-links-v1.json must declare JSON Schema draft 2020-12")
    if subject_schema.get("$id") != "https://diffusion.love/news/schema-subject-links-v1.json":
        raise ContractError("schema-subject-links-v1.json has an unexpected canonical $id")


def atomic_write(path: Path, content: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def run(news_dir: Path, check: bool, validate_only: bool) -> int:
    check_schema_document(news_dir)
    records = load_items(news_dir)
    packets = load_subject_packets(news_dir, records)
    if validate_only:
        print(f"validated {len(records)} canonical items")
        return 0
    outputs = build_outputs(records, packets)
    if check:
        mismatches = [
            name
            for name, expected in outputs.items()
            if not (news_dir / name).is_file()
            or (news_dir / name).read_bytes() != expected
        ]
        if mismatches:
            print(
                "generated files are stale: " + ", ".join(mismatches),
                file=sys.stderr,
            )
            print("run: python scripts/build_news.py", file=sys.stderr)
            return 1
        print(f"validated {len(records)} items; generated files are current")
        return 0

    for name, content in outputs.items():
        atomic_write(news_dir / name, content)
    print(f"validated {len(records)} items; rebuilt {len(outputs)} generated files")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail if a committed generated file differs from a deterministic rebuild",
    )
    mode.add_argument(
        "--validate-only",
        action="store_true",
        help="validate schema and canonical items without building indexes",
    )
    parser.add_argument(
        "--news-dir",
        type=Path,
        default=DEFAULT_NEWS_DIR,
        help="news directory (defaults to the repository news/ directory)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(args.news_dir.resolve(), args.check, args.validate_only)
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
