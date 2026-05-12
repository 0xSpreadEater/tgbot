import re

MAX_LEN = 512

_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ()\"\-:_$#@.]*$")
_AND_RE = re.compile(r"\bAND\b")
_WHITESPACE_RE = re.compile(r"\s+")
_LOWER_OR_RE = re.compile(r"\bor\b")


def _parens_balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _quotes_balanced(s: str) -> bool:
    return s.count('"') % 2 == 0


def normalize(raw: str) -> tuple[str, list[str]]:
    """Validate and normalize an X (Twitter) search query.

    Returns (normalized_query, warnings). Raises ValueError on hard rejects.
    """
    if raw is None:
        raise ValueError("query is empty")
    s = raw.strip()
    if not s:
        raise ValueError("query is empty")
    if len(s) > MAX_LEN:
        raise ValueError(f"query too long ({len(s)} chars; max {MAX_LEN})")
    if not _ALLOWED_RE.match(s):
        bad = sorted({ch for ch in s if not _ALLOWED_RE.match(ch)})
        raise ValueError(f"disallowed character(s) in query: {''.join(bad)!r}")
    if not _parens_balanced(s):
        raise ValueError("unbalanced parentheses")
    if not _quotes_balanced(s):
        raise ValueError("unbalanced double quotes")

    s = _AND_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()

    if not s:
        raise ValueError("query is empty after normalization")

    warnings: list[str] = []
    if _LOWER_OR_RE.search(s):
        warnings.append("lowercase 'or' found — did you mean OR?")
    if "-" not in s:
        warnings.append("no excludes (-is:retweet, lang:en, etc.) — expect noise")

    return s, warnings
