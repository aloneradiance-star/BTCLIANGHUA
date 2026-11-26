"""Local Binance USD-M daily trade archives for verified 1m execution ordering."""

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException, localcontext
from hashlib import file_digest
from os import fstat
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from zlib import error as DecompressionError

from baqs.market_data.schema import FinalCandle

_DAILY_ARCHIVE = re.compile(r"(BTCUSDT|ETHUSDT)-trades-(\d{4}-\d{2}-\d{2})\.zip")
_CHECKSUM = re.compile(r"[0-9a-fA-F]{64}")
_HEADER = ["id", "price", "qty", "quote_qty", "time", "is_buyer_maker"]


class TradeArchiveError(ValueError):
    """A local archive cannot establish the minute's trade order."""


@dataclass(frozen=True, slots=True)
class _Archive:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedTradeMinute:
    archive_name: str
    archive_sha256: str
    trades: tuple[tuple[int, Decimal], ...]
    candle_fingerprint: tuple[object, ...]


class TradeArchive:
    """Load pinned local ZIPs on demand, retaining verified minutes across replay folds."""

    def __init__(self, archives: dict[tuple[str, date], _Archive]) -> None:
        self._archives = archives
        self._minutes: dict[tuple[str, int], VerifiedTradeMinute] = {}

    @classmethod
    def from_manifest(cls, manifest: Path) -> "TradeArchive":
        try:
            value: object = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TradeArchiveError("Cannot read trade archive manifest") from exc
        if not isinstance(value, dict) or not isinstance(items := value.get("archives"), list):
            raise TradeArchiveError("Trade archive manifest needs an archives list")
        archives: dict[tuple[str, date], _Archive] = {}
        for entry in items:
            if (
                not isinstance(entry, dict)
                or not isinstance(path_value := entry.get("path"), str)
                or not isinstance(digest := entry.get("sha256"), str)
                or _CHECKSUM.fullmatch(digest) is None
            ):
                raise TradeArchiveError("Archive entry needs a path and a SHA-256 digest")
            path = manifest.parent / path_value
            match = _DAILY_ARCHIVE.fullmatch(path.name)
            if match is None:
                raise TradeArchiveError("Expected a BTCUSDT or ETHUSDT daily trades ZIP filename")
            symbol, utc_day = match.groups()
            try:
                key = (symbol, date.fromisoformat(utc_day))
            except ValueError as exc:
                raise TradeArchiveError("Invalid archive UTC date") from exc
            if key in archives:
                raise TradeArchiveError(f"Duplicate trade archive for {symbol} on {utc_day}")
            archives[key] = _Archive(path, digest.lower())
        return cls(archives)

    def minute(self, bar: FinalCandle) -> VerifiedTradeMinute | None:
        day = datetime.fromtimestamp(bar.open_ms // 1000, tz=UTC).date()
        archive = self._archives.get((bar.symbol, day))
        if archive is None:
            return None
        key = (bar.symbol, bar.open_ms)
        minute = self._minutes.get(key)
        if minute is None:
            minute = self._read_minute(archive, bar)
            self._minutes[key] = minute
        if minute.candle_fingerprint != bar.fingerprint():
            raise TradeArchiveError("Trades do not reconcile to execution candle")
        return minute

    @staticmethod
    def _read_minute(archive: _Archive, bar: FinalCandle) -> VerifiedTradeMinute:
        member = archive.path.with_suffix(".csv").name
        try:
            with archive.path.open("rb") as stream:
                initial = fstat(stream.fileno())
                if file_digest(stream, "sha256").hexdigest() != archive.sha256:
                    raise TradeArchiveError("Trade archive SHA-256 mismatch")
                stream.seek(0)
                with ZipFile(stream) as zipped:
                    if zipped.namelist() != [member]:
                        raise TradeArchiveError("Trade archive must contain its single dated CSV")
                    with zipped.open(member) as csv_stream:
                        minute = _parse_minute(
                            io.TextIOWrapper(csv_stream, encoding="utf-8-sig"), bar
                        )
                final = fstat(stream.fileno())
                if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                    raise TradeArchiveError("Trade archive changed during verification")
        except TradeArchiveError:
            raise
        except (
            OSError,
            BadZipFile,
            DecompressionError,
            RuntimeError,
            ValueError,
            DecimalException,
            csv.Error,
        ) as exc:
            raise TradeArchiveError("Invalid or unreadable trade archive") from exc
        return VerifiedTradeMinute(archive.path.name, archive.sha256, minute, bar.fingerprint())


def _parse_minute(csv_stream: io.TextIOBase, bar: FinalCandle) -> tuple[tuple[int, Decimal], ...]:
    reader = csv.reader(csv_stream, strict=True)
    if next(reader, None) != _HEADER:
        raise TradeArchiveError("Unsupported USD-M trades CSV header")
    day_ms = bar.open_ms // 86_400_000 * 86_400_000
    points: list[tuple[int, Decimal]] = []
    base_volume, quote_volume = Decimal(0), Decimal(0)
    previous_time, previous_id = -1, -1
    with localcontext() as context:
        context.prec = 50
        for row in reader:
            trade_id, price_text, qty_text, quote_text, time_text, _ = row
            timestamp_ms = int(time_text)
            if not day_ms <= timestamp_ms < day_ms + 86_400_000:
                raise TradeArchiveError(
                    "Archive must contain timestamps in milliseconds on its UTC day"
                )
            if not bar.open_ms <= timestamp_ms < bar.close_ms:
                continue
            identifier = int(trade_id)
            if timestamp_ms < previous_time or identifier <= previous_id:
                raise TradeArchiveError("Trade timestamps or IDs are out of order")
            price, qty, quote_qty = Decimal(price_text), Decimal(qty_text), Decimal(quote_text)
            if (
                not all(value.is_finite() for value in (price, qty, quote_qty))
                or price <= 0
                or qty < 0
                or quote_qty < 0
            ):
                raise TradeArchiveError("Invalid trade price or volume")
            points.append((timestamp_ms, price))
            if len(points) > bar.trade_count:
                raise TradeArchiveError("Trade count exceeds execution candle")
            base_volume += qty
            quote_volume += quote_qty
            previous_time, previous_id = timestamp_ms, identifier

    if not points:
        raise TradeArchiveError("No trades for execution minute")
    prices = [price for _, price in points]
    observed = (
        prices[0],
        max(prices),
        min(prices),
        prices[-1],
        base_volume,
        quote_volume,
        len(points),
    )
    if observed != bar.fingerprint():
        raise TradeArchiveError("Trades do not reconcile to execution candle")
    return tuple(points)
