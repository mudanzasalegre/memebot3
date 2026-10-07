from config.config import _find_project_root


def test_fresh_checkout_root_does_not_require_secrets_or_runtime_data(tmp_path):
    checkout = tmp_path / "checkout"
    package = checkout / "config"
    package.mkdir(parents=True)
    (checkout / "run_bot.py").write_text("# source marker", encoding="utf-8")
    (package / "config.py").write_text("# source marker", encoding="utf-8")
    assert not (checkout / ".env").exists()
    assert not (checkout / "data").exists()
    assert _find_project_root(package) == checkout


def test_source_checkout_takes_precedence_over_incidental_package_data(tmp_path):
    checkout = tmp_path / "checkout"
    package = checkout / "config"
    (package / "data").mkdir(parents=True)
    (checkout / "run_bot.py").write_text("# source marker", encoding="utf-8")
    (package / "config.py").write_text("# source marker", encoding="utf-8")
    assert _find_project_root(package) == checkout
