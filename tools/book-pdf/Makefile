# Build the book PDF.  Usage:
#   make            # build the PDF
#   make preview    # build, then open it (macOS)
#   make book.md    # just run the preprocessor (no LaTeX needed) to inspect output
#   make clean

BUILD := build
ROOT  := ../..
PDF   := $(ROOT)/low-latency-cpp-on-linux.pdf
CHAPTERS := ../../chapters

.PHONY: all pdf preview clean book.md

all: pdf

pdf:
	./build.sh

preview: pdf
	open "$(PDF)"

# Run only the markdown preprocessor — useful to validate the transform without LaTeX.
book.md:
	mkdir -p $(BUILD)
	python3 preprocess.py $(CHAPTERS) $(BUILD)/book.md
	@echo "wrote $(BUILD)/book.md"

clean:
	rm -rf $(BUILD)
	rm -f $(PDF)
