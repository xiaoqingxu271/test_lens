"""Fixed, enumerable AST mutation operators.

The operator set is intentionally small and deterministic. Only nodes inside the
target symbol are considered. Adding an operator requires a new OperatorId and
an update to the evaluation labels.
"""

from __future__ import annotations

import ast
import copy
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from sandbox.runner import TargetSymbol


class OperatorId(str, Enum):
    AOR_ADD_TO_SUB = "AOR_ADD_TO_SUB"
    AOR_SUB_TO_ADD = "AOR_SUB_TO_ADD"
    ROR_GT_TO_GTE = "ROR_GT_TO_GTE"
    ROR_GTE_TO_GT = "ROR_GTE_TO_GT"
    ROR_LT_TO_LTE = "ROR_LT_TO_LTE"
    ROR_LTE_TO_LT = "ROR_LTE_TO_LT"
    ROR_EQ_TO_NE = "ROR_EQ_TO_NE"
    ROR_NE_TO_EQ = "ROR_NE_TO_EQ"
    COR_AND_TO_OR = "COR_AND_TO_OR"
    COR_OR_TO_AND = "COR_OR_TO_AND"
    CR_NUM_0_TO_1 = "CR_NUM_0_TO_1"
    CR_NUM_1_TO_0 = "CR_NUM_1_TO_0"
    CR_TRUE_TO_FALSE = "CR_TRUE_TO_FALSE"
    CR_FALSE_TO_TRUE = "CR_FALSE_TO_TRUE"


DEFAULT_OPERATORS: tuple[OperatorId, ...] = (
    OperatorId.AOR_ADD_TO_SUB,
    OperatorId.AOR_SUB_TO_ADD,
    OperatorId.ROR_GT_TO_GTE,
    OperatorId.ROR_GTE_TO_GT,
    OperatorId.ROR_LT_TO_LTE,
    OperatorId.ROR_LTE_TO_LT,
    OperatorId.ROR_EQ_TO_NE,
    OperatorId.ROR_NE_TO_EQ,
    OperatorId.COR_AND_TO_OR,
    OperatorId.COR_OR_TO_AND,
    OperatorId.CR_NUM_0_TO_1,
    OperatorId.CR_NUM_1_TO_0,
    OperatorId.CR_TRUE_TO_FALSE,
    OperatorId.CR_FALSE_TO_TRUE,
)


class Mutant(BaseModel):
    mutant_id: str
    operator_id: OperatorId
    file: Path
    line: int
    col: int
    original: str
    replacement: str
    mutated_source: str


def operator_ids() -> list[str]:
    return [operator.value for operator in DEFAULT_OPERATORS]


def _compare(
    node: ast.AST,
    target_op: type[ast.cmpop],
    replacement_op: type[ast.cmpop],
) -> ast.AST | None:
    if not isinstance(node, ast.Compare):
        return None
    for index, op in enumerate(node.ops):
        if isinstance(op, target_op):
            new_ops = list(node.ops)
            new_ops[index] = replacement_op()
            replacement = ast.Compare(
                left=node.left, ops=new_ops, comparators=node.comparators
            )
            return ast.copy_location(replacement, node)
    return None


def _bool_op(
    node: ast.AST,
    target_op: type[ast.boolop],
    replacement_op: type[ast.boolop],
) -> ast.AST | None:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, target_op):
        replacement = ast.BoolOp(op=replacement_op(), values=node.values)
        return ast.copy_location(replacement, node)
    return None


def _constant(
    node: ast.AST,
    predicate,
    value,
) -> ast.AST | None:
    if isinstance(node, ast.Constant) and predicate(node.value):
        replacement = ast.Constant(value=value)
        return ast.copy_location(replacement, node)
    return None


def _transform(node: ast.AST, operator: OperatorId) -> ast.AST | None:
    if operator == OperatorId.AOR_ADD_TO_SUB:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            replacement = ast.BinOp(left=node.left, right=node.right, op=ast.Sub())
            return ast.copy_location(replacement, node)
        return None

    if operator == OperatorId.AOR_SUB_TO_ADD:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            replacement = ast.BinOp(left=node.left, right=node.right, op=ast.Add())
            return ast.copy_location(replacement, node)
        return None

    if operator == OperatorId.ROR_GT_TO_GTE:
        return _compare(node, ast.Gt, ast.GtE)
    if operator == OperatorId.ROR_GTE_TO_GT:
        return _compare(node, ast.GtE, ast.Gt)
    if operator == OperatorId.ROR_LT_TO_LTE:
        return _compare(node, ast.Lt, ast.LtE)
    if operator == OperatorId.ROR_LTE_TO_LT:
        return _compare(node, ast.LtE, ast.Lt)
    if operator == OperatorId.ROR_EQ_TO_NE:
        return _compare(node, ast.Eq, ast.NotEq)
    if operator == OperatorId.ROR_NE_TO_EQ:
        return _compare(node, ast.NotEq, ast.Eq)

    if operator == OperatorId.COR_AND_TO_OR:
        return _bool_op(node, ast.And, ast.Or)
    if operator == OperatorId.COR_OR_TO_AND:
        return _bool_op(node, ast.Or, ast.And)

    if operator == OperatorId.CR_NUM_0_TO_1:
        return _constant(node, lambda value: type(value) is int and value == 0, 1)
    if operator == OperatorId.CR_NUM_1_TO_0:
        return _constant(node, lambda value: type(value) is int and value == 1, 0)
    if operator == OperatorId.CR_TRUE_TO_FALSE:
        return _constant(node, lambda value: type(value) is bool and value is True, False)
    if operator == OperatorId.CR_FALSE_TO_TRUE:
        return _constant(node, lambda value: type(value) is bool and value is False, True)

    return None


def _walk_with_paths(
    node: ast.AST,
    path: tuple[tuple[str, int | None], ...] = (),
):
    yield node, path
    for field, value in ast.iter_fields(node):
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, ast.AST):
                    yield from _walk_with_paths(item, path + ((field, index),))
        elif isinstance(value, ast.AST):
            yield from _walk_with_paths(value, path + ((field, None),))


def _replace_at_path(
    tree: ast.AST,
    path: tuple[tuple[str, int | None], ...],
    replacement: ast.AST,
) -> None:
    parent = tree
    for field, index in path[:-1]:
        value = getattr(parent, field)
        parent = value if index is None else value[index]
    field, index = path[-1]
    if index is None:
        setattr(parent, field, replacement)
    else:
        getattr(parent, field)[index] = replacement


def _find_function(tree: ast.AST, target: TargetSymbol) -> ast.AST | None:
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == target.name
        and target.line_start <= getattr(node, "lineno", -1) <= target.line_end
    ]
    if not candidates:
        return None
    for node in candidates:
        if getattr(node, "lineno", -1) == target.line_start:
            return node
    return candidates[0]


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return type(node).__name__


def generate_mutants(
    source_path: Path,
    target: TargetSymbol,
    *,
    operators: tuple[OperatorId, ...] = DEFAULT_OPERATORS,
    seed: int = 0,
) -> list[Mutant]:
    """Generate one mutant per (operator, matching node) pair.

    `seed` is accepted and recorded for determinism/future use; generation itself
    is order-stable and does not use randomness.
    """
    del seed  # generation order is fixed by operator order and AST walk order
    source_path = Path(source_path)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    function = _find_function(tree, target)
    if function is None:
        raise ValueError(f"target symbol {target.symbol_id!r} not found in {source_path}")

    paths = {id(node): path for node, path in _walk_with_paths(tree)}
    candidates = [
        node
        for node in ast.walk(function)
        if node is not function
        and hasattr(node, "lineno")
        and target.line_start <= node.lineno <= target.line_end
    ]
    candidates.sort(
        key=lambda node: (
            getattr(node, "lineno", 0),
            getattr(node, "col_offset", 0),
            type(node).__name__,
        )
    )

    mutants: list[Mutant] = []
    seen: set[tuple[str, int, int]] = set()
    counter = 0
    for operator in operators:
        for node in candidates:
            key = (
                operator.value,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
            )
            if key in seen:
                continue
            replacement = _transform(copy.deepcopy(node), operator)
            if replacement is None:
                continue
            seen.add(key)
            mutated_tree = copy.deepcopy(tree)
            _replace_at_path(mutated_tree, paths[id(node)], replacement)
            ast.fix_missing_locations(mutated_tree)
            mutants.append(
                Mutant(
                    mutant_id=(
                        f"{operator.value}:{target.symbol_id}:"
                        f"{node.lineno}:{getattr(node, 'col_offset', 0)}:{counter}"
                    ),
                    operator_id=operator,
                    file=source_path,
                    line=node.lineno,
                    col=getattr(node, "col_offset", 0),
                    original=_unparse(node),
                    replacement=_unparse(replacement),
                    mutated_source=ast.unparse(mutated_tree),
                )
            )
            counter += 1
    return mutants
