import ast
from itertools import product
from pathlib import Path

import pytest

import mailroom
from mailroom.models import State
from mailroom.state_machine import can_transition

# Written out by hand so a mistaken edit to ALLOWED cannot also change the expectation.
EXPECTED = {
    (State.RECEIVED, State.QUEUED),
    (State.QUEUED, State.PROCESSING),
    (State.PROCESSING, State.READY_TO_FILE),
    (State.PROCESSING, State.NEEDS_REVIEW),
    (State.PROCESSING, State.UNMATCHED),
    (State.PROCESSING, State.FAILED),
    (State.NEEDS_REVIEW, State.READY_TO_FILE),
    (State.NEEDS_REVIEW, State.UNMATCHED),
    (State.UNMATCHED, State.READY_TO_FILE),
    (State.READY_TO_FILE, State.FILED),
    (State.READY_TO_FILE, State.FAILED),
    (State.FAILED, State.QUEUED),
    (State.FAILED, State.READY_TO_FILE),
}


@pytest.mark.parametrize(("from_state", "to_state"), product(State, State))
def test_every_state_pair(from_state, to_state):
    assert can_transition(from_state, to_state) is ((from_state, to_state) in EXPECTED)


def test_filed_is_terminal():
    assert not any(can_transition(State.FILED, to_state) for to_state in State)


def _writes_a_row(call: ast.Call) -> bool:
    """Document(...) or .values(...). Response models may report state freely."""
    function = call.func
    if isinstance(function, ast.Name):
        return function.id == "Document"
    return isinstance(function, ast.Attribute) and function.attr == "values"


def _state_writes(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in ("state", "stage"):
                    lines.append(node.lineno)
        if isinstance(node, ast.Call) and _writes_a_row(node):
            for keyword in node.keywords:
                if keyword.arg == "state":
                    lines.append(node.lineno)
    return lines


def test_only_the_state_machine_writes_state():
    package = Path(mailroom.__file__).parent
    offenders = {}
    for path in package.rglob("*.py"):
        if path.name == "state_machine.py":
            continue
        lines = _state_writes(ast.parse(path.read_text()))
        if lines:
            offenders[path.name] = lines
    assert offenders == {}
