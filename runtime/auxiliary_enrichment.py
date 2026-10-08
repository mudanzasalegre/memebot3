"""Same-snapshot momentum and bounded independent risk heads."""
from __future__ import annotations

import asyncio

from utils.auxiliary_observation import (
    KINDS, observation, checked_auxiliary_observation, trend_observation, accumulation_observation,
)

FIELDS = {"trend": "trend", "early_accumulation": "early_accumulation_sig",
          "rug": "rug_score", "cluster": "cluster_bad"}
RISK_DEADLINE_S = 5.


def prepare_cheap_auxiliary(token: dict) -> None:
    address = str(token.get("address") or "")
    receipts = {kind: observation(kind, address) for kind in KINDS}
    receipts["trend"] = trend_observation(token)
    receipts["early_accumulation"] = accumulation_observation(token)
    receipts = {kind: checked_auxiliary_observation(record, address, kind)
                or observation(kind, address, reason="invalid_original_inputs") for kind, record in receipts.items()}
    token["auxiliary_observations"] = receipts
    for kind, record in receipts.items():
        token[FIELDS[kind]] = record["value"]
    token["trend_fallback_used"] = True  # m5 proxy, never advertised as chart EMA
    token["insider_sig"] = None  # No confirmed insider graph was observed.


async def enrich_entry_risk(token: dict, *, skip=False) -> None:
    from fetcher import rugcheck, helius_cluster
    address = str(token.get("address") or "")
    async def head(kind, fetch):
        record = None
        if not skip:
            try:
                record = await asyncio.wait_for(fetch(address), timeout=RISK_DEADLINE_S)
            except Exception:
                pass
        return kind, (checked_auxiliary_observation(record, address, kind)
                      or observation(kind, address, reason="skipped" if skip else "request_failed_or_unavailable"))
    results = await asyncio.gather(head("rug", rugcheck.fetch_observation),
                                   head("cluster", helius_cluster.fetch_observation))
    for kind, record in results:
        token["auxiliary_observations"][kind] = record
        token[FIELDS[kind]] = record["value"]
