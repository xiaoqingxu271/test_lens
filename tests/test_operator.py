from __future__ import annotations

import ast

from helpers import make_profile
from mutation.operator import DEFAULT_OPERATORS, OperatorId, generate_mutants
from sandbox.runner import TargetSymbol


def test_operator_ids_are_enumerable():
    assert len(DEFAULT_OPERATORS) == len(OperatorId)
    assert len(set(DEFAULT_OPERATORS)) == len(DEFAULT_OPERATORS)


def test_mutant_generation_is_deterministic():
    profile = make_profile("test_strong.py")
    target = profile.target_symbols[0]

    first = generate_mutants(target.file, target)
    second = generate_mutants(target.file, target)

    assert [mutant.mutant_id for mutant in first] == [
        mutant.mutant_id for mutant in second
    ]
    assert [mutant.mutated_source for mutant in first] == [
        mutant.mutated_source for mutant in second
    ]


def test_mutants_are_limited_to_target_symbol(tmp_path):
    source = (
        "def target(x):\n"
        "    return x + 1\n"
        "\n"
        "def other(x):\n"
        "    return x - 1\n"
    )
    source_path = tmp_path / "module.py"
    source_path.write_text(source, encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "target"
    )
    target = TargetSymbol(
        symbol_id="target",
        name="target",
        file=source_path,
        line_start=function.lineno,
        line_end=function.end_lineno or function.lineno,
    )

    mutants = generate_mutants(source_path, target)

    assert mutants
    assert all(mutant.line in (2, 3) for mutant in mutants)
    assert all("x - 1" in mutant.mutated_source for mutant in mutants)
    assert all("x + 1" not in mutant.mutated_source for mutant in mutants)


def test_unsupported_code_has_no_mutants(tmp_path):
    source = "def f():\n    return 'x'\n"
    source_path = tmp_path / "module.py"
    source_path.write_text(source, encoding="utf-8")
    target = TargetSymbol(
        symbol_id="f",
        name="f",
        file=source_path,
        line_start=1,
        line_end=2,
    )

    assert generate_mutants(source_path, target) == []
