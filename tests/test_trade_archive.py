"""Offline trade evidence validation and its effect on simulated funding."""

import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from baqs.backtest import (
    BacktestEngine,
    BacktestResult,
    BacktestUnavailable,
    CostModel,
    EntrySignal,
    FundingCoverage,
    FundingEvent,
)
from baqs.backtest.engine import Side
from baqs.market_data.schema import FinalCandle
from baqs.market_data.trade_archive import TradeArchive, TradeArchiveError

MINUTE = 960_000
SYMBOL = "ETHUSDT"
ARCHIVE_NAME = "ETHUSDT-trades-1970-01-01.zip"
HEADER = "id,price,qty,quote_qty,time,is_buyer_maker\n"


def fixture(
    tmp_path: Path,
    points: list[tuple[int, int]],
    *,
    digest: str | None = None,
    header: str = HEADER,
) -> tuple[TradeArchive, FinalCandle, str]:
    archive = tmp_path / ARCHIVE_NAME
    csv = header + "".join(
        f"{index * 10},{price},1,{price},{MINUTE + ms},false\n"
        for index, (ms, price) in enumerate(points, 1)
    )
    with ZipFile(archive, "w") as output:
        output.writestr(archive.with_suffix(".csv").name, csv)
    archive_sha = sha256(archive.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"archives": [{"path": ARCHIVE_NAME, "sha256": digest or archive_sha}]})
    )
    prices = [price for _, price in points]
    bar = FinalCandle(
        SYMBOL,
        "1m",
        MINUTE,
        MINUTE + 60_000,
        Decimal(prices[0]),
        Decimal(max(prices)),
        Decimal(min(prices)),
        Decimal(prices[-1]),
        Decimal(len(points)),
        Decimal(sum(prices)),
        len(points),
        "REST",
    )
    return TradeArchive.from_manifest(manifest), bar, archive_sha


def replay(
    archive: TradeArchive | None, bar: FinalCandle, offsets: tuple[int, ...], side: Side
) -> BacktestResult:
    earlier = FinalCandle(
        SYMBOL,
        "1m",
        MINUTE - 60_000,
        MINUTE,
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal(100),
        Decimal(1),
        Decimal(100),
        1,
        "REST",
    )
    times = tuple(MINUTE + offset for offset in offsets)
    signal = EntrySignal(
        SYMBOL, earlier.open_ms, side, Decimal(90 if side == "LONG" else 110), "S01"
    )
    events = [FundingEvent(SYMBOL, time, Decimal("0.001"), Decimal(100)) for time in times]
    return BacktestEngine().run(
        [earlier, bar],
        [signal],
        events,
        FundingCoverage(earlier.open_ms, bar.close_ms, times, True),
        CostModel(Decimal(0), Decimal(0)),
        trade_archive=archive,
    )


@pytest.mark.parametrize(
    ("side", "price", "reason"),
    [
        ("LONG", 80, "STOP_LOSS"),
        ("SHORT", 120, "STOP_LOSS"),
        ("LONG", 130, "TAKE_PROFIT"),
        ("SHORT", 70, "TAKE_PROFIT"),
    ],
)
def test_verified_orders_apply_only_funding_strictly_before_exit(
    tmp_path: Path, side: Side, price: int, reason: str
) -> None:
    archive, bar, digest = fixture(tmp_path, [(1, 100), (5, price), (15, 100)])
    result = replay(archive, bar, (3, 9), side)
    trade = result.trades[0]
    assert trade.exit_reason == reason
    sign = 1 if side == "LONG" else -1
    assert trade.funding_cost == sign * trade.quantity * Decimal("0.1")
    proof = result.funding_order_evidence[0]
    assert proof.archive_name == ARCHIVE_NAME
    assert proof.archive_sha256 == digest
    assert proof.trade_count == 3
    assert proof.first_trade_ms == MINUTE + 1
    assert proof.funding_before_exit_ms == (MINUTE + 3,)
    assert proof.funding_after_exit_ms == (MINUTE + 9,)
    assert replay(archive, bar, (3, 9), side) == result


@pytest.mark.parametrize("offset", [5, 7, 9])
def test_regime_of_possible_exit_order_bounds_cannot_be_guessed(
    tmp_path: Path, offset: int
) -> None:
    source, bar, _ = fixture(tmp_path, [(1, 100), (5, 130), (9, 80), (15, 100)])
    with pytest.raises(
        BacktestUnavailable, match=f"AMBIGUOUS_INTRAMINUTE_FUNDING: {MINUTE + offset}"
    ):
        replay(source, bar, (offset,), "LONG")


def test_both_brackets_keep_adverse_stop_first_when_funding_order_is_known(tmp_path: Path) -> None:
    source, bar, _ = fixture(tmp_path, [(1, 100), (5, 130), (9, 80), (15, 100)])
    result = replay(source, bar, (3, 11), "LONG")
    assert result.trades[0].exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert result.trades[0].exit_reference_price == Decimal(90)
    assert result.funding_order_evidence[0].first_target_crossing_ms == MINUTE + 5
    assert result.funding_order_evidence[0].first_stop_crossing_ms == MINUTE + 9
    assert result.funding_order_evidence[0].funding_before_exit_ms == (MINUTE + 3,)


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [("checksum", "SHA-256 mismatch"), ("volume", "do not reconcile"), ("header", "CSV header")],
)
def test_invalid_evidence_cannot_change_a_trade(
    tmp_path: Path, corruption: str, reason: str
) -> None:
    archive, bar, _ = fixture(
        tmp_path,
        [(1, 100), (5, 80), (15, 100)],
        digest="0" * 64 if corruption == "checksum" else None,
        header="unexpected\n" if corruption == "header" else HEADER,
    )
    if corruption == "volume":
        bar = replace(bar, quote_volume=bar.quote_volume + 1)
    with pytest.raises(BacktestUnavailable, match=f"INVALID_INTRAMINUTE_TRADE_EVIDENCE.*{reason}"):
        replay(archive, bar, (3,), "LONG")


def test_no_matching_day_keeps_existing_guard(tmp_path: Path) -> None:
    _, bar, _ = fixture(tmp_path, [(1, 100), (5, 80), (15, 100)])
    manifest = tmp_path / "empty.json"
    manifest.write_text('{"archives":[]}')
    with pytest.raises(BacktestUnavailable, match=f"AMBIGUOUS_INTRAMINUTE_FUNDING: {MINUTE + 3}"):
        replay(TradeArchive.from_manifest(manifest), bar, (3,), "LONG")


def test_out_of_order_trade_inputs_fail_closed(tmp_path: Path) -> None:
    source, bar, _ = fixture(tmp_path, [(5, 100), (3, 80), (15, 100)])
    with pytest.raises(TradeArchiveError, match="out of order"):
        source.minute(bar)


def test_cached_minute_is_bound_to_the_candle(tmp_path: Path) -> None:
    source, bar, _ = fixture(tmp_path, [(1, 100), (5, 80), (15, 100)])
    assert source.minute(bar) is not None
    with pytest.raises(TradeArchiveError, match="do not reconcile"):
        source.minute(replace(bar, volume=bar.volume + 1))
