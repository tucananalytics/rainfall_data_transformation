#!/usr/bin/env python3
"""Transform CRU TS 2.1 ``.pre`` precipitation files into a relational table.

The CRU (Climatic Research Unit) ``.pre`` format stores monthly precipitation
on a global grid as a header followed by repeating per-location blocks::

    Tyndall Centre grim file created on 22.01.2004 at 17:57 by Dr. Tim Mitchell
    .pre = precipitation (mm)
    CRU TS 2.1
    [Long=-180.00, 180.00] [Lati= -90.00,  90.00] [Grid X,Y= 720, 360]
    [Boxes=   67420] [Years=1991-2000] [Multi=    0.1000] [Missing=-999]
    Grid-ref=   1, 148
     3020 2820 3040 2880 1740 1360  980  990 1410 1770 2580 2630   <- year 1991
     ... (one row per year, 12 monthly columns) ...

This script reads such a file, normalises it to one row per
(location, month), and loads it into a database table of the shape:

    Xref | Yref | Date | Value

Design notes
------------
* **Fixed-width parsing, not whitespace splitting.** Each monthly value
  occupies a 5-character right-justified column. A 5-digit value (e.g. a
  precipitation total recorded as ``13385``) fills its column completely and
  abuts its neighbour with no separating space, so ``str.split()`` silently
  merges columns and corrupts the data. We slice by column position instead.
* **Stdlib only.** Uses ``sqlite3`` from the standard library so the script
  runs anywhere Python runs, with no install step. The database is created by
  the script if it does not exist (``--database``); an in-memory DB is also
  supported for testing.
* **Header-driven.** Year range, the ``Multi`` scale factor and the missing
  sentinel are read from the header rather than hard-coded, so the same code
  works for other CRU TS 2.x ``.pre`` extracts.

The raw integers are scaled by the header ``Multi`` factor (0.1 for ``.pre``),
i.e. a raw ``3020`` represents 302.0 mm. The scaled value is stored alongside
the raw value; see ``--raw-values`` to store integers verbatim instead.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

LOGGER = logging.getLogger("cru_pre")

# Each monthly value is right-justified in a fixed 5-character field.
FIELD_WIDTH = 5
MONTHS_PER_YEAR = 12

GRID_REF_RE = re.compile(r"Grid-ref=\s*(-?\d+)\s*,\s*(-?\d+)")
YEARS_RE = re.compile(r"Years=\s*(\d{4})\s*-\s*(\d{4})")
MULTI_RE = re.compile(r"Multi=\s*([0-9.]+)")
MISSING_RE = re.compile(r"Missing=\s*(-?\d+)")


@dataclass(frozen=True)
class Header:
    """Parsed values from the CRU ``.pre`` header block."""

    start_year: int
    end_year: int
    multi: float
    missing: int

    @property
    def n_years(self) -> int:
        return self.end_year - self.start_year + 1


@dataclass(frozen=True)
class Record:
    """One normalised observation: precipitation for a location-month."""

    xref: int
    yref: int
    obs_date: date
    value: Optional[float]  # None when the source value is the missing sentinel


class PreFormatError(ValueError):
    """Raised when the input does not conform to the expected ``.pre`` layout."""


def parse_header(lines: Sequence[str]) -> Header:
    """Extract year range, scale factor and missing sentinel from the header.

    The header is the set of lines preceding the first ``Grid-ref=`` line.
    """
    blob = "\n".join(lines)

    years = YEARS_RE.search(blob)
    if not years:
        raise PreFormatError("Could not find 'Years=YYYY-YYYY' in header.")
    start_year, end_year = int(years.group(1)), int(years.group(2))
    if end_year < start_year:
        raise PreFormatError(f"End year {end_year} precedes start year {start_year}.")

    multi_match = MULTI_RE.search(blob)
    multi = float(multi_match.group(1)) if multi_match else 1.0

    missing_match = MISSING_RE.search(blob)
    missing = int(missing_match.group(1)) if missing_match else -999

    header = Header(start_year, end_year, multi, missing)
    LOGGER.info(
        "Header: years %d-%d (%d), scale=%.4g, missing=%d",
        header.start_year, header.end_year, header.n_years, header.multi, header.missing,
    )
    return header


def split_fixed_width(line: str) -> list[int]:
    """Split a data line into 12 integers using fixed 5-char columns.

    Whitespace splitting is unsafe here: a 5-digit value fills its whole field
    and runs into the next column, so ``split()`` would merge them. Slicing by
    column position is robust to that.
    """
    stripped = line.rstrip("\n")
    expected = FIELD_WIDTH * MONTHS_PER_YEAR
    if len(stripped) < expected:
        # Pad short final lines (files may omit trailing spaces); fail loudly if
        # the deficit is too large to be mere trailing-space trimming.
        if expected - len(stripped) > FIELD_WIDTH:
            raise PreFormatError(
                f"Data line too short ({len(stripped)} chars, expected {expected}): {line!r}"
            )
        stripped = stripped.ljust(expected)

    values: list[int] = []
    for col in range(MONTHS_PER_YEAR):
        chunk = stripped[col * FIELD_WIDTH : (col + 1) * FIELD_WIDTH].strip()
        try:
            values.append(int(chunk))
        except ValueError as exc:
            raise PreFormatError(f"Non-integer value {chunk!r} in line: {line!r}") from exc
    return values


def iter_records(lines: Iterable[str], header: Header) -> Iterator[Record]:
    """Yield :class:`Record` objects from the data section of the file.

    Streams line by line so arbitrarily large files use constant memory.
    """
    current_xref: Optional[int] = None
    current_yref: Optional[int] = None
    year_offset = 0  # which year within the current block we are on

    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue

        grid_match = GRID_REF_RE.match(line.strip())
        if grid_match:
            if current_xref is not None and year_offset != header.n_years:
                LOGGER.warning(
                    "Location (%s, %s) had %d year-rows, expected %d.",
                    current_xref, current_yref, year_offset, header.n_years,
                )
            current_xref = int(grid_match.group(1))
            current_yref = int(grid_match.group(2))
            year_offset = 0
            continue

        if current_xref is None:
            # Still inside the header block; skip.
            continue

        monthly = split_fixed_width(line)
        year = header.start_year + year_offset
        if year_offset >= header.n_years:
            LOGGER.warning(
                "Extra data row beyond declared years at (%s, %s); skipping.",
                current_xref, current_yref,
            )
            continue

        for month_index, raw_value in enumerate(monthly, start=1):
            if raw_value == header.missing:
                value: Optional[float] = None
            else:
                value = round(raw_value * header.multi, 4)
            yield Record(
                xref=current_xref,
                yref=current_yref,
                obs_date=date(year, month_index, 1),
                value=value,
            )
        year_offset += 1


def read_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as handle:
        return handle.readlines()


def split_header_and_body(lines: Sequence[str]) -> tuple[list[str], list[str]]:
    """Partition file lines into the header block and the data block."""
    for index, line in enumerate(lines):
        if GRID_REF_RE.match(line.strip()):
            return list(lines[:index]), list(lines[index:])
    raise PreFormatError("No 'Grid-ref=' line found; not a valid .pre file.")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

DDL = """
CREATE TABLE IF NOT EXISTS precipitation (
    id     INTEGER PRIMARY KEY,
    Xref   INTEGER NOT NULL,
    Yref   INTEGER NOT NULL,
    Date   TEXT    NOT NULL,   -- ISO-8601 'YYYY-MM-DD' (first of month)
    Value  REAL,               -- precipitation in mm; NULL where source = missing
    UNIQUE (Xref, Yref, Date)
);
"""


def connect(database: str) -> sqlite3.Connection:
    """Open (creating if needed) the target database. ``:memory:`` is allowed."""
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def load_records(
    conn: sqlite3.Connection,
    records: Iterable[Record],
    batch_size: int = 10_000,
) -> int:
    """Insert records in batches inside a single transaction. Returns the count."""
    sql = (
        "INSERT OR REPLACE INTO precipitation (Xref, Yref, Date, Value) "
        "VALUES (?, ?, ?, ?)"
    )
    total = 0
    batch: list[tuple[int, int, str, Optional[float]]] = []
    cursor = conn.cursor()
    for rec in records:
        batch.append((rec.xref, rec.yref, rec.obs_date.isoformat(), rec.value))
        if len(batch) >= batch_size:
            cursor.executemany(sql, batch)
            total += len(batch)
            batch.clear()
            LOGGER.debug("Inserted %d rows so far...", total)
    if batch:
        cursor.executemany(sql, batch)
        total += len(batch)
    conn.commit()
    return total


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_precip_loc ON precipitation (Xref, Yref);"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_precip_date ON precipitation (Date);")
    conn.commit()


# --------------------------------------------------------------------------- #
# Orchestration / CLI
# --------------------------------------------------------------------------- #

def transform_file(
    input_path: Path,
    database: str,
    *,
    raw_values: bool = False,
) -> int:
    """End-to-end: read, parse header, transform, create table, load. Returns rows."""
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    LOGGER.info("Reading %s", input_path)
    lines = read_lines(input_path)
    header_lines, body_lines = split_header_and_body(lines)
    header = parse_header(header_lines)
    if raw_values:
        header = Header(header.start_year, header.end_year, 1.0, header.missing)
        LOGGER.info("--raw-values set: storing integer values without scaling.")

    conn = connect(database)
    try:
        create_schema(conn)
        rows = load_records(conn, iter_records(body_lines, header))
        create_indexes(conn)
        LOGGER.info("Loaded %d rows into '%s'.", rows, database)
        return rows
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transform a CRU TS .pre precipitation file into a database table.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-f", "--file", dest="input_file", required=True, type=Path,
        help="Path to the input .pre file.",
    )
    parser.add_argument(
        "-d", "--database", default="precipitation.db",
        help="SQLite database path. Created if absent. Use ':memory:' for a transient DB.",
    )
    parser.add_argument(
        "--raw-values", action="store_true",
        help="Store raw integer values instead of applying the header 'Multi' scale factor.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        rows = transform_file(
            args.input_file, args.database, raw_values=args.raw_values
        )
    except (FileNotFoundError, PreFormatError) as exc:
        LOGGER.error("%s", exc)
        return 1
    LOGGER.info("Done. %d observations loaded.", rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
