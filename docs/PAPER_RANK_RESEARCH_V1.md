# Paper Rank Research V1

`config/profiles/paper_rank_research_v1.env` is the recommended paper profile after the 48h baseline.

It keeps live disabled and optimization lock enabled while applying the first strategy adjustments:

- research rank canary enabled in paper only;
- green sniper shadow-first with restricted-buy filters;
- late momentum research-only;
- post-partial protection enabled in paper only;
- model/live promotion disabled.

Apply only when explicitly needed with `scripts/apply_profile.py paper_rank_research_v1`. The profile does not contain secrets.
