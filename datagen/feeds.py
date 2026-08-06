"""Feed dialect writers: six distributors/DSPs, six CSV habits (§3.1).

Each dialect renders the *dirty* statement lines for one (feed, period) into the exact
bytes that land in ``/data/inbox`` — column names, delimiters, date and number formats
all differ per feed, so Phase 3's normalizer has real work to do. Rendering is pure
formatting: the canonical values live on the ``StatementLine``; a parser reading the CSV
back recovers them exactly.

Dialects:

- ``kinetic_us``     comma CSV, ISO period, plain decimals, USD
- ``meridian_eu``    semicolon CSV, ``MM.YYYY`` period, decimal comma, EUR
- ``pulsewave_uk``   comma CSV, ``Jul-2025`` period, GBP
- ``northstar_retail`` comma CSV, US date range columns, UPC-as-barcode, blank ISRC, USD/GBP
- ``vantage_jp``     tab-separated, ``YYYY/MM`` period, whole-yen amounts, no currency column
- ``syncbridge_lic`` comma CSV, per-placement fee lines, no units column (one placement each)
"""

from __future__ import annotations

from datagen.config import WorldConfig, period_end_date, period_start_date
from datagen.worldmodel import StatementLine

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def drop_filename(dialect: str, period: str) -> str:
    y, m = period[:4], period[5:7]
    return {
        "kinetic_us": f"kinetic_digital_{period}.csv",
        "meridian_eu": f"meridian_{y}{m}_abrechnung.csv",
        "pulsewave_uk": f"pulsewave_royalties_{_MONTHS[int(m) - 1]}{y}.csv",
        "northstar_retail": f"northstar_retail_{m}-{y}.csv",
        "vantage_jp": f"vantage_{y}_{m}.tsv",
        "syncbridge_lic": f"syncbridge_licensing_{period}.csv",
    }[dialect]


def _decimal_comma(amount: str) -> str:
    return amount.replace(".", ",")


def render_feed_csv(dialect: str, lines: list[StatementLine], config: WorldConfig) -> str:
    """Render one drop. ``lines`` must already be this (feed, period)'s dirty lines."""
    ordered = sorted(lines, key=lambda ln: ln.id)
    out: list[str] = []

    if dialect == "kinetic_us":
        out.append("report_period,isrc,upc,store,country,quantity,net_revenue,currency")
        for ln in ordered:
            out.append(
                f"{ln.period},{ln.isrc},{ln.upc or ''},{ln.store},{ln.territory},"
                f"{ln.units},{ln.gross_amount},{ln.currency}"
            )
    elif dialect == "meridian_eu":
        out.append("zeitraum;isrc;upc;shop;land;menge;betrag;waehrung")
        for ln in ordered:
            zeitraum = f"{ln.period[5:7]}.{ln.period[:4]}"
            out.append(
                f"{zeitraum};{ln.isrc};{ln.upc or ''};{ln.store};{ln.territory};"
                f"{ln.units};{_decimal_comma(str(ln.gross_amount))};{ln.currency}"
            )
    elif dialect == "pulsewave_uk":
        out.append(
            "Statement Month,Track ISRC,Release UPC,Store Name,Territory,Units,Amount,Currency"
        )
        for ln in ordered:
            month = f"{_MONTHS[int(ln.period[5:7]) - 1]}-{ln.period[:4]}"
            out.append(
                f"{month},{ln.isrc},{ln.upc or ''},{ln.store},{ln.territory},"
                f"{ln.units},{ln.gross_amount},{ln.currency}"
            )
    elif dialect == "northstar_retail":
        out.append(
            "period_start,period_end,barcode,isrc,retailer,region,units_sold,invoice_amount,curr"
        )
        for ln in ordered:
            start = period_start_date(ln.period)
            end = period_end_date(ln.period)
            out.append(
                f"{start.month:02d}/{start.day:02d}/{start.year},"
                f"{end.month:02d}/{end.day:02d}/{end.year},"
                f"{ln.upc or ''},{ln.isrc},{ln.store},{ln.territory},{ln.units},"
                f"{ln.gross_amount},{ln.currency}"
            )
    elif dialect == "vantage_jp":
        out.append("month\tisrc\tupc\tterritory\tplays\trevenue_jpy")
        for ln in ordered:
            month = f"{ln.period[:4]}/{ln.period[5:7]}"
            yen = str(int(ln.gross_amount))  # whole yen; datagen guarantees integrality
            out.append(f"{month}\t{ln.isrc}\t{ln.upc or ''}\t{ln.territory}\t{ln.units}\t{yen}")
    elif dialect == "syncbridge_lic":
        out.append("license_month,isrc,upc,placement,territory,fee_usd,currency")
        placements = config.pools.sync_placements
        for ln in ordered:
            placement = placements[ln.id % len(placements)]
            out.append(
                f"{ln.period},{ln.isrc},{ln.upc or ''},{placement},{ln.territory},"
                f"{ln.gross_amount},{ln.currency}"
            )
    else:
        raise ValueError(f"unknown dialect {dialect!r}")

    return "\n".join(out) + "\n"
