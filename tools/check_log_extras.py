#!/usr/bin/env python
"""Static check: forbid LogRecord-reserved keys inside `extra=` dict literals.

Logging fails with KeyError when you pass `extra={"name": ...}` because
`name` is already on the LogRecord (the logger name). This check walks
src/ and tests/ with AST and flags any extra= dict literal that contains
a reserved key.

Exit codes:
  0 = clean
  1 = collisions found
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime",
    }
)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords or []:
            if kw.arg != "extra":
                continue
            value = kw.value
            if not isinstance(value, ast.Dict):
                continue
            for key in value.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in RESERVED_KEYS
                ):
                    violations.append((key.lineno, key.value))
    return violations


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    targets = [root / "src", root / "tests"]
    failed = False
    for target in targets:
        if not target.exists():
            continue
        for py in sorted(target.rglob("*.py")):
            for lineno, key in _scan_file(py):
                rel = py.relative_to(root)
                print(
                    f"{rel}:{lineno}: extra= uses reserved LogRecord key "
                    f"'{key}'",
                    file=sys.stderr,
                )
                failed = True
    if failed:
        print(
            "check_log_extras FAILED: rename the offending keys.",
            file=sys.stderr,
        )
        return 1
    print("check_log_extras OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
