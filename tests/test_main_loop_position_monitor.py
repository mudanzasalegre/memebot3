from __future__ import annotations

import ast
from pathlib import Path


def test_main_loop_checks_positions_exactly_once_outside_validation_loop() -> None:
    source = Path("run_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="run_bot.py")
    main_loop = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main_loop"
    )

    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(main_loop):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    calls = [
        node
        for node in ast.walk(main_loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_check_positions"
    ]
    assert len(calls) == 1

    ancestor = parent[calls[0]]
    inside_cycle = False
    while ancestor is not main_loop:
        assert not isinstance(ancestor, (ast.For, ast.AsyncFor, ast.If))
        inside_cycle = inside_cycle or isinstance(ancestor, ast.While)
        ancestor = parent[ancestor]
    assert inside_cycle
