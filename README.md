# niri-harness

Drive a native Linux desktop application from a script, inside an isolated
nested [niri](https://github.com/YaLTeR/niri) session, and read the result back
as text.

It exists because verifying a GUI change on a Wayland desktop otherwise costs an
enormous amount of effort and produces unreliable evidence: you screenshot, look
at the image, guess pixel coordinates, click, screenshot again, and hope. This
turns that loop into commands with names:

```console
$ niri-harness session start app --root /tmp/run --cmd 'exec ./target/debug/my-app'
session=app display=wayland-3 output=1600x1000 transform=Normal
window=2 dev.example.MyApp size=[1600, 1000] focused=True title='My App'

$ niri-harness observe
text runs (76):
  [t3]     9,   64   55x16  Projects 1
  [t8]  1617,   66   35x13  Open
  [t50]   45,  833   42x12  Files (7)
  ...

$ niri-harness click "Open repository" --settle 3
action=click t8 'Open repository' at 1634,72
frame_changed_fraction=0.99996
changed=True
```

You click *labels*, not coordinates. The harness locates on-screen text with OCR
and reports what it clicked, whether the screen actually changed as a result,
and where every screenshot went.

## Isolation

The application runs inside its own nested compositor with its own output, its
own pointer and its own keyboard. Your focused window, your workspace, your
pointer position and your clipboard are never touched, so a run can proceed
while you work.

The harness's own window on the host is tiled like any other and does **not**
take focus: the self-test asserts that it is neither focused nor floating, so a
session cannot interrupt what you are doing. Nothing in the host's layout is
moved, resized or refocused.

Input is injected through `zwlr_virtual_pointer_v1` and `wtype` against the
*nested* compositor only. The nested niri disables animations, hot corners,
gaps and key repeat: repeat matters because `wtype` presses and releases faster
than repeat detection settles, so with it on, typing `echo` delivers
`eeeeeeeeeeechhhhhhhhhhhooooo`.

### Viewport

Because the nested compositor draws into a host window, the host's tiling
decides the framebuffer's shape, and taking that shape by force would mean
floating or refocusing the window. `--width` instead sets the nested output's
scale, so the application lays out against the logical width you asked for:

```console
fit: viewport 1598x1770 logical (framebuffer 1905x2110, scale 1.191)
```

Width is the dimension a layout responds to - whether a sidebar collapses or a
tab strip compacts - and screenshots and pointer coordinates stay in
framebuffer pixels, so clicking is unaffected. `--width host` accepts the host's
own sizing.

`--portal` additionally puts the session on a private D-Bus with its own
`portals.conf`, so a GTK or COSMIC file chooser opens *inside* the nested
session instead of escaping to your desktop.

## Install

```console
$ make          # builds the pointer injector, fetches the OCR model
$ make check    # runs the self-test
```

Requires `niri`, `wayland-scanner`, a C compiler, `pkg-config`,
`wayland-client`, `tesseract`, `wtype`, `grim` and Python 3.11+ with Pillow.
The first command that needs an artefact runs `make` for you.

## Commands

Global flags come **before** the subcommand: `niri-harness --no-heal observe`.

| Command | What it does |
| --- | --- |
| `session start NAME --root DIR --cmd CMD` | Start a nested session running `CMD`. Fails if the application never appears. |
| `session list` / `session stop NAME` | Inspect and end sessions. |
| `observe` | Heal, screenshot, and report window geometry plus every located label. |
| `click TARGET` | Click a label, a `tN` reference or `X,Y`. `--double`, `--button right`. |
| `type TEXT` / `key CHORD` | Keyboard input, e.g. `key ctrl+shift+w`. |
| `scroll AXIS AMOUNT` | Scroll, optionally `--at TARGET`. |
| `drag FROM TO` | Press, move, release. |
| `wait TEXT` | Wait for text to appear, or `--gone` to disappear. |
| `shot NAME` | Keep a screenshot; `--scale`, `--region`. |
| `heal` | Focus and maximise the window, and report what it repaired. |
| `logs` | Tail the application's output. |

`--json` on any command emits the same payload as JSON.

### Targets

A target is a label, a reference or a coordinate:

- `"Open repository"` — matched case-insensitively, exact match preferred over
  substring. An ambiguous substring is an error naming the candidates, never a
  guess. Repeated exact labels resolve to the first in reading order and the
  action line says so: `(first of 3; others t9@..., t14@...)`.
- `t14` — a reference from the last `observe`.
- `640,480` — explicit output coordinates.

OCR sometimes glues an adjacent icon onto a label, so `observe` may report
`se cagents-smoke-fixture` or `M © src/main.rs`. Substring matching absorbs
that.

### Evidence

Every action reports `frame_changed_fraction` and `changed`. A click that should
have done something and reports `changed=False` is a real finding: the harness
distinguishes "I clicked and nothing happened" from "I clicked the wrong place",
which a screenshot alone cannot.

`observe` also reports `painted`. A near-uniform frame is not treated as a
failure — a dark terminal holding one line legitimately looks like that — but it
is reported, because a blank window usually means the application is wedged.

## Failure modes it reports rather than hides

- **An application that cannot start.** The launcher records the command's exit
  status, so a bad path fails in under half a second with status and log tail,
  instead of after the full timeout with "no windows". A failed start does not
  stay registered: the nested compositor is still healthy, so liveness must not
  be inferred from it.
- **A dialog stealing focus.** `heal` focuses and maximises the main window,
  which is wrong while a modal is up. Pass `--no-heal` for dialog work.
- **An uncorrectable output transform.** Reported as
  `healed: transform Flipped180 not correctable`. Screenshot space and pointer
  space still agree; only the presentation is flipped.

## Self-test

```console
$ make check
```

Pure tests cover label matching, tile de-duplication, baseline merging and OCR
in **both** polarities — dark text on light and light text on dark — because a
desktop mixes both and reading only one silently loses half the screen. Two
end-to-end tests start real nested sessions: one runs a terminal and reads its
output back, the other asserts a nonexistent command fails immediately and
deregisters. They skip, loudly, when a dependency or host session is missing.

## Layout

```
bin/niri-harness   the whole tool
src/vptr.c         virtual-pointer injector for the nested compositor
share/nested.kdl   nested niri configuration: no animations, no hot corners
protocols/         wlr-virtual-pointer protocol XML
tests/             self-test
```

`NIRI_HARNESS_STATE` overrides where session records live.
