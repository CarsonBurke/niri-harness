# niri-harness

Script a native Linux GUI application by name, not by pixel.

`niri-harness` runs an application inside its own nested, headless
[niri](https://github.com/YaLTeR/niri) compositor, tells you what controls it
can see, lets you click and type against those controls by label, and reports
in plain text whether each action actually changed the screen. It was built
for coding agents that need to verify GUI changes without reading screenshots,
and it works just as well from a shell script or a test suite.

```console
$ niri-harness session start app --root /tmp/run --cmd 'exec ./target/debug/my-app'
session=app display=wayland-2 output=1600x1000 transform=Normal
window=2 dev.example.MyApp size=[1600, 1000] focused=True title='My App'
fit: viewport 1600x1000 at scale 1 (headless; no host window)

$ niri-harness observe
...
controls the application declares, then what is only on screen (12):
  [a1]    39,    0   75x35  Open repository [push button]
  [a2]   822,   41  233x34  src/main.rs [page tab]
  [a5]  1438,  143   89x34  Build [push button] (disabled)
  ...

$ niri-harness click "Open repository" --settle 3
action=click a1 push button 'Open repository' at 76,17
frame_changed_fraction=0.99996
changed=True
...

$ niri-harness session stop app
```

## Why

Checking a GUI change on Wayland usually goes: take a screenshot, look at it,
guess a coordinate, click, take another screenshot, and hope. That is slow, the
evidence is weak, and it takes over your desktop while it runs.

niri-harness replaces that loop with:

- **Named targets.** Controls come from the application's accessibility tree,
  with OCR as the fallback, so you write `click Save`, not `click 812,44`.
- **Evidence you can check.** Every action reports how much of the frame
  changed. `changed=False` after a click that should have done something is a
  real finding, not a guess.
- **Full isolation.** By default, your windows, focus, pointer, clipboard and D-Bus session
  are never touched, so a run can go on while you keep working.

## Requirements

- Linux with [niri](https://github.com/YaLTeR/niri) installed. You do not need
  to be running niri as your desktop.
- Python 3.11+ with [Pillow](https://python-pillow.org/) and NumPy
- `tesseract`, `grim`, `wtype`, `Xvfb`, and `dbus-run-session` (from dbus)
- To build: `make`, `curl`, a C compiler, `pkg-config`, `wayland-scanner`, and the
  `wayland-client` and `xkbcommon` development headers
- Optional: `at-spi2-core` and PyGObject. With them, `observe` reads the
  application's own widget tree. Without them it uses OCR only.

`Xvfb` is only needed for the default headless backend, and
`dbus-run-session` is not needed with `--host-bus`.

## Install

```console
$ make                                 # build the input injectors, fetch the OCR model
$ make check                           # run the self-test
$ make PREFIX="$HOME/.local" install   # or leave PREFIX at /usr/local
$ niri-harness --help
```

`make install` puts `niri-harness` in `$PREFIX/bin` and its runtime files in
`$PREFIX/lib/niri-harness`. `DESTDIR` works for package staging. If you run
`bin/niri-harness` from a checkout, it calls `make` itself the first time it
needs a build artifact.

## Usage

### 1. Start a session

```console
$ niri-harness session start NAME --root DIR --cmd 'CMD'
```

This starts a nested compositor, runs `CMD` through a shell inside it, and
waits for a window to appear. `DIR` receives the logs, screenshots and state
for the session. If the command exits without mapping a window, `start` fails
at once with its exit status and the end of the log. If it keeps running but
never maps one, `start` fails after `--timeout`. A failed start is not left
registered.

| Option | Effect |
| --- | --- |
| `--width PX` | Logical width the app lays out against (default 1600; 320 to 7680 headless). `host` accepts whatever size the backend gives. |
| `--backend window` | Draw into a visible window on your desktop instead of headless `Xvfb`, so you can watch. |
| `--host-bus` | Share your D-Bus session instead of getting a private one. This also turns off the accessibility tree, so the session uses OCR only. |
| `--portal {gtk,gnome,cosmic,kde}` | Make file-chooser portals open inside the session. Cannot be combined with `--host-bus`. |
| `--env K=V` | Set an environment variable for the application. Repeatable. |
| `--no-window-wait` | Return once the compositor is up, for commands that never open a window. |
| `--timeout SECONDS` | How long to wait for the window (default 40). |

`session list` shows sessions and `session stop [NAME]` tears one down.
`session env NAME` prints `export` lines for the nested `WAYLAND_DISPLAY` and
`NIRI_SOCKET`, so you can point your own Wayland tools at the session. The
session's private D-Bus address is in `DIR/session.bus`.

### 2. Look

```console
$ niri-harness observe
```

This prints the window's geometry and every control or text run it can find.
Each one has a reference: `aN` for controls from the accessibility tree and
`tN` for text read off the screen. It also reports `painted`, which flags a
blank window. A blank window usually means the application is stuck.
`--no-text` skips OCR.

### 3. Act

| Command | What it does |
| --- | --- |
| `click TARGET` | Click. `--double`, `--button right\|middle`, `--stale-ok` to reuse the last `observe` instead of looking again. |
| `type TEXT` | Type literal text into the focused widget. |
| `key CHORD...` | Press chords, e.g. `key ctrl+s`, `key ctrl+shift+w Escape`. |
| `scroll v\|h NOTCHES` | Scroll; `--at TARGET` moves the pointer there first. |
| `drag FROM TO` | Press at one target, move to another, release. `--steps`, `--button`. |
| `wait TEXT` | Block until text appears, or until it disappears with `--gone`. `--timeout`. |
| `shot NAME` | Save a screenshot. `--scale`, `--region LEFT,TOP,RIGHT,BOTTOM`. |
| `heal` | Close an open Overview, focus and maximise the window, and report what it repaired. Also runs before every action. |
| `logs` | Show the end of the application's output. `--lines`. |

`click`, `type`, `key`, `scroll` and `drag` accept `--settle SECONDS` to wait
for the UI to settle and `--shot NAME` to keep the resulting frame. They all
report `frame_changed_fraction` and `changed`.

### Targets

A target can be:

- **A label**, such as `"Open repository"`. Matching ignores case, and an exact
  match beats a substring match. A substring that matches more than one thing
  is an error that lists the candidates. When a label appears more than once,
  the harness prefers an operable control over plain text, then an enabled
  control over a disabled one, then reading order. The output names the ones it
  skipped.
- **A reference** from the last `observe`, such as `a14` or `t3`.
- **Coordinates**: `640,480`, in the same framebuffer pixels that `observe`
  reports. In a headless session these equal logical pixels. Under
  `--backend window` the output may be scaled, and the harness converts for
  you.

The harness refuses to click a control the application reports as disabled.
Pass its coordinates if you want to click it anyway.

OCR sometimes glues an icon onto a label, so you might see `M © src/main.rs`.
Substring matching means `click src/main.rs` still works.

### Global options

Put these **before** the subcommand, e.g. `niri-harness --json observe`.

| Option | Effect |
| --- | --- |
| `--session NAME` | Which session to use. Optional when only one is running. |
| `--window TEXT` | Act on the window whose app id or title contains `TEXT`. |
| `--json` | Print the same result as JSON. |
| `--no-heal` | Skip the focus and maximise step. Use this when a dialog is open. |
| `--observer auto\|a11y\|ocr` | Where labels come from (see below). |

## How it finds controls

With `--observer auto` (the default), the harness asks the application's
accessibility tree first. The tree gives names, roles and enabled state, and
reading it takes about a hundred milliseconds, against roughly a second for an
OCR pass. Each session has its own accessibility bus, so only its own windows
show up.

Trees are often incomplete. Terminals, games and canvases usually expose
nothing, and some toolkits leave items unnamed; GTK4 menu items are a common
case. So `auto` also reads the screen, merges the two sources, and drops OCR
readings that repeat a control the tree already described. Before reporting a
label as missing, it always checks the screen too.

- `--observer a11y` uses only the tree. It is the fastest option and the one to
  use in tight loops. It refuses `wait --gone` for a label it has never seen,
  because a label missing from the tree may still be on screen.
- `--observer ocr` uses only the screen.

Accessibility coordinates are relative to the window, and Wayland does not tell
a client where its window sits. The harness only trusts tree coordinates when
it can place the window: when the window fills the output, which `heal`
ensures, or when it is floating and niri reports its position. Otherwise it
falls back to OCR. When the tree was available but not used, `observe` says
why.

## Isolation

Each session gets:

- **Its own compositor.** By default niri draws into a private `Xvfb`, so no
  window appears on your desktop. This also keeps the app fast. A hidden host
  window stops being painted, so input stalls; measured here, a click took
  3.7 s with a host window and 64 ms headless.
- **Its own input devices.** Pointer and keyboard input go through virtual
  devices on the nested compositor only.
- **Its own D-Bus session.** Without one, single-instance apps such as
  `gnome-text-editor` would pass their work to the copy already running on your
  desktop and exit.
- **Its own accessibility bus**, when `at-spi2-core` is installed and
  `--host-bus` is not used.

Screenshots use `grim`'s screencopy protocol rather than niri's screenshot
action, so they never touch your clipboard or send notifications.

`session stop` kills the session's whole process group, including the
accessibility bus, which otherwise outlives the app. Before it signals
anything, it checks both the recorded pid and that process's start time, so a
stale record can never kill an unrelated process that has reused the pid.

The nested niri config (`share/nested.kdl`) turns off animations, hot corners,
gaps and key repeat. Screenshots then show settled frames, the pointer cannot
open the Overview by accident, and injected keystrokes are not read as held
keys.

## Troubleshooting

- **A dialog keeps losing focus.** `heal` focuses the main window before every
  action. Pass `--no-heal` while you work with a modal dialog.
- **`click` says `changed=False`.** Either the click did nothing, which is worth
  investigating, or the UI needed longer. Retry with `--settle 2`.
- **`start` fails with "exited with status N without mapping a window".**
  Read the log tail it prints. If the app is single-instance, check that you did not pass
  `--host-bus`.
- **A control is missing from `observe`.** It may be disabled, off screen, or
  unreadable to OCR at the current size. Try `--observer ocr`, or a larger
  `--width`.

## Self-test

```console
$ make check
```

Unit tests cover label matching, merging the tree with OCR, per-window
attribution, process-group cleanup, and OCR of both dark-on-light and
light-on-dark text. End-to-end tests start real nested sessions. They check
that a terminal's output can be read back, that a bad command fails fast, that
a session does not share your D-Bus session, that a headless session maps no
host window, that a GTK app can be driven through its tree without reading any
pixels, and that `stop` leaves no processes behind.

Tests whose dependencies are missing are skipped, with a message. The
`--backend window` test also skips by default because it maps a window on your
desktop. Set `NIRI_HARNESS_TEST_HOST_WINDOW=1` to run it.

## Layout

```
bin/niri-harness      the CLI
share/a11y_probe.py   reads a session's accessibility tree (run as a subprocess)
share/nested.kdl      nested niri configuration
src/vptr.c            virtual pointer injector
src/vkbd.c            virtual keyboard injector for text
protocols/            Wayland protocol XML for the injectors
tests/                self-test
```

Session records live in `$XDG_RUNTIME_DIR/niri-harness`. Set
`NIRI_HARNESS_STATE` to use a different directory.
