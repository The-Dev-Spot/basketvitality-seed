"""Regression coverage for OFF's legacy and localized brand export headers."""
import contextlib
import csv
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path

import cut_off_nl as cutter


FIXTURE = Path(__file__).parent / "fixtures" / "off_sample.tsv.gz"


class BrandHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def export_with_headers(self, variant):
        with gzip.open(FIXTURE, "rt", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            fields, rows = list(reader.fieldnames), list(reader)
        if variant == "localized":
            fields[fields.index("brands")] = "brands_en"
            for row in rows:
                row["brands_en"] = row.pop("brands")
        elif variant == "both":
            fields.insert(0, "brands_en")
            for row in rows:
                row["brands_en"] = "Conflicting localized brand" if row["brands"].strip() else ""
        elif variant == "blank_legacy":
            fields.insert(0, "brands_en")
            for index, row in enumerate(rows):
                row["brands_en"] = row["brands"]
                row["brands"] = "" if index % 2 == 0 else "   "
        elif variant == "missing":
            fields.remove("brands")
            for row in rows:
                row.pop("brands")
        else:
            raise ValueError(variant)
        path = self.root / f"{variant}.tsv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fields, delimiter="\t", quoting=csv.QUOTE_NONE)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def products(self, output, manifest):
        path = output / manifest["seedFile"]
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        self.assertEqual(manifest["rowCount"], len(rows))
        return rows, path

    def country(self, source):
        output = Path(tempfile.mkdtemp(dir=self.root))
        with contextlib.redirect_stdout(io.StringIO()):
            manifest = cutter.cut(str(source), output, "en:netherlands", None)
        return self.products(output, manifest)

    def neighbours(self, source, primary, include_ungraded=False):
        output = Path(tempfile.mkdtemp(dir=self.root))
        with contextlib.redirect_stdout(io.StringIO()):
            manifest = cutter.cut_neighbours(
                str(source), output, primary, "en:netherlands",
                ["en:germany", "en:belgium", "en:france"], 1,
                {"carrefour", "delhaize", "boni"}, not include_ungraded, None,
            )
        return self.products(output, manifest)[0]

    def test_legacy_fixture_keeps_country_and_neighbour_filters(self):
        rows, primary = self.country(FIXTURE)
        self.assertEqual(4, len(rows))
        self.assertEqual(["Testmerk", None, "Sapmerk", None], [r["brands"] for r in rows])
        graded = self.neighbours(FIXTURE, primary)
        self.assertEqual(["1000000000079"], [r["barcode"] for r in graded])
        all_rows = self.neighbours(FIXTURE, primary, include_ungraded=True)
        self.assertEqual(
            ["1000000000079", "1000000000086"], [r["barcode"] for r in all_rows]
        )

    def test_localized_header_preserves_every_country_field(self):
        expected, _ = self.country(FIXTURE)
        actual, _ = self.country(self.export_with_headers("localized"))
        self.assertEqual(expected, actual)

    def test_localized_header_preserves_neighbour_brand_filter(self):
        _, primary = self.country(FIXTURE)
        localized = self.export_with_headers("localized")
        for include_ungraded in (False, True):
            with self.subTest(include_ungraded=include_ungraded):
                self.assertEqual(
                    self.neighbours(FIXTURE, primary, include_ungraded),
                    self.neighbours(localized, primary, include_ungraded),
                )

    def test_legacy_brand_wins_when_both_headers_exist(self):
        expected, primary = self.country(FIXTURE)
        both = self.export_with_headers("both")
        actual, both_primary = self.country(both)
        self.assertEqual(expected, actual)
        for include_ungraded in (False, True):
            with self.subTest(include_ungraded=include_ungraded):
                self.assertEqual(
                    self.neighbours(FIXTURE, primary, include_ungraded),
                    self.neighbours(both, both_primary, include_ungraded),
                )

    def test_localized_brand_fills_empty_and_whitespace_legacy_cells(self):
        expected, primary = self.country(FIXTURE)
        blank_legacy = self.export_with_headers("blank_legacy")
        with self.subTest(mode="country"):
            actual, _ = self.country(blank_legacy)
            self.assertEqual(expected, actual)
        for include_ungraded in (False, True):
            with self.subTest(mode="neighbours", include_ungraded=include_ungraded):
                self.assertEqual(
                    self.neighbours(FIXTURE, primary, include_ungraded),
                    self.neighbours(blank_legacy, primary, include_ungraded),
                )

    def test_missing_both_headers_is_rejected_in_each_mode(self):
        _, primary = self.country(FIXTURE)
        missing = self.export_with_headers("missing")
        operations = {
            "country": lambda: self.country(missing),
            "neighbours": lambda: self.neighbours(missing, primary),
        }
        for mode, operation in operations.items():
            with self.subTest(mode=mode):
                with self.assertRaises(SystemExit) as error:
                    operation()
                self.assertEqual(
                    "input is missing expected columns: ['brands']", str(error.exception)
                )


if __name__ == "__main__":
    unittest.main()
