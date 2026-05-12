from bebop_bot.extractors import (
    KNOWN_BUILDER_HANDLES,
    classify_chain_for_cashtag,
    detect_ape_language,
    detect_backing_event,
    detect_builder_ape_overlap,
    detect_composition_language,
    detect_deploy_language,
    detect_fair_launch_language,
    extract_builder_handles,
    extract_cashtags,
    extract_dictionary_phrases,
    extract_evm_addresses,
    extract_handles,
    extract_solana_addresses,
)


def test_extract_cashtags():
    assert extract_cashtags("loving $PEPE and $wif here") == ["PEPE", "WIF"]
    assert extract_cashtags("price is $100") == []
    assert extract_cashtags("") == []


def test_extract_cashtags_dedupe_case():
    # Same symbol mentioned twice — list may include duplicates which is fine
    out = extract_cashtags("$apyx and $APYX")
    assert "APYX" in out


def test_extract_evm_addresses_lowercase():
    addr = "0x" + "Aa" * 20
    assert extract_evm_addresses(f"hey {addr} here") == [addr.lower()]


def test_extract_solana_addresses():
    # Realistic-shaped Solana address (mixed case base58)
    sol = "So11111111111111111111111111111111111111112"
    out = extract_solana_addresses(f"swap {sol} on raydium")
    assert sol in out
    # Long digit run alone is rejected
    assert extract_solana_addresses("1234567890" * 4) == []


def test_extract_dictionary_phrases_word_boundary():
    dictionary = [
        {"term": "PT loop", "weight": 1.0, "display_name": "PT loop"},
        {"term": "v4 hook", "weight": 1.0, "display_name": "v4 hook"},
    ]
    # case-insensitive match with word boundary
    found = extract_dictionary_phrases(
        "deposited pt loop on morpho then v4 hook", dictionary,
    )
    terms = {t for t, _ in found}
    assert "PT loop" in terms
    assert "v4 hook" in terms


def test_extract_dictionary_phrases_no_substring_false_positive():
    dictionary = [{"term": "PT loop", "weight": 1.0, "display_name": "PT loop"}]
    # 'carpet looped' should NOT match 'PT loop'
    found = extract_dictionary_phrases("carpet looped here", dictionary)
    assert found == []


def test_extract_dictionary_phrases_string_list():
    found = extract_dictionary_phrases("fair launch incoming", ["fair launch"])
    assert any(t == "fair launch" for t, _ in found)


def test_extract_dictionary_phrases_punctuation_term():
    """Terms with non-word chars (e.g. '(3,3)') match as substring."""
    dictionary = [{"term": "(3,3)", "weight": 1.0, "display_name": "(3,3)"}]
    found = extract_dictionary_phrases("OHM (3,3) is back", dictionary)
    assert any(t == "(3,3)" for t, _ in found)


def test_classify_chain_for_cashtag_with_evm():
    addr = "0x" + "f" * 40
    chain = classify_chain_for_cashtag(f"check {addr} on base chain", "PEPE")
    assert chain in ("base", "ethereum")


def test_classify_chain_for_cashtag_solana_keyword():
    assert classify_chain_for_cashtag("aping $WIF on pump.fun", "WIF") == "solana"


def test_classify_chain_unknown():
    assert classify_chain_for_cashtag("no chain clue here", "XYZ") == "unknown"


def test_extract_handles():
    assert extract_handles("hey @ctrl and @Acme") == ["acme", "ctrl"]


def test_extract_builder_handles_with_dynamic_set():
    handles = {"customhandle"}
    found = extract_builder_handles("hey @customhandle and @nobody", handles)
    assert found == ["customhandle"]


def test_extract_builder_handles_module_fallback():
    found = extract_builder_handles("hey @ctrl and @nobody")
    assert "ctrl" in found
    assert "nobody" not in found


def test_detect_composition_language():
    text = "looping PT collateral on Morpho for recursive yield"
    hits = detect_composition_language(text)
    assert any("PT collateral" in h or "recursive" in h for h in hits)


def test_detect_fair_launch_language():
    hits = detect_fair_launch_language("fair launch, no presale, no team allocation")
    assert "fair launch" in hits
    assert "no presale" in hits
    assert "no team allocation" in hits


def test_detect_ape_language():
    assert "aped" in detect_ape_language("aped into this thing")


def test_detect_deploy_language():
    assert "just deployed" in detect_deploy_language("just deployed today")


def test_detect_builder_ape_overlap_positive():
    text = "just deployed the new pool, aping into it now"
    assert detect_builder_ape_overlap(text) is True


def test_detect_builder_ape_overlap_negative():
    assert detect_builder_ape_overlap("just deployed the new pool") is False
    assert detect_builder_ape_overlap("aped into it") is False
    assert detect_builder_ape_overlap("") is False


def test_detect_backing_event_paradigm():
    hits = detect_backing_event("Paradigm seed funding announced today")
    assert hits


def test_detect_backing_event_listing():
    hits = detect_backing_event("just listed on Binance, big news")
    assert hits


def test_detect_backing_event_negative():
    assert detect_backing_event("nothing relevant here") == []


def test_known_builder_handles_lowercase():
    for h in KNOWN_BUILDER_HANDLES:
        assert h == h.lower()
