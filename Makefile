.PHONY: install build artifacts test quiz clean

install:
	uv sync

build:
	uv run kanjibon build --input data/kanjidic2.xml --db output/kanjidic2.sqlite

quiz:
	uv run --package oboeru oboeru

artifacts: build
	uv run kanjibon artifacts --db output/kanjidic2.sqlite --output-dir output

test:
	uv run pytest

clean:
	rm -f output/kanjidic2.sqlite* output/manifest.json
