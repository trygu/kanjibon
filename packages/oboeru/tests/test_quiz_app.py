"""Tests for the oboeru quiz app.

The quiz only depends on the kanjibon database *contract* (the kanji_seed and
distractor_pool views), so tests run against a tiny hand-written fixture DB —
no kanjibon import needed.
"""

import sqlite3
from pathlib import Path

import pytest
from oboeru import app as quiz

FIXTURE_SQL = """
CREATE TABLE kanji_seed(literal, lvl, freq, grade, main_meaning, on_prime, kun_prime);
CREATE TABLE distractor_pool(lvl, meaning);
CREATE TABLE kanji_meaning(literal, lang, meaning);
INSERT INTO kanji_seed VALUES
  ('水', 5, 60, 1, 'water', 'スイ', 'みず'),
  ('日', 5, 1, 1, 'day', 'ニチ・ジツ', 'ひ'),
  ('韓', 2, 445, 8, 'Korea', 'カン', '');
INSERT INTO distractor_pool VALUES
  (5, 'water'), (5, 'day'), (5, 'sun'), (5, 'Japan'), (2, 'Korea');
INSERT INTO kanji_meaning VALUES
  ('水', 'en', 'water'),
  ('日', 'en', 'day'), ('日', 'en', 'sun'), ('日', 'en', 'Japan'),
  ('韓', 'en', 'Korea');
"""


@pytest.fixture()
def quiz_env(tmp_path):
    db = tmp_path / "kanji.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(FIXTURE_SQL)
    conn.close()
    return db, tmp_path / "progress.sqlite"


def test_selection_weights(quiz_env):
    # Unseen kanji beat seen ones; wrong answers beat mastered boxes
    assert quiz.KanjiDB._weight(None) > quiz.KanjiDB._weight((5, 0, 3))
    assert quiz.KanjiDB._weight((0, 3, 0)) > quiz.KanjiDB._weight((3, 0, 3))


@pytest.mark.asyncio
async def test_quiz_flow(quiz_env):
    db, progress_path = quiz_env
    app = quiz.KanjiQuizApp(db, progress_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert type(app.screen).__name__ == "StartScreen"

        # Pick N5 (fixture has 水 and 日 at lvl 5)
        await pilot.click("#lvl-5")
        await pilot.pause()
        screen = app.screen
        assert type(screen).__name__ == "QuizScreen"
        assert len(screen.questions) == 2  # min(10, pool size)
        assert len(list(screen.query(".answer"))) == 4

        # Answer the first question correctly via its button
        q = screen.questions[screen.index]
        await pilot.click(f"#ans-{q.correct_index}")
        await pilot.pause()
        assert screen.correct_count == 1
        assert screen.streak == 1
        assert not screen.query_one("#feedback").has_class("hidden")

        # Progress was persisted: one correct, zero wrong for that kanji
        row = (
            sqlite3.connect(progress_path)
            .execute(
                "SELECT correct, wrong, box FROM progress WHERE literal=?", (q.literal,)
            )
            .fetchone()
        )
        assert row == (1, 0, 1)

        # Advance to the next question
        await pilot.press("enter")
        await pilot.pause()
        assert screen.index == 1

        # Finish the round -> summary screen + session row saved
        q2 = screen.questions[screen.index]
        wrong_index = next(i for i in range(len(q2.choices)) if i != q2.correct_index)
        screen.action_answer(wrong_index)
        await pilot.pause()
        screen.action_next()
        await pilot.pause()
        assert type(app.screen).__name__ == "SummaryScreen"
        sessions = (
            sqlite3.connect(progress_path)
            .execute("SELECT lvl, questions, correct, best_streak FROM session")
            .fetchall()
        )
        assert sessions == [(5, 2, 1, 1)]


@pytest.mark.asyncio
async def test_arrow_key_navigation(quiz_env):
    db, progress_path = quiz_env
    app = quiz.KanjiQuizApp(db, progress_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#lvl-5")
        await pilot.pause()
        screen = app.screen

        # First option is pre-focused; arrows move the highlight.
        # (disabled buttons are skipped; fixture's 日 question has only 2 choices)
        enabled = [b for b in screen.query(".answer") if not b.disabled]
        assert app.focused is enabled[0]
        await pilot.press("down")
        assert app.focused is enabled[1 % len(enabled)]
        await pilot.press("up")
        assert app.focused is enabled[0]
        # ...and it wraps around
        await pilot.press("up")
        assert app.focused is enabled[-1]

        # Enter answers the highlighted choice (and does not skip ahead)
        await pilot.press("enter")
        await pilot.pause()
        assert screen.answered
        assert screen.index == 0

        # After answering, focus is dropped: enter advances to next question
        assert app.focused is None
        await pilot.press("enter")
        await pilot.pause()
        assert screen.index == 1


@pytest.mark.asyncio
async def test_start_screen_keyboard(quiz_env):
    db, progress_path = quiz_env
    app = quiz.KanjiQuizApp(db, progress_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert type(screen).__name__ == "StartScreen"

        # First level button is pre-focused
        first = screen.query("#levels Button").first()
        assert app.focused is first

        # Arrows move the highlight between level buttons
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not first
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is first

        # Enter starts the highlighted level
        await pilot.press("enter")
        await pilot.pause()
        assert type(app.screen).__name__ == "QuizScreen"
        assert app.screen.lvl == 5


@pytest.mark.asyncio
async def test_reference_glyph(quiz_env):
    db, progress_path = quiz_env
    app = quiz.KanjiQuizApp(db, progress_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#lvl-5")
        await pilot.pause()
        screen = app.screen
        stats = str(screen.query_one("#stats").content)
        q = screen.questions[screen.index]
        if isinstance(quiz.kanji_art(q.literal), str):
            # Plain-glyph fallback: the reference would just duplicate it
            assert q.literal not in stats
        else:
            # Block art active: stats bar carries the reference glyph
            assert q.literal in stats


@pytest.mark.asyncio
async def test_round_questions_have_unique_valid_choices(quiz_env):
    db, _ = quiz_env
    kanji = quiz.KanjiDB(db)
    questions = kanji.round_questions(5, 10, {})
    assert len({q.literal for q in questions}) == len(questions)  # no repeats
    for q in questions:
        assert q.choices[q.correct_index] == q.meaning
        assert len(set(q.choices)) == len(q.choices)  # no duplicate choices


def test_kanji_art(quiz_env):
    if quiz._cjk_font(48) is None:
        pytest.skip("no CJK font on this machine")
    art = quiz.kanji_art("水", pixels=24)
    # Big half-block rendering: 12 terminal rows, ink present
    assert len(art.plain.splitlines()) == 12
    assert any(c in art.plain for c in "█▀▄")


def test_splash_card_shape():
    from rich.cells import cell_len

    # The moon is geometric (PIL, no CJK font), so the card is deterministic:
    # 17 rows, 32 cells wide, December card ribbon at the bottom.
    card = quiz.splash_card()
    lines = card.plain.splitlines()
    assert len(lines) == 17
    assert len({cell_len(line) for line in lines}) == 1  # every row fills the card
    assert lines[0][0] == "╭" and lines[-1][0] == "╰"
    assert "十二月" in card.plain and "花  札" in card.plain


def test_kanji_art_fallback_without_font(quiz_env, monkeypatch):
    monkeypatch.setattr(quiz, "CJK_FONT_PATHS", [])
    monkeypatch.setattr(quiz, "BUNDLED_FONT", Path("/nonexistent.otf"))
    quiz._cjk_font.cache_clear()
    assert quiz.kanji_art("水") == "水"
