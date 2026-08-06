"""Feed dialect rendering: six habits, exact formats, lossless values."""

from decimal import Decimal

import pytest

from datagen.config import load_world_config
from datagen.feeds import drop_filename, render_feed_csv
from datagen.worldmodel import StatementLine

CONFIG = load_world_config()


def line(
    store: str,
    currency: str,
    gross: str,
    units: int = 1200,
    isrc: str = "QZFBR2400123",
    upc: str | None = "036847001234",
    period: str = "2025-07",
) -> StatementLine:
    return StatementLine(
        id=10_000_001,
        statement_id=101,
        period=period,
        isrc=isrc,
        upc=upc,
        store=store,
        territory="DE",
        units=units,
        gross_amount=Decimal(gross),
        currency=currency,
        line_hash="cafe",
    )


def test_filenames_per_dialect() -> None:
    assert drop_filename("kinetic_us", "2025-07") == "kinetic_digital_2025-07.csv"
    assert drop_filename("meridian_eu", "2025-07") == "meridian_202507_abrechnung.csv"
    assert drop_filename("pulsewave_uk", "2025-07") == "pulsewave_royalties_Jul2025.csv"
    assert drop_filename("northstar_retail", "2025-07") == "northstar_retail_07-2025.csv"
    assert drop_filename("vantage_jp", "2025-07") == "vantage_2025_07.tsv"
    assert drop_filename("syncbridge_lic", "2025-07") == "syncbridge_licensing_2025-07.csv"


def test_kinetic_us_plain_csv() -> None:
    out = render_feed_csv("kinetic_us", [line("Streamora", "USD", "3.721000")], CONFIG)
    header, row = out.strip().split("\n")
    assert header == "report_period,isrc,upc,store,country,quantity,net_revenue,currency"
    assert row == "2025-07,QZFBR2400123,036847001234,Streamora,DE,1200,3.721000,USD"


def test_meridian_eu_semicolons_and_decimal_comma() -> None:
    out = render_feed_csv("meridian_eu", [line("Wavelet", "EUR", "3.421500")], CONFIG)
    header, row = out.strip().split("\n")
    assert header == "zeitraum;isrc;upc;shop;land;menge;betrag;waehrung"
    assert row == "07.2025;QZFBR2400123;036847001234;Wavelet;DE;1200;3,421500;EUR"


def test_pulsewave_uk_month_names() -> None:
    out = render_feed_csv("pulsewave_uk", [line("Chorusly", "GBP", "2.100000")], CONFIG)
    rows = out.strip().split("\n")
    assert rows[1].startswith("Jul-2025,QZFBR2400123,")


def test_northstar_retail_blank_isrc_and_us_dates() -> None:
    physical = line("VinylPost", "USD", "451.200000", units=48, isrc="")
    out = render_feed_csv("northstar_retail", [physical], CONFIG)
    header, row = out.strip().split("\n")
    assert header.startswith("period_start,period_end,barcode,isrc,")
    assert row.startswith("07/01/2025,07/31/2025,036847001234,,VinylPost,DE,48,")


def test_vantage_jp_tabs_and_whole_yen() -> None:
    jpy = line("Vantage Music", "JPY", "58234.000000", units=19700)
    out = render_feed_csv("vantage_jp", [jpy], CONFIG)
    header, row = out.strip().split("\n")
    assert header == "month\tisrc\tupc\tterritory\tplays\trevenue_jpy"
    assert row == "2025/07\tQZFBR2400123\t036847001234\tDE\t19700\t58234"


def test_syncbridge_no_units_column_deterministic_placement() -> None:
    sync = line("SyncBridge", "USD", "7500.000000", units=1)
    out = render_feed_csv("syncbridge_lic", [sync], CONFIG)
    header, row = out.strip().split("\n")
    assert header == "license_month,isrc,upc,placement,territory,fee_usd,currency"
    expected_placement = CONFIG.pools.sync_placements[sync.id % len(CONFIG.pools.sync_placements)]
    assert f",{expected_placement}," in row
    assert row.endswith(",7500.000000,USD")


def test_unknown_dialect_raises() -> None:
    with pytest.raises(ValueError, match="cassette"):
        render_feed_csv("cassette", [], CONFIG)


def test_rows_are_ordered_by_line_id() -> None:
    first = line("Streamora", "USD", "1.000000")
    second = StatementLine(
        id=10_000_002,
        statement_id=101,
        period="2025-07",
        isrc="QZFBR2400124",
        upc=None,
        store="Streamora",
        territory="US",
        units=10,
        gross_amount=Decimal("0.031000"),
        currency="USD",
        line_hash="beef",
    )
    out = render_feed_csv("kinetic_us", [second, first], CONFIG)
    rows = out.strip().split("\n")[1:]
    assert rows[0].split(",")[1] == "QZFBR2400123"
    assert rows[1].split(",")[1] == "QZFBR2400124"
    # A None UPC renders as an empty field.
    assert rows[1].split(",")[2] == ""
