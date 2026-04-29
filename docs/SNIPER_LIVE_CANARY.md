# Sniper Live Canary

Live canary is off by default.

Enable only with:

```powershell
.\scripts\apply_profile.ps1 sniper_live_canary
python scripts\sniper_quality_gate.py
```

Rules:

- `GREEN_SNIPER_REQUIRE_ROUTE_LIVE=true`
- `GREEN_SNIPER_LIVE_SIZE_SOL=0.01`
- `GREEN_SNIPER_LIVE_MAX_OPEN=1`
- `GREEN_SNIPER_LIVE_MAX_DAILY_BUYS=3`
- `GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL=0.05`

The canary pauses green sniper entries only. It never pauses sells.
