# Sniper Profiles

Apply profiles with:

```powershell
.\scripts\apply_profile.ps1 sniper_paper
.\scripts\apply_profile.ps1 sniper_live_canary
.\scripts\apply_profile.ps1 conservative
```

The profile script backs up `.env` and preserves secret-looking variables such as keys, tokens, RPC URLs and private values.

Profiles:

- `conservative`: disables green sniper and keeps non-pump regimes in shadow.
- `sniper_paper`: enables aggressive paper sniper, hot queue and proxy route/liquidity for dataset.
- `sniper_live_canary`: enables green sniper live canary with route required and small fixed size.
