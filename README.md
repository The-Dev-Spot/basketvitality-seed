# Basket Vitality product seed

The Dutch product database used by [Basket Vitality](https://basketvitality.com), and the script that makes it. It's a filtered slice of [Open Food Facts](https://world.openfoodfacts.org).

## Get the data

| Release | What it is |
|---|---|
| [`seed-nl-latest`](https://github.com/The-Dev-Spot/basketvitality-seed/releases/tag/seed-nl-latest) | Rebuilt nightly from the Open Food Facts export. |
| [`seed-nl-app-1.1.7`](https://github.com/The-Dev-Spot/basketvitality-seed/releases/tag/seed-nl-app-1.1.7) | The exact file inside app version 1.1.7, cut on 2026-07-23. |

Each release has `seed-netherlands.jsonl.gz` (gzipped JSON, one product per line) and `manifest.json` (version, row count and SHA-256).

## How the seed differs from Open Food Facts

`scripts/seed/cut_off_nl.py` makes every change:

- It keeps products tagged for the Netherlands.
- It keeps only the fields the app uses, and renames them.
- It drops values outside their valid range, such as a NOVA group that isn't 1 to 4.

The script's `--mode neighbours` option cuts products from neighbouring markets. The app doesn't currently bundle that output.

## Run it

Requires Python 3.12. There are no other dependencies.

```sh
python3 -m unittest discover -s scripts/seed -p 'test_*.py'
curl -fLO https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz
python3 scripts/seed/cut_off_nl.py --input en.openfoodfacts.org.products.csv.gz --output-dir seed-out
```

## Licences

- **Data:** the seed is a derivative of the Open Food Facts database and is available under the [Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/). Individual contents are under the [Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/). Attribution: © Open Food Facts contributors. See [DATA-LICENSE.txt](DATA-LICENSE.txt).
- **Code:** MIT. See [LICENSE](LICENSE).

The data can be wrong or out of date, because volunteers add it. It isn't medical advice. Basket Vitality isn't affiliated with Open Food Facts.
