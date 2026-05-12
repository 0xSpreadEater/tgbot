import pytest

from bebop_bot.query_parser import normalize


def test_basic_passthrough():
    out, warnings = normalize("$bebop (jam OR rfq) -is:retweet")
    assert out == "$bebop (jam OR rfq) -is:retweet"
    assert warnings == []


def test_standalone_AND_becomes_space():
    out, _ = normalize("foo AND bar")
    assert out == "foo bar"


def test_AND_inside_word_preserved():
    out, _ = normalize("BANDS")
    assert out == "BANDS"


def test_whitespace_collapsed():
    out, _ = normalize("  foo    bar    baz  ")
    assert out == "foo bar baz"


def test_unbalanced_parens_rejected():
    with pytest.raises(ValueError, match="unbalanced parentheses"):
        normalize("((unbalanced")


def test_unbalanced_quotes_rejected():
    with pytest.raises(ValueError, match="unbalanced double quotes"):
        normalize('"oops')


def test_empty_rejected():
    with pytest.raises(ValueError, match="empty"):
        normalize("")
    with pytest.raises(ValueError, match="empty"):
        normalize("   ")


def test_too_long_rejected():
    with pytest.raises(ValueError, match="too long"):
        normalize("a" * 513)


def test_disallowed_chars_rejected():
    with pytest.raises(ValueError, match="disallowed character"):
        normalize("foo & bar")
    with pytest.raises(ValueError, match="disallowed character"):
        normalize("foo;bar")


def test_lowercase_or_warning():
    _, warnings = normalize("foo or bar -is:retweet")
    assert any("lowercase 'or'" in w for w in warnings)


def test_no_excludes_warning():
    _, warnings = normalize("foo OR bar")
    assert any("no excludes" in w for w in warnings)


def test_seeded_queries_normalize_cleanly():
    from bebop_bot.seed import SEED_TOPICS
    for name, query in SEED_TOPICS:
        out, warnings = normalize(query)
        assert out, f"topic {name!r} produced empty normalized query"
        assert not any("no excludes" in w for w in warnings), (
            f"topic {name!r} unexpectedly warned about excludes"
        )
