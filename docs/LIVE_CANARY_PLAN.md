# Live Canary Plan

Authoritative promotion protocol: `docs/LIVE_PROMOTION_PROTOCOL.md`.

Live remains conservative: `GREEN_SNIPER_LIVE_SIZE_SOL=0.01`, max one open position, route required, max three daily buys and `0.05 SOL` daily loss cap.

Apply only after paper gates pass:

```powershell
python scripts/apply_profile.py live_canary_safe
python scripts/strategy_quality_gate.py
```

Stop immediately on liquidity crush, provider critical state, or daily loss cap.

Checked-in live profiles are templates only. Runtime live start requires a generated `manual_canary_v1` profile with fresh operator approval.
