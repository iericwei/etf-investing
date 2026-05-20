#!/usr/bin/env python3
"""Compatibility entry point for the local market-data service."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing.market_data_service import main

if __name__ == "__main__":
    main()
