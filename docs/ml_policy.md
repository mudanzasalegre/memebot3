# ML Policy

The runtime is lane-aware and conservative by default.

- `ML_GATE_MODE=shadow` never blocks buys.
  At the ML policy boundary, global `shadow` and `off` cannot be upgraded
  to admission/risk vetoes by green-lane or risk flags. Advisory values
  remain diagnostic; this never overrides base strategy, age, liquidity,
  route, capital, provider, execution or live-permission checks.
- In active ML modes, an enforced risk veto remains non-bypassable even
  for explicit PAPER exploration. The green decision reports the same
  generic risk rejection that the native final guard already enforces.
- `ML_GATE_MODE=lane_aware` keeps live profit lanes in `sizing_only` unless a lane report and manual config allow more risk.
- Research and unknown lanes do not buy live unless `ML_ALLOW_RESEARCH_LIVE=true` or `ML_ALLOW_UNKNOWN_LIVE=true`.
- Risk and EV models are optional. Missing `risk_model.pkl` or `ev_model.pkl` returns `None` and does not break entry decisions.
- An enabled active risk veto uses independent checked-risk readiness, not
  the unrelated primary-entry `activation_ready` flag. The standalone
  policy keyword `risk_prediction_ready` defaults to `False`; only the
  native checked reader supplies it. A probability scalar or a truthy
  readiness value is not sufficient. This does not activate any model.
- Every risk-family head and configured compatibility risk requires
  `calibrated_binary_probability` through the existing financial, temporal,
  calibration, token-cluster, schema, input and provenance checks.
  An in-range percentage-point regression is not a probability. Generic
  regression readers remain available for their legitimate workflows.
  Invalid/nonfinite/out-of-range/Boolean risk remains unknown, never a
  fabricated healthy zero or an enforced risk veto.

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
