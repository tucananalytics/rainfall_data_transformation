#!/usr/bin/env python3
"""Tests for cru_pre_to_db. Run with: python -m unittest test_cru_pre_to_db -v

Uses only the standard library (unittest) so it runs without pytest installed.
"""
from __future__ import annotations

import unittest
from datetime import date

import cru_pre_to_db as mod


HEADER = [
    "Tyndall Centre grim file created on 22.01.2004 at 17:57 by Dr. Tim Mitchell\n",
    ".pre = precipitation (mm)\n",
    "CRU TS 2.1\n",
    "[Long=-180.00, 180.00] [Lati= -90.00,  90.00] [Grid X,Y= 720, 360]\n",
    "[Boxes=   67420] [Years=1991-2000] [Multi=    0.1000] [Missing=-999]\n",
]


class TestHeader(unittest.TestCase):
    def test_parse_header(self):
        h = mod.parse_header(HEADER)
        self.assertEqual(h.start_year, 1991)
        self.assertEqual(h.end_year, 2000)
        self.assertEqual(h.n_years, 10)
        self.assertAlmostEqual(h.multi, 0.1)
        self.assertEqual(h.missing, -999)

    def test_missing_years_raises(self):
        with self.assertRaises(mod.PreFormatError):
            mod.parse_header(["no useful header here"])


class TestFixedWidthSplit(unittest.TestCase):
    def test_normal_row(self):
        line = " 3020 2820 3040 2880 1740 1360  980  990 1410 1770 2580 2630"
        self.assertEqual(
            mod.split_fixed_width(line),
            [3020, 2820, 3040, 2880, 1740, 1360, 980, 990, 1410, 1770, 2580, 2630],
        )

    def test_glued_five_digit_values(self):
        # The real-world trap: a 5-digit value fills its 5-char field and abuts
        # the neighbour with no space. Whitespace splitting would merge them.
        line = " 1044 262413385 2892 1305 1228  824 1858 1220 3491 1648 1547"
        result = mod.split_fixed_width(line)
        self.assertEqual(len(result), 12)
        self.assertEqual(result[0], 1044)
        self.assertEqual(result[1], 2624)   # would be 262413385 under naive split
        self.assertEqual(result[2], 13385)
        self.assertEqual(result[3], 2892)

    def test_split_would_have_failed(self):
        # Demonstrates that the naive approach is genuinely wrong on this data.
        line = " 1044 262413385 2892 1305 1228  824 1858 1220 3491 1648 1547"
        self.assertNotEqual(len(line.split()), 12)


class TestRecords(unittest.TestCase):
    def _two_year_block(self):
        return [
            "Grid-ref=   1, 148\n",
            " 3020 2820 3040 2880 1740 1360  980  990 1410 1770 2580 2630\n",
            " -999 2820 3040 2880 1740 1360  980  990 1410 1770 2580 2630\n",
        ]

    def test_scaling_and_dates(self):
        header = mod.Header(1991, 1992, 0.1, -999)
        records = list(mod.iter_records(self._two_year_block(), header))
        self.assertEqual(len(records), 24)  # 2 years x 12 months
        first = records[0]
        self.assertEqual((first.xref, first.yref), (1, 148))
        self.assertEqual(first.obs_date, date(1991, 1, 1))
        self.assertAlmostEqual(first.value, 302.0)  # 3020 * 0.1
        # second year, first month
        jan_1992 = records[12]
        self.assertEqual(jan_1992.obs_date, date(1992, 1, 1))

    def test_missing_becomes_none(self):
        header = mod.Header(1991, 1992, 0.1, -999)
        records = list(mod.iter_records(self._two_year_block(), header))
        # The -999 is the first month of year 2 (record index 12).
        self.assertIsNone(records[12].value)


class TestEndToEnd(unittest.TestCase):
    def test_in_memory_load(self):
        import sqlite3
        body = [
            "Grid-ref=   1, 148\n",
            " 3020 2820 3040 2880 1740 1360  980  990 1410 1770 2580 2630\n",
        ]
        header = mod.Header(1991, 1991, 0.1, -999)
        conn = sqlite3.connect(":memory:")
        mod.create_schema(conn)
        n = mod.load_records(conn, mod.iter_records(body, header))
        self.assertEqual(n, 12)
        cur = conn.execute(
            "SELECT Xref, Yref, Date, Value FROM precipitation ORDER BY Date LIMIT 1"
        )
        row = cur.fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 148)
        self.assertEqual(row[2], "1991-01-01")
        self.assertAlmostEqual(row[3], 302.0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
