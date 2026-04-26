# ML Policy

The runtime is lane-aware and conservative by default.

- `ML_GATE_MODE=shadow` never blocks buys.
- `ML_GATE_MODE=lane_aware` keeps live profit lanes in `sizing_only` unless a lane report and manual config allow more risk.
- Research and unknown lanes do not buy live unless `ML_ALLOW_RESEARCH_LIVE=true` or `ML_ALLOW_UNKNOWN_LIVE=true`.
- Risk and EV models are optional. Missing `risk_model.pkl` or `ev_model.pkl` returns `None` and does not break entry decisions.

Default safe profile:

```env
ML_GATE_MODE=lane_aware
ML_LIVE_PROFIT_MODE=sizing_only
ML_RESEARCH_MODE=shadow
ML_ALLOW_RESEARCH_LIVE=false
ML_ALLOW_UNKNOWN_LIVE=false
ML_RISK_VETO_ENABLED=false
ML_SIZING_ENABLED=true
```
