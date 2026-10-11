"""USD valuation projections: reported cap is never FDV or supply times price."""
from utils.market_observation import market_number

CAP_ALIASES = ("marketCap", "marketCapUsd", "market_cap", "mcap")
FDV_ALIASES = ("fdv", "fdvUsd", "fullyDilutedValuation", "fullyDilutedMarketCap",
               "fully_diluted_valuation")


def _field(raw, canonical, aliases, *, provider=False):
    # Key presence, not truthiness: measured zero and explicit unknown win.
    for key in aliases if provider else (canonical, *aliases):
        if key in raw:
            return market_number(raw[key], canonical)
    return None


def project_valuation(raw, *, provider=False):
    if not isinstance(raw, dict):
        raw = {}
    return {"market_cap_usd": _field(raw, "market_cap_usd", CAP_ALIASES[:2] if provider else CAP_ALIASES, provider=provider),
            "fdv_usd": _field(raw, "fdv_usd", FDV_ALIASES, provider=provider)}
