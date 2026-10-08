"""Durable first-partial intake. Recovery never submits or invents observations."""
from __future__ import annotations

import copy
import datetime as dt
import time
from pathlib import Path
from types import SimpleNamespace
from analytics import runner_price_policy

from runtime.paper_archive import PaperArchiveError, paper_snapshot, entry_identity, read_closed_trade
from utils.atomic_json import read_json_strict, write_json_atomic

VERSION = "paper_runner_first_partial_source_v1"
_REPAIR_STATE: dict[str, tuple[float, int]] = {}


class RunnerEnrollmentError(RuntimeError):
    pass


def _json_cells(directory: Path) -> list[Path]:
    try:
        return sorted(path for path in directory.iterdir() if path.name.endswith(".json"))
    except FileNotFoundError:
        return []  # Missing first-launch storage, not an unreadable existing source.


def source_metadata(path: Path) -> tuple:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return str(path), None, None


def capture_source(entry: dict, *, captured_at: dt.datetime) -> dict:
    from research_loop import runner_forward as rf
    prefix = paper_snapshot(entry, entry["token_address"])
    prefix.pop("runner_research_source", None)
    payload = {"captured_at": captured_at.isoformat(), "prefix": prefix}
    return {"version": VERSION, **payload, "payload_sha256": rf._hash(payload)}


def validate_source(source: dict) -> tuple[str, dt.datetime]:
    from research_loop import runner_forward as rf
    try:
        if (not isinstance(source, dict) or set(source) != {"version", "captured_at", "prefix", "payload_sha256"}
                or source["version"] != VERSION or not isinstance(source["prefix"], dict)
                or source["payload_sha256"] != rf._hash({"captured_at": source["captured_at"], "prefix": source["prefix"]})):
            raise RunnerEnrollmentError("Corrupt first-partial source")
        prefix, stamp = source["prefix"], rf._time(source["captured_at"])
        opened, first = rf._time(prefix.get("opened_at")), rf._time(prefix.get("first_partial_at"))
        if (stamp is None or opened is None or stamp < opened or prefix.get("dry_run") is not True
                or prefix.get("closed") is True or prefix.get("partial_taken") is not True
                or type(prefix.get("partial_fill_events")) is not int or prefix["partial_fill_events"] != 1
                or (prefix.get("first_partial_at") is not None and first != stamp)):
            raise RunnerEnrollmentError("Not an original first-partial source")
        identity = entry_identity(prefix)
        if identity and prefix.get("buy_signature") != "SIM-" + identity:
            raise RunnerEnrollmentError("First-partial buy lineage conflicts")
        from execution.paper_execution_fx import validate_entry
        if validate_entry(prefix, amount_sol=prefix.get("amount_sol"), not_after=opened):
            from execution.paper_first_partial_cash import reconstruct
            reconstruct(prefix, captured_at=stamp)
        first_exit = prefix.get("first_partial_exit_intent_id")
        if first_exit is not None:
            events = [event for event in prefix.get("exit_fill_events", []) if event.get("intent_id") == first_exit]
            if len(events) != 1:
                raise RunnerEnrollmentError("Missing original first-partial fill receipt")
            event, response = events[0], events[0]["response"]
            if (event.get("qty_before") != prefix.get("entry_qty") or response.get("partial") is not True
                    or response.get("venue") != "paper" or response.get("qty_sold") != prefix.get("realized_qty")
                    or response.get("qty_left") != prefix.get("qty_lamports") or rf._time(response.get("filled_at")) != stamp
                    or response.get("exit_intent_id") != first_exit or response.get("signature") != "SIM-EXIT-" + first_exit
                    or (prefix.get("quantity_basis") == "quoted_raw_spl_units"
                        and response.get("price_source_close") != "jupiter_reverse_quote")):
                raise RunnerEnrollmentError("First-partial fill lineage/accounting conflicts")
        return rf.case_identity(prefix), stamp
    except (PaperArchiveError, KeyError, ValueError, TypeError, OverflowError) as exc:
        raise RunnerEnrollmentError("Unreadable first-partial source") from exc


def _case_matches(expected: dict, actual: dict) -> bool:
    fields = ("version", "case_id", "cohort_id", "cohort_started_at", "cohort_ends_at",
        "registered_at", "baseline_id", "token", "prefix")
    return (isinstance(actual, dict) and all(actual.get(key) == expected.get(key) for key in fields)
        and set(actual.get("arms") or {}) == set(expected["arms"])
        and all(actual["arms"][key].get("parameters") == arm["parameters"] for key, arm in expected["arms"].items()))


def _confirm_receipt(directory: Path, identity: str, source: dict) -> bool:
    path = directory / "enrollment_receipts" / (identity + ".json")
    if not path.exists(): return False
    source_path = directory / "enrollment_sources" / (identity + ".json")
    if not source_path.exists() or read_json_strict(source_path) != source:
        raise RunnerEnrollmentError("Acknowledged original intake is missing or corrupt")
    receipt = read_json_strict(path)
    if (not isinstance(receipt, dict) or receipt.get("version") != VERSION
            or receipt.get("case_id") != identity or receipt.get("source_sha256") != source["payload_sha256"]
            or receipt.get("status") not in {"enrolled", "excluded", "invalid"}):
        raise RunnerEnrollmentError("Enrollment receipt conflicts with its source")
    from research_loop import runner_forward as rf
    paths = [directory / state / (identity + ".json") for state in ("active", "closed", "invalid")
        if (directory / state / (identity + ".json")).exists()]
    if receipt["status"] == "excluded":
        if paths: raise RunnerEnrollmentError("Excluded intake unexpectedly has a research case")
    else:
        _, captured = validate_source(source)
        expected = rf.prepare_partial_case(source["prefix"],
            cfg=SimpleNamespace(PAPER_RUNNER_RESEARCH_ENABLED=True), now=captured)
        if len(paths) != 1 or expected is None:
            raise RunnerEnrollmentError("Acknowledged case is missing or no longer comparable")
        actual = read_json_strict(paths[0])
        if not _case_matches(expected, actual) or actual.get("enrollment_source_sha256") != source["payload_sha256"]:
            raise RunnerEnrollmentError("Acknowledged case evidence conflicts")
    return True


def register_source(source: dict, *, root=None, cfg=None, now=None) -> dict:
    from research_loop import runner_forward as rf
    cfg = rf.CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True:
        return {"status": "disabled", "created": False}
    try:
        identity, captured = validate_source(source)
        stamp = now or rf._now()
        if stamp.tzinfo is None or stamp < captured:
            raise RunnerEnrollmentError("Recovery precedes the actual first partial")
        directory = rf._directory(root)
        intake = directory / "enrollment_sources" / (identity + ".json")
        if intake.exists():
            if read_json_strict(intake) != source:
                raise RunnerEnrollmentError("An original intake cannot be overwritten")
        else: write_json_atomic(intake, source)
        if _confirm_receipt(directory, identity, source):
            receipt = read_json_strict(directory / "enrollment_receipts" / (identity + ".json"))
            return {"status": receipt["status"], "created": False}
        expected = rf.prepare_partial_case(source["prefix"], cfg=cfg, now=captured)
        existing = [directory / state / (identity + ".json") for state in ("active", "closed", "invalid")
            if (directory / state / (identity + ".json")).exists()]
        if len(existing) > 1: raise RunnerEnrollmentError("Multiple states for one enrollment")
        if existing:
            actual = read_json_strict(existing[0])
            if (expected is None or not _case_matches(expected, actual)
                    or actual.get("enrollment_source_sha256") != source["payload_sha256"]):
                raise RunnerEnrollmentError("Existing case conflicts with its original intake")
            status, created = ("invalid" if existing[0].parent.name == "invalid" else "enrolled"), False
        elif expected is None: status, created = "excluded", False
        else:
            from research_loop import entry_gate_forward
            combined = rf.active_tokens(root) | entry_gate_forward.active_tokens(root)
            if (len(list((directory / "active").glob("*.json"))) >= rf.MAX_ACTIVE
                    or (expected["token"] not in combined and len(combined) >= rf.MAX_ACTIVE)):
                rf._write(directory / "coverage_gaps" / f"{expected['cohort_id']}_{identity}.json",
                    {"cohort_id": expected["cohort_id"], "case_id": identity, "reason": "research_capacity_exceeded"})
                return {"status": "pending", "created": False}
            case = copy.deepcopy(expected)
            case["enrollment_source_sha256"] = source["payload_sha256"]
            case["enrolled_at"] = stamp.isoformat()
            case["enrollment_delay_seconds"] = (stamp - captured).total_seconds()
            case["observation_gap_limit_exceeded"] = case["enrollment_delay_seconds"] > 300
            status, state = "enrolled", "active"
            if stamp > rf._time(case["cohort_ends_at"]) + dt.timedelta(hours=rf.MAX_SETTLEMENT_HOURS):
                case["invalid_reason"] = "first_partial_recovery_after_settlement_deadline"
                status, state = "invalid", "invalid"
            rf._write(directory / state / (identity + ".json"), case)
            rf._ACTIVE_INDEX.pop(str(directory), None)
            created = True
        receipt = {"version": VERSION, "case_id": identity, "source_sha256": source["payload_sha256"], "status": status}
        if not _confirm_receipt(directory, identity, source):
            write_json_atomic(directory / "enrollment_receipts" / (identity + ".json"), receipt)
        return {"status": status, "created": created}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, PaperArchiveError) as exc:
        raise RunnerEnrollmentError("First-partial enrollment remains pending") from exc


def repair_sources(*, root: Path, portfolio: dict, cfg, force=False, limit=8) -> dict:
    """Bound reads/retries across current entries, durable intakes and closed cells."""
    from research_loop import runner_forward as rf
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True:
        return {"status": "disabled", "attempted": 0, "failed": 0}
    root, stamp = Path(root).resolve(), time.monotonic()
    key = str(root)
    previous, cursor = _REPAIR_STATE.get(key, (0., 0))
    if not force and stamp - previous < 30:
        return {"status": "throttled", "attempted": 0, "failed": 0}
    directory = rf._directory(root)
    if len(_REPAIR_STATE) >= 32 and key not in _REPAIR_STATE:
        _REPAIR_STATE.pop(next(iter(_REPAIR_STATE)))
    try:
        candidates = [("portfolio", token) for token in portfolio]
        candidates += [("intake", path) for path in _json_cells(directory / "enrollment_sources")]
        candidates += [("archive", path) for path in _json_cells(root / "data" / "paper_closed_trades")]
    except OSError:
        _REPAIR_STATE[key] = (stamp, cursor)
        return {"status": "pending", "attempted": 0, "failed": 1, "source_scan_failed": True}
    examined = attempted = failed = 0
    limit = max(1, min(8, int(limit)))
    while candidates and examined < min(128, len(candidates)) and attempted < limit:
        kind, value = candidates[cursor % len(candidates)]
        cursor += 1
        examined += 1
        counted = False
        try:
            if kind == "portfolio": source = portfolio.get(value, {}).get("runner_research_source")
            elif kind == "intake": source = read_json_strict(value)
            else: source = read_closed_trade(value).get("runner_research_source")
            if source is None: continue
            identity, _ = validate_source(source)
            if kind == "intake" and value.name != identity + ".json":
                raise RunnerEnrollmentError("Intake filename conflicts with its original identity")
            if _confirm_receipt(directory, identity, source): continue
            attempted += 1
            counted = True
            result = register_source(source, root=root, cfg=cfg)
            failed += result["status"] == "pending"
        except (RunnerEnrollmentError, OSError, ValueError, TypeError, KeyError, AttributeError, PaperArchiveError):
            if not counted: attempted += 1
            failed += 1
    _REPAIR_STATE[key] = (stamp, cursor % len(candidates) if candidates else 0)
    return {"status": "pending" if failed else "ok", "attempted": attempted, "failed": failed}


def source_paths(root: Path) -> list[Path]:
    from research_loop import runner_forward as rf
    return ([root / "data" / "paper_portfolio.json"]
        + _json_cells(root / "data" / "paper_closed_trades")
        + _json_cells(rf._directory(root) / "enrollment_sources")
        + _json_cells(rf._directory(root) / "coverage_gaps"))


def _capture_gap_applies(row: dict, cohort: str) -> bool:
    from research_loop import runner_forward as rf
    gap = row.get("runner_research_capture_failed")
    if not gap: return False
    if not isinstance(gap, dict): return True
    stamp = rf._time(gap.get("captured_at"))
    if stamp is None: return True
    policy = runner_price_policy.parse_policy(gap.get("runner_trailing_policy"))
    day = stamp.strftime("%Y%m%d")
    return cohort == day + "_" + rf._policy_id(policy) if policy else cohort.startswith(day + "_")


def population_matches(root: Path, cohort: str, cases: list[dict]) -> bool:
    """Check the entire captured eligible population, not just surviving cases."""
    from research_loop import runner_forward as rf
    try:
        sources, expected = [], {}
        data = root / "data"
        try:
            portfolio = read_json_strict(data / "paper_portfolio.json")
        except FileNotFoundError:
            portfolio = {}
        if not isinstance(portfolio, dict) or any(not isinstance(row, dict) for row in portfolio.values()): return False
        if any(_capture_gap_applies(row, cohort) for row in portfolio.values()): return False
        sources += [row["runner_research_source"] for row in portfolio.values() if "runner_research_source" in row]
        for path in _json_cells(data / "paper_closed_trades"):
            row = read_closed_trade(path)
            if _capture_gap_applies(row, cohort): return False
            if "runner_research_source" in row: sources.append(row["runner_research_source"])
        for path in _json_cells(rf._directory(root) / "enrollment_sources"):
            source = read_json_strict(path)
            identity, _ = validate_source(source)
            if path.name != identity + ".json": return False
            sources.append(source)
        checked_cfg = SimpleNamespace(PAPER_RUNNER_RESEARCH_ENABLED=True)
        for source in sources:
            identity, captured = validate_source(source)
            candidate = rf.prepare_partial_case(source["prefix"], cfg=checked_cfg, now=captured)
            if candidate is None or candidate["cohort_id"] != cohort: continue
            if identity in expected and expected[identity][0] != source: return False
            expected[identity] = (source, candidate)
        if not expected or set(expected) != {case["case_id"] for case in cases}: return False
        for case in cases:
            source, prepared = expected[case["case_id"]]
            if not _case_matches(prepared, case) or case.get("enrollment_source_sha256") != source["payload_sha256"]: return False
        return not any(path.name.startswith(cohort + "_") for path in _json_cells(rf._directory(root) / "coverage_gaps"))
    except (RunnerEnrollmentError, PaperArchiveError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
