"""Bounded live lookup against the official UK CoFID 2021 workbook."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from io import BytesIO
import ipaddress
import math
import posixpath
import socket
import time
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import httpx

from food_catalog import normalize_food_name

COFID_URL = (
    "https://assets.publishing.service.gov.uk/media/60538b91e90e07527df82ae4/"
    "McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021..xlsx"
)
COFID_HOST = "assets.publishing.service.gov.uk"
COFID_EDITION = "2021"
TIMEOUT_SECONDS = 5.0
MAX_DOWNLOAD_BYTES = 6 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 128
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
CACHE_TTL_SECONDS = 24 * 60 * 60
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

# Each public query is explicitly tied to one exact workbook identity. This is
# intentionally not a fuzzy matcher: an unknown preparation or cut stays a miss.
_EXACT_IDENTITIES = {
    "grilled chicken breast": ("chicken breast grilled without skin meat only",),
    "chicken breast grilled": ("chicken breast grilled without skin meat only",),
    "grilled chicken breast without skin": ("chicken breast grilled without skin meat only",),
    "cooked rice": (
        "rice white easy cook boiled in unsalted water",
        "rice white long grain boiled in unsalted water",
    ),
}

_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
_inflight: asyncio.Task[dict[str, dict[str, Any]]] | None = None
_lock: asyncio.Lock | None = None


class CoFIDError(Exception):
    """Base error for a bounded CoFID lookup failure."""


class CoFIDFetchError(CoFIDError):
    """The fixed official workbook could not be fetched safely."""


class CoFIDFormatError(CoFIDError):
    """The downloaded workbook is malformed or exceeds parser limits."""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "MacroCoach/1.0 (CoFID public-source lookup)"},
    )


def _public_host_addresses() -> list[str]:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(
        COFID_HOST, 443, type=socket.SOCK_STREAM
    )})
    if not addresses:
        raise CoFIDFetchError("official source did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise CoFIDFetchError("official source resolved to a non-public address")
    return addresses


async def _fetch_workbook() -> bytes:
    """Fetch only the allowlisted official workbook with strict transport bounds."""
    try:
        allowed_addresses = set(await asyncio.to_thread(_public_host_addresses))
        async with _client() as client:
            async with client.stream("GET", COFID_URL) as response:
                if response.is_redirect or response.status_code != 200:
                    raise CoFIDFetchError(f"official source returned {response.status_code}")
                try:
                    declared = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    declared = 0
                if declared > MAX_DOWNLOAD_BYTES:
                    raise CoFIDFetchError("official workbook exceeds download limit")
                stream = response.extensions.get("network_stream")
                if stream is not None:
                    peer = stream.get_extra_info("server_addr")
                    if peer and str(peer[0]) not in allowed_addresses:
                        raise CoFIDFetchError("official source connection address changed")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise CoFIDFetchError("official workbook exceeds download limit")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not body.startswith(b"PK"):
                    raise CoFIDFormatError("official source was not an XLSX archive")
                return body
    except asyncio.CancelledError:
        raise
    except CoFIDError:
        raise
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        raise CoFIDFetchError("official source fetch failed") from exc


async def fetch_workbook() -> bytes:
    """Fetch the fixed workbook within one wall-clock deadline."""
    try:
        return await asyncio.wait_for(_fetch_workbook(), TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise CoFIDFetchError("official source fetch timed out") from exc


def _safe_archive(archive: ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise CoFIDFormatError("workbook has too many archive entries")
    total = 0
    for entry in entries:
        normalized = posixpath.normpath(entry.filename)
        if normalized.startswith("../") or normalized.startswith("/"):
            raise CoFIDFormatError("workbook contains an unsafe archive path")
        total += entry.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise CoFIDFormatError("workbook exceeds expanded-size limit")
        if entry.compress_size and entry.file_size / entry.compress_size > 250:
            raise CoFIDFormatError("workbook entry exceeds compression-ratio limit")
    expanded = 0
    for entry in entries:
        with archive.open(entry) as source:
            while chunk := source.read(64 * 1024):
                expanded += len(chunk)
                if expanded > MAX_UNCOMPRESSED_BYTES:
                    raise CoFIDFormatError("workbook exceeds expanded-size limit")


def _xml(archive: ZipFile, name: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(archive.read(name))
    except (KeyError, ElementTree.ParseError, ValueError) as exc:
        raise CoFIDFormatError(f"invalid workbook XML: {name}") from exc


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive, "xl/sharedStrings.xml")
    return ["".join(node.text or "" for node in item.iter(_NS + "t"))
            for item in root.findall(_NS + "si")]


def _sheet_path(archive: ZipFile) -> str:
    workbook = _xml(archive, "xl/workbook.xml")
    relationship_id = next((sheet.attrib.get(_OFFICE_REL) for sheet in workbook.iter(_NS + "sheet")
                            if sheet.attrib.get("name") == "1.3 Proximates"), None)
    if not relationship_id:
        raise CoFIDFormatError("Proximates sheet is missing")
    relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
    target = next((rel.attrib.get("Target") for rel in relationships.iter(_REL_NS + "Relationship")
                   if rel.attrib.get("Id") == relationship_id), None)
    if not target:
        raise CoFIDFormatError("Proximates sheet relationship is missing")
    path = posixpath.normpath(posixpath.join("xl", target))
    if not path.startswith("xl/worksheets/"):
        raise CoFIDFormatError("Proximates sheet target is unsafe")
    return path


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_NS + "t")).strip()
    value = cell.find(_NS + "v")
    raw = (value.text or "").strip() if value is not None else ""
    if cell.attrib.get("t") == "s" and raw:
        try:
            return shared[int(raw)].strip()
        except (IndexError, ValueError) as exc:
            raise CoFIDFormatError("invalid shared-string reference") from exc
    return raw


def _column(reference: str) -> str:
    return "".join(character for character in reference if character.isalpha())


def _nutrient(raw: str) -> tuple[float | None, str | None]:
    value = raw.strip()
    if not value or value.casefold() == "n":
        return None, "missing"
    if value.casefold() == "tr":
        return 0.0, "trace"
    try:
        number = float(value)
    except ValueError as exc:
        raise CoFIDFormatError("invalid nutrient value") from exc
    if not math.isfinite(number) or number < 0 or number > 1000:
        raise CoFIDFormatError("nutrient value is out of range")
    return number, None


def parse_workbook(payload: bytes) -> dict[str, dict[str, Any]]:
    """Parse exact food rows and original per-100g nutrient semantics from XLSX."""
    if not payload.startswith(b"PK"):
        raise CoFIDFormatError("payload is not an XLSX archive")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            _safe_archive(archive)
            shared = _shared_strings(archive)
            sheet = _xml(archive, _sheet_path(archive))
            rows: dict[str, dict[str, Any]] = {}
            for row in sheet.iter(_NS + "row"):
                values = {_column(cell.attrib.get("r", "")): _cell_value(cell, shared)
                          for cell in row.findall(_NS + "c")}
                code, name = values.get("A", "").strip(), values.get("B", "").strip()
                if not code or not name or code == "Food Code":
                    continue
                calories, calories_flag = _nutrient(values.get("M", ""))
                protein, protein_flag = _nutrient(values.get("J", ""))
                fat, fat_flag = _nutrient(values.get("K", ""))
                carbs, carbs_flag = _nutrient(values.get("L", ""))
                aoac, aoac_flag = _nutrient(values.get("Z", ""))
                nsp, nsp_flag = _nutrient(values.get("Y", ""))
                if None in (calories, protein, fat, carbs):
                    continue
                fiber = aoac if aoac is not None else nsp
                if fiber is None:
                    continue
                fiber_method = "AOAC" if aoac is not None else ("NSP" if nsp is not None else None)
                qualifiers = {key: flag for key, flag in {
                    "calories": calories_flag, "protein": protein_flag, "fat": fat_flag,
                    "carbs": carbs_flag, "fiber": aoac_flag if aoac is not None else nsp_flag,
                }.items() if flag}
                normalized = normalize_food_name(name)
                macros = {"calories": calories, "protein": protein,
                          "carbs": carbs, "fat": fat, "fiber": fiber}
                rows[normalized] = {
                    "name": name,
                    "macros_per_100g": dict(macros),
                    "macros_per_serving": dict(macros),
                    "serving_size": "100 g (assumed serving; source values per 100 g)",
                    "source": f"CoFID 2021: food {code}, per 100 g",
                    "basis": name,
                    "nutrient_qualifiers": qualifiers,
                    "attribution": {
                        "provider": "UK CoFID", "external_id": code,
                        "source_url": COFID_URL, "source_edition": COFID_EDITION,
                        "original_basis": "per_100g", "serving_basis": "per_100g",
                        "carbohydrate_definition": "available_carbohydrate_monosaccharide_equivalents",
                        "fiber_method": fiber_method, "verification_state": "official_source_exact_row",
                        "cache_allowed": True,
                    },
                }
            if not rows:
                raise CoFIDFormatError("workbook contains no usable Proximates rows")
            return rows
    except CoFIDFormatError:
        raise
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise CoFIDFormatError("malformed XLSX archive") from exc


async def _load() -> dict[str, dict[str, Any]]:
    async def operation() -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(parse_workbook, await fetch_workbook())
    try:
        return await asyncio.wait_for(operation(), TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise CoFIDFetchError("official source operation timed out") from exc


def clear_cache() -> None:
    """Clear process-local adapter state (used by isolated tests)."""
    global _cache, _inflight, _lock
    _cache = None
    _inflight = None
    _lock = None


async def _rows() -> dict[str, dict[str, Any]]:
    global _cache, _inflight, _lock
    now = time.monotonic()
    if _cache is not None and now - _cache[0] <= CACHE_TTL_SECONDS:
        return _cache[1]
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _cache is not None and time.monotonic() - _cache[0] <= CACHE_TTL_SECONDS:
            return _cache[1]
        if _inflight is None or _inflight.done():
            _inflight = asyncio.create_task(_load())
        task = _inflight
    try:
        rows = await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        async with _lock:
            if _inflight is task:
                _inflight = None
        raise
    async with _lock:
        if _inflight is task:
            _cache = (time.monotonic(), rows)
            _inflight = None
    return rows


async def lookup(query: str) -> dict[str, Any] | None:
    """Return one allowlisted exact CoFID identity, fetching the workbook if stale."""
    normalized = normalize_food_name(query)
    source_identities = _EXACT_IDENTITIES.get(normalized)
    if source_identities is None:
        return None
    try:
        rows = await _rows()
        row = next((rows.get(identity) for identity in source_identities
                    if rows.get(identity) is not None), None)
    except CoFIDError:
        return None
    return deepcopy(row) if row is not None else None
