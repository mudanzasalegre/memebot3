# Paper Rank Research V1

`config/profiles/paper_rank_research_v1.env` is the recommended paper profile after the 48h baseline.

It keeps live disabled and optimization lock enabled while applying the first strategy adjustments:

- research rank canary enabled in paper only;
- green sniper shadow-first with restricted-buy filters;
- late momentum research-only;
- post-partial protection enabled in paper only;
- model/live promotion disabled.

Profiles are not applied implicitly by filename. Use either:

- `python tools/apply_profile.py --profile paper_rank_research_v1` to merge it into `.env` with a backup;
- `CONFIG_PROFILE=paper_rank_research_v1` to load it explicitly at runtime.

The profile does not contain secrets.
