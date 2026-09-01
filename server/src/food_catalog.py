"""Deterministic food identity and provenance helpers."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping

BLANK_LEGACY_NORMALIZED_NAME = "legacy unnamed food"
BLANK_LEGACY_DISPLAY_NAME = "Unnamed legacy food (unverified)"
BLANK_LEGACY_PRODUCT_IDENTITY = "legacy:blank-name"


def normalize_food_name(value: str) -> str:
    """Normalize a human food name without guessing brand or product identity."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def legacy_food_identity(value: str) -> tuple[str, str, str]:
    """Return a non-empty, collision-safe identity for a legacy food name."""
    normalized = normalize_food_name(value)
    if normalized:
        return normalized, str(value), ""
    return (
        BLANK_LEGACY_NORMALIZED_NAME,
        BLANK_LEGACY_DISPLAY_NAME,
        BLANK_LEGACY_PRODUCT_IDENTITY,
    )


def evidence_hash(metadata: Mapping[str, Any]) -> str:
    """Hash canonical structured evidence; raw provider payloads are never required."""
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def provenance_state(macro_source: str) -> tuple[str, bool]:
    """Classify a log snapshot conservatively without promoting community data."""
    source = str(macro_source or "").strip().casefold()
    if source.startswith("estimate") or "composite: estimate" in source:
        return "estimate", False
    if source.startswith("curated restaurant:"):
        return "official_curated", True
    if source.startswith("known food:"):
        return "internal_curated", True
    if source.startswith("openfoodfacts barcode:"):
        return "exact_identifier", True
    return "community_observed", False
