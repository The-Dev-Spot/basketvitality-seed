"""HOM-1633: OFF's two TSV dialects.

The legacy export wrote fields raw: a name can start with a lone quote, and
reading it as CSV would swallow the rows after it. The exporter OFF switched
to on 2026-10-21 (csv.writer, tab, minimal quoting) wraps any field that
contains a quote and doubles the quotes inside it. All data here is synthetic.
"""
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

# Synthetic names with literal double quotes, keyed by fixture barcode.
QUOTED_NAMES = {
    "1000000000017": 'Testmelk "Halfvol" 1L',
    "1000000000055": 'Testsap ""dubbel""',
}
QUOTED_BRAND = ("1000000000055", 'Sapmerk "Plus"')


class ExportDialectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with gzip.open(FIXTURE, "rt", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            self.fields, self.rows = list(reader.fieldnames), list(reader)

    def write_quoted(self) -> Path:
        """What OFF's current exporter writes: minimal quoting, tab delimiter."""
        rows = [dict(r) for r in self.rows]
        for row in rows:
            if row["code"] in QUOTED_NAMES:
                row["product_name"] = QUOTED_NAMES[row["code"]]
            if row["code"] == QUOTED_BRAND[0]:
                row["brands"] = QUOTED_BRAND[1]
        path = self.root / "quoted.tsv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, self.fields, delimiter="\t", quoting=csv.QUOTE_MINIMAL,
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def write_legacy(self, names: dict[str, str]) -> Path:
        """The legacy dialect: fields joined raw, whatever they contain."""
        rows = [dict(r) for r in self.rows]
        for row in rows:
            if row["code"] in names:
                row["product_name"] = names[row["code"]]
        path = self.root / "legacy.tsv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            stream.write("\t".join(self.fields) + "\n")
            for row in rows:
                stream.write("\t".join(row[f] or "" for f in self.fields) + "\n")
        return path

    def country(self, source: Path, dialect: str = "auto") -> list[dict]:
        output = Path(tempfile.mkdtemp(dir=self.root))
        with contextlib.redirect_stdout(io.StringIO()):
            manifest = cutter.cut(str(source), output, "en:netherlands", None, dialect=dialect)
        with gzip.open(output / manifest["seedFile"], "rt", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        self.assertEqual(manifest["rowCount"], len(rows))
        return rows

    def by_barcode(self, rows):
        return {r["barcode"]: r for r in rows}

    def test_quoted_export_decodes_names_and_brands(self):
        rows = self.by_barcode(self.country(self.write_quoted()))
        self.assertEqual('Testmelk "Halfvol" 1L', rows["1000000000017"]["name"])
        self.assertEqual('Testsap ""dubbel""', rows["1000000000055"]["name"])
        self.assertEqual('Sapmerk "Plus"', rows["1000000000055"]["brands"])

    def test_quoted_export_keeps_rows_and_other_fields(self):
        legacy = self.country(FIXTURE)
        quoted = self.country(self.write_quoted())
        self.assertEqual([r["barcode"] for r in legacy], [r["barcode"] for r in quoted])
        for before, after in zip(legacy, quoted):
            for key in before:
                if key not in ("name", "brands"):
                    self.assertEqual(before[key], after[key], f"{before['barcode']} {key}")

    def test_legacy_lone_quote_does_not_swallow_the_next_rows(self):
        path = self.write_legacy({"1000000000017": '"Testmelk Halfvol'})
        rows = self.country(path)
        self.assertEqual(4, len(rows), "every Dutch row survives")
        self.assertEqual('"Testmelk Halfvol', self.by_barcode(rows)["1000000000017"]["name"])

    def test_auto_detects_each_dialect(self):
        legacy = self.write_legacy({"1000000000017": '"Testmelk Halfvol'})
        self.assertEqual("legacy", cutter.sniff_dialect(legacy))
        self.assertEqual("quoted", cutter.sniff_dialect(self.write_quoted()))

    def test_forced_legacy_leaves_quoted_fields_raw(self):
        rows = self.by_barcode(self.country(self.write_quoted(), dialect="legacy"))
        self.assertEqual('"Testmelk ""Halfvol"" 1L"', rows["1000000000017"]["name"])

    def test_decode_field(self):
        self.assertEqual('a "b" c', cutter.decode_quoted_field('"a ""b"" c"'))
        self.assertEqual("", cutter.decode_quoted_field('""'))
        self.assertEqual('"open', cutter.decode_quoted_field('"open'), "a lone quote is not a wrapped field")
        self.assertEqual('a "b"', cutter.decode_quoted_field('a "b"'), "unwrapped stays as is")
        self.assertEqual('"x" y "z"', cutter.decode_quoted_field('"x" y "z"'), "inner lone quotes: not one wrapped field")


if __name__ == "__main__":
    unittest.main()
