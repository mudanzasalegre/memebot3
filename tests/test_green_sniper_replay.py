from __future__ import annotations

from backtest.green_sniper_replay import replay_green_sniper


def test_green_sniper_replay_runs_without_db(tmp_path) -> None:
    report = replay_green_sniper(tmp_path)
    assert report["green_sniper_aggressive"]["trades"] == 0
