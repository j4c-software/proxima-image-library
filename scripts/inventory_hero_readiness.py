#!/usr/bin/env python3
"""Print a JSON dry-run inventory. This command never changes catalog data."""

import json

from src.catalog_inventory import inventory_current_catalog


if __name__ == "__main__":
    print(json.dumps(inventory_current_catalog(), indent=2, ensure_ascii=False))
