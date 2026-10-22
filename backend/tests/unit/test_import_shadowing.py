"""Static guard: a function-local import must not shadow a module-level name.

A local import binds the name for the *entire* function body in Python, not from
its line onwards. Importing `AlertService` halfway down `StrategyRuntimeEngine.tick`
therefore broke the `TradingService` construction near the top with
`UnboundLocalError` — and because the scheduler catches and logs per-tick
exceptions, the app kept running while no strategy ever evaluated.

This test walks the AST instead of relying on coverage: the defect is invisible
to any test that does not execute the affected branch.
"""

from __future__ import annotations

import ast
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _local_import_sites(function: ast.AST) -> dict[str, int]:
    sites: dict[str, int] = {}
    for node in ast.walk(function):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                sites[alias.asname or alias.name.split(".")[0]] = node.lineno
    return sites


def _is_top_level(node: ast.AST, function: ast.AST) -> bool:
    """True when `node` belongs directly to `function`, not a nested scope."""
    for child in ast.iter_child_nodes(function):
        if child is node:
            return True
    return False


def test_no_function_local_import_shadows_a_module_name():
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_names = _module_level_names(tree)
        for function in [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            # Imports nested inside another function are a different scope and
            # cannot affect this one.
            local = _local_import_sites(function)
            for name, line in local.items():
                if name not in module_names:
                    continue
                # A local import at the top of a function that deliberately
                # rebinds is harmless only if nothing above uses the name; that
                # requires flow analysis. Rejecting the pattern outright keeps
                # the rule simple and the failure mode impossible.
                offenders.append(f"{path.relative_to(APP_ROOT)}:{line} {function.name} -> {name}")

    assert not offenders, (
        "function-local imports shadow module-level names (UnboundLocalError risk):\n"
        + "\n".join(offenders)
    )
