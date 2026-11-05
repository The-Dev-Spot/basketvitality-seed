#!/usr/bin/env python3
"""Cut country slices from the Open Food Facts CSV export into the Basket Vitality seed format.

Two modes:

  --mode country     (default) one country's products, as before.
                     seed-<country>.jsonl.gz + manifest.json

  --mode neighbours  the NEIGHBOUR COMPLEMENT (HOM-1186): products from adjacent
                     markets that we do not already have, whose brand we already
                     stock here. On the 2026-09-04 export: 84,628 such products,
                     64,198 of them gradeable (FR 39,143 / DE 22,295 / BE 8,841)
                     from 277 vouching brands, 3.85 MB compressed — against
                     12,553 gradeable products in the Dutch cut today.
                     seed-neighbours.jsonl.gz + manifest-neighbours.json

                     NOT BUNDLED, and the measurement is the reason. Merged into
                     the seed it looks like a triumph — catalogue "No score yet"
                     falls 82.5% -> 43.5% — but that is pure dilution: the
                     unscored row count is unchanged at 59,208. Not one product
                     already in the cut became gradeable; the denominator just
                     grew. Against the real-receipt corpus, +64,198 rows and
                     +3.85 MB moved grocery lines matched 170 -> 173 /208 and
                     graded 80 -> 81 /208. One line, for more bytes than the
                     entire Dutch seed. Kept as a measurement tool and for a
                     possible on-demand channel, where size is not the cost.

Why a brand filter at all: `countries_tags` records where a contributor saw a
product, not where it is sold, so "tagged Germany" alone pulls in a million
products that will never touch a Dutch shelf. A brand we already stock is the
cheapest honest signal that a foreign row might. It has to be a brand with real
presence, though — Auchan has FOUR rows in the Dutch cut and would otherwise
drag in 16,958 French ones.

Runs nightly in GitHub Actions (see .github/workflows/off-seed.yml). The app never
talks to OFF for bulk data — it downloads the compiled seed only (ADR-0023, HOM-720).

Notes tied to the 2026-07-11 spike/verification:
  - The CSV export serves `environmental_score_grade`; the v2 API serves `ecoscore_grade`.
    Both land in the seed as `ecoGrade`.
  - `fruits-vegetables-nuts-estimate...` is not in the CSV export; `fruitsVegNutsPct`
    stays null (stored grades cover ~88% of the NL head; local computation treats
    missing positives as 0, matching OFF). Upgrade path: the JSONL export carries it.
"""
import argparse
import csv
import gzip
import hashlib
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = {
    "code": "barcode",
    "product_name": "name",
    "brands": "brands",
    "quantity": "quantity",
    "nutriscore_grade": "nutriGrade",
    "environmental_score_grade": "ecoGrade",  # export-side name for ecoscore_grade
    "categories_tags": "categoryTags",
}
# NOVA processing group (1-4). Kept separate from FLOAT_COLUMNS because it is a
# category, not a measurement — emitting 4.0 for "ultra-processed" would invite
# arithmetic that means nothing.
INT_COLUMNS = {
    "nova_group": "novaGroup",
}
FLOAT_COLUMNS = {
    "energy-kj_100g": "energyKj",
    "energy-kcal_100g": "energyKcal",
    "sugars_100g": "sugars",
    "saturated-fat_100g": "saturatedFat",
    "fat_100g": "fat",
    "salt_100g": "salt",
    "proteins_100g": "proteins",
    "fiber_100g": "fiber",
}

# The 2023 Nutri-Score calculator returns null without these four
# (NutriScoreCalculator2023.compute); everything else only shifts the score.
NUTRISCORE_REQUIRED = ("energy-kj_100g", "sugars_100g", "saturated-fat_100g", "salt_100g")

ATTRIBUTIONS = [
    "Product data © Open Food Facts contributors — https://world.openfoodfacts.org — "
    "Open Database License (ODbL) v1.0",
]


def parse_float(raw: str):
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_nova(raw: str):
    """NOVA group as 1-4, or None.

    OFF carries free-text junk and out-of-range values in this column, and a
    NOVA group outside 1-4 is meaningless rather than merely imprecise — so
    anything unexpected is dropped instead of being passed through.
    """
    if not raw:
        return None
    try:
        value = int(float(raw))
    except (ValueError, OverflowError):
        # OverflowError: float("inf")/float("1e400") succeed, int() then
        # overflows — inside the stated free-text-junk threat model (HOM-787).
        return None
    return value if 1 <= value <= 4 else None


def parse_analysis(raw: str):
    """OFF `ingredients_analysis_tags` -> (vegetarianStatus, veganStatus, palmOil).

    Verdict strings are the app's enum names (DietStatus / PalmOilStatus); the
    mapping MIRRORS OffBarcodeLookup.dietStatusFrom/palmOilFrom (HOM-762) and
    is pinned by the off-seed smoke assertions — change both together
    (HOM-891). Exact tag membership, never substring: "en:vegan" must not
    match inside "en:vegan-status-unknown". Unknown/absent stays None — a
    missing analysis is not a claim (ADR-0023). Column is optional: OFF
    dropping it must not kill the cut (HOM-787 §6 principle).
    """
    if not raw:
        return None, None, None
    tags = {t.strip().lower() for t in raw.split(",")}

    def diet(axis: str):
        if f"en:{axis}" in tags:
            return "YES"
        if f"en:non-{axis}" in tags:
            return "NO"
        if f"en:maybe-{axis}" in tags:
            return "MAYBE"
        return None

    if "en:palm-oil-free" in tags:
        palm = "FREE"
    elif "en:palm-oil" in tags:
        palm = "CONTAINS"
    elif "en:may-contain-palm-oil" in tags:
        palm = "MAYBE"
    else:
        palm = None
    return diet("vegetarian"), diet("vegan"), palm


_ADDITIVE_CODE = re.compile(r"e\d{3,4}[a-z]*")


def parse_additives(raw: str):
    """OFF `additives_tags` (`en:e330,en:e322i`) -> compact `"e330,e322i"`, or None.

    Keeps the code after the last colon and only real E-number shapes — OFF's
    tag list carries occasional free-text noise that would otherwise become a
    garbage additive. Order-preserving dedup. Absent column -> None (graceful,
    since not every OFF export carries this field)."""
    if not raw:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw.split(","):
        code = tag.strip().split(":")[-1].lower()
        if _ADDITIVE_CODE.fullmatch(code) and code not in seen:
            seen.add(code)
            out.append(code)
    return ",".join(out) or None


def unescape_text(value: str) -> str:
    """OFF CSV text can be HTML-entity-encoded, sometimes doubly
    ("Lay&amp;#039;s" — HOM-883). Unescape to a bounded fixpoint."""
    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


NON_WORD_BRAND = re.compile(r"[^0-9a-z]+")


def norm_brands(text: str | None) -> set[str]:
    """OFF brands are free text and often multi-valued: 'Zuivelmeester,Lidl'.

    Two-character brands are dropped: 'ah' would match far too much across
    markets to be evidence of anything.
    """
    out: set[str] = set()
    for part in (text or "").split(","):
        brand = NON_WORD_BRAND.sub(" ", part.strip().lower()).strip()
        if len(brand) >= 3:
            out.add(brand)
    return out


def load_primary(seed_path: Path) -> tuple[set[str], Counter]:
    """Barcodes and brand frequencies of the cut we already ship."""
    barcodes: set[str] = set()
    brands: Counter = Counter()
    with gzip.open(seed_path, "rt", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            barcodes.add(row["barcode"])
            for brand in norm_brands(row.get("brands")):
                brands[brand] += 1
    return barcodes, brands


def build_product(row: dict) -> dict | None:
    """One OFF export row → one seed record, or None if it names nothing."""
    barcode = (row.get("code") or "").strip()
    name = unescape_text((row.get("product_name") or "").strip())
    if not barcode or not name:
        return None
    product = {out: (row.get(col) or "").strip() or None for col, out in COLUMNS.items()}
    for field in ("brands", "quantity"):
        if product.get(field):
            product[field] = unescape_text(product[field])
    product.update({out: parse_float(row.get(col) or "") for col, out in FLOAT_COLUMNS.items()})
    product.update({out: parse_nova(row.get(col) or "") for col, out in INT_COLUMNS.items()})
    product["additives"] = parse_additives(row.get("additives_tags") or "")
    vegetarian, vegan, palm = parse_analysis(row.get("ingredients_analysis_tags") or "")
    product["vegetarianStatus"] = vegetarian
    product["veganStatus"] = vegan
    product["palmOil"] = palm
    product["barcode"] = barcode
    product["name"] = name
    return product


def open_export(input_path: str):
    """The export, from a path or from stdin ('-').

    Streaming matters: the file is ~9 GB and a machine with a full disk can
    still process it through a pipe.
    """
    if input_path == "-":
        return gzip.open(sys.stdin.buffer, "rt", encoding="utf-8", errors="replace", newline="")
    opener = gzip.open if input_path.endswith(".gz") else open
    return opener(input_path, "rt", encoding="utf-8", errors="replace", newline="")


def export_rows(fin):
    """Validate the export and expose its brand display values as `brands`.

    OFF's October 2026 tag migration removed the raw `brands` column from
    the export in favour of `brands_en`. Accept either schema, including
    exports that retain an empty legacy column alongside the localized one.
    Normalize before both product construction and neighbour brand filtering.
    """
    reader = csv.DictReader(fin, delimiter="\t", quoting=csv.QUOTE_NONE)
    fields = set(reader.fieldnames or [])
    if "brands_en" in fields:
        fields.add("brands")
    # INT_COLUMNS (nova_group) is deliberately NOT mandatory (HOM-787):
    # optional metadata may be absent, but required product data must exist.
    missing = ({*COLUMNS} | {*FLOAT_COLUMNS}) - fields
    if missing:
        sys.exit(f"input is missing expected columns: {sorted(missing)}")
    for row in reader:
        if not (row.get("brands") or "").strip():
            row["brands"] = row.get("brands_en")
        yield row


def cut(input_path: str, output_dir: Path, country_tag: str, max_rows: int | None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    seed_name = f"seed-{country_tag.split(':')[-1]}.jsonl.gz"
    seed_path = output_dir / seed_name

    kept = scanned = skipped_invalid = 0
    with open_export(input_path) as fin, \
            gzip.open(seed_path, "wt", encoding="utf-8") as fout:
        for row in export_rows(fin):
            scanned += 1
            if max_rows and scanned > max_rows:
                break
            if country_tag not in (row.get("countries_tags") or ""):
                continue
            product = build_product(row)
            if product is None:
                skipped_invalid += 1
                continue
            fout.write(json.dumps(product, ensure_ascii=False, separators=(",", ":")) + "\n")
            kept += 1

    sha256 = hashlib.sha256(seed_path.read_bytes()).hexdigest()
    manifest = {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "country": country_tag,
        "seedFile": seed_name,
        "rowCount": kept,
        "sha256": sha256,
        "attributions": ATTRIBUTIONS,
        "generator": "cut_off_nl.py",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"scanned={scanned} kept={kept} skipped_invalid={skipped_invalid} -> {seed_path}")
    return manifest


def cut_neighbours(
    input_path: str,
    output_dir: Path,
    primary_seed: Path,
    primary_country: str,
    neighbour_tags: list[str],
    min_brand_rows: int,
    brand_denylist: set[str],
    require_nutriments: bool,
    max_rows: int | None,
) -> dict:
    """Products from adjacent markets we do not have, vouched for by a brand
    we already stock (HOM-1186).

    Three filters, each earning its place from the 2026-09-04 measurement:

      * not already ours, and not tagged for the primary country — this is a
        complement, not a re-cut.
      * carries a brand with at least [min_brand_rows] products in the primary
        cut. At a threshold of one the brand-overlap probe returned hundreds of
        thousands of rows of mostly French supermarket own-brand; at twenty the
        complement is 84,628 and the brand list reads like a Dutch shelf.
      * [brand_denylist] removes the banners that survive the threshold on
        mis-tagged rows but do not trade here at all (Carrefour, Delhaize,
        Boni have 40, 31 and 33 rows in the Dutch cut respectively).

    [require_nutriments] keeps only rows the Nutri-Score calculator can
    actually use — 76% of the complement. Turning it off adds the other 20,430,
    which carry a name and a category and so could in principle be graded by the
    generic-food twin (HOM-1174).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seed_path = output_dir / "seed-neighbours.jsonl.gz"

    barcodes, brand_counts = load_primary(primary_seed)
    qualifying = {
        brand for brand, n in brand_counts.items()
        if n >= min_brand_rows and brand not in brand_denylist
    }
    if not qualifying:
        sys.exit(
            f"no brand in {primary_seed} reaches --min-brand-rows {min_brand_rows}; "
            "the complement would be empty"
        )

    kept = scanned = 0
    per_country: Counter = Counter()
    with open_export(input_path) as fin, \
            gzip.open(seed_path, "wt", encoding="utf-8") as fout:
        for row in export_rows(fin):
            scanned += 1
            if max_rows and scanned > max_rows:
                break
            countries = row.get("countries_tags") or ""
            if primary_country in countries:
                continue
            hits = [tag for tag in neighbour_tags if tag in countries]
            if not hits:
                continue
            if (row.get("code") or "").strip() in barcodes:
                continue
            if not (norm_brands(row.get("brands")) & qualifying):
                continue
            if require_nutriments and not all(
                (row.get(col) or "").strip() for col in NUTRISCORE_REQUIRED
            ):
                continue
            product = build_product(row)
            if product is None:
                continue
            fout.write(json.dumps(product, ensure_ascii=False, separators=(",", ":")) + "\n")
            kept += 1
            for tag in hits:
                per_country[tag] += 1

    sha256 = hashlib.sha256(seed_path.read_bytes()).hexdigest()
    manifest = {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "primaryCountry": primary_country,
        "neighbours": neighbour_tags,
        "minBrandRows": min_brand_rows,
        "brandDenylist": sorted(brand_denylist),
        "requireNutriments": require_nutriments,
        "qualifyingBrands": len(qualifying),
        "seedFile": seed_path.name,
        "rowCount": kept,
        "rowsPerCountry": dict(per_country),
        "sha256": sha256,
        "attributions": ATTRIBUTIONS,
        "generator": "cut_off_nl.py --mode neighbours",
    }
    (output_dir / "manifest-neighbours.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"scanned={scanned} kept={kept} brands={len(qualifying)} "
        f"per_country={dict(per_country)} -> {seed_path}"
    )
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="OFF products CSV/TSV (optionally .gz)")
    p.add_argument("--output-dir", default="seed-out")
    p.add_argument("--country", default="en:netherlands", help="countries_tags value to keep")
    p.add_argument("--max-rows", type=int, default=None, help="scan cap, for tests/smoke runs")
    p.add_argument("--min-rows", type=int, default=0, help="fail if fewer rows kept (sanity gate)")
    p.add_argument("--mode", choices=("country", "neighbours"), default="country")
    p.add_argument("--primary-seed", help="neighbours mode: the seed we already ship")
    p.add_argument(
        "--neighbours",
        default="en:germany,en:belgium,en:france",
        help="neighbours mode: comma-separated countries_tags values to draw from",
    )
    p.add_argument(
        "--min-brand-rows", type=int, default=20,
        help="neighbours mode: how many products a brand needs in the primary cut to vouch",
    )
    p.add_argument(
        "--brand-denylist", default="carrefour,delhaize,boni",
        help="neighbours mode: brands to ignore however many rows they have",
    )
    p.add_argument(
        "--include-ungraded", action="store_true",
        help="neighbours mode: keep rows without the four Nutri-Score fields too",
    )
    args = p.parse_args()

    if args.mode == "neighbours":
        if not args.primary_seed:
            sys.exit("--mode neighbours requires --primary-seed")
        manifest = cut_neighbours(
            args.input,
            Path(args.output_dir),
            Path(args.primary_seed),
            args.country,
            [t.strip() for t in args.neighbours.split(",") if t.strip()],
            args.min_brand_rows,
            {b.strip().lower() for b in args.brand_denylist.split(",") if b.strip()},
            not args.include_ungraded,
            args.max_rows,
        )
    else:
        manifest = cut(args.input, Path(args.output_dir), args.country, args.max_rows)
    if manifest["rowCount"] < args.min_rows:
        sys.exit(f"sanity gate failed: kept {manifest['rowCount']} < required {args.min_rows}")


if __name__ == "__main__":
    main()
