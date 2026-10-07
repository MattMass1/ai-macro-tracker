"""Fixtures and regressions for the bounded official CoFID workbook adapter."""
from __future__ import annotations

import asyncio
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

import cofid_lookup


def cofid_xlsx_fixture(*rows: tuple[str, ...]) -> bytes:
    """Build a minimal XLSX fixture with CoFID's real Proximates columns."""
    headers = ("Food Code", "Food Name", "Description", "Group", "Previous",
               "Main data references", "Footnote", "Water (g)", "Total nitrogen (g)",
               "Protein (g)", "Fat (g)", "Carbohydrate (g)", "Energy (kcal) (kcal)",
               "Energy (kJ) (kJ)", "Starch (g)", "Oligosaccharide (g)",
               "Total sugars (g)", "Glucose (g)", "Galactose (g)", "Fructose (g)",
               "Sucrose (g)", "Maltose (g)", "Lactose (g)", "Alcohol (g)",
               "NSP (g)", "AOAC fibre (g)")
    all_rows = (headers,) + rows

    def cell(column: int, row: int, value: str) -> str:
        letters = ""
        number = column
        while number:
            number, remainder = divmod(number - 1, 26)
            letters = chr(65 + remainder) + letters
        escaped = (str(value).replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        return f'<c r="{letters}{row}" t="inlineStr"><is><t>{escaped}</t></is></c>'

    sheet = "".join(
        f'<row r="{row_index}">' + "".join(
            cell(column_index, row_index, value)
            for column_index, value in enumerate(values, 1)
            if value != ""
        ) + "</row>"
        for row_index, values in enumerate(all_rows, 1)
    )
    content_types = """<?xml version="1.0"?>
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
      <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
      <Default Extension="xml" ContentType="application/xml"/>
      <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
      <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
    </Types>"""
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="1.3 Proximates" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
    </Relationships>"""
    sheet_xml = ("<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">"
                 f"<sheetData>{sheet}</sheetData></worksheet>")
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


CHICKEN = ("18-323", "Chicken, breast, grilled without skin, meat only", "fixture row",
           "M", "", "fixture reference", "", "", "", "32.0", "2.2", "0.0",
           "148", "", "", "", "", "", "", "", "", "", "", "", "", "0.0")
TRACE_NO_FIBER = ("99-001", "Example root, cooked", "fixture row", "V", "",
                  "fixture reference", "", "", "", "1.2", "0.1", "Tr", "20",
                  "", "", "", "", "", "", "", "", "", "", "", "N", "")
RICE = ("11-999", "Rice, white, easy cook, boiled in unsalted water", "fixture row", "C", "",
        "fixture reference", "", "", "", "2.6", "0.4", "28.0", "130",
        "", "", "", "", "", "", "", "", "", "", "", "0.4", "")


def test_parser_exact_row_preserves_units_and_rejects_missing_fiber():
    parsed = cofid_lookup.parse_workbook(cofid_xlsx_fixture(CHICKEN, TRACE_NO_FIBER))
    chicken = parsed["chicken breast grilled without skin meat only"]
    assert chicken["macros_per_100g"] == {
        "calories": 148.0, "protein": 32.0, "carbs": 0.0, "fat": 2.2, "fiber": 0.0,
    }
    assert chicken["attribution"]["external_id"] == "18-323"
    assert chicken["attribution"]["original_basis"] == "per_100g"
    assert chicken["attribution"]["carbohydrate_definition"] == "available_carbohydrate_monosaccharide_equivalents"
    assert chicken["macros_per_serving"] == chicken["macros_per_100g"]
    assert chicken["serving_size"].startswith("100 g")
    assert "example root cooked" not in parsed


@pytest.mark.parametrize("raw", ["NaN", "Inf", "-Inf"])
def test_parser_rejects_non_finite_nutrients(raw):
    bad = list(CHICKEN)
    bad[9] = raw
    with pytest.raises(cofid_lookup.CoFIDFormatError):
        cofid_lookup.parse_workbook(cofid_xlsx_fixture(tuple(bad)))


@pytest.mark.parametrize("payload", [b"<html>blocked</html>", b"not-a-zip", b"PK\x03\x04bad"])
def test_parser_rejects_html_and_malformed_archives(payload):
    with pytest.raises(cofid_lookup.CoFIDFormatError):
        cofid_lookup.parse_workbook(payload)


async def test_fetch_is_fixed_url_no_auth_and_rejects_redirect_404_429(monkeypatch):
    seen = []
    statuses = iter((302, 404, 429))

    def handler(request):
        seen.append(request)
        return httpx.Response(next(statuses), headers={"Location": "https://example.com/evil"})

    monkeypatch.setattr(cofid_lookup, "_public_host_addresses", lambda: ["151.101.0.204"])
    monkeypatch.setattr(cofid_lookup, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False,
        timeout=cofid_lookup.TIMEOUT_SECONDS,
    ))
    for _ in range(3):
        with pytest.raises(cofid_lookup.CoFIDFetchError):
            await cofid_lookup.fetch_workbook()
    assert all(request.url == httpx.URL(cofid_lookup.COFID_URL) for request in seen)
    assert all("authorization" not in request.headers for request in seen)


async def test_fetch_timeout_and_oversize_are_bounded(monkeypatch):
    monkeypatch.setattr(cofid_lookup, "_public_host_addresses", lambda: ["151.101.0.204"])
    monkeypatch.setattr(cofid_lookup, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))),
        follow_redirects=False, timeout=cofid_lookup.TIMEOUT_SECONDS,
    ))
    with pytest.raises(cofid_lookup.CoFIDFetchError):
        await cofid_lookup.fetch_workbook()

    monkeypatch.setattr(cofid_lookup, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, headers={"Content-Length": str(cofid_lookup.MAX_DOWNLOAD_BYTES + 1)}, content=b"x"
        )), follow_redirects=False, timeout=cofid_lookup.TIMEOUT_SECONDS,
    ))
    with pytest.raises(cofid_lookup.CoFIDFetchError):
        await cofid_lookup.fetch_workbook()


async def test_lookup_requires_allowlisted_exact_identity_and_caches_single_flight(monkeypatch):
    cofid_lookup.clear_cache()
    calls = 0
    release = asyncio.Event()
    fixture = cofid_xlsx_fixture(CHICKEN)

    async def fetch():
        nonlocal calls
        calls += 1
        await release.wait()
        return fixture

    monkeypatch.setattr(cofid_lookup, "fetch_workbook", fetch)
    tasks = [asyncio.create_task(cofid_lookup.lookup("grilled chicken breast")) for _ in range(2)]
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(*tasks)
    assert calls == 1
    assert first == second
    assert first["name"] == CHICKEN[1]
    assert await cofid_lookup.lookup("chicken") is None
    assert calls == 1


async def test_cooked_rice_uses_only_an_exact_supported_workbook_row(monkeypatch):
    cofid_lookup.clear_cache()
    monkeypatch.setattr(
        cofid_lookup, "fetch_workbook",
        lambda: asyncio.sleep(0, result=cofid_xlsx_fixture(CHICKEN, RICE)),
    )
    found = await cofid_lookup.lookup("cooked rice")
    assert found is not None
    assert found["name"] == RICE[1]
    assert found["attribution"]["verification_state"] == "official_source_exact_row"
    assert await cofid_lookup.lookup("rice") is None


async def test_cancelled_waiter_does_not_cancel_or_duplicate_shared_fetch(monkeypatch):
    cofid_lookup.clear_cache()
    calls = 0
    release = asyncio.Event()

    async def fetch():
        nonlocal calls
        calls += 1
        await release.wait()
        return cofid_xlsx_fixture(CHICKEN)

    monkeypatch.setattr(cofid_lookup, "fetch_workbook", fetch)
    cancelled = asyncio.create_task(cofid_lookup.lookup("grilled chicken breast"))
    survivor = asyncio.create_task(cofid_lookup.lookup("grilled chicken breast"))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    assert (await survivor)["attribution"]["external_id"] == "18-323"
    assert calls == 1


async def test_cache_expires_at_ttl(monkeypatch):
    cofid_lookup.clear_cache()
    calls = 0
    clock = 100.0

    async def fetch():
        nonlocal calls
        calls += 1
        return cofid_xlsx_fixture(CHICKEN)

    monkeypatch.setattr(cofid_lookup, "fetch_workbook", fetch)
    monkeypatch.setattr(cofid_lookup.time, "monotonic", lambda: clock)
    assert await cofid_lookup.lookup("grilled chicken breast") is not None
    clock += cofid_lookup.CACHE_TTL_SECONDS - 1
    assert await cofid_lookup.lookup("grilled chicken breast") is not None
    clock += 2
    assert await cofid_lookup.lookup("grilled chicken breast") is not None
    assert calls == 2
