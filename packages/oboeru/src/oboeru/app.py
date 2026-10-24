"""oboeru (覚える) — terminal kanji quiz on a kanjibon database.

Uses the database "app contract": questions come from the kanji_seed view
(one row per kanji, by JLPT level) and distractors from distractor_pool.

Progress (per-kanji correct/wrong + session history) is stored in a separate
SQLite file, so the app remembers you between runs: unseen and previously
missed kanji come back more often (Leitner boxes).

Run:  oboeru [--db /path/to/kanjidic2.sqlite]  (defaults to the bundled DB)
Keys: 1-4 or ↑/↓ + enter answer, enter next, q back/quit.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from PIL import Image, ImageDraw, ImageFont
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, ProgressBar, Static

QUESTIONS_PER_ROUND = 10
MAX_BOX = 4  # Leitner box cap; box >= 3 counts as "mastered"
LEVEL_NAMES = {5: "N5", 4: "N4", 3: "N3", 2: "N2", 1: "N1"}

# Short praise words for a correct answer (rotated at random)
PRAISE = ("すごい！", "よくやった！", "見事！", "完璧！")


def round_rank(correct: int, total: int) -> str:
    """One-line Japanese rank for the round summary, by accuracy."""
    acc = correct / total if total else 0
    if acc == 1:
        return "満点！  perfect round 🎉"
    if acc >= 0.8:
        return "優秀  excellent"
    if acc >= 0.6:
        return "良し  good"
    if acc >= 0.4:
        return "まだまだ  keep going"
    return "がんばろう  let's try again"


# Bundled font first (checked below): identical kanji art on every machine,
# including bare `uvx oboeru` venvs. System fonts are the fallback for dev
# checkouts where the bundled font isn't present yet.
BUNDLED_FONT = Path(__file__).parent / "fonts" / "NotoSansCJKjp-Regular.otf"

# First match wins: macOS, Linux (Noto), Windows
CJK_FONT_PATHS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]


@lru_cache(maxsize=4)
def _cjk_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Bundled CJK font if present, else first available system font, else None."""
    for path in (BUNDLED_FONT, *CJK_FONT_PATHS):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return None


def kanji_art(literal: str, pixels: int = 24, color: str = "#e0af68"):
    """Render a kanji glyph as big half-block art (▀ ▄ █), two image rows
    per terminal row. Falls back to the plain literal when no CJK font is
    available."""
    font = _cjk_font(pixels * 2)
    if font is None:
        return literal
    # Draw large, crop to the glyph, scale into a square canvas
    img = Image.new("L", (pixels * 4, pixels * 4), 0)
    ImageDraw.Draw(img).text(
        (pixels * 2, pixels * 2), literal, font=font, fill=255, anchor="mm"
    )
    bbox = img.getbbox()
    if bbox is None:
        return literal
    img = img.crop(bbox)
    img.thumbnail((pixels, pixels), Image.LANCZOS)
    canvas = Image.new("L", (pixels, pixels), 0)
    canvas.paste(img, ((pixels - img.width) // 2, (pixels - img.height) // 2))
    # Two vertical pixels per cell: top+bottom=█, top=▀, bottom=▄
    art = Text()
    px = canvas.load()
    for y in range(0, pixels - 1, 2):
        for x in range(pixels):
            top, bottom = px[x, y] > 127, px[x, y + 1] > 127
            if top:
                art.append("█" if bottom else "▀", style=color)
            elif bottom:
                art.append("▄", style=color)
            else:
                art.append(" ")
        if y + 2 < pixels:
            art.append("\n")
    return art


def nudge(widget, frames: list[tuple[int, int]], step: float = 0.06) -> None:
    """Frame-based offset animation (slide/shake/bounce).

    textual's styles.animate() can't blend the offset property, so we step
    through (x, y) frames on a timer instead — simple and version-proof.
    """
    frames_iter = iter(frames)
    state = {"timer": None}

    def tick() -> None:
        if not widget.is_mounted:
            state["timer"].stop()
            return
        try:
            widget.styles.offset = next(frames_iter)
        except StopIteration:
            state["timer"].stop()

    try:
        widget.styles.offset = next(frames_iter)
    except StopIteration:
        return
    state["timer"] = widget.set_interval(step, tick)


@dataclass
class Question:
    literal: str
    meaning: str
    on_readings: str
    kun_readings: str
    choices: list[str]
    correct_index: int


class KanjiDB:
    """Read-only access to the kanjibon database via its app contract views."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)

    def levels(self) -> list[tuple[int, int]]:
        """Available JLPT levels with kanji counts, easiest first."""
        return self.conn.execute(
            "SELECT lvl, COUNT(*) FROM kanji_seed GROUP BY lvl ORDER BY lvl DESC"
        ).fetchall()

    def round_questions(self, lvl: int, n: int, progress: dict) -> list[Question]:
        """Pick n kanji without replacement, weighted by learning need."""
        rows = self.conn.execute(
            "SELECT literal, main_meaning, on_prime, kun_prime"
            " FROM kanji_seed WHERE lvl=?",
            (lvl,),
        ).fetchall()
        pool = [(self._weight(progress.get(row[0])), row) for row in rows]
        picked = []
        for _ in range(min(n, len(pool))):
            hit = random.uniform(0, sum(w for w, _ in pool))
            acc = 0.0
            for i, (w, row) in enumerate(pool):
                acc += w
                if acc >= hit:
                    picked.append(row)
                    pool.pop(i)
                    break
        return [self._make_question(lvl, row) for row in picked]

    @staticmethod
    def _weight(p: tuple[int, int, int] | None) -> float:
        """Selection weight: unseen > often-wrong > mastered."""
        if p is None:
            return 6.0
        _correct, wrong, box = p
        return (1.0 + 2.0 * wrong) / (1.0 + box) + 0.1

    def _make_question(self, lvl: int, row) -> Question:
        literal, meaning, on_prime, kun_prime = row
        distractors = [
            d
            for (d,) in self.conn.execute(
                """SELECT DISTINCT meaning FROM distractor_pool
                   WHERE lvl=? AND meaning<>?
                     AND meaning NOT IN
                         (SELECT meaning FROM kanji_meaning WHERE literal=?)
                   ORDER BY RANDOM() LIMIT 3""",
                (lvl, meaning, literal),
            )
        ]
        choices = [meaning] + distractors
        random.shuffle(choices)
        return Question(
            literal, meaning, on_prime, kun_prime, choices, choices.index(meaning)
        )


class ProgressDB:
    """Per-kanji progress + session history (separate from the kanji data)."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS progress(
                literal TEXT PRIMARY KEY,
                correct INTEGER NOT NULL DEFAULT 0,
                wrong INTEGER NOT NULL DEFAULT 0,
                box INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                played_at TEXT NOT NULL,
                lvl INTEGER NOT NULL,
                questions INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                best_streak INTEGER NOT NULL
            );
        """)

    def progress_map(self) -> dict[str, tuple[int, int, int]]:
        """literal -> (correct, wrong, box) for all seen kanji."""
        return {
            lit: (c, w, b)
            for lit, c, w, b in self.conn.execute(
                "SELECT literal, correct, wrong, box FROM progress"
            )
        }

    def record(self, literal: str, ok: bool) -> None:
        row = self.conn.execute(
            "SELECT correct, wrong, box FROM progress WHERE literal=?", (literal,)
        ).fetchone()
        correct, wrong, box = row if row else (0, 0, 0)
        if ok:
            correct += 1
            box = min(MAX_BOX, box + 1)
        else:
            wrong += 1
            box = 0  # back to square one (Leitner)
        self.conn.execute(
            "INSERT OR REPLACE INTO progress"
            "(literal, correct, wrong, box, last_seen) VALUES (?,?,?,?,?)",
            (literal, correct, wrong, box, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def save_session(
        self, lvl: int, questions: int, correct: int, best_streak: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO session(played_at, lvl, questions, correct, best_streak)"
            " VALUES (?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                lvl,
                questions,
                correct,
                best_streak,
            ),
        )
        self.conn.commit()

    def lifetime(self) -> tuple[int, int, int, int]:
        """(total answers, total correct, mastered kanji, sessions played)."""
        answers, correct = self.conn.execute(
            "SELECT COALESCE(SUM(correct) + SUM(wrong), 0),"
            "       COALESCE(SUM(correct), 0) FROM progress"
        ).fetchone()
        mastered = self.conn.execute(
            "SELECT COUNT(*) FROM progress WHERE box >= 3"
        ).fetchone()[0]
        sessions = self.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        return answers, correct, mastered, sessions


class StartScreen(Screen):
    BINDINGS: ClassVar = [
        Binding("up", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("q", "quit_app", "Quit"),
    ]

    TITLE = "K A N J I   Q U I Z"

    def compose(self) -> ComposeResult:
        answers, correct, mastered, sessions = self.app.progress.lifetime()
        acc = f"{100 * correct / answers:.0f}%" if answers else "—"
        yield Vertical(
            Static("漢 字", id="logo"),
            Static("", id="title"),
            Static(
                f"answered {answers} ・ accuracy {acc}"
                f" ・ mastered {mastered} ・ sessions {sessions}",
                id="life",
            ),
            Horizontal(
                *[
                    Button(f"{LEVEL_NAMES[lvl]}\n{count} kanji", id=f"lvl-{lvl}")
                    for lvl, count in self.app.kanji.levels()
                ],
                id="levels",
            ),
            Static("pick a level — ↑/↓ + enter to start, q to quit", id="hint"),
            id="start",
        )

    def on_mount(self) -> None:
        # Pre-focus the first level for keyboard play (↑/↓ + enter)
        self.query_one("#levels Button", Button).focus()
        # Typewriter title + gentle entrances
        self._title_len = 0
        self._title_timer = self.set_interval(0.06, self._type_title)
        logo = self.query_one("#logo", Static)
        logo.styles.opacity = 0.0
        logo.styles.animate("opacity", 1.0, duration=0.8)
        for i, btn in enumerate(self.query("#levels Button")):
            btn.styles.opacity = 0.0
            btn.styles.animate("opacity", 1.0, duration=0.3, delay=0.5 + 0.08 * i)

    def _type_title(self) -> None:
        if not self.is_mounted:
            self._title_timer.stop()
            return
        self._title_len += 1
        self.query_one("#title", Static).update(self.TITLE[: self._title_len])
        if self._title_len >= len(self.TITLE):
            self._title_timer.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("lvl-"):
            self.app.switch_screen(QuizScreen(int(event.button.id.split("-")[1])))

    def action_quit_app(self) -> None:
        self.app.exit()


class QuizScreen(Screen):
    BINDINGS: ClassVar = [
        Binding("1", "answer(0)", show=False),
        Binding("2", "answer(1)", show=False),
        Binding("3", "answer(2)", show=False),
        Binding("4", "answer(3)", show=False),
        Binding("up", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("enter", "next", "Next"),
        Binding("space", "next", show=False),
        Binding("q", "to_levels", "Levels"),
    ]

    def __init__(self, lvl: int):
        super().__init__()
        self.lvl = lvl
        self.questions: list[Question] = []
        self.index = 0
        self.score = 0
        self.streak = 0
        self.best_streak = 0
        self.correct_count = 0
        self.missed: list[Question] = []
        self.answered = False
        self.art_active = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(id="stats"),
            ProgressBar(total=QUESTIONS_PER_ROUND, show_eta=False, id="bar"),
            Horizontal(
                Vertical(Static(id="kanji"), id="kanji-box"),
                Vertical(
                    *[Button(id=f"ans-{i}", classes="answer") for i in range(4)],
                    Static(id="feedback", classes="hidden"),
                    Static(id="readings", classes="hidden"),
                    id="side",
                ),
                id="main",
            ),
            Static("1-4 / ↑↓ answer ・ enter next ・ q levels", id="quiz-hint"),
            id="quiz",
        )

    def on_mount(self) -> None:
        self.questions = self.app.kanji.round_questions(
            self.lvl, QUESTIONS_PER_ROUND, self.app.progress.progress_map()
        )
        self.show_question()

    def show_question(self) -> None:
        q = self.questions[self.index]
        self.answered = False
        kanji = self.query_one("#kanji", Static)
        art = kanji_art(q.literal, self.app.kanji_size)
        kanji.update(art)
        # Remember whether the block art is active: the stats bar shows a
        # small reference glyph in that case (redundant when the art *is*
        # the plain glyph).
        self.art_active = isinstance(art, Text)
        # Fade + slide the kanji in
        kanji.styles.opacity = 0.0
        kanji.styles.animate("opacity", 1.0, duration=0.25)
        nudge(kanji, [(0, -2), (0, -1), (0, 0)], step=0.05)
        for widget_id in ("#feedback", "#readings"):
            self.query_one(widget_id, Static).add_class("hidden")
        for i, btn in enumerate(self.query(".answer")):
            btn.label = q.choices[i] if i < len(q.choices) else "—"
            btn.disabled = i >= len(q.choices)
            btn.remove_class("correct", "wrong")
            # Staggered entrance
            btn.styles.opacity = 0.0
            btn.styles.animate("opacity", 1.0, duration=0.2, delay=0.08 + 0.05 * i)
        # Pre-focus the first option for keyboard play (↑/↓ + enter)
        self.query_one("#ans-0", Button).focus()
        self.query_one("#bar", ProgressBar).update(progress=self.index)
        self.update_stats()

    def update_stats(self) -> None:
        fire = "🔥🔥" if self.streak >= 5 else "🔥"
        flame = f"   {fire} streak {self.streak}" if self.streak >= 2 else ""
        ref = f"{self.questions[self.index].literal}   " if self.art_active else ""
        self.query_one("#stats", Static).update(
            f"{ref}{LEVEL_NAMES[self.lvl]}"
            f"   {self.index + 1}/{len(self.questions)}"
            f"   score {self.score}{flame}"
        )

    def action_answer(self, i: int) -> None:
        if self.answered:
            return
        q = self.questions[self.index]
        if i >= len(q.choices):
            return
        self.answered = True
        ok = i == q.correct_index
        buttons = list(self.query(".answer"))
        for j, btn in enumerate(buttons):
            btn.disabled = True
            if j == q.correct_index:
                btn.add_class("correct")
        # Nothing left to select: drop focus so enter means "next"
        self.app.set_focus(None)
        feedback = self.query_one("#feedback", Static)
        kanji = self.query_one("#kanji", Static)
        if ok:
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            gained = 10 + 5 * (self.streak - 1)
            self.score += gained
            self.correct_count += 1
            feedback.update(f"✓ {random.choice(PRAISE)}  +{gained}")
            feedback.remove_class("bad")
            feedback.add_class("ok")
            if self.streak >= 2:
                # Bounce the stats bar to celebrate the streak
                nudge(
                    self.query_one("#stats", Static),
                    [(0, -1), (0, 0), (0, -1), (0, 0)],
                    step=0.07,
                )
        else:
            buttons[i].add_class("wrong")
            self.streak = 0
            self.missed.append(q)
            feedback.update(f"✗ {q.literal} means “{q.meaning}”")
            feedback.remove_class("ok")
            feedback.add_class("bad")
            # Shake the kanji panel
            nudge(kanji, [(2, 0), (-2, 0), (1, 0), (-1, 0), (0, 0)], step=0.05)
        # Fade the verdict in
        feedback.styles.opacity = 0.0
        feedback.styles.animate("opacity", 1.0, duration=0.2)
        self.app.progress.record(q.literal, ok)
        self.query_one("#readings", Static).update(
            f"on: {q.on_readings or '—'}    kun: {q.kun_readings or '—'}"
            "    —    enter for next →"
        )
        feedback.remove_class("hidden")
        self.query_one("#readings", Static).remove_class("hidden")
        self.update_stats()

    def action_next(self) -> None:
        if not self.answered:
            return
        self.index += 1
        if self.index >= len(self.questions):
            self.app.progress.save_session(
                self.lvl, len(self.questions), self.correct_count, self.best_streak
            )
            self.app.switch_screen(
                SummaryScreen(
                    self.lvl,
                    self.correct_count,
                    len(self.questions),
                    self.best_streak,
                    self.score,
                    self.missed,
                )
            )
        else:
            self.show_question()

    def action_to_levels(self) -> None:
        self.app.switch_screen(StartScreen())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("ans-"):
            self.action_answer(int(event.button.id.split("-")[1]))


class SummaryScreen(Screen):
    BINDINGS: ClassVar = [
        Binding("enter", "again", "Again"),
        Binding("q", "levels", "Levels"),
    ]

    def __init__(
        self,
        lvl: int,
        correct: int,
        total: int,
        best_streak: int,
        score: int,
        missed: list[Question],
    ):
        super().__init__()
        self.lvl = lvl
        self.correct = correct
        self.total = total
        self.best_streak = best_streak
        self.score = score
        self.missed = missed

    def compose(self) -> ComposeResult:
        rows = [
            Static("R O U N D   C O M P L E T E", id="sum-title"),
            Static(round_rank(self.correct, self.total), id="sum-rank"),
            Static("", id="sum-stats"),
        ]
        if self.missed:
            lines = "\n".join(f"  {q.literal}   {q.meaning}" for q in self.missed)
            rows.append(Static(f"review these:\n{lines}", id="sum-missed"))
        rows.append(
            Horizontal(
                Button("Again (enter)", id="again"),
                Button("Levels (q)", id="levels"),
                Button("Quit", id="quit"),
                id="sum-buttons",
            )
        )
        yield Vertical(*rows, id="summary")

    def on_mount(self) -> None:
        # Score counts up from zero
        self._shown_score = 0
        self._render_stats()
        if self.score:
            self._score_timer = self.set_interval(0.04, self._tick_score)

    def _render_stats(self) -> None:
        acc = 100 * self.correct / self.total if self.total else 0
        self.query_one("#sum-stats", Static).update(
            f"{LEVEL_NAMES[self.lvl]} ・ score {self._shown_score} ・ "
            f"✓ {self.correct}/{self.total} ({acc:.0f}%) ・ "
            f"🔥 best streak {self.best_streak}"
        )

    def _tick_score(self) -> None:
        if not self.is_mounted:
            self._score_timer.stop()
            return
        self._shown_score = min(
            self.score, self._shown_score + max(1, self.score // 25)
        )
        self._render_stats()
        if self._shown_score >= self.score:
            self._score_timer.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "again":
            self.action_again()
        elif event.button.id == "levels":
            self.action_levels()
        elif event.button.id == "quit":
            self.app.exit()

    def action_again(self) -> None:
        self.app.switch_screen(QuizScreen(self.lvl))

    def action_levels(self) -> None:
        self.app.switch_screen(StartScreen())


class KanjiQuizApp(App):
    CSS = """
    Screen { background: #1a1b26; color: #c0caf5; }
    #start, #quiz, #summary { align: center middle; }

    #logo { text-align: center; color: #e0af68; text-style: bold; }
    #title { text-align: center; color: #7aa2f7; text-style: bold; }
    #life { text-align: center; color: #9ece6a; margin: 1 0; }
    #levels { height: auto; align: center middle; }
    #levels Button { width: 16; margin: 0 1; }
    #hint { text-align: center; color: #565f89; margin-top: 2; }

    #stats { text-align: center; color: #e0af68; text-style: bold; }
    #bar { width: 60; margin: 0 0 1 0; }
    #main { height: auto; align: center middle; }
    #kanji-box { height: auto; margin: 0 1; }
    #kanji {
        border: heavy #7aa2f7;
        background: #24283b;
        padding: 1 3;
        text-align: center;
        text-style: bold;
    }
    #side { height: auto; margin: 0 1; }
    .answer { width: 44; margin-bottom: 1; }
    .answer:focus { background: #414868; text-style: bold; }
    .answer.correct { background: #9ece6a; color: #1a1b26; text-style: bold; }
    .answer.wrong { background: #f7768e; color: #1a1b26; text-style: bold; }
    #quiz-hint { text-align: center; color: #565f89; margin-top: 1; }
    #feedback { text-style: bold; margin-top: 1; }
    #feedback.ok { color: #9ece6a; }
    #feedback.bad { color: #f7768e; }
    #readings { color: #bb9af7; }
    .hidden { display: none; }

    #sum-title { text-align: center; color: #7aa2f7; text-style: bold; }
    #sum-rank { text-align: center; color: #bb9af7; text-style: bold; margin-bottom: 1; }
    #sum-stats { text-align: center; color: #e0af68; margin: 1 0; }
    #sum-missed {
        text-align: center;
        color: #f7768e;
        border: round #f7768e;
        padding: 1 3;
        margin: 1 0;
    }
    #sum-buttons { height: auto; align: center middle; margin-top: 1; }
    #sum-buttons Button { margin: 0 1; }
    """

    def __init__(self, db_path: Path, progress_path: Path, kanji_size: int = 24):
        super().__init__()
        self.kanji = KanjiDB(db_path)
        self.progress = ProgressDB(progress_path)
        self.kanji_size = kanji_size

    def on_mount(self) -> None:
        self.push_screen(StartScreen())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="path to kanjidic2.sqlite (default: the bundled database)",
    )
    parser.add_argument(
        "--progress",
        type=Path,
        default=None,
        help="progress database (default: alongside --db)",
    )
    parser.add_argument(
        "--kanji-size",
        type=int,
        default=24,
        help="kanji art size in pixels (terminal rows = size/2; default: 24)",
    )
    args = parser.parse_args()
    bundled = Path(__file__).with_name("kanjidic2.sqlite")
    db = args.db or (bundled if bundled.exists() else Path("output/kanjidic2.sqlite"))
    if not db.exists():
        parser.error(
            f"{db} not found — pass --db (get kanjidic2.sqlite from "
            "github.com/trygu/kanjibon/releases)"
        )
    if args.progress:
        progress = args.progress
    elif db == bundled:
        # The bundled DB lives in site-packages (read-only), so keep progress
        # in the user's home directory.
        progress = Path.home() / ".oboeru" / "quiz_progress.sqlite"
        progress.parent.mkdir(parents=True, exist_ok=True)
    else:
        progress = db.with_name("quiz_progress.sqlite")
    KanjiQuizApp(db, progress, kanji_size=args.kanji_size).run()


if __name__ == "__main__":
    main()
