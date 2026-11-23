"""Bitemporal, immutable raw storage (WP 1.3, architecture §7, ADR-0002).

Layout: raw/<source>/year=YYYY/<partition_date>__v<NNN>.parquet

- One file per (source, partition date, version). Files are never rewritten or deleted.
- A re-issued exchange file is a new version with a higher ingest_seq. It **replaces the
  whole day** for that source: rows missing from the correction disappear from as-of views
  once the correction is known.
- known_at follows the §8 convention for the first version (18:00 IST on the trade date)
  and the real arrival time for corrections. ingested_at always records the real arrival
  time, so audits and replays can filter on either.
- The same file (same SHA-256) dropped twice never creates a second version.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

from pi.store.clock import known_at_convention, require_aware

META_COLUMNS = ("ingest_seq", "known_at", "ingested_at", "known_at_synthetic", "source_file_sha")
_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})__v(\d{3})\.parquet$")


@dataclass(frozen=True)
class Source:
    name: str
    partition_col: str
    columns: dict[str, pl.DataType]


def _src(name: str, columns: dict[str, pl.DataType]) -> Source:
    return Source(name=name, partition_col="trade_date", columns=columns)


# Canonical raw schemas. Parsers (WP 1.2) map each exchange format onto these.
SOURCES: dict[str, Source] = {
    "bhavcopy": _src(
        "bhavcopy",
        {
            "trade_date": pl.Date(),
            "isin": pl.String(),
            "symbol": pl.String(),
            "series": pl.String(),
            "open": pl.Float64(),
            "high": pl.Float64(),
            "low": pl.Float64(),
            "close": pl.Float64(),
            "prev_close": pl.Float64(),
            "volume": pl.Int64(),
            "value": pl.Float64(),
            "trades": pl.Int64(),
        },
    ),
    "delivery": _src(
        "delivery",
        {
            "trade_date": pl.Date(),
            "symbol": pl.String(),  # the delivery (MTO) file has no ISIN; join on symbol+series
            "series": pl.String(),
            "traded_qty": pl.Int64(),
            "deliv_qty": pl.Int64(),
            "deliv_pct": pl.Float64(),
        },
    ),
}


class RawStoreError(Exception):
    pass


@dataclass(frozen=True)
class Version:
    source: str
    partition_date: date
    ingest_seq: int
    known_at: datetime
    ingested_at: datetime
    known_at_synthetic: bool
    source_file_sha: str
    path: Path


class RawStore:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir

    # ------------------------------------------------------------------ helpers

    def _source(self, name: str) -> Source:
        try:
            return SOURCES[name]
        except KeyError as exc:
            raise RawStoreError(f"unknown source {name!r}") from exc

    def _dir(self, source: str, d: date) -> Path:
        return self.raw_dir / source / f"year={d.year}"

    def _files(self, source: str) -> list[Path]:
        root = self.raw_dir / source
        if not root.is_dir():
            return []
        return sorted(p for p in root.glob("year=*/*.parquet") if _FILE_RE.match(p.name))

    @staticmethod
    def _connect() -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        return con

    # ------------------------------------------------------------------ reads

    def versions(self, source: str, partition_date: date) -> list[Version]:
        self._source(source)
        folder = self._dir(source, partition_date)
        paths = sorted(folder.glob(f"{partition_date.isoformat()}__v*.parquet"))
        out = []
        with self._connect() as con:
            for path in paths:
                m = _FILE_RE.match(path.name)
                if not m:
                    continue
                row = con.execute(
                    "SELECT ingest_seq, known_at, ingested_at, known_at_synthetic, "
                    "source_file_sha FROM read_parquet(?) LIMIT 1",
                    [str(path)],
                ).fetchone()
                assert row is not None
                out.append(
                    Version(
                        source=source,
                        partition_date=partition_date,
                        ingest_seq=int(row[0]),
                        known_at=require_aware(row[1]),
                        ingested_at=require_aware(row[2]),
                        known_at_synthetic=bool(row[3]),
                        source_file_sha=str(row[4]),
                        path=path,
                    )
                )
        return sorted(out, key=lambda v: v.ingest_seq)

    def corrections(self, source: str) -> list[Version]:
        """Every version after the first (re-issued files), across all dates."""
        self._source(source)
        dates = sorted(
            {
                date.fromisoformat(m.group(1))
                for p in self._files(source)
                if (m := _FILE_RE.match(p.name)) and int(m.group(2)) > 1
            }
        )
        return [v for d in dates for v in self.versions(source, d) if v.ingest_seq > 1]

    def partitions(self, source: str) -> list[date]:
        """Every partition date that has at least one version, whenever it arrived."""
        self._source(source)
        dates = {
            date.fromisoformat(m.group(1))
            for p in self._files(source)
            if (m := _FILE_RE.match(p.name))
        }
        return sorted(dates)

    def as_of(
        self,
        source: str,
        cutoff: datetime,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        """Rows as they were known at `cutoff`: per partition date, the latest version
        with known_at <= cutoff. Optional inclusive date range on the partition column."""
        src = self._source(source)
        cutoff = require_aware(cutoff)
        files = [
            str(p)
            for p in self._files(source)
            if (m := _FILE_RE.match(p.name))
            and (start is None or date.fromisoformat(m.group(1)) >= start)
            and (end is None or date.fromisoformat(m.group(1)) <= end)
        ]
        empty = pl.DataFrame(schema={**src.columns, **_meta_schema()})
        if not files:
            return empty
        col = src.partition_col
        sql = f"""
            WITH v AS (
                SELECT * FROM read_parquet(?, union_by_name = true) WHERE known_at <= ?
            ),
            latest AS (SELECT {col} AS pd, max(ingest_seq) AS seq FROM v GROUP BY {col})
            SELECT v.* FROM v JOIN latest ON v.{col} = latest.pd AND v.ingest_seq = latest.seq
            ORDER BY v.{col}
        """
        with self._connect() as con:
            table = con.sql(sql, params=[files, cutoff]).to_arrow_table()
        df = pl.from_arrow(table)
        assert isinstance(df, pl.DataFrame)
        if df.height == 0:
            return empty
        return df.select(list(empty.columns))

    # ------------------------------------------------------------------ writes

    def write(
        self,
        source: str,
        partition_date: date,
        frame: pl.DataFrame,
        *,
        source_file_sha: str,
        ingested_at: datetime,
        known_at: datetime | None = None,
        known_at_synthetic: bool = False,
    ) -> Version | None:
        """Store one version of one day's file. Returns None if this exact file
        (same SHA-256) is already stored for that day."""
        src = self._source(source)
        ingested_at = require_aware(ingested_at)
        frame = self._validate(src, partition_date, frame)

        existing = self.versions(source, partition_date)
        if any(v.source_file_sha == source_file_sha for v in existing):
            return None
        seq = (existing[-1].ingest_seq + 1) if existing else 1
        if known_at is None:
            if existing:
                # A correction is known when it arrives, and always after the version it replaces.
                known_at = max(ingested_at, existing[-1].known_at + timedelta(microseconds=1))
            else:
                # First version: the §8 convention, even if the file was dropped in late.
                known_at = known_at_convention(partition_date)
        known_at = require_aware(known_at)

        stamped = frame.with_columns(
            pl.lit(seq, dtype=pl.Int32()).alias("ingest_seq"),
            pl.lit(known_at).alias("known_at"),
            pl.lit(ingested_at).alias("ingested_at"),
            pl.lit(known_at_synthetic).alias("known_at_synthetic"),
            pl.lit(source_file_sha).alias("source_file_sha"),
        )
        folder = self._dir(source, partition_date)
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"{partition_date.isoformat()}__v{seq:03d}.parquet"
        fd, tmp_name = tempfile.mkstemp(dir=folder, suffix=".tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            stamped.write_parquet(tmp)
            os.link(tmp, final)  # fails if `final` exists: raw files are never overwritten
        except FileExistsError as exc:
            raise RawStoreError(f"{final} already exists; concurrent writer?") from exc
        finally:
            tmp.unlink(missing_ok=True)
        return Version(
            source=source,
            partition_date=partition_date,
            ingest_seq=seq,
            known_at=known_at,
            ingested_at=ingested_at,
            known_at_synthetic=known_at_synthetic,
            source_file_sha=source_file_sha,
            path=final,
        )

    @staticmethod
    def _validate(src: Source, partition_date: date, frame: pl.DataFrame) -> pl.DataFrame:
        if frame.height == 0:
            raise RawStoreError(f"{src.name}: refusing to store an empty file")
        reserved = set(frame.columns) & set(META_COLUMNS)
        if reserved:
            raise RawStoreError(f"{src.name}: reserved columns present: {sorted(reserved)}")
        missing = set(src.columns) - set(frame.columns)
        extra = set(frame.columns) - set(src.columns)
        if missing or extra:
            raise RawStoreError(
                f"{src.name}: schema mismatch, missing={sorted(missing)} extra={sorted(extra)}"
            )
        try:
            frame = frame.select([pl.col(c).cast(t, strict=True) for c, t in src.columns.items()])
        except pl.exceptions.PolarsError as exc:
            raise RawStoreError(f"{src.name}: column types do not match: {exc}") from exc
        dates = frame.get_column(src.partition_col).unique().to_list()
        if dates != [partition_date]:
            raise RawStoreError(
                f"{src.name}: file must contain only {partition_date}, found {sorted(dates)}"
            )
        return frame


def _meta_schema() -> dict[str, pl.DataType]:
    return {
        "ingest_seq": pl.Int32(),
        "known_at": pl.Datetime("us", "UTC"),
        "ingested_at": pl.Datetime("us", "UTC"),
        "known_at_synthetic": pl.Boolean(),
        "source_file_sha": pl.String(),
    }
