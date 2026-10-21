# oboeru （覚える）

*Oboeru* is Japanese for "to memorize / learn by heart" — which is exactly what
this terminal kanji quiz helps you do. Fast, keyboard-driven, and just
addictive enough to keep you coming back.

Built with [textual](https://textual.textualize.io/) on
[KANJIDIC2](https://www.edrdg.org/wiki/index.php/KANJIDIC_Project) data.

## Features

- Rounds of 10 multiple-choice "kanji → meaning" questions per JLPT level (N5–N1)
- Big half-block kanji art (CJK font is bundled, so it renders everywhere;
  falls back to plain text only if the font file is missing), with a small
  reference glyph in the stats bar
- Adaptive selection: unseen and previously missed kanji come back more often
  (Leitner boxes), mastered kanji fade away
- Sessions are saved: per-kanji progress + session history in a small SQLite
  file; the menu shows lifetime accuracy and mastered count
- Score with streak bonuses 🔥, animations, and a review list after each round

## Install

```bash
pip install oboeru
# or run directly without installing:
uvx oboeru
```

The KANJIDIC2 database (built by
[kanjibon](https://github.com/trygu/kanjibon)) is bundled with the package,
so no setup is needed. Pass `--db /path/to/kanjidic2.sqlite` if you have your
own copy.

## Run

```bash
oboeru
oboeru --help        # all options (--db, --kanji-size, --progress)
```

Keys: `↑/↓` + `enter` pick a level, then `1-4` or `↑/↓` + `enter` to
answer, `enter` next question, `q` back/quit.

Progress is stored in `~/.oboeru/quiz_progress.sqlite` (or next to a custom
`--db` database) unless `--progress` says otherwise — your streaks survive
restarts.

## License

MIT
