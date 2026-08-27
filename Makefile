PREFIX ?= $(CURDIR)
BUILD  := $(CURDIR)/build

WAYLAND_CFLAGS := $(shell pkg-config --cflags wayland-client)
WAYLAND_LIBS   := $(shell pkg-config --libs wayland-client)

PROTO_XML := protocols/wlr-virtual-pointer.xml
PROTO_SRC := $(BUILD)/wlr-virtual-pointer-unstable-v1-protocol.c
PROTO_HDR := $(BUILD)/wlr-virtual-pointer-unstable-v1-client-protocol.h

# Tesseract's English model and the one config the harness asks for. The
# system package does not reliably ship the language data, and pointing
# TESSDATA_PREFIX at our own directory hides the system's configs, so the
# directory has to be self-contained.
TESSDATA_DIR := share/tessdata
TESSDATA := $(TESSDATA_DIR)/eng.traineddata
TESSCONFIG := $(TESSDATA_DIR)/configs/tsv
TESSDATA_URL := https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata

.PHONY: all clean check

all: $(BUILD)/vptr $(TESSDATA) $(TESSCONFIG)

$(TESSDATA):
	@mkdir -p $(dir $@)
	curl -sfL -o $@.part $(TESSDATA_URL)
	@mv $@.part $@

$(TESSCONFIG):
	@mkdir -p $(dir $@)
	printf 'tessedit_create_tsv 1\n' > $@

$(BUILD):
	@mkdir -p $(BUILD)

$(PROTO_HDR): $(PROTO_XML) | $(BUILD)
	wayland-scanner client-header $< $@

$(PROTO_SRC): $(PROTO_XML) | $(BUILD)
	wayland-scanner private-code $< $@

$(BUILD)/vptr: src/vptr.c $(PROTO_SRC) $(PROTO_HDR) | $(BUILD)
	$(CC) -O2 -Wall -I$(BUILD) -o $@ src/vptr.c $(PROTO_SRC) \
		$(WAYLAND_CFLAGS) $(WAYLAND_LIBS)

check: all
	./tests/run.sh

clean:
	rm -rf $(BUILD)
