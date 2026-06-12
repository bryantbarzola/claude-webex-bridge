from config import _parse_space_modes


def test_empty_returns_empty_map():
    assert _parse_space_modes("") == {}
    assert _parse_space_modes("   ") == {}


def test_single_pair():
    assert _parse_space_modes("roomA:yolo") == {"roomA": "yolo"}


def test_multiple_pairs_and_whitespace():
    assert _parse_space_modes(" roomA:yolo , roomB:strict ") == {
        "roomA": "yolo",
        "roomB": "strict",
    }


def test_unknown_mode_falls_back_to_strict():
    # 'safe' (no approval UX) and garbage both downgrade to strict
    assert _parse_space_modes("roomA:safe,roomB:bogus") == {
        "roomA": "strict",
        "roomB": "strict",
    }


def test_malformed_entry_is_skipped():
    # entries without exactly one colon are ignored, valid ones still parsed
    assert _parse_space_modes("noColon,roomB:yolo,a:b:c") == {"roomB": "yolo"}
