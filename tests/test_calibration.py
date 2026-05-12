"""Tests for the /calibration command surface."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bebop_bot.db import Db, init_db
from bebop_bot.handlers import _parse_calibration_add_payload, cmd_calibration_root


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _make_context(db, args):
    app = SimpleNamespace(bot_data={"db_wrapper": db})
    return SimpleNamespace(application=app, args=list(args))


def _make_update():
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))
    return update, message


async def _run_root(db, args):
    update, message = _make_update()
    ctx = _make_context(db, args)
    # Bypass @restricted by calling the wrapped function directly.
    await cmd_calibration_root.__wrapped__(update, ctx)  # type: ignore[attr-defined]
    return message


# ---------------------------------------------------------------------------
# parse helper
# ---------------------------------------------------------------------------

def test_parse_valid_payload():
    today = date.today().isoformat()
    payload = (
        f"Test case 1 | base | {today} | novel_mechanism | "
        "This is a long enough rationale describing the example."
    )
    name, chain, date_iso, signals, rationale = _parse_calibration_add_payload(payload)
    assert name == "Test case 1"
    assert chain == "base"
    assert date_iso == today
    assert signals == ["novel_mechanism"]
    assert rationale.startswith("This is a long enough rationale")


def test_parse_too_few_fields():
    with pytest.raises(ValueError, match="5 pipe"):
        _parse_calibration_add_payload("Name | base | 2026-01-01 | novel_mechanism")


def test_parse_invalid_date_future():
    future = (date.today() + timedelta(days=10)).isoformat()
    payload = (
        f"Future case | base | {future} | novel_mechanism | "
        "Rationale at least twenty chars long."
    )
    with pytest.raises(ValueError, match="on or before today"):
        _parse_calibration_add_payload(payload)


def test_parse_invalid_date_too_old():
    payload = (
        "Old case | base | 2010-01-01 | novel_mechanism | "
        "Rationale at least twenty chars long."
    )
    with pytest.raises(ValueError, match="on or after 2020"):
        _parse_calibration_add_payload(payload)


def test_parse_invalid_signal():
    payload = (
        "Bad signal | base | 2024-06-01 | market_pump | "
        "Rationale at least twenty chars long."
    )
    with pytest.raises(ValueError, match="Invalid signal"):
        _parse_calibration_add_payload(payload)


def test_parse_short_rationale():
    payload = "Short | base | 2024-06-01 | novel_mechanism | too short"
    with pytest.raises(ValueError, match="RATIONALE"):
        _parse_calibration_add_payload(payload)


def test_parse_signal_dedupe():
    payload = (
        "Dedupe | base | 2024-06-01 | novel_mechanism, novel_mechanism, known_builder | "
        "Rationale at least twenty chars long."
    )
    _, _, _, signals, _ = _parse_calibration_add_payload(payload)
    assert signals == ["novel_mechanism", "known_builder"]


# ---------------------------------------------------------------------------
# Integration tests via the actual handler
# ---------------------------------------------------------------------------

async def test_add_list_remove_lifecycle(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        # add
        today = date.today().isoformat()
        msg = await _run_root(db, [
            "add", "Test", "case", "1", "|", "base", "|", today, "|",
            "novel_mechanism", "|", "Short", "test", "rationale", "20+",
            "chars", "for", "validation.",
        ])
        msg.reply_text.assert_awaited()
        last = msg.reply_text.await_args.args[0]
        assert "Added calibration example" in last
        assert "Test case 1" in last

        # show
        msg2 = await _run_root(db, ["show", "test", "case", "1"])
        body = msg2.reply_text.await_args.args[0]
        assert "Test case 1" in body
        assert "Source: user_added" in body
        assert "novel_mechanism" in body

        # list shows the entry with [user-added] marker
        msg3 = await _run_root(db, ["list"])
        listing = msg3.reply_text.await_args.args[0]
        assert "Test case 1" in listing
        assert "[user-added]" in listing

        # remove without confirm shows preview
        msg4 = await _run_root(db, ["remove", "Test", "case", "1"])
        preview = msg4.reply_text.await_args.args[0]
        assert "Remove calibration example?" in preview

        # entry still present
        present = await db.get_viral_seed_example_by_name("Test case 1")
        assert present is not None

        # remove with confirm deletes
        msg5 = await _run_root(db, ["remove", "Test", "case", "1", "confirm"])
        assert "Removed" in msg5.reply_text.await_args.args[0]
        assert await db.get_viral_seed_example_by_name("Test case 1") is None

        # double-confirm responds with "No example named"
        msg6 = await _run_root(db, ["remove", "Test", "case", "1", "confirm"])
        assert "No example named" in msg6.reply_text.await_args.args[0]
    finally:
        await conn.close()


async def test_add_duplicate_name_rejected(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        # Seed example names are already present; pick the first seeded one.
        seeds = await db.get_viral_seed_examples()
        assert seeds, "expected seeded examples"
        name = seeds[0]["name"]

        today = date.today().isoformat()
        msg = await _run_root(db, [
            "add", name, "|", "base", "|", today, "|", "novel_mechanism", "|",
            "Some", "duplicate", "rationale", "twenty", "chars", "long",
            "blah", "blah", "blah",
        ])
        reply = msg.reply_text.await_args.args[0]
        assert "already exists" in reply
    finally:
        await conn.close()


async def test_add_invalid_signal_rejected(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        msg = await _run_root(db, [
            "add", "BadSig", "|", "base", "|", "2024-01-01", "|",
            "market_pump", "|",
            "Rationale", "long", "enough", "to", "pass", "validation",
            "twenty", "chars", "minimum.",
        ])
        reply = msg.reply_text.await_args.args[0]
        assert "Rejected" in reply
        assert "Invalid signal" in reply
    finally:
        await conn.close()


async def test_add_short_payload_rejected(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        msg = await _run_root(db, ["add", "Only", "|", "two", "fields"])
        reply = msg.reply_text.await_args.args[0]
        assert "Rejected" in reply
        assert "5 pipe" in reply
    finally:
        await conn.close()


async def test_show_unknown_name_returns_friendly_message(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        msg = await _run_root(db, ["show", "nonexistent-example-xyz"])
        reply = msg.reply_text.await_args.args[0]
        assert "No example named" in reply
    finally:
        await conn.close()


async def test_window_computed_from_date(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        msg = await _run_root(db, [
            "add", "WindowTest", "|", "base", "|", "2026-05-01", "|",
            "novel_mechanism", "|", "Rationale", "twenty", "chars", "minimum",
            "padding", "padding", "padding.",
        ])
        assert "Added" in msg.reply_text.await_args.args[0]
        ex = await db.get_viral_seed_example_by_name("WindowTest")
        assert ex is not None
        assert ex["window_start"] == "2026-04-17"
        assert ex["window_end"] == "2026-05-01"
        assert ex["source"] == "user_added"
    finally:
        await conn.close()


async def test_calibration_no_args_shows_usage(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        msg = await _run_root(db, [])
        reply = msg.reply_text.await_args.args[0]
        assert "/calibration" in reply
        assert "Subcommands" in reply
    finally:
        await conn.close()
