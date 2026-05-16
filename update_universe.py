#!/usr/bin/env python3
"""
Three Masters Bot — Månadsvis Universum-uppdatering

Hämtar aktuella komponenter från:
  1. S&P 500    (Wikipedia)
  2. Nasdaq 100 (Wikipedia)
  3. Russell 1000 (iShares ETF holdings, bästa tillgängliga free source)

Skriver resultatet till universe.txt i bot-mappen.
Körs automatiskt 1:a varje månad via cron (se nedan).

Cron-installation (kör som habil):
    crontab -e
    # Lägg till:
    0 6 1 * * cd /home/habil/three-masters-bot && venv/bin/python update_universe.py >> logs/universe_update.log 2>&1

Manuell körning:
    cd /home/habil/three-masters-bot && venv/bin/python update_universe.py
"""
from __future__ import annotations
import logging
import sys
from datetime import datetime
from pathlib import Path

import requests

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

UNIVERSE_FILE = Path(__file__).parent / "universe.txt"
_TIMEOUT      = 15


def _fetch_sp500() -> list[str]:
    """Hämtar S&P 500-komponenter från Wikipedia."""
    try:
        import pandas as pd
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )
        df = tables[0]
        col = next((c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()), df.columns[0])
        symbols = [str(s).strip().replace(".", "-") for s in df[col].tolist()]
        symbols = [s for s in symbols if s and s.isascii() and len(s) <= 6]
        _log.info("S&P 500: %d symboler hämtade", len(symbols))
        return symbols
    except Exception as e:
        _log.warning("S&P 500 fetch misslyckades: %s", e)
        return []


def _fetch_nasdaq100() -> list[str]:
    """Hämtar Nasdaq 100-komponenter från Wikipedia."""
    try:
        import pandas as pd
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            attrs={"id": "constituents"},
        )
        df = tables[0]
        col = next((c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()), df.columns[0])
        symbols = [str(s).strip().replace(".", "-") for s in df[col].tolist()]
        symbols = [s for s in symbols if s and s.isascii() and len(s) <= 6]
        _log.info("Nasdaq 100: %d symboler hämtade", len(symbols))
        return symbols
    except Exception as e:
        _log.warning("Nasdaq 100 fetch misslyckades: %s", e)
        return []


def _fetch_russell1000() -> list[str]:
    """Hämtar Russell 1000-komponenter via iShares IWB ETF holdings (CSV)."""
    try:
        import pandas as pd
        url = "https://www.ishares.com/us/products/239707/ISHARES-RUSSELL-1000-ETF/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
        r = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        from io import StringIO
        # iShares CSV has a few header rows before the actual data
        lines = r.text.splitlines()
        # Find the header row (contains "Ticker")
        header_idx = next((i for i, l in enumerate(lines) if "Ticker" in l), None)
        if header_idx is None:
            raise ValueError("Ticker-kolumn saknas i iShares CSV")
        df = pd.read_csv(StringIO("\n".join(lines[header_idx:])))
        col = next((c for c in df.columns if "ticker" in c.lower()), None)
        if col is None:
            raise ValueError("Ingen ticker-kolumn hittades")
        symbols = [str(s).strip() for s in df[col].dropna().tolist()]
        symbols = [s for s in symbols if s and s.isascii() and 1 <= len(s) <= 6
                   and s not in ("-", "CASH", "USD")]
        _log.info("Russell 1000 (iShares IWB): %d symboler hämtade", len(symbols))
        return symbols
    except Exception as e:
        _log.warning("Russell 1000 fetch misslyckades (iShares): %s — hoppar över", e)
        return []


def update_universe() -> int:
    """Hämtar alla index-komponenter, deduplicerar och skriver universe.txt.
    Returnerar antalet unika symboler."""
    _log.info("=== Månadsvis universum-uppdatering startar %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))

    sp500   = _fetch_sp500()
    ndx100  = _fetch_nasdaq100()
    russ    = _fetch_russell1000()

    combined = sorted(set(sp500) | set(ndx100) | set(russ))

    # Grundläggande filter: bara rena tickers (inga units, warrants, preferred)
    def _is_clean(s: str) -> bool:
        return (s.isalpha() or ("-" in s and all(p.isalpha() for p in s.split("-", 1))))

    combined = [s for s in combined if _is_clean(s)]

    if not combined:
        _log.error("Inga symboler hämtades — universe.txt uppdaterades EJ")
        return 0

    UNIVERSE_FILE.write_text("\n".join(combined) + "\n")
    _log.info("universe.txt uppdaterad: %d symboler (SP500=%d, NDX100=%d, R1000=%d)",
              len(combined), len(sp500), len(ndx100), len(russ))
    return len(combined)


if __name__ == "__main__":
    n = update_universe()
    if n == 0:
        sys.exit(1)
    print(f"✓ universe.txt uppdaterad med {n} symboler — {datetime.now().strftime('%Y-%m-%d')}")
