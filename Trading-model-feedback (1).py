"""
Meta_Raptor v6.0 Dynamic Universe Builder
Screens Alpaca universe and blocks leveraged ETFs at source
"""
import logging
import os
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import RaptorConfig
from universe import is_leveraged_etf

logger = logging.getLogger("raptor.universe")

SLOW_SECTORS = {"Utilities"}

EXCLUSIONS = {
    "BRK.A", "BRK.B",
}

class UniverseBuilder:
    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self._cache: Optional[Tuple[str, List[Dict]]] = None
        self._cache_dir = os.path.join("cache", "universe")
        os.makedirs(self._cache_dir, exist_ok=True)

    def _get_tradeable_assets(self) -> List[Dict]:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        client = TradingClient(
            apiHere are **clean rewrites** with leveraged-ETF blocking built in at the source. Copy each block into Notepad, save over your old files, then delete the universe cache so it rebuilds without SOXL/SOXS/SQQQ.

**After saving:**