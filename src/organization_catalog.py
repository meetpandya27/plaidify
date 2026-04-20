"""Generated organization directory for institution discovery."""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.core.blueprint import load_blueprint
from src.logging_config import get_logger

settings = get_settings()
logger = get_logger("organization_catalog")

_US_REGIONS = (
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
    ("DC", "District of Columbia"),
)

_CANADA_REGIONS = (
    ("AB", "Alberta"),
    ("BC", "British Columbia"),
    ("MB", "Manitoba"),
    ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"),
    ("NT", "Northwest Territories"),
    ("NU", "Nunavut"),
    ("ON", "Ontario"),
    ("PE", "Prince Edward Island"),
    ("QC", "Quebec"),
    ("SK", "Saskatchewan"),
    ("YT", "Yukon"),
)

_CATEGORY_SPECS = (
    {
        "key": "finance",
        "label": "Finance",
        "brands": (
            "North Harbor Bank",
            "Summit Credit Union",
            "Civic First Financial",
            "Maple Trust",
            "Riverstone Banking Group",
            "Prairie Savings",
            "Anchor Point Bank",
            "Lakeside Financial",
            "Northern Ledger Bank",
            "Cedar Peak Credit Union",
            "Frontier Capital Bank",
            "Community Horizon Bank",
            "Granite State Trust",
            "Everline Credit Union",
            "Redwood Reserve Bank",
            "Aurora Financial Co.",
        ),
        "variants": (
            "Digital Banking",
            "Member Center",
            "Regional Branch",
            "Commercial Banking",
        ),
        "search_terms": ("bank", "credit union", "finance", "lending"),
    },
    {
        "key": "utility",
        "label": "Utilities",
        "brands": (
            "Everstream Electric",
            "Northline Utilities",
            "Cedar Ridge Power",
            "Riverbend Energy",
            "Summit Grid Services",
            "Lakeshore Utility Co.",
            "Prairie Light & Power",
            "Maple Current Energy",
            "Frontier Water & Power",
            "HarborView Utilities",
        ),
        "variants": (
            "Customer Portal",
            "Residential Service",
            "Energy Services",
            "Community Utility",
        ),
        "search_terms": ("utility", "electric", "power", "hydro", "water"),
    },
    {
        "key": "insurance",
        "label": "Insurance",
        "brands": (
            "Harbor Shield Insurance",
            "Summit Mutual",
            "Maple Guard Assurance",
            "Civic Benefit Group",
            "Riverstone Coverage",
            "North Peak Assurance",
            "Anchor Policy Network",
            "Prairie Risk Partners",
        ),
        "variants": (
            "Policy Portal",
            "Claims Center",
            "Member Services",
            "Coverage Hub",
        ),
        "search_terms": ("insurance", "policy", "claims", "benefits"),
    },
    {
        "key": "telecom",
        "label": "Telecom",
        "brands": (
            "Northern Signal",
            "Prairie Connect",
            "Harbor Wireless",
            "Maple Fiber",
            "Summit Mobile",
            "Riverline Communications",
        ),
        "variants": (
            "Wireless Account",
            "Home Internet",
            "Business Service",
            "Subscriber Portal",
        ),
        "search_terms": ("telecom", "wireless", "internet", "mobile"),
    },
    {
        "key": "healthcare",
        "label": "Healthcare",
        "brands": (
            "Civic Health Network",
            "Maple Care Alliance",
            "Northern Family Health",
            "Summit Medical Group",
            "River Valley Health",
        ),
        "variants": (
            "Patient Portal",
            "Benefits Center",
            "Member Access",
            "Care Account",
        ),
        "search_terms": ("health", "patient", "medical", "benefits"),
    },
    {
        "key": "government",
        "label": "Government",
        "brands": (
            "Civic Services Office",
            "Regional Benefits Administration",
            "Citizen Access Bureau",
            "State Revenue Center",
        ),
        "variants": (
            "Citizen Services",
            "Benefits Portal",
            "Tax Center",
            "Resident Access",
        ),
        "search_terms": ("government", "benefits", "tax", "citizen"),
    },
)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@lru_cache(maxsize=1)
def _load_connector_templates() -> dict[str, dict[str, Any]]:
    connectors_path = Path(settings.connectors_dir).resolve()
    templates: dict[str, dict[str, Any]] = {}

    if not connectors_path.is_dir():
        return templates

    for path in sorted(connectors_path.glob("*.json")):
        try:
            blueprint = load_blueprint(path)
        except Exception as exc:
            logger.warning("Failed to load connector template %s: %s", path.name, exc)
            continue

        tags = blueprint.tags or []
        if "internal" in tags or "fixture" in tags:
            continue

        templates[path.stem] = {
            "site": path.stem,
            "name": blueprint.name,
            "domain": blueprint.domain,
            "has_mfa": blueprint.mfa is not None,
            "tags": tags,
        }

    return templates


def _resolve_template_site(category: str, country_code: str, templates: dict[str, dict[str, Any]]) -> str:
    if not templates:
        return ""

    preferred_sites: tuple[str, ...]
    if category == "utility" and country_code == "CA":
        preferred_sites = ("hydro_one",)
    elif category == "finance":
        preferred_sites = tuple(templates)
    else:
        preferred_sites = ("hydro_one",)

    for candidate in preferred_sites:
        if candidate in templates:
            return candidate

    return next(iter(templates), "")


def _serialize(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "search_text"}


@lru_cache(maxsize=1)
def get_organization_catalog() -> tuple[dict[str, Any], ...]:
    templates = _load_connector_templates()
    if not templates:
        return tuple()

    countries = (
        ("US", "United States", _US_REGIONS),
        ("CA", "Canada", _CANADA_REGIONS),
    )
    catalog: list[dict[str, Any]] = []

    for country_code, country_name, regions in countries:
        for spec in _CATEGORY_SPECS:
            template_site = _resolve_template_site(spec["key"], country_code, templates)
            template = templates.get(template_site, {})

            for region_code, region_name in regions:
                for brand in spec["brands"]:
                    for variant in spec["variants"]:
                        organization_name = f"{brand} - {region_name} {variant}"
                        organization_id = "-".join(
                            (
                                spec["key"],
                                country_code.lower(),
                                region_code.lower(),
                                _slugify(brand),
                                _slugify(variant),
                            )
                        )
                        search_text = " ".join(
                            (
                                organization_name,
                                brand,
                                spec["label"],
                                spec["key"],
                                country_name,
                                country_code,
                                region_name,
                                region_code,
                                *spec["search_terms"],
                            )
                        ).lower()
                        catalog.append(
                            {
                                "organization_id": organization_id,
                                "name": organization_name,
                                "brand": brand,
                                "category": spec["key"],
                                "category_label": spec["label"],
                                "country": country_name,
                                "country_code": country_code,
                                "region": region_name,
                                "region_code": region_code,
                                "service_area": f"{region_name}, {country_name}",
                                "site": template_site,
                                "template_name": template.get("name", template_site),
                                "template_domain": template.get("domain"),
                                "has_mfa": bool(template.get("has_mfa")),
                                "supported": True,
                                "read_only": True,
                                "search_text": search_text,
                            }
                        )

    return tuple(catalog)


def get_organization_summary() -> dict[str, Any]:
    catalog = get_organization_catalog()
    category_counts = Counter(entry["category"] for entry in catalog)
    country_counts = Counter(entry["country_code"] for entry in catalog)
    template_counts = Counter(entry["site"] for entry in catalog)

    return {
        "total_count": len(catalog),
        "categories": [
            {
                "key": spec["key"],
                "label": spec["label"],
                "count": category_counts.get(spec["key"], 0),
            }
            for spec in _CATEGORY_SPECS
        ],
        "countries": [
            {
                "code": country_code,
                "label": country_name,
                "count": country_counts.get(country_code, 0),
            }
            for country_code, country_name, _ in (
                ("US", "United States", _US_REGIONS),
                ("CA", "Canada", _CANADA_REGIONS),
            )
        ],
        "connector_templates": [
            {
                "site": site,
                "count": count,
            }
            for site, count in sorted(template_counts.items())
        ],
    }


def _normalize_country_filter(value: str | None) -> str | None:
    if not value:
        return None

    normalized = value.strip().lower()
    if normalized in {"us", "usa", "united states", "united states of america"}:
        return "US"
    if normalized in {"ca", "can", "canada"}:
        return "CA"
    return value.strip().upper()


def _score_entry(entry: dict[str, Any], query: str, tokens: list[str]) -> int | None:
    haystack = entry["search_text"]
    if any(token not in haystack for token in tokens):
        return None

    score = 0
    name = entry["name"].lower()
    brand = entry["brand"].lower()
    region = entry["region"].lower()
    category = entry["category_label"].lower()
    country = entry["country"].lower()

    if query in name:
        score += 120
    if query in brand:
        score += 80
    if query in region:
        score += 30
    if query in category:
        score += 24
    if query in country:
        score += 24
    score += 4 if entry["has_mfa"] else 0
    return score


def search_organizations(
    *,
    q: str | None = None,
    category: str | None = None,
    country: str | None = None,
    site: str | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict[str, Any]:
    catalog = list(get_organization_catalog())

    normalized_category = category.strip().lower() if category else None
    normalized_country = _normalize_country_filter(country)
    normalized_site = site.strip().lower() if site else None
    query = (q or "").strip().lower()
    tokens = [token for token in re.split(r"\s+", query) if token]

    if normalized_category:
        catalog = [
            entry
            for entry in catalog
            if entry["category"] == normalized_category
            or entry["category_label"].lower() == normalized_category
        ]
    if normalized_country:
        catalog = [entry for entry in catalog if entry["country_code"] == normalized_country]
    if normalized_site:
        catalog = [entry for entry in catalog if entry["site"].lower() == normalized_site]

    if query:
        scored_entries: list[tuple[int, dict[str, Any]]] = []
        for entry in catalog:
            score = _score_entry(entry, query, tokens)
            if score is not None:
                scored_entries.append((score, entry))
        scored_entries.sort(key=lambda item: (-item[0], item[1]["name"]))
        filtered = [entry for _, entry in scored_entries]
    else:
        filtered = sorted(
            catalog,
            key=lambda entry: (
                entry["country_code"] != "US",
                entry["category"],
                entry["region"],
                entry["name"],
            ),
        )

    total_count = len(filtered)
    paged_results = filtered[offset : offset + limit]
    summary = get_organization_summary()

    return {
        "query": q or "",
        "count": total_count,
        "returned": len(paged_results),
        "offset": offset,
        "limit": limit,
        "results": [_serialize(entry) for entry in paged_results],
        "summary": summary,
    }


def get_organization_by_id(organization_id: str) -> dict[str, Any] | None:
    for entry in get_organization_catalog():
        if entry["organization_id"] == organization_id:
            return _serialize(entry)
    return None
