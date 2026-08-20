.PHONY: all build run clean


all: run


build:
	python build.py

run:
	python build.py --serve

clean:
	rm -rf ./site

