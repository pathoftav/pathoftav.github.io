.PHONY: all build clean


all: build


build:
	python build.py

clean:
	rm -rf ./docs

