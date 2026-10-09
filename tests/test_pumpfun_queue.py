import datetime as dt

import pytest

from fetcher import pumpfun


@pytest.fixture(autouse=True)
def _reset_queue(monkeypatch: pytest.MonkeyPatch):
    async def _already_started() -> None:
        return None

    pumpfun._buffer.clear()
    pumpfun._pending.clear()
    pumpfun._seen.clear()
    monkeypatch.setattr(pumpfun, "_ensure_started", _already_started)
    monkeypatch.setattr(pumpfun, "_BUFFER_MAX", 500)
    monkeypatch.setattr(pumpfun, "_SEEN_TTL_MIN", 60.0)
    yield
    pumpfun._buffer.clear()
    pumpfun._pending.clear()
    pumpfun._seen.clear()


def _token(address: str, created_at: dt.datetime | None = None) -> dict:
    return {
        "address": address,
        "created_at": created_at or pumpfun.utc_now(),
        "discovered_via": "pumpfun",
    }


@pytest.mark.asyncio
async def test_get_latest_pumpfun_preserves_fifo_arrival_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pumpfun, "_LIMIT_RETURN", 3)
    for address in ("A", "B", "C"):
        assert pumpfun._enqueue_event(_token(address)) is True

    batch = await pumpfun.get_latest_pumpfun()

    assert [token["address"] for token in batch] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_consecutive_batches_do_not_repeat_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pumpfun, "_LIMIT_RETURN", 2)
    for address in ("A", "B", "C", "D", "E"):
        assert pumpfun._enqueue_event(_token(address)) is True

    batches = [
        await pumpfun.get_latest_pumpfun(),
        await pumpfun.get_latest_pumpfun(),
        await pumpfun.get_latest_pumpfun(),
        await pumpfun.get_latest_pumpfun(),
    ]

    assert [[token["address"] for token in batch] for batch in batches] == [
        ["A", "B"],
        ["C", "D"],
        ["E"],
        [],
    ]
    assert pumpfun._enqueue_event(_token("A")) is False


@pytest.mark.asyncio
async def test_more_than_limit_is_delivered_across_batches_without_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pumpfun, "_LIMIT_RETURN", 75)
    addresses = [f"mint-{index:03d}" for index in range(200)]
    for address in addresses:
        assert pumpfun._enqueue_event(_token(address)) is True

    delivered = []
    while batch := await pumpfun.get_latest_pumpfun():
        delivered.extend(token["address"] for token in batch)

    assert delivered == addresses
    assert len(delivered) == len(set(delivered)) == 200


def test_deduplication_expires_after_configured_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(pumpfun, "utc_now", lambda: now[0])
    monkeypatch.setattr(pumpfun, "_SEEN_TTL_MIN", 10.0)

    assert pumpfun._enqueue_event(_token("A", now[0])) is True
    assert pumpfun._drain_events(1)[0]["address"] == "A"
    assert pumpfun._enqueue_event(_token("A", now[0])) is False

    now[0] += dt.timedelta(minutes=11)
    assert pumpfun._enqueue_event(_token("A", now[0])) is True
