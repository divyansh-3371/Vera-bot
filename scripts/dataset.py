"""Load the expanded dataset (run dataset/generate_dataset.py --out dataset/expanded first)."""
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPANDED = ROOT / "dataset" / "expanded"


def load(expanded: Path = EXPANDED):
    def by(folder, key):
        out = {}
        for f in sorted(glob.glob(str(expanded / folder / "*.json"))):
            d = json.load(open(f, encoding="utf-8"))
            out[d[key]] = d
        return out
    cats = by("categories", "slug")
    merchants = by("merchants", "merchant_id")
    customers = by("customers", "customer_id")
    triggers = by("triggers", "id")
    pairs = json.load(open(expanded / "test_pairs.json", encoding="utf-8"))["pairs"]
    return cats, merchants, customers, triggers, pairs
