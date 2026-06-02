# CRU `.pre` precipitation transform & load

Transforms a CRU TS 2.1 `.pre` precipitation file into a relational table of the
form **`Xref | Yref | Date | Value`** and loads it into a SQLite database.

## Running it

```bash
# Default: creates ./precipitation.db
python cru_pre_to_db.py --file cru-ts-2-10_1991-2000-cutdown.pre

# Specify the database location
python cru_pre_to_db.py --file input.pre --database /path/to/output.db

# Store raw integers instead of applying the header scale factor
python cru_pre_to_db.py --file input.pre --raw-values

# Verbose (debug) logging
python cru_pre_to_db.py --file input.pre -v
```

Run `python cru_pre_to_db.py --help` for all options.

**No third-party dependencies** — it uses only the Python standard library
(`sqlite3`), so it runs on any Python 3.9+ install with no `pip install` step.
The database is created by the script if it does not already exist; pass
`--database :memory:` for a throwaway in-memory database.

## Tests

```bash
python -m unittest test_cru_pre_to_db -v
```

## How the file maps to the table

The header is read for three things rather than hard-coding them, so the script
also works on other CRU TS 2.x `.pre` extracts:

| Header field        | Used for                                              |
|---------------------|-------------------------------------------------------|
| `[Years=1991-2000]` | the year each data row corresponds to                 |
| `[Multi= 0.1000]`   | scale factor: raw `3020` &rarr; `302.0` mm            |
| `[Missing=-999]`    | sentinel; stored as SQL `NULL`                        |

Each `Grid-ref= X, Y` block has one **row per year** and 12 **columns per month**
(Jan&ndash;Dec), so each block expands to `n_years x 12` table rows, dated to the
first of each month (`YYYY-MM-01`).

## The one non-obvious thing: fixed-width, not space-delimited

The values look space-separated, but they are actually **right-justified in
fixed 5-character columns**. When a value reaches 5 digits it fills its column
and touches the next one with no separating space:

```
 1044 262413385 2892 ...
        ^^^^^----- this is two values: 2624 and 13385, not "262413385"
```

Splitting on whitespace silently merges those columns and corrupts the data.
The script slices each line at 5-character boundaries instead, which is robust
to this. There is a regression test (`test_glued_five_digit_values`) covering
exactly this case, and one (`test_split_would_have_failed`) documenting that the
naive approach genuinely breaks on the supplied file.

In the supplied `cutdown` file this affects real records &mdash; e.g. location
(48, 222) for March 1991 has a raw value of `13385`, i.e. **1338.5 mm**, which is
the maximum in the dataset.

## Output verification (supplied file)

- 5,226 grid locations &times; 10 years &times; 12 months = **627,120 rows**
- Date range `1991-01-01` to `2000-12-01`
- First location (1, 148), Jan 1991 = 302.0 mm (raw 3020 &times; 0.1) &mdash;
  matches the worked example in the task brief.

## Design choices worth noting

- **Streaming parse.** Records are yielded lazily and inserted in batched
  transactions, so memory use is constant regardless of file size.
- **`INSERT OR REPLACE` + `UNIQUE (Xref, Yref, Date)`.** Re-running the script is
  idempotent rather than producing duplicate rows.
- **Indexes** on `(Xref, Yref)` and `Date` are created after load for typical
  query patterns (by location, by time).
- **Fails loudly** on malformed input (`PreFormatError`) rather than loading
  partial/garbage data silently.

## Choosing a different database

SQLite was chosen because the brief asks for a database "created within the
script" with no external setup. The parsing/transform layer (`iter_records`,
`split_fixed_width`, `parse_header`) is fully decoupled from the database layer
(`connect`, `create_schema`, `load_records`), so retargeting to PostgreSQL or
SQL Server is a matter of swapping the small DB section &mdash; the DDL and the
parameterised `executemany` translate directly.
