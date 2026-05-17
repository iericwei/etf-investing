#!/usr/bin/env python3
"""Compatibility entry point. Real implementation lives in src/etf_investing/web_app.py."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing.web_app import main

if __name__ == "__main__":
    main()
