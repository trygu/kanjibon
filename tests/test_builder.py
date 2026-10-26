import sqlite3
from pathlib import Path

from kanjibon.builder import _calculate_modern_jlpt, build_sqlite

FIXTURE = Path(__file__).parent / "fixtures" / "sample_kanjidic2.xml"


def test_modern_jlpt_mapping():
    # Legacy JLPT levels (1-4) take priority
    assert _calculate_modern_jlpt(1, 60, 4) == 5
    assert _calculate_modern_jlpt(3, 300, 3) == 4
    assert _calculate_modern_jlpt(5, 700, 2) == 3
    assert _calculate_modern_jlpt(8, 50, 1) == 2

    # No legacy data: grade + frequency estimates
    assert _calculate_modern_jlpt(1, 100, None) == 5
    assert _calculate_modern_jlpt(4, 300, None) == 4
    assert _calculate_modern_jlpt(6, 700, None) == 3
    assert _calculate_modern_jlpt(8, 445, None) == 2  # jouyou with freq
    assert _calculate_modern_jlpt(8, None, None) == 3  # broad N3-to-N1 migration withdrawn
    for literal, level in {"拶": 3, "痩": 3, "戚": 3, "溺": 2, "僅": 2, "籠": 2}.items():
        assert _calculate_modern_jlpt(8, None, None, literal) == level
        assert _calculate_modern_jlpt(8, None, 4, literal) == 5  # legacy evidence wins
    assert _calculate_modern_jlpt(6, None, None) == 3  # primary-school fallback unchanged
    assert _calculate_modern_jlpt(None, None, None) == 1  # non-jouyou
    assert _calculate_modern_jlpt(9, 2000, None) == 1  # jinmeiyou is not jouyou


def test_build_sqlite(tmp_path: Path):
    db = tmp_path / "k2.sqlite"
    n = build_sqlite(FIXTURE, db, batch_size=10)
    assert n == 5
    con = sqlite3.connect(db)

    # Test main kanji table
    cur = con.execute("select literal, grade, stroke_count, freq, jlpt from kanji")
    rows = {row[0]: row for row in cur.fetchall()}
    assert rows["水"] == ("水", 1, 4, 60, 5)  # Legacy JLPT 4 -> N5
    assert rows["逢"][2] == 10  # First stroke count wins, miscounts ignored
    assert rows["韓"][4] == 2  # Jouyou with freq, no legacy JLPT -> N2
    assert rows["柿"][4] == 3  # existing fallback retained
    assert rows["逢"][4] == 1  # Non-jouyou -> N1

    # Test quiz app contract views exist
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
    views = [row[0] for row in cur.fetchall()]
    assert "kanji_seed" in views
    assert "distractor_pool" in views

    # Test kanji_seed view format - water should be N5 so lvl=5
    cur = con.execute(
        "SELECT literal, lvl, main_meaning, on_prime, kun_prime FROM kanji_seed WHERE literal='水'"
    )
    seed_row = cur.fetchone()
    assert seed_row[0] == "水"  # literal
    assert seed_row[1] == 5  # lvl: N5 level
    assert seed_row[2] == "water"  # main_meaning
    assert seed_row[3] == "スイ"  # on_prime
    assert seed_row[4] == "みず"  # kun_prime

    # Source order is preserved: first meaning is primary, not
    # alphabetically first ("Japan" sorts before "day")
    cur = con.execute(
        "SELECT main_meaning, on_prime, kun_prime FROM kanji_seed WHERE literal='日'"
    )
    row = cur.fetchone()
    assert row == ("day", "ニチ・ジツ", "ひ")

    # Test distractor_pool view exists and has correct format
    cur = con.execute("SELECT lvl, meaning FROM distractor_pool WHERE meaning='water'")
    distractor_row = cur.fetchone()
    assert distractor_row[0] == 5  # lvl
    assert distractor_row[1] == "water"  # meaning

    # Test readings
    cur = con.execute(
        "select type, reading from kanji_reading where literal='水' order by type"
    )
    rows = cur.fetchall()
    assert rows == [("kun", "みず"), ("on", "スイ")]

    # Nanori are parsed in source order
    cur = con.execute(
        "SELECT reading FROM kanji_nanori WHERE literal='水' ORDER BY seq"
    )
    assert [r[0] for r in cur.fetchall()] == ["み", "みず"]

    # Query codes are parsed; SKIP misclassifications are tagged separately
    cur = con.execute(
        "SELECT qc_type, value FROM kanji_query_code WHERE literal='水' ORDER BY seq"
    )
    assert cur.fetchall() == [
        ("skip", "1-1-3"),
        ("skip_misclass", "4-3-1"),
        ("sh_desc", "0a7.14"),
        ("four_corner", "1290.0"),
    ]

    # Child tables reference kanji(literal)
    for table in (
        "kanji_radical",
        "kanji_reading",
        "kanji_meaning",
        "kanji_variant",
        "kanji_nanori",
        "kanji_query_code",
    ):
        fk = con.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        assert fk and fk[0][2] == "kanji" and fk[0][3] == "literal"

    # Referential integrity holds for the whole build
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []

    # Schema version is stamped after finalization
    (version,) = con.execute("PRAGMA user_version").fetchone()
    assert version == 1003

    con.close()
