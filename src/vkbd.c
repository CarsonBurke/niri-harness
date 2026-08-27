// Minimal virtual-keyboard text injector for a *nested* Wayland compositor.
//
// Why this exists rather than calling wtype
// -----------------------------------------
// wtype connects, uploads a keymap, types, and disconnects on every
// invocation, and it switches keymaps mid-string when a character needs one.
// Against a nested compositor that cost 2.8 seconds per character and hung
// outright past about twenty characters, and the harness's timeout then killed
// it mid-string. The result was silent truncation: a 35-character commit
// message arrived as 17 characters with no error anywhere, which is the worst
// possible failure for a verification tool - it looks exactly like an
// application bug. It cost one false defect report before it was caught.
//
// This uploads ONE keymap for the whole run and sends every key over that one
// connection, so a string costs one round trip plus a few milliseconds per
// key. It injects into whatever WAYLAND_DISPLAY names, which the harness
// always points at the nested instance, so the host's real keyboard is never
// touched.

// memfd_create is a GNU extension; the keymap has to be handed over as a file
// descriptor and an anonymous one needs no path to collide with.
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>

#include "virtual-keyboard-unstable-v1-client-protocol.h"

// Wayland carries evdev keycodes; xkb keycodes are the same numbers offset by
// eight, and the difference is a silent off-by-one-row bug if forgotten.
#define EVDEV_OFFSET 8

// Long enough for a client to process each key, short enough that a commit
// message is typed in well under a second.
#define DEFAULT_KEY_DELAY_MS 4

static struct wl_seat *seat = NULL;
static struct zwp_virtual_keyboard_manager_v1 *manager = NULL;

static void registry_global(void *data, struct wl_registry *registry, uint32_t name,
                            const char *interface, uint32_t version) {
  (void)data;
  (void)version;
  if (!strcmp(interface, wl_seat_interface.name)) {
    seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
  } else if (!strcmp(interface, zwp_virtual_keyboard_manager_v1_interface.name)) {
    manager = wl_registry_bind(registry, name, &zwp_virtual_keyboard_manager_v1_interface, 1);
  }
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name) {
  (void)data;
  (void)registry;
  (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static uint32_t now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

// Where a keysym lives in the keymap: which key produces it, and whether shift
// is needed to reach it. Resolved from the keymap itself rather than assumed,
// so a non-US layout still types the character asked for if the keymap can
// produce it at all.
struct key_site {
  xkb_keycode_t keycode;
  int needs_shift;
  int found;
};

static struct key_site locate(struct xkb_keymap *keymap, xkb_keysym_t target) {
  struct key_site site = {0, 0, 0};
  xkb_keycode_t min = xkb_keymap_min_keycode(keymap);
  xkb_keycode_t max = xkb_keymap_max_keycode(keymap);
  for (xkb_keycode_t code = min; code <= max; code++) {
    xkb_layout_index_t layouts = xkb_keymap_num_layouts_for_key(keymap, code);
    for (xkb_layout_index_t layout = 0; layout < layouts; layout++) {
      xkb_level_index_t levels = xkb_keymap_num_levels_for_key(keymap, code, layout);
      // Only the first two levels are reachable with shift alone. A character
      // that needs AltGr is reported as unavailable rather than typed wrongly.
      for (xkb_level_index_t level = 0; level < levels && level < 2; level++) {
        const xkb_keysym_t *syms = NULL;
        int count = xkb_keymap_key_get_syms_by_level(keymap, code, layout, level, &syms);
        for (int i = 0; i < count; i++) {
          if (syms[i] == target) {
            site.keycode = code;
            site.needs_shift = level > 0;
            site.found = 1;
            return site;
          }
        }
      }
    }
  }
  return site;
}

static int keymap_fd(const char *text, size_t *size_out) {
  size_t size = strlen(text) + 1;
  int fd = memfd_create("vkbd-keymap", MFD_CLOEXEC);
  if (fd < 0) {
    return -1;
  }
  if (ftruncate(fd, (off_t)size) < 0) {
    close(fd);
    return -1;
  }
  void *map = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (map == MAP_FAILED) {
    close(fd);
    return -1;
  }
  memcpy(map, text, size);
  munmap(map, size);
  *size_out = size;
  return fd;
}

// Decodes one UTF-8 sequence, returning the codepoint and advancing the cursor.
// Invalid bytes are consumed one at a time as U+FFFD so a bad byte cannot make
// this loop forever.
static uint32_t next_codepoint(const unsigned char **cursor) {
  const unsigned char *p = *cursor;
  unsigned char lead = *p;
  int extra;
  uint32_t value;
  if (lead < 0x80) {
    *cursor = p + 1;
    return lead;
  } else if ((lead & 0xe0) == 0xc0) {
    extra = 1;
    value = lead & 0x1f;
  } else if ((lead & 0xf0) == 0xe0) {
    extra = 2;
    value = lead & 0x0f;
  } else if ((lead & 0xf8) == 0xf0) {
    extra = 3;
    value = lead & 0x07;
  } else {
    *cursor = p + 1;
    return 0xfffd;
  }
  for (int i = 1; i <= extra; i++) {
    if ((p[i] & 0xc0) != 0x80) {
      *cursor = p + 1;
      return 0xfffd;
    }
    value = (value << 6) | (uint32_t)(p[i] & 0x3f);
  }
  *cursor = p + extra + 1;
  return value;
}

int main(int argc, char **argv) {
  unsigned delay_ms = DEFAULT_KEY_DELAY_MS;
  int argi = 1;
  while (argi < argc && !strcmp(argv[argi], "-d")) {
    if (argi + 1 >= argc) {
      fprintf(stderr, "vkbd: -d needs a millisecond value\n");
      return 2;
    }
    delay_ms = (unsigned)atoi(argv[argi + 1]);
    argi += 2;
  }
  if (argi >= argc) {
    fprintf(stderr, "usage: vkbd [-d MS] TEXT\n");
    return 2;
  }
  const char *text = argv[argi];

  struct wl_display *display = wl_display_connect(NULL);
  if (!display) {
    fprintf(stderr, "vkbd: cannot connect to WAYLAND_DISPLAY=%s\n",
            getenv("WAYLAND_DISPLAY") ? getenv("WAYLAND_DISPLAY") : "(unset)");
    return 1;
  }
  struct wl_registry *registry = wl_display_get_registry(display);
  wl_registry_add_listener(registry, &registry_listener, NULL);
  wl_display_roundtrip(display);
  if (!seat || !manager) {
    fprintf(stderr, "vkbd: compositor offers no %s\n",
            manager ? "wl_seat" : "zwp_virtual_keyboard_manager_v1");
    return 1;
  }

  struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
  if (!context) {
    fprintf(stderr, "vkbd: no xkb context\n");
    return 1;
  }
  // The compositor is told exactly this keymap, so the layout the keycodes are
  // resolved against and the layout they are interpreted in are the same one.
  // Anything else types mojibake on a non-US host.
  struct xkb_rule_names names = {
      .rules = NULL, .model = NULL, .layout = "us", .variant = NULL, .options = NULL};
  struct xkb_keymap *keymap = xkb_keymap_new_from_names(context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
  if (!keymap) {
    fprintf(stderr, "vkbd: cannot compile a keymap\n");
    return 1;
  }
  char *keymap_text = xkb_keymap_get_as_string(keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
  if (!keymap_text) {
    fprintf(stderr, "vkbd: cannot serialise the keymap\n");
    return 1;
  }
  size_t keymap_size = 0;
  int fd = keymap_fd(keymap_text, &keymap_size);
  if (fd < 0) {
    fprintf(stderr, "vkbd: cannot publish the keymap: %s\n", strerror(errno));
    return 1;
  }

  struct zwp_virtual_keyboard_v1 *keyboard =
      zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(manager, seat);
  zwp_virtual_keyboard_v1_keymap(keyboard, XKB_KEYMAP_FORMAT_TEXT_V1, fd, (uint32_t)keymap_size);
  close(fd);
  // The keymap must be in the compositor's hands before any key refers to it.
  wl_display_roundtrip(display);

  const xkb_mod_index_t shift_index = xkb_keymap_mod_get_index(keymap, XKB_MOD_NAME_SHIFT);
  const uint32_t shift_mask = shift_index == XKB_MOD_INVALID ? 0 : (1u << shift_index);

  int unavailable = 0;
  int typed = 0;
  const unsigned char *cursor = (const unsigned char *)text;
  int shift_held = 0;
  while (*cursor) {
    uint32_t codepoint = next_codepoint(&cursor);
    xkb_keysym_t keysym = xkb_utf32_to_keysym(codepoint);
    struct key_site site = locate(keymap, keysym);
    if (!site.found) {
      fprintf(stderr, "vkbd: no key produces U+%04X in this keymap\n", codepoint);
      unavailable++;
      continue;
    }
    if (site.needs_shift != shift_held) {
      zwp_virtual_keyboard_v1_modifiers(keyboard, site.needs_shift ? shift_mask : 0, 0, 0, 0);
      shift_held = site.needs_shift;
    }
    uint32_t key = (uint32_t)site.keycode - EVDEV_OFFSET;
    zwp_virtual_keyboard_v1_key(keyboard, now_ms(), key, WL_KEYBOARD_KEY_STATE_PRESSED);
    zwp_virtual_keyboard_v1_key(keyboard, now_ms(), key, WL_KEYBOARD_KEY_STATE_RELEASED);
    wl_display_flush(display);
    typed++;
    if (delay_ms) {
      struct timespec pause = {.tv_sec = delay_ms / 1000,
                               .tv_nsec = (long)(delay_ms % 1000) * 1000000L};
      nanosleep(&pause, NULL);
    }
  }
  if (shift_held) {
    zwp_virtual_keyboard_v1_modifiers(keyboard, 0, 0, 0, 0);
  }
  // Every key must reach the compositor before this process exits, or the
  // client is left with a partial string and no error - the exact failure this
  // tool replaced.
  wl_display_roundtrip(display);
  zwp_virtual_keyboard_v1_destroy(keyboard);
  wl_display_roundtrip(display);
  wl_display_disconnect(display);

  free(keymap_text);
  xkb_keymap_unref(keymap);
  xkb_context_unref(context);

  if (unavailable) {
    fprintf(stderr, "vkbd: typed %d character(s), %d unavailable\n", typed, unavailable);
    return 1;
  }
  return 0;
}
