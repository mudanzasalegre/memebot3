from __future__ import annotations

import ast
import asyncio
import datetime as dt
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest


def _run_bot_tree() -> ast.Module:
    source = Path("run_bot.py").read_text(encoding="utf-8")
    return ast.parse(source, filename="run_bot.py")


def test_main_loop_starts_running_before_background_research_refresh() -> None:
    tree = _run_bot_tree()
    main_loop = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main_loop"
    )

    running_index = next(
        index
        for index, statement in enumerate(main_loop.body)
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_runtime_process_state"
            for target in statement.targets
        )
        and isinstance(statement.value, ast.Constant)
        and statement.value.value == "running"
    )
    schedule_index = next(
        index
        for index, statement in enumerate(main_loop.body)
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "_schedule_background_task"
    )
    discovery_loop_index = next(
        index for index, statement in enumerate(main_loop.body) if isinstance(statement, ast.While)
    )

    schedule_call = main_loop.body[schedule_index].value
    assert isinstance(schedule_call, ast.Call)
    refresh_call = schedule_call.args[0]
    assert isinstance(refresh_call, ast.Call)
    assert isinstance(refresh_call.func, ast.Name)
    assert refresh_call.func.id == "_refresh_reports_once"
    assert running_index < schedule_index < discovery_loop_index

    discovery_loop = main_loop.body[discovery_loop_index]
    assert isinstance(discovery_loop, ast.While)
    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "fetch_candidate_pairs"
        for node in ast.walk(discovery_loop)
    )

    awaited_initial_refreshes = []
    for node in ast.walk(main_loop):
        if not (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_refresh_reports_once"
        ):
            continue
        source_keyword = next(
            (keyword for keyword in node.value.keywords if keyword.arg == "source"),
            None,
        )
        if (
            source_keyword is not None
            and isinstance(source_keyword.value, ast.Constant)
            and source_keyword.value.value == "research_scorecard_init"
        ):
            awaited_initial_refreshes.append(node)
    assert awaited_initial_refreshes == []


def test_paper_startup_backgrounds_balance_but_live_awaits_it() -> None:
    tree = _run_bot_tree()
    main_loop = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main_loop"
    )
    balance_branch = next(
        node
        for node in main_loop.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "DRY_RUN"
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_schedule_initial_paper_wallet_refresh"
            for child in ast.walk(node)
        )
    )

    paper_schedules = [
        child
        for statement in balance_branch.body
        for child in ast.walk(statement)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "_schedule_initial_paper_wallet_refresh"
    ]
    paper_awaits = [
        child
        for statement in balance_branch.body
        for child in ast.walk(statement)
        if isinstance(child, ast.Await)
    ]
    live_awaits = [
        child
        for statement in balance_branch.orelse
        for child in ast.walk(statement)
        if isinstance(child, ast.Await)
        and isinstance(child.value, ast.Call)
        and isinstance(child.value.func, ast.Name)
        and child.value.func.id == "_load_initial_wallet_balance"
    ]

    assert len(paper_schedules) == 1
    assert paper_awaits == []
    assert len(live_awaits) == 1

    refresh_balance = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_refresh_balance"
    )
    assert any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "_initial_paper_wallet_refresh_task"
        and node.attr == "done"
        for node in ast.walk(refresh_balance)
    )


@pytest.mark.asyncio
async def test_core_report_startup_skips_fresh_but_runs_stale_or_missing() -> None:
    tree = _run_bot_tree()
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_oldest_core_report_timestamp", "_maybe_regenerate_core_reports"}
    ]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    now = dt.datetime(2026, 7, 15, 17, 30, tzinfo=dt.timezone.utc)
    generated: list[Path] = []

    class _Log:
        @staticmethod
        def info(*_args: object) -> None:
            return None

        @staticmethod
        def warning(*_args: object) -> None:
            return None

    namespace = {
        "Optional": Optional,
        "dt": dt,
        "asyncio": asyncio,
        "CFG": SimpleNamespace(
            CORE_REPORTS_AUTO_REGEN_ENABLED=True,
            CORE_REPORTS_REGEN_INTERVAL_MIN=30,
            CORE_REPORTS_REGEN_ON_CLOSES=25,
        ),
        "REQUIRED_CORE_REPORTS": ("a.json", "b.json"),
        "PROJECT_ROOT": Path("root"),
        "_core_reports_regen_lock": asyncio.Lock(),
        "_last_core_reports_regen_at": None,
        "_last_core_reports_regen_sold": 0,
        "_stats": {"sold": 0},
        "utc_now": lambda: now,
        "parse_iso_utc": lambda value: dt.datetime.fromisoformat(value) if value else None,
        "report_freshness": lambda _root: pytest.fail("explicit freshness snapshot expected"),
        "_run_core_reports_regeneration_isolated": None,
        "_note_runtime_error": lambda *_args: None,
        "log": _Log(),
    }
    exec(compile(module, "run_bot.py", "exec"), namespace)
    maybe_regenerate = namespace["_maybe_regenerate_core_reports"]

    async def _regenerate() -> dict[str, object]:
        generated.append(Path("root"))
        return {"warnings": {}}

    namespace["_run_core_reports_regeneration_isolated"] = _regenerate

    def _snapshot(age_minutes: int, *, missing: bool = False) -> dict[str, object]:
        timestamp = (now - dt.timedelta(minutes=age_minutes)).isoformat()
        return {
            "missing": ["a.json"] if missing else [],
            "reports": {
                name: {"exists": True, "mtime_utc": timestamp}
                for name in ("a.json", "b.json")
            },
        }

    await maybe_regenerate(source="startup", force=False, freshness_snapshot=_snapshot(5))
    assert generated == []
    assert namespace["_last_core_reports_regen_at"] == now - dt.timedelta(minutes=5)

    namespace["_last_core_reports_regen_at"] = None
    await maybe_regenerate(source="startup", force=False, freshness_snapshot=_snapshot(31))
    assert generated == [Path("root")]

    generated.clear()
    namespace["_last_core_reports_regen_at"] = None
    await maybe_regenerate(
        source="startup",
        force=False,
        freshness_snapshot=_snapshot(0, missing=True),
    )
    assert generated == [Path("root")]


def test_main_loop_core_report_startup_is_retained_and_not_forced() -> None:
    tree = _run_bot_tree()
    main_loop = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main_loop"
    )
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(main_loop):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    startup_call = next(
        node
        for node in ast.walk(main_loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_maybe_regenerate_core_reports"
        and any(
            keyword.arg == "source"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "startup"
            for keyword in node.keywords
        )
    )
    force = next(keyword.value for keyword in startup_call.keywords if keyword.arg == "force")
    assert isinstance(force, ast.Constant)
    assert force.value is False
    assert any(keyword.arg == "freshness_snapshot" for keyword in startup_call.keywords)

    ancestor = parent[startup_call]
    while not (
        isinstance(ancestor, ast.Call)
        and isinstance(ancestor.func, ast.Name)
        and ancestor.func.id == "_schedule_background_task"
    ):
        ancestor = parent[ancestor]


@pytest.mark.asyncio
async def test_position_notional_repair_never_uses_current_fx_for_missing_history() -> None:
    tree = _run_bot_tree()
    repair = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_repair_position_entry_notionals"
    )
    module = ast.fix_missing_locations(ast.Module(body=[repair], type_ignores=[]))

    class _Column:
        @staticmethod
        def is_not(_value: object) -> object:
            return object()

    class _Position:
        buy_amount_sol = _Column()

    class _Statement:
        def where(self, _predicate: object) -> "_Statement":
            return self

    class _Result:
        def __init__(self, rows: list[SimpleNamespace]) -> None:
            self._rows = rows

        def scalars(self) -> "_Result":
            return self

        def all(self) -> list[SimpleNamespace]:
            return self._rows

    class _Session:
        def __init__(self, rows: list[SimpleNamespace]) -> None:
            self.rows = rows
            self.commits = 0

        async def execute(self, _statement: object) -> _Result:
            return _Result(self.rows)

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            return None

    price_calls = 0

    async def _get_sol_usd() -> float:
        nonlocal price_calls
        price_calls += 1
        return 150.0

    namespace = {
        "SessionLocal": object,
        "Position": _Position,
        "math": math,
        "select": lambda _model: _Statement(),
        "get_sol_usd": _get_sol_usd,
        "_seal_closed_trade_metrics": lambda *_args: None,
        "_refresh_position_trade_metrics": lambda *_args: False,
        "log": SimpleNamespace(info=lambda *_args: None),
    }
    exec(compile(module, "run_bot.py", "exec"), namespace)
    repair_notionals = namespace["_repair_position_entry_notionals"]

    empty_session = _Session([])
    assert await repair_notionals(empty_session) == 0
    assert price_calls == 0

    complete = SimpleNamespace(
        buy_amount_sol=0.1,
        entry_notional_usd=15.0,
        closed=False,
        realized_qty=0,
    )
    complete_session = _Session([complete])
    assert await repair_notionals(complete_session) == 0
    assert price_calls == 0
    assert complete_session.commits == 0

    missing = SimpleNamespace(
        buy_amount_sol=0.1,
        entry_notional_usd=None,
        closed=False,
        realized_qty=0,
    )
    missing_session = _Session([missing])
    assert await repair_notionals(missing_session) == 0
    assert price_calls == 0
    assert missing.entry_notional_usd is None
    assert missing_session.commits == 0

    missing.closed = True
    assert await repair_notionals(missing_session) == 0
    assert missing.entry_notional_usd is None and price_calls == 0
    known_closed = SimpleNamespace(buy_amount_sol=.1, entry_notional_usd=15., closed=True, realized_qty=0)
    known_session = _Session([known_closed])
    assert await repair_notionals(known_session) == 1 and known_session.commits == 1
    assert known_closed.entry_notional_usd == 15. and price_calls == 0


@pytest.mark.asyncio
async def test_background_task_is_retained_and_failure_is_consumed() -> None:
    tree = _run_bot_tree()
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_schedule_background_task"
    )
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    background_tasks: set[asyncio.Task[object]] = set()
    noted_errors: list[tuple[str, str]] = []
    warnings: list[tuple[object, ...]] = []

    class _Log:
        @staticmethod
        def debug(*_args: object) -> None:
            return None

        @staticmethod
        def warning(*args: object) -> None:
            warnings.append(args)

    namespace = {
        "asyncio": asyncio,
        "_background_tasks": background_tasks,
        "_note_runtime_error": lambda context, exc: noted_errors.append((context, str(exc))),
        "log": _Log(),
    }
    exec(compile(module, "run_bot.py", "exec"), namespace)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _failing_refresh() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("refresh failed")

    schedule = namespace["_schedule_background_task"]
    task = schedule(
        _failing_refresh(),
        name="research-scorecard-init",
        error_context="research_scorecard_init",
    )
    await started.wait()
    assert task in background_tasks

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.done()
    assert task not in background_tasks
    assert noted_errors == [("research_scorecard_init", "refresh failed")]
    assert warnings
