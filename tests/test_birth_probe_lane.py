from __future__ import annotations

from ml.data_contract import reconstruct_entry_lane


def test_birth_probe_reconstructs_separately() -> None:
    assert reconstruct_entry_lane({"entry_subtype": "paper_birth_probe"}) == "pump_early_birth_probe"
    assert reconstruct_entry_lane({"gate_profile": "green_sniper_birth_probe"}) == "pump_early_birth_probe"
