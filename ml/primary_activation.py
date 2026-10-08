"""One atomic selector for immutable primary model/metadata/threshold bundles."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import io
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import joblib

from utils.atomic_json import read_json_strict, write_json_atomic
from ml.entry_probability import supported_entry_model
from ml.financial_targets import supported_financial_training
from features.context_encoding import checked_context_schema
from features.numeric_encoding import checked_numeric_schema
from features.auxiliary_semantics import checked_semantics_schema, AuxiliarySemanticsError

VERSION = "atomic_primary_bundle_v1"
COMPONENTS = {"model.pkl", "model.meta.json", "thresholds.by_lane.json", "threshold.json", "acceptance.json"}


@contextmanager
def registry_lock(path: Path):
    """OS-owned, nonblocking lock: no TTL can steal a live writer's lock."""
    lock = Path(path).with_name("." + Path(path).name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        if not lock.stat().st_size:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Model registry mutation already active") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_registry(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    value = read_json_strict(path)
    if not isinstance(value, dict):
        raise ValueError("Model registry must be an object; preserve corrupt state")
    return value


def active_epoch(registry: dict, model_path: Path) -> str:
    selection = registry.get("primary_activation")
    if selection is not None:
        if not isinstance(selection, dict) or selection.get("version") != VERSION:
            raise ValueError("Unsupported primary selector")
        revision = selection.get("revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{32}", revision):
            raise ValueError("Invalid primary selector revision")
        return revision
    # A first migration also detects an external change to legacy model bytes.
    pieces = [Path(model_path), Path(model_path).with_suffix(".meta.json")]
    return sha256(b"".join(sha256(path.read_bytes()).digest() if path.exists() else b"missing" for path in pieces)).hexdigest()


def selected_reference(registry_path: Path, models_dir: Path, model_alias: Path):
    registry = read_registry(registry_path)
    selection = registry.get("primary_activation")
    if selection is None:
        return None
    active_epoch(registry, model_alias)
    if Path(selection.get("model_alias", "")).resolve() != Path(model_alias).resolve():
        raise ValueError("Primary selector does not match configured model path")
    reference = selection.get("active")
    paths = reference_paths(reference, registry_path, models_dir)
    return {"reference": deepcopy(reference), "paths": paths, "revision": selection["revision"]}


def reference_paths(reference: dict, registry_path: Path, models_dir: Path) -> dict[str, Path]:
    if not isinstance(reference, dict) or not isinstance(reference.get("bundle_path"), str):
        raise ValueError("Missing primary bundle reference")
    relative = Path(reference["bundle_path"])
    if relative.is_absolute():
        raise ValueError("Primary bundle must have a relative path")
    directory = (Path(registry_path).parent / relative).resolve()
    if not directory.is_relative_to(Path(models_dir).resolve()) or not directory.is_dir():
        raise ValueError("Primary bundle outside model store or absent")
    hashes = reference.get("sha256")
    if (not isinstance(hashes, dict) or set(hashes) != COMPONENTS
            or any(not isinstance(value, str) or len(value) != 64
                   or any(c not in "0123456789abcdef" for c in value) for value in hashes.values())):
        raise ValueError("Incomplete primary bundle hashes")
    paths = {name: directory / name for name in COMPONENTS}
    if any(not path.resolve().is_relative_to(directory) for path in paths.values()):
        raise ValueError("Primary component escapes its bundle")
    return paths


def read_bundle(reference: dict, registry_path: Path, models_dir: Path, *, captured_payloads=None):
    paths = reference_paths(reference, registry_path, models_dir)
    if captured_payloads is None:
        payloads = {name: path.read_bytes() for name, path in paths.items()}
    else:
        if (not isinstance(captured_payloads, dict) or set(captured_payloads) != COMPONENTS
                or any(type(value) is not bytes for value in captured_payloads.values())):
            raise ValueError("Incomplete captured primary bundle bytes")
        payloads = dict(captured_payloads)
    if any(sha256(payloads[name]).hexdigest() != reference["sha256"][name] for name in COMPONENTS):
        raise ValueError("Primary bundle checksum mismatch")
    def invalid_constant(value):
        raise ValueError("Nonfinite bundle JSON")
    documents = {name: json.loads(payloads[name], parse_constant=invalid_constant)
                 for name in COMPONENTS if name != "model.pkl"}
    meta = documents["model.meta.json"]
    from features.builder import ALLOWED_FEATURES
    from ml.primary_champion import supported_approval
    features = meta.get("features") if isinstance(meta, dict) else None
    if (not isinstance(meta, dict) or meta.get("activation_ready") is not True
            or meta.get("artifact_model_id") != reference.get("model_id")
            or not isinstance(features, list) or not features or len(set(features)) != len(features)
            or any(feature not in ALLOWED_FEATURES for feature in features)
            or (meta.get("validation_split") or {}).get("label_availability_purged") is not True
            or sha256(payloads["model.pkl"]).hexdigest() != meta.get("model_sha256")
            or not supported_financial_training(meta, entry=True)
            or not checked_context_schema(meta, meta.get("features") or [])
            or not checked_numeric_schema(meta, meta.get("features") or [])
            or not supported_approval(documents["acceptance.json"], payloads["model.pkl"], payloads["model.meta.json"])
            or documents["threshold.json"] != (meta.get("threshold_result") or {})
            or documents["thresholds.by_lane.json"] != (meta.get("thresholds_by_lane") or {})):
        raise ValueError("Primary bundle approval/threshold/metadata mismatch")
    if not checked_semantics_schema(meta, features):
        raise AuxiliarySemanticsError("Primary bundle auxiliary generation is incompatible")
    model = joblib.load(io.BytesIO(payloads["model.pkl"]))
    if not supported_entry_model(model, meta):
        raise ValueError("Primary bundle model/calibrator mismatch")
    return model, deepcopy(meta), documents, paths


def write_bundle(payloads: dict[str, bytes], *, registry_path: Path, models_dir: Path, model_id: str) -> dict:
    if set(payloads) != COMPONENTS:
        raise ValueError("Incomplete primary bundle")
    root = (Path(models_dir) / ".primary_bundles").resolve()
    if not root.is_relative_to(Path(models_dir).resolve()):
        raise ValueError("Primary bundle root escapes model store")
    directory = root / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in payloads.items():
        path = directory / name
        with path.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    return {"model_id": model_id, "bundle_path": directory.relative_to(Path(registry_path).parent.resolve()).as_posix(),
            "sha256": {name: sha256(value).hexdigest() for name, value in payloads.items()}}


def legacy_archive(model_alias: Path, *, registry_path: Path, models_dir: Path, metrics_dir: Path):
    paths = {"model.pkl": model_alias, "model.meta.json": model_alias.with_suffix(".meta.json"),
             "threshold.json": metrics_dir / "recommended_threshold.json",
             "thresholds.by_lane.json": metrics_dir / "recommended_thresholds.by_lane.json"}
    existing = {name: path.read_bytes() for name, path in paths.items() if path.exists()}
    if not existing:
        return None
    root = (Path(models_dir) / ".legacy_archives").resolve()
    if not root.is_relative_to(Path(models_dir).resolve()):
        raise ValueError("Legacy archive escapes model store")
    directory = root / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in existing.items():
        with (directory / name).open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return directory.resolve().relative_to(Path(registry_path).parent.resolve()).as_posix()


def refresh_legacy_mirrors(reference: dict, *, registry_path: Path, models_dir: Path,
                           model_alias: Path, metrics_dir: Path) -> list[str]:
    """Non-authoritative compatibility exports AFTER the single commit point."""
    paths = reference_paths(reference, registry_path, models_dir)
    targets = {"model.pkl": model_alias, "model.meta.json": model_alias.with_suffix(".meta.json"),
               "threshold.json": metrics_dir / "recommended_threshold.json",
               "thresholds.by_lane.json": metrics_dir / "recommended_thresholds.by_lane.json"}
    errors = []
    for name, target in targets.items():
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            with temporary.open("xb") as handle:
                handle.write(paths[name].read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError:
            errors.append(name)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    errors.append(name + ":temporary_cleanup")
    return errors


def commit_selection(registry: dict, *, registry_path: Path, active: dict, previous: dict | None,
                     model_alias: Path, legacy: str | None = None) -> dict:
    from datetime import datetime, timezone
    selected = {**registry, "active_model_id": active["model_id"],
        "previous_model_id": previous.get("model_id") if previous else None,
        "active_since_utc": datetime.now(timezone.utc).isoformat(), "status": "active",
        "primary_activation": {"version": VERSION, "revision": uuid4().hex,
            "model_alias": str(model_alias.resolve()), "active": deepcopy(active),
            "previous": deepcopy(previous), "legacy_archive": legacy}}
    write_json_atomic(registry_path, selected)
    return selected
