PREFIX ?= /usr/local
DESTDIR ?=
RUNTIME_DIR := $(DESTDIR)$(PREFIX)/lib/niri-harness
BIN_DIR := $(DESTDIR)$(PREFIX)/bin
BUILD  := $(CURDIR)/build

INSTALL ?= install

WAYLAND_CFLAGS := $(shell pkg-config --cflags wayland-client xkbcommon)
WAYLAND_LIBS   := $(shell pkg-config --libs wayland-client xkbcommon)

PTR_XML := protocols/wlr-virtual-pointer.xml
PTR_SRC := $(BUILD)/wlr-virtual-pointer-unstable-v1-protocol.c
PTR_HDR := $(BUILD)/wlr-virtual-pointer-unstable-v1-client-protocol.h

KBD_XML := protocols/virtual-keyboard.xml
KBD_SRC := $(BUILD)/virtual-keyboard-unstable-v1-protocol.c
KBD_HDR := $(BUILD)/virtual-keyboard-unstable-v1-client-protocol.h

CAP_XML := protocols/wlr-screencopy.xml
CAP_SRC := $(BUILD)/wlr-screencopy-protocol.c
CAP_HDR := $(BUILD)/wlr-screencopy-client-protocol.h

# Tesseract's English model and the one config the harness asks for. The
# system package does not reliably ship the language data, and pointing
# TESSDATA_PREFIX at our own directory hides the system's configs, so the
# directory has to be self-contained.
TESSDATA_DIR := share/tessdata
TESSDATA := $(TESSDATA_DIR)/eng.traineddata
TESSCONFIG := $(TESSDATA_DIR)/configs/tsv
TESSDATA_URL := https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata

.PHONY: all install clean check

all: $(BUILD)/vptr $(BUILD)/vkbd $(BUILD)/vcap $(TESSDATA) $(TESSCONFIG)

install: all
	$(INSTALL) -Dm755 bin/niri-harness $(RUNTIME_DIR)/bin/niri-harness
	$(INSTALL) -Dm755 $(BUILD)/vptr $(RUNTIME_DIR)/build/vptr
	$(INSTALL) -Dm755 $(BUILD)/vkbd $(RUNTIME_DIR)/build/vkbd
	$(INSTALL) -Dm755 $(BUILD)/vcap $(RUNTIME_DIR)/build/vcap
	$(INSTALL) -Dm644 share/nested.kdl $(RUNTIME_DIR)/share/nested.kdl
	$(INSTALL) -Dm755 share/a11y_probe.py $(RUNTIME_DIR)/share/a11y_probe.py
	$(INSTALL) -Dm644 $(TESSDATA) $(RUNTIME_DIR)/share/tessdata/eng.traineddata
	$(INSTALL) -Dm644 $(TESSCONFIG) $(RUNTIME_DIR)/share/tessdata/configs/tsv
	$(INSTALL) -d $(BIN_DIR)
	ln -sfn ../lib/niri-harness/bin/niri-harness $(BIN_DIR)/niri-harness

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

$(CAP_HDR): $(CAP_XML) | $(BUILD)
	wayland-scanner client-header $< $@

$(CAP_SRC): $(CAP_XML) | $(BUILD)
	wayland-scanner private-code $< $@

$(BUILD)/vcap: src/vcap.c $(CAP_SRC) $(CAP_HDR) | $(BUILD)
	$(CC) -O2 -Wall -I$(BUILD) -o $@ src/vcap.c $(CAP_SRC) \
		$(WAYLAND_CFLAGS) $(WAYLAND_LIBS)

$(BUILD)/vptr: src/vptr.c $(PTR_SRC) $(PTR_HDR) | $(BUILD)
	$(CC) -O2 -Wall -I$(BUILD) -o $@ src/vptr.c $(PTR_SRC) \
		$(WAYLAND_CFLAGS) $(WAYLAND_LIBS)

$(BUILD)/vkbd: src/vkbd.c $(KBD_SRC) $(KBD_HDR) | $(BUILD)
	$(CC) -O2 -Wall -I$(BUILD) -o $@ src/vkbd.c $(KBD_SRC) \
		$(WAYLAND_CFLAGS) $(WAYLAND_LIBS)

check: all
	python3 -I -B tests/raw_capture.py
	./tests/run.sh

clean:
	rm -rf $(BUILD)
