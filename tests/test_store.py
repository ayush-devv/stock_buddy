"""WP 1.3: bitemporal raw store, time conventions and the snapshot."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from pi.store import db
from pi.store.clock import (
    IST,
    decision_cutoff,
    known_at_convention,
    latest_due_trade_date,
    require_aware,
)
from pi.store.raw import RawStore, RawStoreError
from tests.helpers import bhav_rows, sha_of

D = date(2026, 11, 20)  # a Friday


def ist(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


@pytest.fixture
def store(tmp_path: Path) -> RawStore:
    return RawStore(tmp_path / "raw")


def write(store: RawStore, frame: pl.DataFrame, at: datetime, d: date = D):
    return store.write("bhavcopy", d, frame, source_file_sha=sha_of(frame), ingested_at=at)


# --------------------------------------------------------------------------- clock


def test_known_at_convention_is_1800_ist() -> None:
    assert known_at_convention(D) == ist(2026, 11, 20, 18).astimezone(UTC)


def test_decision_cutoff_is_end_of_day_ist() -> None:
    cutoff = decision_cutoff(D)
    assert cutoff.astimezone(IST).date() == D
    assert cutoff + timedelta(microseconds=1) == ist(2026, 11, 21, 0).astimezone(UTC)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (ist(2026, 11, 20, 17, 59), date(2026, 11, 19)),
        (ist(2026, 11, 20, 18, 0), date(2026, 11, 20)),
        (ist(2026, 11, 21, 1, 0), date(2026, 11, 20)),
    ],
)
def test_latest_due_trade_date_flips_at_1800_ist(now: datetime, expected: date) -> None:
    assert latest_due_trade_date(now) == expected


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        require_aware(datetime(2026, 1, 1, 12))  # noqa: DTZ001


# --------------------------------------------------------------------------- writes


def test_first_version_uses_the_convention_even_if_dropped_late(store: RawStore) -> None:
    v = write(store, bhav_rows(D), at=ist(2026, 11, 23, 9))
    assert v is not None
    assert v.ingest_seq == 1
    assert v.known_at == known_at_convention(D)
    assert v.ingested_at == ist(2026, 11, 23, 9).astimezone(UTC)
    assert v.path.name == "2026-11-20__v001.parquet"


def test_same_file_twice_creates_no_new_version(store: RawStore) -> None:
    frame = bhav_rows(D)
    assert write(store, frame, at=ist(2026, 11, 20, 19)) is not None
    assert write(store, frame, at=ist(2026, 11, 20, 20)) is None
    assert len(store.versions("bhavcopy", D)) == 1


def test_raw_files_are_never_rewritten(store: RawStore) -> None:
    v1 = write(store, bhav_rows(D, close=100.0), at=ist(2026, 11, 20, 19))
    assert v1 is not None
    before = hashlib.sha256(v1.path.read_bytes()).hexdigest()
    write(store, bhav_rows(D, close=101.0), at=ist(2026, 11, 22, 10))
    assert hashlib.sha256(v1.path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("frame", "match"),
    [
        (bhav_rows(D).drop("close"), "schema mismatch"),
        (bhav_rows(D).with_columns(pl.lit(1).alias("extra")), "schema mismatch"),
        (bhav_rows(D).with_columns(pl.lit(1).alias("ingest_seq")), "reserved"),
        (bhav_rows(D).head(0), "empty"),
        (pl.concat([bhav_rows(D), bhav_rows(D + timedelta(days=1))]), "only 2026-11-20"),
        (bhav_rows(D).with_columns(pl.lit("x").alias("volume")), "types"),
    ],
)
def test_invalid_frames_are_refused(store: RawStore, frame: pl.DataFrame, match: str) -> None:
    with pytest.raises(RawStoreError, match=match):
        write(store, frame, at=ist(2026, 11, 20, 19))


def test_unknown_source_is_refused(store: RawStore) -> None:
    with pytest.raises(RawStoreError, match="unknown source"):
        store.as_of("nope", decision_cutoff(D))


# --------------------------------------------------------------------------- as-of reads


def test_empty_store_returns_empty_frame_with_schema(store: RawStore) -> None:
    df = store.as_of("bhavcopy", decision_cutoff(D))
    assert df.height == 0
    assert "close" in df.columns and "known_at" in df.columns


def test_query_for_d_never_sees_rows_known_later(store: RawStore) -> None:
    write(store, bhav_rows(D), at=ist(2026, 11, 20, 19))
    # Before 18:00 IST on D, D's file is not yet known.
    assert store.as_of("bhavcopy", ist(2026, 11, 20, 17, 59)).height == 0
    df = store.as_of("bhavcopy", decision_cutoff(D))
    assert df.height == 1
    assert (df.get_column("known_at") <= decision_cutoff(D)).all()


def test_correction_is_a_new_version_seen_only_after_it_arrives(store: RawStore) -> None:
    write(store, bhav_rows(D, close=100.0), at=ist(2026, 11, 20, 19))
    v2 = write(store, bhav_rows(D, close=101.5), at=ist(2026, 11, 22, 10))
    assert v2 is not None and v2.ingest_seq == 2
    assert v2.known_at == ist(2026, 11, 22, 10).astimezone(UTC)

    as_of_d = store.as_of("bhavcopy", decision_cutoff(D))
    assert as_of_d.get_column("close").to_list() == [100.0]  # what we knew on D
    later = store.as_of("bhavcopy", decision_cutoff(date(2026, 11, 23)))
    assert later.get_column("close").to_list() == [101.5]
    assert later.get_column("ingest_seq").to_list() == [2]


def test_correction_on_d_itself_is_visible_to_d(store: RawStore) -> None:
    write(store, bhav_rows(D, close=100.0), at=ist(2026, 11, 20, 18, 30))
    write(store, bhav_rows(D, close=100.5), at=ist(2026, 11, 20, 21))
    assert store.as_of("bhavcopy", decision_cutoff(D)).get_column("close").to_list() == [100.5]


def test_correction_replaces_the_whole_day(store: RawStore) -> None:
    two = bhav_rows(D, rows=[("INE000A01011", "AAA", "EQ"), ("INE000B01011", "BBB", "EQ")])
    one = bhav_rows(D, rows=[("INE000A01011", "AAA", "EQ")])
    write(store, two, at=ist(2026, 11, 20, 19))
    write(store, one, at=ist(2026, 11, 21, 10))
    later = store.as_of("bhavcopy", decision_cutoff(date(2026, 11, 23)))
    assert later.get_column("symbol").to_list() == ["AAA"]


def test_range_filter_and_partitions(store: RawStore) -> None:
    days = [date(2026, 11, 18), date(2026, 11, 19), D]
    for d in days:
        write(store, bhav_rows(d), at=ist(2026, 11, 20, 19), d=d)
    assert store.partitions("bhavcopy") == days
    df = store.as_of("bhavcopy", decision_cutoff(D), start=days[1], end=days[1])
    assert df.get_column("trade_date").to_list() == [days[1]]


# --------------------------------------------------------------------------- database


def test_state_round_trip(tmp_path: Path) -> None:
    con = db.connect(tmp_path / "pi.duckdb")
    db.ensure_schema(con, db.CORE_DDL)
    assert db.get_state(con, "k") is None
    db.set_state(con, "k", "v1")
    db.set_state(con, "k", "v2")
    assert db.get_state(con, "k") == "v2"
    con.close()


def test_snapshot_is_readable_and_does_not_block_the_writer(tmp_path: Path) -> None:
    path, snap = tmp_path / "pi.duckdb", tmp_path / "pi_read.duckdb"
    con = db.connect(path)
    db.ensure_schema(con, db.CORE_DDL)
    db.set_state(con, "gate_state", "NOT_EVALUATED")
    con.execute("CHECKPOINT")
    con.close()
    db.export_snapshot(path, snap)

    reader = db.connect(snap, read_only=True)  # e.g. the UI, left open
    writer = db.connect(path)
    db.set_state(writer, "gate_state", "RESEARCH")
    writer.execute("CHECKPOINT")
    writer.close()
    db.export_snapshot(path, snap)  # replacing the file under an open reader is fine

    assert db.get_state(reader, "gate_state") == "NOT_EVALUATED"  # old snapshot, consistent
    reader.close()
    fresh = db.connect(snap, read_only=True)
    assert db.get_state(fresh, "gate_state") == "RESEARCH"
    fresh.close()
