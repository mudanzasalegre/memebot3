import importlib.util
import sys
import types
from pathlib import Path


def _load_pumpfun_module():
    sys.modules.setdefault(
        "utils.data_utils",
        types.SimpleNamespace(sanitize_token_data=lambda token: token),
    )
    spec = importlib.util.spec_from_file_location(
        "pumpfun_under_test",
        Path(__file__).resolve().parents[1] / "fetcher" / "pumpfun.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pumpfun = _load_pumpfun_module()


def test_build_ws_url_appends_pumpportal_api_key() -> None:
    url = pumpfun._build_ws_url("wss://pumpportal.fun/api/data", "abc123")

    assert url == "wss://pumpportal.fun/api/data?api-key=abc123"


def test_build_ws_url_keeps_existing_api_key() -> None:
    url = pumpfun._build_ws_url("wss://pumpportal.fun/api/data?api-key=from-url", "from-env")

    assert url == "wss://pumpportal.fun/api/data?api-key=from-url"


def test_resolve_ws_config_disables_default_without_required_key() -> None:
    _, reason = pumpfun._resolve_ws_config(
        base_url="wss://pumpportal.fun/api/data",
        api_key="",
        require_api_key=True,
        enabled=True,
    )

    assert reason is not None
    assert "PUMPPORTAL_API_KEY" in reason


def test_redact_ws_url_hides_api_key() -> None:
    safe = pumpfun._redact_ws_url("wss://pumpportal.fun/api/data?api-key=secret&x=1")

    assert "secret" not in safe
    assert "api-key=%2A%2A%2A" in safe
