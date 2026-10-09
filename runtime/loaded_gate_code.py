"""Bounded loaded callable/source agreement, not whole-process attestation.

Compile source bytes without executing them. Check the actual namespace and
code object rather than trusting a callable's copied name or __wrapped__.
No operator inputs/default objects, credentials or absolute paths are exported.
"""
from __future__ import annotations

import ast
from collections import OrderedDict
import contextlib
import hashlib
import importlib
import json
from functools import lru_cache
from pathlib import Path
import re
import sys
from types import CodeType, FunctionType, MappingProxyType

VERSION = "loaded_entry_gate_bindings_v1"
ENTRY = MappingProxyType({
    "rank_canary": ("analytics/research_rank_canary.py", "evaluate_research_rank_canary"),
    "sniper_subprofile": ("analytics/sniper_research_subprofiles.py", "evaluate_sniper_research_subprofile"),
    "late_momentum": ("analytics/late_momentum_watch.py", "evaluate_late_momentum_watch"),
    "moonshot": ("analytics/moonshot_micro_lottery.py", "evaluate_moonshot_micro_lottery"),
})
CORE = MappingProxyType({
    "runtime/paper_entry_policy.py": ("entry_config", "parameter_scope", "composition_scope", "baseline_scope"),
    "research_loop/entry_gate_policy.py": ("profile_decision", "gate_decision", "_gate_decision", "load_selection", "compare_cohort", "incumbent_profile"),
    "research_loop/entry_gate_forward.py": ("capture_gate",),
})
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_<][A-Za-z0-9_<>]*)*\Z")
QUALNAME = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*|<lambda>)(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|<locals>|<lambda>))*\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
_CODE_CACHE: OrderedDict[int, tuple[CodeType, str]] = OrderedDict()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _constant(value):
    if isinstance(value, CodeType):
        return {"code": _code(value)}
    if value is None or value is Ellipsis:
        return {"type": type(value).__name__}
    if type(value) in (bool, int, str):
        return {"type": type(value).__name__, "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if type(value) in (float, complex):
        # Repr preserves nonfinite constants without emitting non-JSON numbers.
        return {"type": type(value).__name__, "value": repr(value)}
    if isinstance(value, (tuple, frozenset)):
        parts = [_constant(item) for item in value]
        if isinstance(value, frozenset):
            parts.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"type": type(value).__name__, "items": parts}
    raise ValueError("unsupported code constant")


def _code(code):
    # Deliberately exclude filename/line tables: identical LF/CRLF sources in
    # two roots must agree. Exception handlers, closures and nested code count.
    return {"name": code.co_name, "qualname": code.co_qualname,
        "argcount": code.co_argcount, "posonly": code.co_posonlyargcount,
        "kwonly": code.co_kwonlyargcount, "flags": code.co_flags,
        "nlocals": code.co_nlocals, "stacksize": code.co_stacksize,
        "bytecode": code.co_code.hex(), "exceptions": code.co_exceptiontable.hex(),
        "names": code.co_names, "varnames": code.co_varnames,
        "freevars": code.co_freevars, "cellvars": code.co_cellvars,
        "constants": [_constant(item) for item in code.co_consts]}


def code_digest(code):
    if not isinstance(code, CodeType):
        raise ValueError("Python code object required")
    # Code objects are immutable. Cache by object identity with a strong
    # reference, not equality, filename, mtime or a mutable function object.
    cached = _CODE_CACHE.get(id(code))
    if cached is not None and cached[0] is code:
        return cached[1]
    result = _digest(_code(code))
    _CODE_CACHE[id(code)] = (code, result)
    while len(_CODE_CACHE) > 4096:
        _CODE_CACHE.popitem(last=False)
    return result


@lru_cache(maxsize=128)
def _compiled(relative, content, optimize):
    # Cache by complete normalized bytes, never mtime or a caller's digest.
    tree = ast.parse(content, filename=relative)
    own = tuple(node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    imports = tuple((alias.asname or alias.name, node.module.replace(".", "/") + ".py", alias.name)
        for node in tree.body if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
        for alias in node.names)
    index = {}
    def collect(code):
        index.setdefault(code.co_qualname, set()).add(code_digest(code))
        for value in code.co_consts:
            if isinstance(value, CodeType):
                collect(value)
    collect(compile(content, relative, "exec", dont_inherit=True, optimize=optimize))
    return own, index, imports


def _module(relative):
    return importlib.import_module(relative[:-3].replace("/", "."))


def _owner(function):
    module = function.__module__
    if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module):
        raise ValueError("unproved callable module")
    return module.replace(".", "/") + ".py"


def _contextmanager(function):
    """Recognize the exact standard wrapper, not arbitrary __wrapped__ chains."""
    if function.__globals__ is not vars(contextlib):
        return function, None
    content = Path(contextlib.__file__).read_bytes().replace(b"\r\n", b"\n")
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("oversized runtime wrapper source")
    _, expected, _ = _compiled("stdlib/contextlib.py", content, sys.flags.optimize)
    digest = code_digest(function.__code__)
    if (function.__code__.co_qualname != "contextmanager.<locals>.helper"
            or digest not in expected.get("contextmanager.<locals>.helper", set())
            or function.__code__.co_freevars != ("func",)
            or not function.__closure__ or len(function.__closure__) != 1):
        raise ValueError("unproved context-manager wrapper")
    original = function.__closure__[0].cell_contents
    if not isinstance(original, FunctionType) or getattr(function, "__wrapped__", None) is not original:
        raise ValueError("inconsistent runtime wrapper target")
    return original, {"kind": "stdlib_contextmanager", "code_sha256": digest,
        "source_sha256": hashlib.sha256(content).hexdigest()}


def snapshot(gate, *, root, sources, leaves):
    allowed = {row["path"] for row in sources}
    indexes = {}
    for relative in allowed:
        content = (root / relative).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(content).hexdigest() != next(row["sha256"] for row in sources if row["path"] == relative):
            raise ValueError("source changed during loaded-code capture")
        indexes[relative] = _compiled(relative, content, sys.flags.optimize)
    expected_aliases = {relative: {alias for alias, owner, original in value[2]
        if owner in indexes and original in indexes[owner][0]} for relative, value in indexes.items()}
    records, visited = [], set()
    def check(binding_path, name, function):
        if not isinstance(function, FunctionType):
            raise ValueError("declared callable replaced by an unproved object")
        binding = function
        function, wrapper = _contextmanager(function)
        owner = _owner(function)
        if owner not in allowed:
            raise ValueError("loaded callable has an undeclared owner")
        module = sys.modules.get(function.__module__)
        if module is None or function.__globals__ is not vars(module):
            raise ValueError("foreign callable globals")
        if (".__closure__." not in name and function.__code__.co_qualname in indexes[owner][0]
                and vars(module).get(function.__code__.co_qualname) is not binding):
            raise ValueError("stale imported callable export")
        digest = code_digest(function.__code__)
        if digest not in indexes[owner][1].get(function.__code__.co_qualname, set()):
            raise ValueError("loaded callable disagrees with original source")
        key = (binding_path, name)
        if key in visited:
            return
        visited.add(key)
        records.append({"path": binding_path, "name": name, "owner": owner,
            "qualname": function.__code__.co_qualname, "code_sha256": digest, "wrapper": wrapper})
        if len(records) > 256:
            raise ValueError("loaded callable graph exceeds bounded scope")
        # Direct globals aliases are the actual values read by this code.
        for alias in function.__code__.co_names:
            value = function.__globals__.get(alias)
            if alias in indexes[owner][0] or alias in expected_aliases[owner]:
                check(owner, alias, value)
            elif isinstance(value, FunctionType) and _owner(value) in allowed:
                check(owner, alias, value)
        for freevar, cell in zip(function.__code__.co_freevars, function.__closure__ or ()):
            value = cell.cell_contents
            if isinstance(value, FunctionType):
                check(owner, name + ".__closure__." + freevar, value)
    for relative in leaves[gate]:
        module = _module(relative)
        for name in indexes[relative][0]:
            check(relative, name, vars(module).get(name))
        for name in sorted(expected_aliases[relative]):
            check(relative, name, vars(module).get(name))
        # Include imported functions from the declared common dependencies,
        # even if referenced only from nested comprehensions/code objects.
        for name, value in tuple(vars(module).items()):
            if isinstance(value, FunctionType) and _owner(value) in allowed:
                check(relative, name, value)
    for relative, names in CORE.items():
        module = _module(relative)
        for name in names:
            check(relative, name, vars(module).get(name))
    records.sort(key=lambda item: (item["path"], item["name"]))
    value = {"version": VERSION, "gate": gate,
        "runtime": {"implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag,
                    "optimize": sys.flags.optimize}, "bindings": records}
    return {**value, "sha256": _digest(value)}


def valid(value, *, gate, allowed):
    try:
        if (not isinstance(value, dict) or set(value) != {"version", "gate", "runtime", "bindings", "sha256"}
                or value["version"] != VERSION or value["gate"] != gate):
            return False
        runtime = value["runtime"]
        if (not isinstance(runtime, dict) or set(runtime) != {"implementation", "cache_tag", "optimize"}
                or runtime["implementation"] != "cpython" or not isinstance(runtime["cache_tag"], str)
                or not re.fullmatch(r"cpython-[0-9]+", runtime["cache_tag"])
                or type(runtime["optimize"]) is not int or not 0 <= runtime["optimize"] <= 2):
            return False
        rows = value["bindings"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
            return False
        identities = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"path", "name", "owner", "qualname", "code_sha256", "wrapper"}
                    or row["path"] not in allowed or row["owner"] not in allowed
                    or not isinstance(row["name"], str) or not NAME.fullmatch(row["name"])
                    or not isinstance(row["qualname"], str) or not QUALNAME.fullmatch(row["qualname"])
                    or not isinstance(row["code_sha256"], str) or not SHA.fullmatch(row["code_sha256"])):
                return False
            wrapper = row["wrapper"]
            if wrapper is not None and (not isinstance(wrapper, dict)
                    or set(wrapper) != {"kind", "code_sha256", "source_sha256"}
                    or wrapper["kind"] != "stdlib_contextmanager"
                    or any(not isinstance(wrapper[key], str) or not SHA.fullmatch(wrapper[key])
                        for key in ("code_sha256", "source_sha256"))):
                return False
            identities.append((row["path"], row["name"]))
        required = {(relative, name) for relative, names in CORE.items() for name in names}
        required.add(ENTRY[gate])
        return (required <= set(identities) and identities == sorted(set(identities)) and isinstance(value["sha256"], str)
            and value["sha256"] == _digest({key: value[key] for key in ("version", "gate", "runtime", "bindings")}))
    except (ValueError, TypeError, KeyError):
        return False
