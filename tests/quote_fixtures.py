"""Complete synthetic receipts; never provider or profitability evidence."""
from datetime import datetime, timezone
from fetcher import jupiter_router as router

SOL = router.SOL_MINT
TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def v1_quote(source, target, amount, output, *, now=None, impact_bps=20):
    slip = router.routing_quote_slippage_bps()
    body = {"inputMint": source, "outputMint": target, "inAmount": str(amount), "outAmount": str(output),
        "otherAmountThreshold": str(max(1, int(output * (10000 - slip) // 10000))),
        "swapMode": "ExactIn", "slippageBps": slip, "priceImpactPct": str(impact_bps / 10000),
        "routePlan": [{"swapInfo": {"ammKey": TOKEN, "inputMint": source, "outputMint": target,
            "inAmount": str(amount), "outAmount": str(output)}, "percent": 100}]}
    q = router._checked_quote(body, input_mint=source, output_mint=target, amount=amount, slippage=slip, direct=False)
    q.other["received_at_utc"] = (now or datetime.now(timezone.utc)).isoformat()
    return q


def v2_body(source=SOL, target=TOKEN, amount=100000000, output=1000, *, family="jupiterz", impact=.2):
    slip = router.routing_quote_slippage_bps()
    body = {"mode": "ultra", "router": family, "inputMint": source, "outputMint": target,
        "inAmount": str(amount), "outAmount": str(output), "swapMode": "ExactIn", "slippageBps": slip,
        "otherAmountThreshold": str(max(1, output * (10000 - slip) // 10000)),
        "priceImpact": impact, "transaction": None, "taker": None, "transactionVersion": 0}
    if family == "metis":
        body["routePlan"] = [{"swapInfo": {"ammKey": TOKEN, "inputMint": source, "outputMint": target,
            "inAmount": str(amount), "outAmount": str(output)}, "percent": 100}]
    return body


def v2_quote(source=SOL, target=TOKEN, amount=100000000, output=1000, *, now=None, family="jupiterz", impact=.2):
    return router._checked_v2_quote(v2_body(source, target, amount, output, family=family, impact=impact),
        input_mint=source, output_mint=target, amount=amount, slippage=router.routing_quote_slippage_bps(), now=now)
