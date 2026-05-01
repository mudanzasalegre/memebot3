from __future__ import annotations

from tools.config_effect_audit import build_config_effect_audit


def test_config_effect_audit_detects_refs_and_placebos(tmp_path) -> None:
    (tmp_path / "config" / "profiles").mkdir(parents=True)
    (tmp_path / ".env.example").write_text("USED_FLAG=false\nPLACEBO_FLAG=false\n", encoding="utf-8")
    (tmp_path / "config" / "profiles" / "paper.env").write_text("USED_FLAG=true\nPLACEBO_FLAG=true\n", encoding="utf-8")
    (tmp_path / "module.py").write_text("import os\nprint('USED_FLAG')\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_flags.py").write_text("def test_x(): assert 'USED_FLAG'\n", encoding="utf-8")
    report = build_config_effect_audit(tmp_path)
    assert report["flags"]["USED_FLAG"]["status"] == "active_tested"
    assert report["flags"]["PLACEBO_FLAG"]["status"] == "possible_placebo_enabled"
