"""Tests for safe FTS5 parts search query construction."""

from __future__ import annotations

import sqlite3
import unittest

from src.core.part_search import SEARCH_COLUMNS, build_parts_search_query


class PartSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        columns = ",".join(f'"{column}"' for column in SEARCH_COLUMNS)
        self.connection.execute(
            f"CREATE VIRTUAL TABLE parts USING fts5 ({columns}, tokenize='trigram')"
        )
        self.connection.execute(
            f"INSERT INTO parts ({columns}) VALUES ({','.join('?' for _ in SEARCH_COLUMNS)})",
            (
                "C1",
                "ACME'S-1",
                "SOIC-8",
                "8",
                "Basic",
                "42",
                'ACME "Precision"',
                "precision amplifier",
                "1-:0.1",
                "Amplifiers",
                "{}",
            ),
        )
        self.connection.create_collation(
            "naturalsort", lambda left, right: (left > right) - (left < right)
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_quotes_are_bound_and_escaped(self) -> None:
        query, values = build_parts_search_query(
            {
                "keyword": "precision",
                "manufacturer": 'ACME "Precision"',
                "part_no": "ACME'S-1",
                "basic": True,
                "extended": False,
                "stock": True,
            },
            "LCSC Part",
            "ASC",
        )
        self.assertNotIn("ACME", query)
        self.assertEqual(len(self.connection.execute(query, values).fetchall()), 1)

    def test_short_keyword_apostrophe_does_not_break_sql(self) -> None:
        query, values = build_parts_search_query(
            {"keyword": "'", "basic": True, "extended": True, "stock": False},
            "MFR.Part",
            "DESC",
        )
        self.connection.execute(query, values).fetchall()

    def test_invalid_sort_column_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_parts_search_query({"keyword": "amp"}, 'x"; DROP TABLE parts', "ASC")


if __name__ == "__main__":
    unittest.main()
