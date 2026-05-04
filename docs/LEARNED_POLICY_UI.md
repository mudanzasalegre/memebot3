# Learned Policy UI

The UI upgrade adds a read-only Policy Center for the local PR-00..PR-36 learned-policy stack.

## Route

- `/policy`

## API endpoints

- `/api/v1/policy/safety`
- `/api/v1/policy/replay`
- `/api/v1/policy/decision-ledger`
- `/api/v1/policy/funnel-attribution`
- `/api/v1/policy/trade-diagnostics`
- `/api/v1/policy/runner-capture`
- `/api/v1/policy/proposals`
- `/api/v1/policy/model-registry`
- `/api/v1/policy/preflight`
- `/api/v1/policy/config-effect-audit`
- `/api/v1/policy/current-baseline`
- `/api/v1/policy/paper-forward`
- `/api/v1/policy/drift`

## Safety Contract

- The page is read-only.
- It does not train or promote models.
- It does not change `.env` or runtime config.
- It does not activate live canary.
- Live remains blocked unless replay, paper forward, provider/route checks and manual approval are satisfied outside the UI.

## Surfaces

- Safety gates and invariants.
- Policy replay comparison.
- Canonical decision ledger tail and action/lane mix.
- Funnel attribution final state and blockers.
- Trade diagnostics and runner capture.
- Model family registry status.
- Candidate strategy proposals.

