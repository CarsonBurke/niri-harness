// One full-output SHM capture. No composition, scaling, input or clipboard.
// Protocol XML is vendored from wayland-protocols-wlr 0.3.12 (MIT).
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-screencopy-client-protocol.h"

#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "vcap currently supports little-endian XRGB8888 only"
#endif

#define MAX_BYTES (64u * 1024u * 1024u)
static struct wl_display *display;
static struct wl_registry *registry;
static struct wl_output *output;
static struct wl_shm *shm;
static struct wl_buffer *buffer;
static struct zwlr_screencopy_manager_v1 *manager;
static struct zwlr_screencopy_frame_v1 *frame;
static uint32_t output_global, output_count, wanted_w, wanted_h;
static uint32_t width, height, stride, flags;
static int32_t mode_w, mode_h, transform = -1, wanted_transform;
static char output_name[129];
static const char *wanted_name;
static bool failed, ready, got_buffer, got_flags, got_done, got_xrgb;
static void *pixels = MAP_FAILED;
static size_t size;
static int64_t deadline;

static int64_t now_ms(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts)) { failed = true; return 0; }
  return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

// prepare_read/flush/poll bounds all dispatch, including registry sync. The
// existing Python capture timeout remains the outer child/reap boundary.
static bool dispatch(void) {
  if (failed || now_ms() >= deadline) return false;
  while (wl_display_prepare_read(display) != 0) {
    int count = wl_display_dispatch_pending(display);
    if (count < 0 || failed || now_ms() >= deadline) return false;
    if (count > 0) return true;
  }
  int rc = wl_display_flush(display);
  if (rc < 0 && errno != EAGAIN) {
    wl_display_cancel_read(display);
    return false;
  }
  struct pollfd fd = {wl_display_get_fd(display), POLLIN, 0};
  if (rc < 0) fd.events |= POLLOUT;
  int64_t left = deadline - now_ms();
  if (left <= 0) { wl_display_cancel_read(display); return false; }
  rc = poll(&fd, 1, (int)left);
  if (rc <= 0 || (fd.revents & (POLLERR | POLLHUP | POLLNVAL))) {
    wl_display_cancel_read(display);
    return false;
  }
  if (fd.revents & POLLIN) {
    if (wl_display_read_events(display) < 0) return false;
  } else {
    wl_display_cancel_read(display);
  }
  int count = wl_display_dispatch_pending(display);
  return count >= 0 && !failed && now_ms() < deadline;
}

static void sync_done(void *data, struct wl_callback *cb, uint32_t serial) {
  *(bool *)data = true;
  wl_callback_destroy(cb);
}
static const struct wl_callback_listener sync_listener = {.done = sync_done};
static bool sync_display(void) {
  bool done = false;
  struct wl_callback *cb = wl_display_sync(display);
  wl_callback_add_listener(cb, &sync_listener, &done);
  while (!done && !failed) if (!dispatch()) return false;
  return done && !failed;
}

static void out_geometry(void *d, struct wl_output *o, int32_t x, int32_t y,
                         int32_t pw, int32_t ph, int32_t sub, const char *make,
                         const char *model, int32_t tr) { transform = tr; }
static void out_mode(void *d, struct wl_output *o, uint32_t f, int32_t w,
                     int32_t h, int32_t refresh) {
  if (f & WL_OUTPUT_MODE_CURRENT) { mode_w = w; mode_h = h; }
}
static void out_done(void *d, struct wl_output *o) {}
static void out_scale(void *d, struct wl_output *o, int32_t factor) {}
static void out_name(void *d, struct wl_output *o, const char *name) {
  if (strlen(name) >= sizeof(output_name)) { failed = true; return; }
  strcpy(output_name, name);
}
static void out_description(void *d, struct wl_output *o, const char *description) {}
static const struct wl_output_listener output_listener = {
  .geometry = out_geometry, .mode = out_mode, .done = out_done,
  .scale = out_scale, .name = out_name, .description = out_description,
};
static void shm_format(void *d, struct wl_shm *s, uint32_t format) {
  if (format == WL_SHM_FORMAT_XRGB8888) got_xrgb = true;
}
static const struct wl_shm_listener shm_listener = {.format = shm_format};
static void buffer_release(void *d, struct wl_buffer *b) {}
static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};
static void global(void *d, struct wl_registry *reg, uint32_t name,
                   const char *interface, uint32_t version) {
  if (!strcmp(interface, wl_output_interface.name)) {
    output_count++;
    if (output_count != 1 || version < 4) { failed = true; return; }
    output_global = name;
    output = wl_registry_bind(reg, name, &wl_output_interface, 4);
    wl_output_add_listener(output, &output_listener, NULL);
  } else if (!strcmp(interface, wl_shm_interface.name)) {
    if (shm) { failed = true; return; }
    shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
    wl_shm_add_listener(shm, &shm_listener, NULL);
  } else if (!strcmp(interface, zwlr_screencopy_manager_v1_interface.name)) {
    if (manager || version < 3) { failed = true; return; }
    manager = wl_registry_bind(reg, name, &zwlr_screencopy_manager_v1_interface, 3);
  }
}
static void removed(void *d, struct wl_registry *reg, uint32_t name) {
  if (name == output_global) failed = true;
}
static const struct wl_registry_listener registry_listener = {
  .global = global, .global_remove = removed,
};

static void frame_buffer(void *d, struct zwlr_screencopy_frame_v1 *f,
                         uint32_t format, uint32_t w, uint32_t h, uint32_t s) {
  if (got_buffer || format != WL_SHM_FORMAT_XRGB8888 || w != wanted_w ||
      h != wanted_h || s < w * 4u || s % 4u || s > w * 4u + 4096u ||
      (uint64_t)s * h > MAX_BYTES) { failed = true; return; }
  got_buffer = true; width = w; height = h; stride = s;
}
static void frame_flags(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t value) {
  if (got_flags || (value & ~ZWLR_SCREENCOPY_FRAME_V1_FLAGS_Y_INVERT)) failed = true;
  got_flags = true; flags = value;
}
static void frame_ready(void *d, struct zwlr_screencopy_frame_v1 *f,
                        uint32_t hi, uint32_t lo, uint32_t ns) {
  if (!buffer || !got_flags || ns >= 1000000000u) failed = true;
  ready = true;
}
static void frame_failed(void *d, struct zwlr_screencopy_frame_v1 *f) { failed = true; }
static void frame_damage(void *d, struct zwlr_screencopy_frame_v1 *f,
                         uint32_t x, uint32_t y, uint32_t w, uint32_t h) { failed = true; }
static void frame_dmabuf(void *d, struct zwlr_screencopy_frame_v1 *f,
                         uint32_t format, uint32_t w, uint32_t h) {}
static void frame_done(void *d, struct zwlr_screencopy_frame_v1 *f) {
  if (got_done || !got_buffer || failed) { failed = true; return; }
  got_done = true;
  size = (size_t)stride * height;
  int fd = memfd_create("niri-harness-frame", MFD_CLOEXEC);
  if (fd < 0) { failed = true; return; }
  if (ftruncate(fd, (off_t)size)) { close(fd); failed = true; return; }
  pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (pixels == MAP_FAILED) { close(fd); failed = true; return; }
  struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, (int32_t)size);
  buffer = wl_shm_pool_create_buffer(pool, 0, width, height, stride, WL_SHM_FORMAT_XRGB8888);
  wl_buffer_add_listener(buffer, &buffer_listener, NULL);
  wl_shm_pool_destroy(pool);
  close(fd);
  zwlr_screencopy_frame_v1_copy(f, buffer);
}
static const struct zwlr_screencopy_frame_v1_listener frame_listener = {
  .buffer = frame_buffer, .flags = frame_flags, .ready = frame_ready,
  .failed = frame_failed, .damage = frame_damage, .linux_dmabuf = frame_dmabuf,
  .buffer_done = frame_done,
};

static bool identity(void) {
  return !failed && output_count == 1 && !strcmp(output_name, wanted_name) &&
    mode_w == (int32_t)wanted_w && mode_h == (int32_t)wanted_h &&
    transform == wanted_transform;
}
static bool number(const char *s, uint32_t *value) {
  char *end; errno = 0; unsigned long n = strtoul(s, &end, 10);
  if (errno || !*s || *end || n == 0 || n > 32768) return false;
  *value = (uint32_t)n; return true;
}

int main(int argc, char **argv) {
  int status = 1;
  if (argc != 5 || strlen(argv[1]) == 0 || strlen(argv[1]) > 128 ||
      strspn(argv[1], "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-") != strlen(argv[1]) ||
      !number(argv[2], &wanted_w) || !number(argv[3], &wanted_h) ||
      (strcmp(argv[4], "0") && strcmp(argv[4], "6"))) return 2;
  wanted_name = argv[1]; wanted_transform = atoi(argv[4]);
  // Never silently use an inherited connection FD instead of the selected
  // private WAYLAND_DISPLAY. No display fallback or compositor mutation.
  if (!getenv("WAYLAND_DISPLAY") || !*getenv("WAYLAND_DISPLAY")) return 2;
  unsetenv("WAYLAND_SOCKET");
  deadline = now_ms() + 20000;
  display = wl_display_connect(NULL);
  if (!display) goto done;
  registry = wl_display_get_registry(display);
  wl_registry_add_listener(registry, &registry_listener, NULL);
  if (!sync_display() || !sync_display() || !identity() || !shm || !got_xrgb || !manager) goto done;
  frame = zwlr_screencopy_manager_v1_capture_output(manager, 0, output);
  zwlr_screencopy_frame_v1_add_listener(frame, &frame_listener, NULL);
  while (!ready && !failed) if (!dispatch()) goto done;
  if (!ready || !identity() || !sync_display() || !identity()) goto done;
  // Header describes untouched SHM rows; Python performs only lossless
  // channel/orientation conversion. The raster never passes through grim.
  if (printf("{\"name\":\"%s\",\"width\":%u,\"height\":%u,\"stride\":%u,"
             "\"format\":\"XRGB8888LE\",\"transform\":%d,\"flags\":%u}\n",
             output_name, width, height, stride, transform, flags) < 0 ||
      fwrite(pixels, 1, size, stdout) != size || fflush(stdout)) goto done;
  status = 0;
done:
  if (frame) zwlr_screencopy_frame_v1_destroy(frame);
  if (buffer) wl_buffer_destroy(buffer);
  if (pixels != MAP_FAILED) munmap(pixels, size);
  if (manager) zwlr_screencopy_manager_v1_destroy(manager);
  if (shm) wl_shm_destroy(shm);
  if (output) wl_output_release(output);
  if (registry) wl_registry_destroy(registry);
  if (display) wl_display_disconnect(display);
  if (status) fprintf(stderr, "full-output capture refused or failed\n");
  return status;
}
