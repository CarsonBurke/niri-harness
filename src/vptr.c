// Minimal virtual-pointer injector for a *nested* Wayland compositor.
// Injects only into the compositor named by WAYLAND_DISPLAY, so the host
// session's real pointer is never touched.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

#define BTN_LEFT 0x110
#define BTN_RIGHT 0x111
#define BTN_MIDDLE 0x112

static struct wl_seat *seat;
static struct wl_output *output;
static struct zwlr_virtual_pointer_manager_v1 *mgr;
static struct zwlr_virtual_pointer_v1 *ptr;
static struct wl_display *dpy;
static uint32_t out_w = 0, out_h = 0;

static uint32_t now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static void out_geom(void *d, struct wl_output *o, int32_t x, int32_t y,
                     int32_t pw, int32_t ph, int32_t sub, const char *make,
                     const char *model, int32_t tr) {}
static void out_mode(void *d, struct wl_output *o, uint32_t flags, int32_t w,
                     int32_t h, int32_t refresh) {
  if (flags & WL_OUTPUT_MODE_CURRENT) { out_w = (uint32_t)w; out_h = (uint32_t)h; }
}
static void out_done(void *d, struct wl_output *o) {}
static void out_scale(void *d, struct wl_output *o, int32_t f) {}
static void out_name(void *d, struct wl_output *o, const char *n) {}
static void out_desc(void *d, struct wl_output *o, const char *n) {}
static const struct wl_output_listener out_listener = {
    out_geom, out_mode, out_done, out_scale, out_name, out_desc};

static void reg_global(void *data, struct wl_registry *reg, uint32_t name,
                       const char *iface, uint32_t version) {
  if (!strcmp(iface, wl_seat_interface.name))
    seat = wl_registry_bind(reg, name, &wl_seat_interface, 1);
  else if (!strcmp(iface, wl_output_interface.name) && !output) {
    output = wl_registry_bind(reg, name, &wl_output_interface, 2);
    wl_output_add_listener(output, &out_listener, NULL);
  } else if (!strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name))
    mgr = wl_registry_bind(reg, name,
                           &zwlr_virtual_pointer_manager_v1_interface, 2);
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t n) {}
static const struct wl_registry_listener reg_listener = {reg_global, reg_remove};

static uint32_t btn_of(const char *s) {
  if (!strcmp(s, "left") || !strcmp(s, "l")) return BTN_LEFT;
  if (!strcmp(s, "right") || !strcmp(s, "r")) return BTN_RIGHT;
  if (!strcmp(s, "middle") || !strcmp(s, "m")) return BTN_MIDDLE;
  fprintf(stderr, "unknown button %s\n", s);
  exit(2);
}

// Relative motion is the only unambiguous way to place this pointer.
// `motion_absolute` takes an extent pair naming the caller's coordinate space,
// and the space reported by `wl_output.mode` is the output's PHYSICAL mode
// while every coordinate that `niri msg -j windows` reports is LOGICAL, so on
// any scaled output the two disagree by the scale factor and clicks land
// somewhere other than the requested point. Pinning to the origin with a
// saturating relative move and then stepping out by logical pixels needs no
// extents at all.
static void pin_origin(void) {
  for (int i = 0; i < 8; i++) {
    zwlr_virtual_pointer_v1_motion(ptr, now_ms(), wl_fixed_from_double(-8192.0),
                                   wl_fixed_from_double(-8192.0));
    zwlr_virtual_pointer_v1_frame(ptr);
  }
  wl_display_flush(dpy);
}

static void do_cmd(int argc, char **argv) {
  const char *op = argv[0];
  if (!strcmp(op, "m") && argc >= 3) {
    zwlr_virtual_pointer_v1_motion_absolute(ptr, now_ms(),
                                            (uint32_t)atoi(argv[1]),
                                            (uint32_t)atoi(argv[2]), out_w,
                                            out_h);
    zwlr_virtual_pointer_v1_frame(ptr);
  } else if (!strcmp(op, "r") && argc >= 3) {
    zwlr_virtual_pointer_v1_motion(ptr, now_ms(), wl_fixed_from_double(atof(argv[1])),
                                   wl_fixed_from_double(atof(argv[2])));
    zwlr_virtual_pointer_v1_frame(ptr);
  } else if (!strcmp(op, "c") && argc >= 2) {
    uint32_t b = btn_of(argv[1]);
    zwlr_virtual_pointer_v1_button(ptr, now_ms(), b, WL_POINTER_BUTTON_STATE_PRESSED);
    zwlr_virtual_pointer_v1_frame(ptr);
    wl_display_flush(dpy);
    struct timespec ts = {0, 40 * 1000000};
    nanosleep(&ts, NULL);
    zwlr_virtual_pointer_v1_button(ptr, now_ms(), b, WL_POINTER_BUTTON_STATE_RELEASED);
    zwlr_virtual_pointer_v1_frame(ptr);
  } else if (!strcmp(op, "d") && argc >= 2) {
    zwlr_virtual_pointer_v1_button(ptr, now_ms(), btn_of(argv[1]), WL_POINTER_BUTTON_STATE_PRESSED);
    zwlr_virtual_pointer_v1_frame(ptr);
  } else if (!strcmp(op, "u") && argc >= 2) {
    zwlr_virtual_pointer_v1_button(ptr, now_ms(), btn_of(argv[1]), WL_POINTER_BUTTON_STATE_RELEASED);
    zwlr_virtual_pointer_v1_frame(ptr);
  } else if (!strcmp(op, "s") && argc >= 3) {
    // s v|h <notches>  (positive vertical = scroll down, positive horizontal = right)
    uint32_t axis = (argv[1][0] == 'h') ? WL_POINTER_AXIS_HORIZONTAL_SCROLL
                                        : WL_POINTER_AXIS_VERTICAL_SCROLL;
    double notches = atof(argv[2]);
    int steps = (int)(notches < 0 ? -notches : notches);
    if (steps < 1) steps = 1;
    double per = notches / steps;
    for (int i = 0; i < steps; i++) {
      zwlr_virtual_pointer_v1_axis_source(ptr, WL_POINTER_AXIS_SOURCE_WHEEL);
      zwlr_virtual_pointer_v1_axis_discrete(ptr, now_ms(), axis,
                                            wl_fixed_from_double(per * 15.0),
                                            per > 0 ? 1 : -1);
      zwlr_virtual_pointer_v1_frame(ptr);
      wl_display_flush(dpy);
      struct timespec ts = {0, 25 * 1000000};
      nanosleep(&ts, NULL);
    }
  } else if (!strcmp(op, "w") && argc >= 2) {
    wl_display_flush(dpy);
    struct timespec ts = {atoi(argv[1]) / 1000,
                          (long)(atoi(argv[1]) % 1000) * 1000000L};
    nanosleep(&ts, NULL);
  } else if (!strcmp(op, "home") && argc >= 1) {
    pin_origin();
  } else if (!strcmp(op, "a") && argc >= 3) {
    // Absolute placement in logical pixels, via the origin.
    pin_origin();
    double x = atof(argv[1]), y = atof(argv[2]);
    // Step in bounded increments so no single motion is discarded as a jump.
    double done_x = 0, done_y = 0;
    while (done_x < x || done_y < y) {
      double dx = x - done_x, dy = y - done_y;
      if (dx > 256.0) dx = 256.0;
      if (dy > 256.0) dy = 256.0;
      zwlr_virtual_pointer_v1_motion(ptr, now_ms(), wl_fixed_from_double(dx),
                                     wl_fixed_from_double(dy));
      zwlr_virtual_pointer_v1_frame(ptr);
      done_x += dx;
      done_y += dy;
    }
    wl_display_flush(dpy);
  } else {
    fprintf(stderr, "bad command: %s\n", op);
    exit(2);
  }
  wl_display_flush(dpy);
}

int main(int argc, char **argv) {
  dpy = wl_display_connect(NULL);
  if (!dpy) { fprintf(stderr, "no wayland display\n"); return 1; }
  struct wl_registry *reg = wl_display_get_registry(dpy);
  wl_registry_add_listener(reg, &reg_listener, NULL);
  wl_display_roundtrip(dpy);
  wl_display_roundtrip(dpy);
  if (!mgr || !seat) { fprintf(stderr, "missing virtual pointer manager\n"); return 1; }
  fprintf(stderr, "extents=%ux%u output=%p\n", out_w, out_h, (void*)output);
  if (getenv("VPTR_NO_OUTPUT"))
    ptr = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(mgr, seat);
  else
    ptr = zwlr_virtual_pointer_manager_v1_create_virtual_pointer_with_output(
        mgr, seat, output);
  wl_display_roundtrip(dpy);

  // Commands: each argv group separated by ";" token, or read lines from stdin
  // when no args are given.
  if (argc > 1) {
    int start = 1;
    for (int i = 1; i <= argc; i++) {
      if (i == argc || !strcmp(argv[i], ";")) {
        if (i > start) do_cmd(i - start, &argv[start]);
        start = i + 1;
      }
    }
  } else {
    char line[512];
    while (fgets(line, sizeof line, stdin)) {
      char *tok[8]; int n = 0;
      for (char *p = strtok(line, " \t\n"); p && n < 8; p = strtok(NULL, " \t\n"))
        tok[n++] = p;
      if (n) do_cmd(n, tok);
    }
  }
  wl_display_roundtrip(dpy);
  zwlr_virtual_pointer_v1_destroy(ptr);
  wl_display_flush(dpy);
  wl_display_roundtrip(dpy);
  return 0;
}
