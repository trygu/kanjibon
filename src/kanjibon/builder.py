from __future__ import annotations

import sqlite3
from pathlib import Path
from xml.etree import ElementTree as ET


def text(el):
    return el.text.strip() if el is not None and el.text else ""


def _dedup_preserve(items):
    """Remove duplicates while preserving order."""
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA temp_store=MEMORY;
        PRAGMA foreign_keys=ON;  -- validate references during build

        CREATE TABLE IF NOT EXISTS kanji(
            literal TEXT PRIMARY KEY,
            grade INTEGER,
            stroke_count INTEGER,
            freq INTEGER,
            jlpt INTEGER  -- Modern JLPT levels: 1=N1, 2=N2, 3=N3, 4=N4, 5=N5
        );

        CREATE TABLE IF NOT EXISTS kanji_radical(
            literal TEXT NOT NULL REFERENCES kanji(literal),
            rad_value TEXT NOT NULL
        );

        -- seq preserves the KANJIDIC2 source order (most common reading first)
        CREATE TABLE IF NOT EXISTS kanji_reading(
            literal TEXT NOT NULL REFERENCES kanji(literal),
            type TEXT NOT NULL CHECK (type IN ('on','kun')),
            reading TEXT NOT NULL,
            seq INTEGER NOT NULL
        );

        -- seq preserves the KANJIDIC2 source order (primary meaning first)
        CREATE TABLE IF NOT EXISTS kanji_meaning(
            literal TEXT NOT NULL REFERENCES kanji(literal),
            lang TEXT NOT NULL DEFAULT 'en',
            meaning TEXT NOT NULL,
            seq INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kanji_variant(
            literal TEXT NOT NULL REFERENCES kanji(literal),
            var_type TEXT NOT NULL,
            value TEXT NOT NULL
        );

        -- Readings now only associated with names
        CREATE TABLE IF NOT EXISTS kanji_nanori(
            literal TEXT NOT NULL REFERENCES kanji(literal),
            reading TEXT NOT NULL,
            seq INTEGER NOT NULL
        );

        -- Lookup codes: skip, skip_misclass, sh_desc, four_corner, deroo.
        -- SKIP misclassifications (skip_misclass attribute in the XML) are
        -- stored as qc_type 'skip_misclass' to keep real SKIP codes clean.
        CREATE TABLE IF NOT EXISTS kanji_query_code(
            literal TEXT NOT NULL REFERENCES kanji(literal),
            qc_type TEXT NOT NULL,
            value TEXT NOT NULL,
            seq INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_kanji_freq ON kanji(freq);
        CREATE INDEX IF NOT EXISTS idx_reading ON kanji_reading(reading);
        CREATE INDEX IF NOT EXISTS idx_meaning ON kanji_meaning(meaning);
        CREATE INDEX IF NOT EXISTS idx_query_code ON kanji_query_code(qc_type, value);

        -- Unique indexes to prevent duplicates
        CREATE UNIQUE INDEX IF NOT EXISTS ux_reading ON kanji_reading(literal, type, reading);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_meaning ON kanji_meaning(literal, meaning);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_radical ON kanji_radical(literal, rad_value);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_variant ON kanji_variant(literal, var_type, value);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_nanori ON kanji_nanori(literal, reading);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_query_code ON kanji_query_code(literal, qc_type, value);

        -- Performance index for level-filtered app queries (kanji_seed/distractor_pool)
        CREATE INDEX IF NOT EXISTS idx_kanji_seed_lvl ON kanji(jlpt) WHERE jlpt IS NOT NULL;

        -- Priority view: sorted by frequency -> grade -> jlpt with aggregated data
        CREATE VIEW IF NOT EXISTS kanji_priority AS
        SELECT
            k.literal,
            k.grade,
            k.stroke_count,
            k.freq,
            k.jlpt,
            (SELECT m.meaning
             FROM kanji_meaning m
             WHERE m.literal = k.literal AND m.lang = 'en'
             ORDER BY m.seq LIMIT 1) as main_meaning,
            COALESCE((SELECT GROUP_CONCAT(reading, '・')
                     FROM (SELECT reading FROM kanji_reading r
                           WHERE r.literal = k.literal AND r.type = 'on'
                           ORDER BY r.seq)), '') as on_prime,
            COALESCE((SELECT GROUP_CONCAT(reading, '・')
                     FROM (SELECT reading FROM kanji_reading r
                           WHERE r.literal = k.literal AND r.type = 'kun'
                           ORDER BY r.seq)), '') as kun_prime,
            -- Priority score: freq (lower=better), then grade, then jlpt
            CASE
                WHEN k.freq IS NOT NULL THEN k.freq
                WHEN k.grade IS NOT NULL THEN 3000 + k.grade
                WHEN k.jlpt IS NOT NULL THEN 4000 + k.jlpt
                ELSE 9999
            END as priority_score
        FROM kanji k
        ORDER BY priority_score, k.literal;

        -- App contract: kanji_seed view for quiz generation
        CREATE VIEW IF NOT EXISTS kanji_seed AS
        SELECT
            k.literal,
            k.jlpt as lvl,  -- N5=5, N4=4, N3=3, N2=2, N1=1
            k.freq,
            k.grade,
            (SELECT m.meaning
             FROM kanji_meaning m
             WHERE m.literal = k.literal AND m.lang = 'en'
             ORDER BY m.seq LIMIT 1) as main_meaning,
            COALESCE((SELECT GROUP_CONCAT(reading, '・')
                     FROM (SELECT reading FROM kanji_reading r
                           WHERE r.literal = k.literal AND r.type = 'on'
                           ORDER BY r.seq)), '') as on_prime,
            COALESCE((SELECT GROUP_CONCAT(reading, '・')
                     FROM (SELECT reading FROM kanji_reading r
                           WHERE r.literal = k.literal AND r.type = 'kun'
                           ORDER BY r.seq)), '') as kun_prime
        FROM kanji k
        WHERE k.jlpt IS NOT NULL
        AND EXISTS (SELECT 1 FROM kanji_meaning m WHERE m.literal = k.literal AND m.lang = 'en');

        -- App contract: distractor pool for quiz generation
        CREATE VIEW IF NOT EXISTS distractor_pool AS
        SELECT
            k.jlpt as lvl,  -- N5=5, N4=4, N3=3, N2=2, N1=1
            m.meaning
        FROM kanji k
        JOIN kanji_meaning m ON k.literal = m.literal
        WHERE k.jlpt IS NOT NULL AND m.lang = 'en';
        """)
    conn.commit()


def _finalize_db(conn: sqlite3.Connection):
    """Compact, analyze and stamp the database after all data is inserted.

    Running VACUUM/ANALYZE at schema-creation time (i.e. on an empty
    database) is pointless; doing it here gives the query planner real
    statistics and ships a compact single-file artifact.
    """
    conn.executescript("""
        VACUUM;
        ANALYZE;
        PRAGMA wal_checkpoint(TRUNCATE);
        -- Ship in rollback-journal mode: a WAL-mode database needs
        -- -wal/-shm sidecar files at open time, which breaks read-only
        -- consumers (e.g. the DB bundled inside the oboeru wheel).
        PRAGMA journal_mode=DELETE;
        -- Schema version stamp (bump when tables/views change)
        PRAGMA user_version=1003;
        """)


def _create_empty_record():
    """Create an empty kanji record structure."""
    return {
        "literal": "",
        "grade": None,
        "stroke_count": None,
        "freq": None,
        "jlpt": None,
        "radicals": [],
        "readings_on": [],
        "readings_kun": [],
        "meanings_en": [],
        "variants": [],
        "nanori": [],
        "query_codes": [],
    }


def _parse_integer_field(elem):
    """Parse integer field from XML element. Returns None if not a number."""
    if elem.text is None:
        return None
    text = elem.text.strip()
    if not text.isdigit():
        return None
    return int(text)


def _process_start_element(tag, rec):
    """Process XML start elements."""
    if tag == "character":
        return _create_empty_record()
    return rec


def _process_end_element(tag, elem, rec):
    """Process XML end elements and update record."""
    if rec is None:
        return rec

    if tag == "literal":
        rec["literal"] = text(elem)
    elif tag == "grade":
        rec["grade"] = _parse_integer_field(elem)
    elif tag == "stroke_count":
        # KANJIDIC2 DTD: if several counts are given, the FIRST is the
        # accepted count; subsequent ones are common miscounts.
        if rec["stroke_count"] is None:
            rec["stroke_count"] = _parse_integer_field(elem)
    elif tag == "freq":
        rec["freq"] = _parse_integer_field(elem)
    elif tag == "jlpt":
        rec["jlpt"] = _parse_integer_field(elem)
    elif tag == "rad_value":
        _process_radical(elem, rec)
    elif tag == "reading":
        _process_reading(elem, rec)
    elif tag == "meaning":
        _process_meaning(elem, rec)
    elif tag == "variant":
        _process_variant(elem, rec)
    elif tag == "nanori":
        _process_nanori(elem, rec)
    elif tag == "q_code":
        _process_query_code(elem, rec)

    return rec


def _process_radical(elem, rec):
    """Process radical element."""
    if elem.get("rad_type") == "classical":
        value = text(elem)
        if value:
            rec["radicals"].append(value)


def _process_reading(elem, rec):
    """Process reading element."""
    rtype = elem.get("r_type")
    value = text(elem)
    if not value:
        return

    if rtype == "ja_on":
        rec["readings_on"].append(value)
    elif rtype == "ja_kun":
        rec["readings_kun"].append(value)


def _process_meaning(elem, rec):
    """Process meaning element."""
    lang = elem.get("m_lang")
    value = text(elem)
    if value and (lang is None or lang == "en"):
        rec["meanings_en"].append(value)


def _process_variant(elem, rec):
    """Process variant element."""
    vtype = elem.get("var_type") or "unknown"
    value = text(elem)
    if value:
        rec["variants"].append((vtype, value))


def _process_nanori(elem, rec):
    """Process nanori (name reading) element."""
    value = text(elem)
    if value:
        rec["nanori"].append(value)


def _process_query_code(elem, rec):
    """Process q_code element (SKIP, SH descriptor, four corner, De Roo).

    SKIP entries carrying a skip_misclass attribute are common
    *mis*classification codes; they are tagged 'skip_misclass' so real
    SKIP codes stay cleanly queryable.
    """
    qtype = elem.get("qc_type")
    value = text(elem)
    if not qtype or not value:
        return
    if qtype == "skip" and elem.get("skip_misclass"):
        qtype = "skip_misclass"
    rec["query_codes"].append((qtype, value))


def _calculate_modern_jlpt(grade, freq, old_jlpt, literal=None):
    """
    Estimate a modern JLPT level (1=N1 ... 5=N5) for each kanji.

    NOTE: This is a heuristic, not official JLPT data (no official kanji
    lists exist). Legacy KANJIDIC2 levels (1-4, pre-2010 test) are mapped
    first; kanji without legacy data are estimated from school grade and
    frequency so that common jouyou kanji don't all land in N1.
    """
    # Legacy JLPT levels from KANJIDIC2 (old test: 4=easiest, 1=hardest)
    if old_jlpt == 4:
        return 5  # N5 (easiest)
    elif old_jlpt == 3:
        return 4  # N4
    elif old_jlpt == 2:
        return 3  # N3
    elif old_jlpt == 1:
        return 2  # N2 (old 1-kyu spanned roughly N1-N2)

    # No legacy data: estimate from grade + frequency
    if freq is not None and freq <= 200 and grade in (1, 2):
        return 5
    if freq is not None and freq <= 500 and grade in (3, 4):
        return 4
    if freq is not None and freq <= 1000 and grade in (5, 6):
        return 3

    # Trygve's reviewed literal estimates, 2026-10-26. The proposed broad
    # N3-to-N1 fallback migration was explicitly withdrawn; retain N3 here.
    # These exceptions reflect reviewed vocabulary, not a global-rank cutoff.
    if grade == 8 and freq is None:
        return {"拶": 3, "痩": 3, "戚": 3, "溺": 2, "僅": 2, "籠": 2}.get(literal, 3)

    # Remaining jouyou kanji (grades 1-6 = kyouiku, 8 = secondary jouyou)
    if grade in (1, 2, 3, 4, 5, 6, 8):
        return 2 if freq is not None else 3

    # Non-jouyou kanji (hyougai/jinmeiyou) = genuinely advanced/rare
    return 1


def _insert_character_data(cur, rec):
    """Insert character data into database tables."""
    literal = rec["literal"]

    # Calculate modern JLPT level from old JLPT data
    jlpt = _calculate_modern_jlpt(rec["grade"], rec["freq"], rec["jlpt"], literal)

    # Insert main kanji record
    cur.execute(
        "INSERT OR REPLACE INTO kanji(literal, grade, stroke_count, freq, jlpt) VALUES (?,?,?,?,?)",
        (
            literal,
            rec["grade"],
            rec["stroke_count"],
            rec["freq"],
            jlpt,
        ),
    )

    # Dedup and insert related data (seq = order of appearance in the XML,
    # which for KANJIDIC2 reflects importance: primary meaning/reading first)
    for radical in _dedup_preserve(rec["radicals"]):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_radical(literal, rad_value) VALUES (?,?)",
            (literal, radical),
        )

    for seq, reading in enumerate(_dedup_preserve(rec["readings_on"])):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_reading(literal, type, reading, seq) VALUES (?,?,?,?)",
            (literal, "on", reading, seq),
        )

    for seq, reading in enumerate(_dedup_preserve(rec["readings_kun"])):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_reading(literal, type, reading, seq) VALUES (?,?,?,?)",
            (literal, "kun", reading, seq),
        )

    for seq, meaning in enumerate(_dedup_preserve(rec["meanings_en"])):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_meaning(literal, lang, meaning, seq) VALUES (?,?,?,?)",
            (literal, "en", meaning, seq),
        )

    for vtype, value in _dedup_preserve(rec["variants"]):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_variant(literal, var_type, value) VALUES (?,?,?)",
            (literal, vtype, value),
        )

    for seq, reading in enumerate(_dedup_preserve(rec["nanori"])):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_nanori(literal, reading, seq) VALUES (?,?,?)",
            (literal, reading, seq),
        )

    for seq, (qtype, value) in enumerate(_dedup_preserve(rec["query_codes"])):
        cur.execute(
            "INSERT OR IGNORE INTO kanji_query_code(literal, qc_type, value, seq) VALUES (?,?,?,?)",
            (literal, qtype, value, seq),
        )


def build_sqlite(xml_path: Path, db_path: Path, batch_size: int = 500) -> int:
    """Build SQLite database from KANJIDIC2 XML file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Always build from scratch so no stale rows survive from a previous build
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    cur = conn.cursor()

    context = ET.iterparse(str(xml_path), events=("start", "end"))
    _, root = next(context)

    count = 0
    batch = 0
    rec = None

    for event, elem in context:
        tag = elem.tag

        if event == "start":
            rec = _process_start_element(tag, rec)
        elif event == "end":
            if tag == "character" and rec is not None and rec["literal"]:
                _insert_character_data(cur, rec)

                batch += 1
                count += 1
                if batch >= batch_size:
                    conn.commit()
                    batch = 0

                root.clear()
                rec = None
            else:
                rec = _process_end_element(tag, elem, rec)

    if batch:
        conn.commit()
    _finalize_db(conn)
    conn.close()
    return count
