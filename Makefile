PREFIX ?= $(CURDIR)
BUILD  := $(CURDIR)/build

WAYLAND_CFLAGS := $(shell pkg-config --cflags wayland-client xkbcommon)
WAYLAND_LIBS   := $(shell pkg-config --libs wayland-client xkbcommon)

PTR_XML := protocols/wlr-virtual-pointer.xml
PTR_SRC := $(BUILD)/wlr-virtual-pointer-unstable-v1-protocol.c
PTR_HDR := $(BUILD)/wlr-virtual-pointer-unstable-v1-client-protocol.h

KBD_XML := protocols/virtual-keyboard.xml
KBD_SRC := $(BUILD)/virtual-keyboard-unstable-v1-protocol.c
KBD_HDR := $(BUILD)/virtual-keyboard-unstable-v1-client-protocol.h

# Tesseract's English model and the one config the harness asks for. The
# system package does not reliably ship the language data, and pointing
# TESSDATA_PREFIX at our own directory hides the system's configs, so the
# directory has to be self-contained.
TESSDATA_DIR := share/tessdata
TESSDATA := $(TESSDATA_DIR)/eng.traineddata
TESSCONFIG := $(TESSDATA_DIR)/configs/tsv
TESSDATA_URL := https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata

.PHONY: all clean check

all: $(BUILD)/vptr $(BUILD)/vkbd $(TESSDATA) $(TESSCONFIG)

$(TESSDATA):
	@mkdir -p $(dir $@)
	curl -sfL -o $@.part $(TESSDATA_URL)
	@mv $@.part $@

$(TESSCONFIG):
	@mkdir -p $(dir $@)
	printf 'tessedit_create_tsv 1\n' > $@

$(BUILD):
	@mkdir -p $(BUILD)

$(PTR_HDR): $(PTR_XML) | $(BUILD)
	wayland-scanner client-header $< $@

$(PTR_SRC): $(PTR_XML) | $(BUILD)
	wayland-scanner private-code $< $@

$(KBD_HDR): $(KBD_XML) | $(BUILD)
	wayland-scanner client-header $< $@

$(KBD_SRC): $(KBD_XML) | $(BUILD)
	wayland-scanner private-code $< $@

$(BUILD)/vptr: src/vptr.c $(PTR_SRC) $(PTR_HDR) | $(BUILD)
	$(CC) -O2 -Wall -I$(BUILD) -o $@ src/vptr.c $(PTR_SRC) \
		$(WAYLAND_CFLAGS) $(WAYLAND_LIBS)

$(BUILD)/vkbd: src/vkbd.c $(KBD_SRC) $(KBD_HDR) | $(BUILD)
	$(CC) -O2 -Wall -I$(BUILD) -o $@ src/vkbd.c $(KBD_SRC) \
		$(WAYLAND_CFLAGS) $(WAYLAND_LIBS)

check: all
	./tests/run.sh

clean:
	rm -rf $(BUILD)
