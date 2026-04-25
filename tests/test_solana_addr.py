from __future__ import annotations

from utils.solana_addr import is_valid_base58_32, normalize_mint


def test_normalize_mint_keeps_valid_bare_pump_suffix():
    raw = "2m1SVN6Gw6ZmE75xCt6rDLLp3fix5kh71hPCTvKepump"

    assert is_valid_base58_32(raw) is True
    assert normalize_mint(raw) == raw


def test_normalize_mint_strips_delimited_pump_suffix_when_raw_is_invalid():
    cleaned = "2m1SVN6Gw6ZmE75xCt6rDLLp3fix5kh71hPCTvKepump"
    raw = f"{cleaned}_pump"

    assert is_valid_base58_32(raw) is False
    assert normalize_mint(raw) == cleaned


def test_normalize_mint_strips_bare_pump_only_as_invalid_fallback():
    cleaned = "11111111111111111111111111111111"
    raw = f"{cleaned}pump"

    assert is_valid_base58_32(cleaned) is True
    assert is_valid_base58_32(raw) is False
    assert normalize_mint(raw) == cleaned


def test_normalize_mint_rejects_invalid_input():
    assert normalize_mint("not_a_solana_mint") is None
