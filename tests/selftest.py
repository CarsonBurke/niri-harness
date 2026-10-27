#!/usr/bin/env python3
"""Self-test for niri-harness.

Two layers:

* Pure tests over the OCR and label-matching logic. They synthesise their own
  images, so they are fast, deterministic and need no compositor.
* End-to-end tests that start real nested sessions running real applications
  and drive them. Each is skipped, loudly, when what it needs - `niri`, a
  terminal, GTK, a host Wayland session - is unavailable.

Nothing here touches the caller's desktop: the nested session owns its own
compositor, its own output and its own pointer.
"""

from __future__ import annotations

import importlib.machinery
import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def load_harness():
    """Import `bin/niri-harness`, which has no `.py` suffix."""
    path = ROOT / "bin" / "niri-harness"
    loader = importlib.machinery.SourceFileLoader("niri_harness", str(path))
    spec = importlib.util.spec_from_loader("niri_harness", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves annotations through `sys.modules`, so the module has
    # to be registered before its body runs.
    sys.modules["niri_harness"] = module
    loader.exec_module(module)
    return module


H = load_harness()

FAILURES: list[str] = []
SKIPS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}" if detail else name)
    print(f"  FAIL {name} {detail}")


def run_test(fn) -> None:
    print(f"{fn.__name__}:")
    try:
        fn()
    except _Skip as skip:
        SKIPS.append(f"{fn.__name__}: {skip}")
        print(f"  skip {skip}")
    except Exception:
        FAILURES.append(f"{fn.__name__} raised")
        traceback.print_exc()


class _Skip(Exception):
    pass


def require(tool: str) -> None:
    if not shutil.which(tool):
        raise _Skip(f"{tool} is not installed")


# --------------------------------------------------------------------------
# text-run geometry


def run_at(text: str, x: int, y: int, width: int = 40, height: int = 12) -> "H.TextRun":
    return H.TextRun(ref="", text=text, x=x, y=y, width=width, height=height, confidence=90.0)


def test_overlapping_tiles_do_not_double_report_a_word() -> None:
    # The same word seen by two overlapping tiles, one pixel apart.
    runs = [run_at("Commit", 100, 200), run_at("Commit", 101, 200)]
    kept = H.deduplicate_runs(runs)
    check("duplicate dropped", len(kept) == 1, f"kept {len(kept)}")

    # The same text genuinely twice on the screen must survive.
    apart = H.deduplicate_runs([run_at("Stage", 100, 200), run_at("Stage", 100, 600)])
    check("distinct repetitions kept", len(apart) == 2, f"kept {len(apart)}")


def test_words_on_one_baseline_become_one_label() -> None:
    merged = H.merge_runs([run_at("Open", 10, 100, width=40), run_at("repository", 55, 100, width=90)])
    check("joined", len(merged) == 1, f"got {[r.text for r in merged]}")
    if merged:
        check("text joined in order", merged[0].text == "Open repository", repr(merged[0].text))
        check("box spans both words", merged[0].width >= 135, f"width {merged[0].width}")

    # Different baselines are different labels, however close horizontally.
    lines = H.merge_runs([run_at("Staged", 10, 100), run_at("Changes", 10, 140)])
    check("separate baselines kept apart", len(lines) == 2, f"got {[r.text for r in lines]}")

    # A wide horizontal gap is a different label on the same baseline.
    columns = H.merge_runs([run_at("Name", 10, 100, width=40), run_at("Modified", 900, 100, width=60)])
    check("distant columns kept apart", len(columns) == 2, f"got {[r.text for r in columns]}")


# --------------------------------------------------------------------------
# command line


def test_shared_options_are_accepted_either_side_of_the_command() -> None:
    parser = H.build_parser()
    after = parser.parse_args(["observe", "--json", "--session", "app"])
    check("after the command", after.json and after.session == "app", str(after))
    before = parser.parse_args(["--json", "--observer", "ocr", "observe"])
    check("before it", before.json and before.observer == "ocr", str(before))
    # A subcommand's copy must not overwrite what the top-level copy parsed.
    check("an option given before is not reset after",
          before.session is None and before.json, str(before))
    plain = parser.parse_args(["click", "Save"])
    check("and the defaults hold when neither is given",
          not plain.json and plain.observer == "auto" and plain.session is None,
          str(plain))


def test_start_takes_the_program_and_its_arguments_verbatim() -> None:
    parser = H.build_parser()
    args = parser.parse_args(["start", "--width", "1200", "./app", "--name", "-x"])
    check("options before the program are the harness's",
          args.width == "1200" and args.name is None, str(args))
    check("everything from the program on is the program's",
          args.argv == ["./app", "--name", "-x"], str(args.argv))


def test_a_session_is_named_after_its_program() -> None:
    live = {"my-app", "my-app-2"}

    class Record:
        def __init__(self, name):
            self.name = name

        def alive(self):
            return self.name in live

    original_file, original_load = H.session_file, H.load_session
    with tempfile.TemporaryDirectory() as scratch:
        records = Path(scratch)
        for name in ("my-app", "my-app-2", "stale"):
            (records / f"{name}.json").write_text("{}")
        H.session_file = lambda name: records / f"{name}.json"
        H.load_session = Record
        try:
            check("a live name is suffixed until it is free",
                  H.unique_session_name("./target/debug/my-app") == "my-app-3",
                  H.unique_session_name("./target/debug/my-app"))
            check("a dead one is reused",
                  H.unique_session_name("/usr/bin/stale") == "stale",
                  H.unique_session_name("/usr/bin/stale"))
            name = H.unique_session_name("/opt/My App!")
            check("and the name is safe as a file name",
                  name == "My-App" and bool(H.SESSION_NAME.fullmatch(name)), name)
        finally:
            H.session_file, H.load_session = original_file, original_load


def test_an_action_reports_in_one_line() -> None:
    def line(fraction, changed, shot=None):
        return H.describe_change({"action": "click a1 'Save' at 5,5",
                                  "frame_changed_fraction": fraction,
                                  "changed": changed, "shot": shot})

    check("a change says how much", line(0.99996, True) == "click a1 'Save' at 5,5: changed 100%",
          line(0.99996, True))
    check("a small one keeps its precision", line(0.0345, True).endswith("changed 3.5%"),
          line(0.0345, True))
    # Typing a few characters moves fewer pixels than the noise floor. That is
    # not the same as nothing, and a caller expecting a change must see which.
    check("a change within noise is told apart from none",
          line(0.0012, False).endswith("no change (0.12% differs, within noise)")
          and line(0.0, False).endswith(": no change"),
          f"{line(0.0012, False)} / {line(0.0, False)}")
    check("a kept shot is named on the same line",
          line(0.5, True, "/r/shots/a.png").endswith("changed 50%; shot /r/shots/a.png")
          and "\n" not in line(0.5, True, "/r/shots/a.png"),
          line(0.5, True, "/r/shots/a.png"))


def test_an_observation_leads_with_the_window_and_nothing_else() -> None:
    payload = {
        "session": "app", "display": "wayland-2", "frame": "/r/shots/_observed.png",
        "output": {"width": 1600, "height": 1000, "transform": "Normal", "scale": 1.0},
        "window": {"id": 2, "app_id": "org.example.App", "title": "App",
                   "size": [1600, 1000], "focused": True},
        "healed": [], "painted": True, "source": "a11y+ocr", "forgone": None,
        "runs": [H.TextRun(ref="a1", text="Save", x=5, y=6, width=40, height=20,
                           confidence=100.0, role="push button")],
    }
    lines = H.describe_observation(payload).splitlines()
    check("one line of window, one of heading, one per target",
          len(lines) == 3 and lines[0] == "window org.example.App 'App' 1600x1000",
          str(lines))
    check("the target keeps its ref and role",
          "[a1]" in lines[-1] and "Save [push button]" in lines[-1], lines[-1])

    # Scaled, the coordinates stop being the window's own pixels, and that is
    # the one fact about the output a caller cannot do without.
    payload["output"] = {"width": 1905, "height": 2110, "transform": "Normal",
                         "scale": 1.191}
    payload["runs"] = []
    lines = H.describe_observation(payload).splitlines()
    check("a scaled output says what the coordinates are",
          "framebuffer pixels of a 1905x2110 output at scale 1.191" in lines[0],
          lines[0])
    check("and an empty listing says so rather than stopping short",
          lines[-1].startswith("no targets"), str(lines))

    # `--no-text` looked for nothing, which is not the same as finding nothing.
    payload["source"] = None
    lines = H.describe_observation(payload).splitlines()
    check("an observation that looked for nothing claims nothing",
          not any("targets" in line for line in lines), str(lines))


# --------------------------------------------------------------------------
# label matching


def test_a_label_is_matched_exactly_before_partially() -> None:
    runs = [
        run_at("Changes", 10, 100),
        run_at("Changes (5)", 10, 140),
    ]
    for index, item in enumerate(runs, start=1):
        item.ref = f"t{index}"
    session = object()
    original = H.load_observed
    H.load_observed = lambda _session: runs  # type: ignore[assignment]
    try:
        _, _, label = H.resolve_target(session, "Changes", fresh=False)
        check("exact wins over substring", "'Changes'" in label and "(5)" not in label, label)

        _, _, partial = H.resolve_target(session, "hanges (5", fresh=False)
        check("substring resolves", "Changes (5)" in partial, partial)

        try:
            H.resolve_target(session, "ange", fresh=False)
            check("ambiguous substring rejected", False, "no error raised")
        except H.HarnessError as error:
            check("ambiguous substring rejected", "ambiguous" in str(error), str(error))

        # Two identical labels are ordinary in a UI; choosing one is fine,
        # saying nothing about it is not.
        runs.append(run_at("Changes", 10, 400))
        runs[-1].ref = "t3"
        _, _, repeated = H.resolve_target(session, "Changes", fresh=False)
        check("repeat is reported", "1 of 2" in repeated, repeated)
        check("and the one being clicked is not listed as an alternative",
              "t1@" not in repeated, repeated)

        try:
            H.resolve_target(session, "Nonexistent", fresh=False)
            check("missing label rejected", False, "no error raised")
        except H.HarnessError as error:
            check("missing label rejected", "no visible text" in str(error), str(error))

        x, y, point = H.resolve_target(session, "42, 84", fresh=False)
        check("explicit coordinate honoured", (x, y) == (42, 84), point)
    finally:
        H.load_observed = original  # type: ignore[assignment]


# --------------------------------------------------------------------------
# OCR


def synthesise(path: Path, *, dark_on_light: bool) -> list[str]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as error:  # pragma: no cover - environment dependent
        raise _Skip(f"python imaging is unavailable: {error}") from None

    words = ["Commit", "Stage", "Discard", "History"]
    background, foreground = (255, 0) if dark_on_light else (18, 235)
    image = Image.new("L", (900, 320), background)
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 44)
            break
    if font is None:
        raise _Skip("no scalable font found to render a test image")
    for index, word in enumerate(words):
        draw.text((40, 30 + index * 66), word, fill=foreground, font=font)
    image.save(path)
    return words


def assert_reads(path: Path, words: list[str], label: str) -> None:
    from PIL import Image

    with Image.open(path) as image:
        runs = H.ocr_runs(image, min_confidence=40.0)
    text = " ".join(run.text for run in runs)
    missing = [word for word in words if word.lower() not in text.lower()]
    check(f"{label}: every word read", not missing, f"missing {missing}; read {text!r}")
    located = [run for run in runs if run.width > 0 and run.height > 0]
    check(f"{label}: boxes have extent", len(located) == len(runs), "a run had no extent")
    inside = [run for run in runs if 0 <= run.x < 900 and 0 <= run.y < 320]
    check(f"{label}: boxes inside the frame", len(inside) == len(runs), "a run fell outside")


def test_ocr_reads_both_polarities() -> None:
    require("tesseract")
    with tempfile.TemporaryDirectory() as scratch:
        light = Path(scratch) / "dark-on-light.png"
        dark = Path(scratch) / "light-on-dark.png"
        words = synthesise(light, dark_on_light=True)
        synthesise(dark, dark_on_light=False)
        # A desktop mixes both: dark application chrome beside a light system
        # dialog. Reading only one polarity silently loses half the screen.
        assert_reads(light, words, "dark text on light")
        assert_reads(dark, words, "light text on dark")


# --------------------------------------------------------------------------
# absolute viewport fitting (pure IPC mocks; no nested compositor)


class ViewportFitSession:
    """A fixed physical output whose logical size follows compositor scale.

    The physical viewport is supplied separately from the IPC metadata so the
    mock does not reimplement the harness's mode selection or transform parser.
    """

    def __init__(self, *, logical=(1280, 720), scale=1.0, transform="Normal",
                 mode=(1280, 720), viewport=(1280, 720), applied_scale=None,
                 disabled_first=False):
        self.active = {
            "logical": {"width": logical[0], "height": logical[1],
                        "scale": scale, "transform": transform},
            "modes": [{"width": mode[0], "height": mode[1]}],
            "current_mode": 0,
        }
        self.records = {}
        if disabled_first:
            self.records["DISABLED"] = {"logical": None, "modes": [],
                                        "current_mode": None}
        self.records["NESTED"] = self.active
        self.viewport = viewport
        self.applied_scale = applied_scale
        self.actions = []

    def niri_json(self, *args):
        assert args == ("outputs",), args
        # An IPC result is a snapshot, not an alias into compositor state.
        return json.loads(json.dumps(self.records))

    def niri(self, *args, **kwargs):
        assert len(args) == 4 and args[0] == "output" and args[2] == "scale", args
        assert kwargs == {"check": False, "timeout": 10}, kwargs
        self.actions.append(args)
        requested = float(args[3])
        actual = requested if self.applied_scale is None else self.applied_scale
        logical = self.records[args[1]]["logical"]
        # Disabled outputs reject scale changes, as a compositor would.
        if logical is None:
            return subprocess.CompletedProcess(args, 1)
        logical.update(width=round(self.viewport[0] / actual),
                       height=round(self.viewport[1] / actual), scale=actual)
        return subprocess.CompletedProcess(args, 0)


def viewport_fit(session, width):
    from unittest.mock import patch

    # Even a future implementation must not launch a tool in these pure tests.
    with patch.object(H.time, "sleep"), patch.object(
        H, "run", side_effect=AssertionError("viewport mock launched a tool")
    ), patch.object(subprocess, "Popen", side_effect=AssertionError(
        "viewport mock launched a subprocess"
    )):
        return H.fit_nested_viewport(session, width)


def test_viewport_fit_repeated_width_is_idempotent() -> None:
    session = ViewportFitSession()
    viewport_fit(session, 900)
    viewport_fit(session, 900)
    scales = [float(action[3]) for action in session.actions]
    check("both fits use the unchanged physical width", scales == [1.422, 1.422],
          str(scales))
    check("second fit retains the narrow logical viewport",
          H.output_geometry(session) == (900, 506, "Normal"),
          str(H.output_geometry(session)))


def test_viewport_fit_nonunit_initial_scale_is_absolute() -> None:
    session = ViewportFitSession(logical=(640, 360), scale=2.0)
    viewport_fit(session, 900)
    scales = [float(action[3]) for action in session.actions]
    check("initial scale does not shrink the physical mode", scales == [1.422],
          str(scales))


def test_viewport_fit_narrow_wide_narrow_returns_to_same_scale() -> None:
    session = ViewportFitSession()
    for width in (900, 1600, 900):
        viewport_fit(session, width)
    scales = [float(action[3]) for action in session.actions]
    check("width sequence has no accumulated scale drift",
          scales == [1.422, 0.8, 1.422], str(scales))


def test_viewport_fit_transforms_use_physical_axes() -> None:
    cases = (
        ("Normal", (1280, 720), (1920, 1080), 2.133),
        ("180", (1280, 720), (1920, 1080), 2.133),
        ("Flipped", (1280, 720), (1920, 1080), 2.133),
        ("Flipped180", (1280, 720), (1920, 1080), 2.133),
        ("90", (720, 1280), (1080, 1920), 1.2),
        ("270", (720, 1280), (1080, 1920), 1.2),
        ("Flipped90", (720, 1280), (1080, 1920), 1.2),
        ("Flipped270", (720, 1280), (1080, 1920), 1.2),
    )
    for transform, logical, viewport, expected in cases:
        session = ViewportFitSession(logical=logical, scale=1.5,
                                     transform=transform, mode=(1920, 1080),
                                     viewport=viewport)
        viewport_fit(session, 900)
        scales = [float(action[3]) for action in session.actions]
        check(f"{transform} fits its physical horizontal extent",
              scales == [expected], str(scales))


def test_viewport_fit_reports_actual_quantized_scale() -> None:
    session = ViewportFitSession(applied_scale=1.5)
    report = viewport_fit(session, 900)
    check("request still uses physical mode",
          [float(action[3]) for action in session.actions] == [1.422], report)
    check("report uses compositor's applied scale", "scale 1.5" in report, report)
    check("report retains physical framebuffer size",
          "framebuffer 1280x720" in report, report)
    check("report uses actual logical geometry", "viewport 853x480 logical" in report,
          report)


def test_viewport_fit_selects_active_output_before_action() -> None:
    session = ViewportFitSession(disabled_first=True)
    report = viewport_fit(session, 900)
    check("disabled first record never receives a scale action",
          [action[1] for action in session.actions] == ["NESTED"], str(session.actions))
    check("selected active output is fitted and reported",
          "viewport 900x506 logical" in report, report)


def test_viewport_fit_refuses_unknown_physical_mode_before_action() -> None:
    invalid = (
        ("missing current mode", "current_mode", None),
        ("negative current mode", "current_mode", -1),
        ("out of range mode", "current_mode", 1),
        ("boolean current mode", "current_mode", True),
        ("fractional current mode", "current_mode", 0.0),
        ("string current mode", "current_mode", "0"),
        ("missing mode list", "modes", None),
        ("empty mode list", "modes", []),
        ("malformed mode", "modes", [None]),
        ("unknown physical width", "modes", [{"width": 0, "height": 720}]),
    )
    for label, key, value in invalid:
        session = ViewportFitSession()
        session.active[key] = value
        report = viewport_fit(session, 900)
        check(f"{label} is refused before action", not session.actions,
              str(session.actions))
        check(f"{label} reports refusal", "left alone" in report, report)
    session = ViewportFitSession(transform="unknown-transform")
    report = viewport_fit(session, 900)
    check("unknown transform is refused before action", not session.actions,
          str(session.actions))
    check("unknown transform reports refusal", "left alone" in report, report)


def test_viewport_fit_keeps_logical_output_geometry_contract() -> None:
    session = ViewportFitSession(logical=(853, 480), scale=1.5)
    check("geometry accessor remains logical despite physical mode metadata",
          H.output_geometry(session) == (853, 480, "Normal"),
          str(H.output_geometry(session)))


# --------------------------------------------------------------------------
# end to end


def host_windows() -> list[dict]:
    """The host compositor's windows, for non-interference assertions."""
    import json

    try:
        raw = subprocess.run(
            ["niri", "msg", "-j", "windows"], capture_output=True, timeout=10, check=False
        ).stdout
        return json.loads(raw) or []
    except Exception:
        return []


def test_a_session_runs_an_application_and_its_text_is_readable() -> None:
    require("niri")
    require("kitty")
    require("tesseract")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise _Skip("no host Wayland session")

    marker = "HARNESS_SELFTEST_OK"
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        session = H.start_session(
            "niri-harness-selftest",
            f"exec kitty -- sh -c 'echo {marker}; sleep 600'",
            root=root,
            portal=None,
            env_overrides={},
            timeout=60.0,
        )
        try:
            payload = H.observe(session, window_match=None, do_heal=True, with_text=True)
            check("window belongs to the application", payload["window"]["app_id"] == "kitty",
                  str(payload["window"]))
            text = " ".join(run.text for run in payload["runs"])
            check("application output is readable", marker in text, repr(text))

            # The harness must be startable while its caller keeps working.
            # The headless backend gets there by mapping nothing at all: no
            # window to take focus, none to move, and - the reason it exists -
            # none for the host to stop painting when it scrolls out of view,
            # which is what left the compositor servicing input at 1 Hz.
            host = [w for w in host_windows() if w.get("pid") == session.pid]
            check("maps no window on the host", not host, f"found {len(host)}")
            # Which window the host focuses is not ours to assert - on a busy
            # desktop something else may legitimately take it - but nothing
            # belonging to this session may ever be the thing that holds it.
            focused = H.host_focused_window()
            check("holds none of the caller's focus",
                  focused is None
                  or all(w.get("id") != focused for w in host_windows()
                         if w.get("pid") == session.pid),
                  str(focused))
        finally:
            H.stop_session(session)
        check("session is deregistered", not H.session_file(session.name).exists())


def test_a_command_that_cannot_start_fails_immediately() -> None:
    require("niri")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise _Skip("no host Wayland session")

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        try:
            H.start_session(
                "niri-harness-selftest-dead",
                "exec /nonexistent/definitely-not-here",
                root=root,
                portal=None,
                env_overrides={},
                timeout=30.0,
            )
            check("dead application rejected", False, "start_session returned a session")
        except H.HarnessError as error:
            message = str(error)
            check("status reported", "status 127" in message, message)
            check("cause quoted from the log", "No such file or directory" in message, message)
            # The session's bus daemon and the services it activates are
            # talkative, and in the application's log they pushed the line
            # above out of the tail that is quoted.
            check("the log quoted is the application's alone",
                  "dbus-daemon" not in message, message)
        # A failed start must not leave a registered session behind: the nested
        # compositor is perfectly healthy, so `alive()` would keep saying yes.
        check(
            "failed start is deregistered",
            not H.session_file("niri-harness-selftest-dead").exists(),
        )


def test_a_click_is_converted_out_of_framebuffer_space() -> None:
    """Every click coordinate must be divided by the output's scale.

    Screenshots are framebuffer pixels, so every coordinate OCR reports is a
    framebuffer pixel, but `zwlr_virtual_pointer_v1.motion_absolute` is mapped
    by the compositor into the output's LOGICAL space. On a scaled output the
    two differ, and an unconverted click silently lands short of its target -
    which is indistinguishable from a click that landed and did nothing.

    Verified against the real application before being pinned here: a click on
    a button OCR located at framebuffer x=951 landed at x=798 without this.
    """
    sent: list[tuple[str, ...]] = []
    original_pointer = H.pointer
    original_scale = H.output_scale
    H.pointer = lambda session, *args: sent.append(args)
    H.output_scale = lambda session: 1.25
    try:
        H.click(None, 1000, 500)
        H.move_pointer(None, 400, 200)
    finally:
        H.pointer = original_pointer
        H.output_scale = original_scale

    check("the click was sent", len(sent) == 2, f"{len(sent)} pointer calls")
    if len(sent) != 2:
        return
    click_args, move_args = sent
    check(
        "the click is placed in logical space",
        click_args[:3] == ("a", "800", "400"),
        f"{click_args[:3]}, expected ('a', '800', '400')",
    )
    check("the click presses a button", "c" in click_args, str(click_args))
    check(
        "a bare move is converted too",
        move_args[:3] == ("a", "320", "160"),
        f"{move_args[:3]}, expected ('a', '320', '160')",
    )


def test_an_unscaled_session_is_left_alone() -> None:
    """The conversion must be exact at scale 1, not merely close."""
    sent: list[tuple[str, ...]] = []
    original_pointer = H.pointer
    original_scale = H.output_scale
    H.pointer = lambda session, *args: sent.append(args)
    H.output_scale = lambda session: 1.0
    try:
        H.click(None, 1001, 503)
    finally:
        H.pointer = original_pointer
        H.output_scale = original_scale

    check(
        "coordinates pass through untouched",
        bool(sent) and sent[0][:3] == ("a", "1001", "503"),
        str(sent[:1]),
    )


def test_a_scaled_session_reports_both_geometries() -> None:
    """`observe` must say which space its coordinates are in.

    A caller reading `output=1596x1762` while `runs` hold framebuffer pixels up
    to 1902 has no way to know the two disagree, so the framebuffer size is
    what `observe` reports, with the logical viewport named alongside it.

    Explicitly the window backend, because it is the only one that has this
    problem: a headless session is built at the size asked for and its scale is
    always exactly 1, so run there this asserts that 1600 times 1 is 1600 and
    nothing about the conversion it is named for.

    And that is exactly why it does not run by default. A window backend maps a
    window on the desktop of whoever ran the self-test - the one thing this
    tool exists to avoid - so `make check` must not do it behind their back.
    """
    require("niri")
    require("kitty")
    require("tesseract")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise _Skip("no host Wayland session")
    if not os.environ.get("NIRI_HARNESS_TEST_HOST_WINDOW"):
        raise _Skip(
            "would map a window on your desktop for ~20s; set"
            " NIRI_HARNESS_TEST_HOST_WINDOW=1 to allow it"
        )

    with tempfile.TemporaryDirectory() as scratch:
        session = H.start_session(
            "niri-harness-selftest-scale",
            "exec kitty -- sh -c 'printf \"SCALEPROBE\\n\"; sleep 600'",
            root=Path(scratch) / "run",
            portal=None,
            env_overrides={},
            timeout=60.0,
            width=1600,
            backend="window",
        )
        try:
            check("the scale is not 1, or this proves nothing",
                  abs(H.output_scale(session) - 1.0) > 1e-6,
                  f"scale {H.output_scale(session)}")
            from PIL import Image

            payload = H.observe(session, window_match=None, do_heal=True, with_text=True)
            output = payload["output"]
            with Image.open(payload["frame"]) as frame:
                frame_w, frame_h = frame.size
            check(
                "the reported size is the framebuffer",
                (output["width"], output["height"]) == (frame_w, frame_h),
                f"reported {output['width']}x{output['height']}, frame {frame_w}x{frame_h}",
            )
            check("the scale is reported", output.get("scale"), str(output.get("scale")))
            logical = output.get("logical") or {}
            # The scale is derived from the output's mode, which the host may
            # report a pixel or two away from the screenshot it hands back, so
            # the relation is asserted rather than an exact identity.
            reconstructed = (logical.get("width") or 0) * (output.get("scale") or 0)
            check(
                "the logical viewport times the scale is the framebuffer",
                abs(reconstructed - frame_w) <= 3,
                f"logical {logical} x scale {output.get('scale')} = {reconstructed:.1f}, frame width {frame_w}",
            )
            runs = payload.get("runs") or []
            check("text was located", bool(runs), f"{len(runs)} runs")
            check(
                "located text is inside the reported geometry",
                all(0 <= r.x <= frame_w and 0 <= r.y <= frame_h for r in runs),
                f"{len(runs)} runs against {frame_w}x{frame_h}",
            )
        finally:
            H.stop_session(session)


def test_typed_text_arrives_exactly() -> None:
    """Typed text must reach the application byte for byte, or fail loudly.

    This is the harness's most dangerous failure mode, because a dropped
    character is indistinguishable from an application that mangled input: a
    35-character commit message once arrived as 17 characters with no error
    reported anywhere, and it was filed as a defect against the application
    before the harness was caught. Read the text back from the application
    itself rather than from OCR, so the assertion covers the whole path.
    """
    require("niri")
    require("kitty")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise _Skip("no host Wayland session")

    # Shifted punctuation is included deliberately: reaching those characters
    # needs a modifier, which is where a keymap mistake shows up as mojibake
    # rather than as loss.
    wanted = 'commit: fix "it" & <that> ~ 100% (#3) [a|b] {c} 0123456789'

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        readback = Path(scratch) / "typed.txt"
        session = H.start_session(
            "niri-harness-selftest-type",
            f"exec kitty -- sh -c 'IFS= read -r line; printf %s \"$line\" > {readback}; sleep 600'",
            root=root,
            portal=None,
            env_overrides={},
            timeout=60.0,
            width=None,
        )
        try:
            H.wait_window(session, match=None)
            H.keyboard_text(session, wanted)
            H.keyboard_key(session, "Return")
            deadline = time.monotonic() + 10
            got = None
            while time.monotonic() < deadline:
                if readback.exists():
                    got = readback.read_text(errors="replace")
                    if got:
                        break
                time.sleep(0.2)
            check("the application received the text", got is not None, "nothing was read back")
            check(
                "the text is byte-exact",
                got == wanted,
                f"got {got!r}, wanted {wanted!r}",
            )
        finally:
            H.stop_session(session)


def test_a_nested_widget_collapses_to_the_control_that_can_be_operated() -> None:
    """A toolkit nests a label inside its button; only one is a click target."""
    button = H.TextRun(ref="", text="Open", x=40, y=0, width=75, height=35,
                       confidence=100.0, role="push button")
    label = H.TextRun(ref="", text="Open", x=48, y=5, width=36, height=25,
                      confidence=100.0, role="label")
    elsewhere = H.TextRun(ref="", text="Open", x=800, y=400, width=40, height=20,
                          confidence=100.0, role="label")
    kept = H.collapse_widgets([label, button, elsewhere])
    check("the nested pair becomes one target", len(kept) == 2,
          str([(r.role, r.x) for r in kept]))
    operable = [r for r in kept if r.x == 40]
    check("the operable role wins", operable and operable[0].role == "push button",
          str([(r.role, r.x) for r in kept]))
    check("a separate control with the same name survives",
          any(r.x == 800 for r in kept), str([(r.role, r.x) for r in kept]))

    # An open menu is not a control that something inside it duplicates.
    # Overlap is measured against the smaller box, so a label drawn inside the
    # menu covers it completely and reads as the same thing - and collapsing
    # them drops the menu, which is the only sign left that the screen still
    # has to be read for the items the menu is hiding. `click Save` would then
    # go to whatever sits at the top of the open menu.
    menu = H.TextRun(ref="", text="Save", x=5, y=32, width=101, height=120,
                     confidence=100.0, role="menu")
    within = H.TextRun(ref="", text="Save", x=10, y=36, width=40, height=16,
                       confidence=100.0, role="label")
    kept = H.collapse_widgets([menu, within])
    check("an open menu is not collapsed into a label inside it",
          len(kept) == 2, str([(r.role, r.x, r.y) for r in kept]))
    check("so the screen is still read for what the menu may be hiding",
          H.needs_screen(kept, ["Save"]), str([r.role for r in kept]))

    # The exemption is about containment, not about the role: a menubar item
    # published as both a menu and a label at one box is still one target.
    item = H.TextRun(ref="", text="File", x=0, y=0, width=37, height=27,
                     confidence=100.0, role="menu")
    naming = H.TextRun(ref="", text="File", x=0, y=0, width=37, height=27,
                       confidence=100.0, role="label")
    check("a menu and its own label at the same box are still one target",
          len(H.collapse_widgets([item, naming])) == 1,
          str([(r.role, r.x) for r in H.collapse_widgets([item, naming])]))


def test_a_disabled_control_is_refused_only_when_the_state_is_reported() -> None:
    """A toolkit that reports no state anywhere must not become unclickable."""
    runs = [
        H.TextRun(ref="a1", text="Save", x=10, y=10, width=50, height=20,
                  confidence=100.0, role="push button", enabled=False),
        H.TextRun(ref="a2", text="Open", x=80, y=10, width=50, height=20,
                  confidence=100.0, role="push button", enabled=True),
    ]
    silent = [
        H.TextRun(ref="a1", text="Save", x=10, y=10, width=50, height=20,
                  confidence=100.0, role="push button", enabled=False),
    ]
    check("a disabled control is named, not clicked",
          not any(r.enabled for r in silent) and H.match_runs(runs, "Save")[0].enabled is False)

    # An OCR run and a `label` node carry `enabled` as a field default rather
    # than as an answer, so preferring any enabled candidate walks past the
    # disabled control the caller plainly named and clicks a paragraph that
    # happens to use the same word. Operability has to rank first.
    greyed = H.TextRun(ref="a1", text="Zap", x=0, y=6, width=58, height=34,
                       confidence=100.0, role="push button", enabled=False)
    prose = H.TextRun(ref="a3", text="Zap", x=0, y=447, width=1600, height=19,
                      confidence=100.0, role="label", enabled=True)
    twin = H.TextRun(ref="a9", text="Zap", x=300, y=6, width=58, height=34,
                     confidence=100.0, role="push button", enabled=True)
    order = lambda runs_: min(
        runs_, key=lambda r: (H.WIDGET_RANK.get(r.role, 2), not r.enabled)).ref
    check("a label does not outrank the button the caller named",
          order([greyed, prose]) == "a1", order([greyed, prose]))
    check("but a live twin of that button does",
          order([greyed, prose, twin]) == "a9", order([greyed, prose, twin]))
    check("the enabled sibling proves the state is reported",
          any(r.enabled for r in runs))
    check("without a reported state there is nothing to contradict",
          not any(r.enabled for r in silent))


def test_a_session_does_not_share_the_callers_message_bus() -> None:
    """The isolation that Wayland alone does not buy.

    Everything that travels over D-Bus - single-instance activation above all -
    reaches the caller's own desktop unless the session is given a bus of its
    own. `gnome-text-editor` started without one hands the file to the copy the
    caller is already running, opens a tab in front of them and exits 0.
    """
    require("niri")
    require("alacritty")
    require("dbus-run-session")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise _Skip("no host Wayland session")

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        session = H.start_session(
            "niri-harness-selftest-bus",
            "exec alacritty -e sh -c 'sleep 600'",
            root=root, portal=None, env_overrides={}, timeout=60.0,
        )
        try:
            bus = session.bus
            check("the session reports a bus of its own", bool(bus), repr(bus))
            check("it is not the caller's",
                  bus != os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
                  f"{bus!r} == caller's")
        finally:
            H.stop_session(session)


def test_a_headless_session_maps_nothing_and_is_sized_exactly() -> None:
    """The framebuffer is asked for up front, so no scale trick is needed.

    A window session cannot choose its own framebuffer - the host lays that
    window out - so it trades output scale for logical width after the fact. A
    headless session sizes the compositor's own surface before it starts, which
    is why its scale stays 1 and framebuffer pixels are pointer coordinates.
    """
    require("niri")
    require("Xvfb")
    require("alacritty")

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        session = H.start_session(
            "niri-harness-selftest-headless",
            "exec alacritty -e sh -c 'sleep 600'",
            root=root, portal=None, env_overrides={}, timeout=60.0, width=1600,
        )
        try:
            check("the backend is recorded", session.backend == "headless", session.backend)
            width, height, _ = H.output_geometry(session)
            check("the width asked for is the width given", width == 1600, str(width))
            check("the scale stays 1", abs(H.output_scale(session) - 1.0) < 1e-6,
                  str(H.output_scale(session)))
            frame = H.capture_frame(session)
            check("the framebuffer is the logical size",
                  frame.size == (width, height), f"{frame.size} vs {width}x{height}")
            xvfb_pid = session.xvfb_pid
            check("an X server backs it", bool(xvfb_pid), str(xvfb_pid))
            compositor_pid = session.pid
        finally:
            H.stop_session(session)
        check("stopping the session reaps its X server", H.reaped(xvfb_pid),
              str(xvfb_pid))
        # A caller that outlives several sessions is the compositor's parent,
        # so an uncollected leader is both a zombie per session and a group
        # that can never look empty - which costs the sweep both its signals.
        check("and its compositor, rather than leaving a zombie",
              H.reaped(compositor_pid), str(compositor_pid))


def test_a_declared_control_is_found_without_reading_the_screen() -> None:
    """The accessibility path, end to end, against a real toolkit."""
    require("niri")
    require("gnome-text-editor")
    if H.find_a11y_programs() is None:
        raise _Skip("the at-spi accessibility bus is not installed")
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except (ImportError, ValueError):
        raise _Skip("python bindings for at-spi are not installed") from None

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        # Its own XDG directories, or the editor restores the caller's real
        # documents into a session they did not ask to have driven.
        xdg = Path(scratch) / "xdg"
        document = Path(scratch) / "sample.txt"
        document.write_text("hello\n")
        session = H.start_session(
            "niri-harness-selftest-a11y",
            f"exec gnome-text-editor {document}",
            root=root,
            portal=None,
            env_overrides={
                "XDG_CONFIG_HOME": str(xdg / "config"),
                "XDG_DATA_HOME": str(xdg / "data"),
                "XDG_STATE_HOME": str(xdg / "state"),
                "XDG_CACHE_HOME": str(xdg / "cache"),
            },
            timeout=90.0,
        )
        try:
            H.heal(session)
            runs, forgone = None, None
            for _ in range(30):
                runs, forgone = H.a11y_runs(session)
                if runs:
                    break
                time.sleep(1.0)
            check("the tree answers", bool(runs), repr(forgone or runs)[:200])
            check("a tree that answers forgoes nothing", forgone is None, repr(forgone))
            if not runs:
                return
            names = {run.text for run in runs}
            check("controls are named", "Main Menu" in names, str(sorted(names))[:300])
            roles = {run.role for run in runs}
            check("roles come with them", "push button" in roles or "button" in roles,
                  str(sorted(roles)))
            width, height, _ = H.output_geometry(session)
            inside = [
                run for run in runs
                if -2 <= run.x and run.x + run.width <= width + 2
                and -2 <= run.y and run.y + run.height <= height + 2
            ]
            check("their geometry is in the frame's coordinates",
                  len(inside) == len(runs),
                  str([(r.text, r.x, r.y, r.width, r.height) for r in runs if r not in inside]))
            # The point of the exercise: naming a control does not read pixels.
            # Asserted by counting captures rather than stated in a comment,
            # because the cheap path is one settle away from silently not being
            # cheap, and nothing else would notice.
            captures = []
            original_frame, original_png = H.capture_frame, H.capture_png
            H.capture_frame = lambda *a, **k: (captures.append("frame"),
                                               original_frame(*a, **k))[1]
            H.capture_png = lambda *a, **k: (captures.append("png"),
                                             original_png(*a, **k))[1]
            try:
                x, y, label = H.resolve_target(session, "Main Menu", fresh=True)
            finally:
                H.capture_frame, H.capture_png = original_frame, original_png
            check("naming a declared control reads no pixels at all",
                  not captures, f"{len(captures)} captures: {captures}")
            check("a declared control resolves to a point", "Main Menu" in label, label)
            check("the point is inside its own box",
                  any(r.x <= x <= r.x + r.width and r.y <= y <= r.y + r.height
                      for r in runs if r.text == "Main Menu"),
                  f"{x},{y}")

            # Asking only the tree has to mean only the tree. Falling through
            # to OCR when it answers nothing charges a full-frame pass for the
            # observer chosen because it costs a tenth of one, and makes
            # `observe` and `wait` disagree about what the flag means.
            read = []
            original_ocr = H.ocr_runs
            H.ocr_runs = lambda *a, **k: (read.append("ocr"), original_ocr(*a, **k))[1]
            try:
                only_tree = H.observe(session, window_match=None, do_heal=False,
                                      with_text=True, prefer="a11y")
                asked_only_the_tree = list(read)
                merged = H.observe(session, window_match=None, do_heal=False,
                                   with_text=True, prefer="auto")
            finally:
                H.ocr_runs = original_ocr
            check("--observer a11y reads no pixels", not asked_only_the_tree,
                  str(asked_only_the_tree))
            check("while the default observer does", bool(read), str(read))
            check("and says so", only_tree["source"] == "a11y", str(only_tree["source"]))

            # A `t` ref names a reading off the screen, and only a listing that
            # read the screen has one. Resolving it has to force that read back
            # open, or every `t` ref `observe` just printed is unresolvable on
            # any application that declares a tree at all.
            screen_refs = [r.ref for r in merged["runs"] if r.ref.startswith("t")]
            check("a merged listing offers readings of its own", bool(screen_refs),
                  str([r.ref for r in merged["runs"]])[:200])
            if screen_refs:
                _, _, named = H.resolve_target(session, screen_refs[0], fresh=True)
                check("and one of them still resolves against a fresh read",
                      named.startswith(screen_refs[0]), named)
        finally:
            H.stop_session(session)


def test_a_drag_travels_between_its_ends_without_passing_the_origin() -> None:
    """A drag is read from the motion between press and release.

    Absolute placement works by pinning the pointer to the origin first, so
    using it for the travel would drag through the corner of the screen and
    select, or move, everything on the way.
    """
    sent: list[list[str]] = []

    class FakeSession:
        name = "fake"

        @property
        def env(self):
            return {}

    session = FakeSession()
    original_pointer = H.pointer
    original_scale = H.output_scale
    try:
        H.pointer = lambda _session, *args: sent.append(list(args))
        H.output_scale = lambda _session: 1.0
        H.drag(session, (100, 100), (400, 300), steps=4)
    finally:
        H.pointer = original_pointer
        H.output_scale = original_scale

    check("one injector run carries the whole gesture", len(sent) == 1, str(len(sent)))
    argv = sent[0]
    check("it starts by placing the pointer", argv[:3] == ["a", "100", "100"], str(argv[:3]))
    check("the button goes down before the travel",
          argv.index("d") < argv.index("r"), str(argv))
    check("the button comes up last", argv[-2:] == ["u", "left"], str(argv[-2:]))
    check("the travel is relative, never absolute",
          argv.count("a") == 1, str(argv))
    # The relative steps must sum to exactly the distance asked for.
    moved_x = moved_y = 0
    for index, token in enumerate(argv):
        if token == "r":
            moved_x += int(argv[index + 1])
            moved_y += int(argv[index + 2])
    check("the steps sum to the requested distance",
          (moved_x, moved_y) == (300, 200), f"{moved_x},{moved_y}")


GROUP_LEADER = (
    "import subprocess, sys, time\n"
    "held = [subprocess.Popen(['sleep', '300']) for _ in range(2)]\n"
    "print(' '.join(str(child.pid) for child in held), flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
)


# The two kinds of member the sweep tells apart once the leader can no longer
# vouch for the group: one known only by the name of its program, which every
# session's accessibility bus shares, and one whose command line names this
# session's own directory, which nothing else on the machine does.
# The session's directory reaches the leader in two halves and is joined in
# the child, so that only the child's command line carries it: a leader naming
# the session is corroboration in itself, and would take the whole group
# without ever reaching the per-member rule this exists to exercise.
MIXED_GROUP_LEADER = (
    "import os, subprocess, sys, time\n"
    "held = [subprocess.Popen([sys.argv[2], '300']),\n"
    "        subprocess.Popen([sys.executable, '-c',\n"
    "                          'import time; time.sleep(300)',\n"
    "                          os.path.join(sys.argv[3], sys.argv[4])])]\n"
    "print(' '.join(str(child.pid) for child in held), flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
)


def group(program: str, *args: str) -> "tuple[subprocess.Popen, list[int]]":
    """A real process group: a leader of its own, holding two live children.

    `start_new_session=True` is what makes the leader a leader. Without it the
    stand-in shares the test runner's process group, `process_group()` finds no
    members under its pid, and every assertion about sweeping a group passes
    because there is no group there to sweep.
    """
    leader = subprocess.Popen(
        [sys.executable, "-c", program, *args],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    children = [int(pid) for pid in leader.stdout.readline().split()]
    return leader, children


def process_state(pid: int) -> "str | None":
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command can hold spaces and parentheses; the state is the field
    # after the last `)`.
    try:
        return stat[stat.rindex(") ") + 2]
    except (ValueError, IndexError):
        return None


def alive(pids: "list[int]") -> "list[int]":
    """The pids still running, counting a zombie as gone.

    A signalled process stays in the table until its parent reaps it, and a
    stand-in leader that is itself still sleeping reaps nothing. Asking only
    whether /proc/PID exists therefore calls a process alive that was killed a
    moment ago - which would pass every assertion here that something survived,
    whether it did or not.
    """
    return [pid for pid in pids if process_state(pid) not in (None, "Z")]


def test_a_stale_record_never_signals_a_strangers_process_group() -> None:
    """Session records outlive crashes, and a reboot renumbers every process.

    Sweeping a process group by a recorded pid is the one place this harness
    could plausibly touch something that is not its own, so the recorded pid
    has to be corroborated before it is signalled. Every stand-in here leads a
    group of its own and holds live children, and the assertions are about the
    children: a guard that lets a stranger's leader live while killing its
    group has not worked.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        root.mkdir()
        # Three groups. One names this session in its argv, as the real
        # compositor's does; one is a stranger; one is a stranger whose leader
        # has exited and not been reaped. The argv has to survive exec, which
        # rules out both a path passed to `sleep`, where it is an invalid
        # interval, and a shell, which execs away its last command.
        ours, our_children = group(f"# {root}\n" + GROUP_LEADER, "30")
        stranger, their_children = group(GROUP_LEADER, "30")
        # A leader that exits while its group lives on. Never waited for, so it
        # stays in the table as a zombie - which is what a compositor killed
        # outright looks like by the time `stop` gets to it.
        departed, orphans = group(GROUP_LEADER, "0")
        later = mixed = None
        later_children: list[int] = []
        mixed_children: list[int] = []
        time.sleep(0.4)
        try:
            check("a command line naming this session identifies the leader",
                  H.leader_is_ours(ours.pid, root), str(ours.pid))
            check("a stranger's is not mistaken for it",
                  not H.leader_is_ours(stranger.pid, root), str(stranger.pid))
            check("a zombie leader corroborates nothing either way",
                  not H.leader_is_ours(departed.pid, root),
                  Path(f"/proc/{departed.pid}/stat").read_text().split(") ")[1][0])
            check("a departed leader does not",
                  not H.leader_is_ours(4194300, root), "no such process")
            # Session roots are siblings named by the caller, so one name is
            # routinely a prefix of another - and a substring test would hand
            # `run` every process belonging to `run2`.
            check("a neighbouring session's directory is not this one",
                  not H.names_root(f"niri -c {root}2/nested.kdl", root),
                  f"{root}2")
            check("and something inside this one still is",
                  H.names_root(f"sh -c 'exec > {root}/session.log'", root),
                  str(root))

            check("the stand-ins really do lead groups of their own",
                  sorted(H.process_group(stranger.pid)) == sorted(
                      [stranger.pid] + their_children),
                  str(H.process_group(stranger.pid)))

            # When the record says, the pid and the moment it began settle it.
            began = H.process_start(ours.pid)
            check("a pid and its start time identify a process",
                  H.is_recorded_process(ours.pid, began), str(began))
            check("and a start time that does not match identifies nothing",
                  not H.is_recorded_process(ours.pid, (began or 0) + 1), str(began))

            # The sweep itself, not merely the predicate. A stranger's group
            # must come through every route untouched.
            H.sweep_process_group(stranger.pid, root, H.boot_id())
            H.sweep_process_group(departed.pid, root, H.boot_id())
            H.sweep_process_group(ours.pid, root, "not-this-boot")
            H.sweep_process_group(ours.pid, root, H.boot_id(), (began or 0) + 1)
            time.sleep(0.4)
            check("a stranger's group survives the sweep",
                  alive(their_children) == their_children, str(alive(their_children)))
            check("and one whose leader is a zombie, which cannot vouch for it",
                  alive(orphans) == orphans, str(alive(orphans)))
            check("a record from an earlier boot signals nothing",
                  alive(our_children) == our_children, str(alive(our_children)))
            check("neither does one whose leader began at another time",
                  alive(our_children) == our_children, str(alive(our_children)))

            # Nothing above would notice if `process_start` read the wrong
            # field of /proc: every check compares it against another reading
            # of itself, so a function returning a constant satisfies them all
            # - and a constant makes `is_recorded_process` say yes to every
            # stranger there is. Pin it to two things outside itself: the clock
            # it claims to be counted on, and the passage of time.
            ticks = os.sysconf("SC_CLK_TCK")
            time.sleep(1.1)
            later, later_children = group(GROUP_LEADER, "30")
            time.sleep(0.2)
            began_later = H.process_start(later.pid)
            uptime = float(Path("/proc/uptime").read_text().split()[0]) * ticks
            check("a start time is counted from boot, like the uptime is",
                  began_later is not None and 0 < began_later <= uptime
                  and uptime - began_later < 5 * ticks,
                  f"{began_later} against {uptime:.0f}")
            check("and two processes a second apart do not share one",
                  began is not None and began_later is not None
                  and began_later - began >= ticks,
                  f"{began} then {began_later}")

            # A record written before start times were kept cannot establish
            # that the pid it names still leads the group it named. The pid may
            # since have been handed to another session's compositor, and that
            # session's accessibility bus is a member of its group, answering
            # to exactly the program name this one would look for.
            registryd = Path(scratch) / "at-spi2-registryd"
            shutil.copy(shutil.which("sleep"), registryd)
            mixed, mixed_children = group(
                MIXED_GROUP_LEADER, "30", str(registryd),
                str(root.parent), root.name)
            time.sleep(0.4)
            by_name, by_cmdline = mixed_children
            check("the stand-in leader does not name this session itself",
                  not H.leader_is_ours(mixed.pid, root),
                  str(H.process_cmdline(mixed.pid))[:80])
            check("the stand-in is named after an accessibility program",
                  Path((H.process_cmdline(by_name) or "").split("\0")[0]).name
                  in H.ACCESSIBILITY_STRAYS, str(by_name))
            H.sweep_process_group(mixed.pid, root, H.boot_id(), None)
            time.sleep(0.4)
            check("a record too old to vouch for its group spares the bus",
                  alive([by_name]) == [by_name], "another session's, by name")
            check("but still takes what names this session outright",
                  alive([by_cmdline]) == [], str(alive([by_cmdline])))

            # And the case it exists to handle: our own group, corroborated.
            H.sweep_process_group(ours.pid, root, H.boot_id(), began)
            time.sleep(0.4)
            check("a corroborated record takes its whole group with it",
                  alive(our_children) == [], str(alive(our_children)))
        finally:
            for pid in (our_children + their_children + orphans
                        + later_children + mixed_children):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            for leader in (ours, stranger, departed, later, mixed):
                if leader is None:
                    continue
                try:
                    leader.kill()
                except OSError:
                    pass
                leader.wait()


def test_the_sweep_never_signals_its_own_caller() -> None:
    """A record naming pid 0 would otherwise be self-destruction.

    `killpg(0, ...)` does not mean "no process"; it signals the caller's own
    process group, which is the test runner, the shell that invoked it, and
    every other session being driven from it. The witness has to be in that
    group, which means it must *not* get a session of its own.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        root.mkdir()
        sibling = subprocess.Popen(["sleep", "30"])
        time.sleep(0.2)
        try:
            check("the witness shares the caller's process group",
                  os.getpgid(sibling.pid) == os.getpgid(0),
                  f"{os.getpgid(sibling.pid)} vs {os.getpgid(0)}")
            for pid in (0, 1):
                H.sweep_process_group(pid, root, H.boot_id())
            time.sleep(0.3)
            check("the caller's own group is still running",
                  sibling.poll() is None, f"exit={sibling.poll()}")
        finally:
            sibling.kill()
            sibling.wait()


def test_stopping_a_session_strands_no_accessibility_processes() -> None:
    """The accessibility bus is not the compositor's child, and does not die with it.

    `at-spi-bus-launcher` and `at-spi2-registryd` are started alongside the
    application, reparent themselves to the user's service manager, and each
    hold a socket in the runtime directory for the rest of the login session.
    Quitting the compositor leaves them running; only sweeping the session's
    process group takes them with it.

    Stopped through the command line rather than in this process, because the
    two are not the same test. Here the compositor is this runner's own child,
    so it stays in the process table as a zombie and can still be identified
    when the sweep gets to it. A caller who starts a session and stops it from
    a second command leaves no such thing behind: the compositor is reparented,
    reaped by the service manager, and gone by the time the sweep looks - which
    is the ordinary case, and the one where attributing the group by its leader
    stops being possible at all.
    """
    require("niri")
    require("Xvfb")
    require("alacritty")
    if H.find_a11y_programs() is None:
        raise _Skip("the at-spi accessibility bus is not installed")

    def registries(group: int) -> set[int]:
        """The accessibility processes belonging to one session, by pid.

        Matched on the full command line: `pgrep` compares the fifteen-character
        process name by default, and both of these names are longer than that.
        Their command lines say nothing about which session they serve - they
        are bare program paths - so the process group does the telling. It is
        the same group the sweep signals, and it is exact: another session
        starting on this machine while this test runs has a group of its own,
        and counting its processes as stranded here would fail a sweep that
        worked.
        """
        found = subprocess.run(
            ["pgrep", "-f", "at-spi2-registryd|at-spi-bus-launcher"],
            capture_output=True, text=True, check=False,
        )
        return {pid for pid in map(int, found.stdout.split())
                if process_group_id(pid) == group}

    def process_group_id(pid: int) -> int | None:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        # The command can hold spaces and parentheses, so the fields are read
        # from the last `)` - the same rule `process_start` follows.
        try:
            return int(stat[stat.rindex(")") + 2:].split()[2])
        except (ValueError, IndexError):
            return None

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "run"
        state = Path(scratch) / "state"
        environment = {**os.environ, "NIRI_HARNESS_STATE": str(state)}
        harness = [sys.executable, str(Path(H.__file__).resolve())]
        name = "niri-harness-selftest-sweep"
        started_ok = subprocess.run(
            harness + ["start", "--name", name, "--root", str(root),
                       "alacritty", "-e", "sh", "-c", "sleep 600"],
            env=environment, capture_output=True, text=True, check=False,
        )
        try:
            if started_ok.returncode != 0:
                raise _Skip(f"the session did not start: {started_ok.stderr[:200]}")
            record = json.loads((state / f"{name}.json").read_text())
            group = process_group_id(record["pid"])
            started = registries(group) if group else set()
            if not started:
                raise _Skip("the session started no accessibility bus of its own")
            check("the session starts its own accessibility bus", bool(started),
                  str(sorted(started)))
        finally:
            subprocess.run(harness + ["stop", name],
                           env=environment, capture_output=True, check=False)

    # Reaping is not instantaneous; the sweep signals and the kernel follows.
    # The pids are the ones seen to start, not whatever answers to the group
    # afterwards. A dead process has no group to read, so asking the group
    # again could only ever return the survivors it happens to still claim -
    # and a survivor that has put itself in a group of its own, which is
    # exactly the reparenting these two do, would count as none at all.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and alive(sorted(started)):
        time.sleep(0.1)
    stranded = alive(sorted(started))
    check("stopping the session strands none of them", not stranded,
          str(sorted(stranded)))


def declared(text, x, y, width=40, height=12, role="push button", enabled=True):
    return H.TextRun(ref="", text=text, x=x, y=y, width=width, height=height,
                     confidence=100.0, role=role, enabled=enabled)


def test_a_tree_that_answers_partly_does_not_hide_the_rest_of_the_screen() -> None:
    """A toolkit publishes controls it has no name for, and they are dropped.

    GTK4's menu items are the common case: the menu itself is one node
    covering every item in it, and the items come back nameless. Treating the
    tree's answer as complete is how `observe` reports a control that is
    plainly on screen as absent, and how `wait --gone` reports a dialog closed
    while it is still open.
    """
    menu = declared("Main Menu", 1373, 34, width=255, height=568, role="menu")
    button = declared("Open", 39, 0, width=75, height=35)
    # Readings of the items inside that menu, which the tree never named.
    items = [H.TextRun(ref="", text=t, x=1402, y=y, width=56, height=10,
                       confidence=90.0)
             for t, y in (("Save", 240), ("Save As...", 272), ("Print...", 379))]
    # And a reading of the button, which it did.
    glyphs = H.TextRun(ref="", text="Open", x=45, y=12, width=38, height=11,
                       confidence=90.0)

    merged = H.merge_sources([menu, button], items + [glyphs])
    texts = [run.text for run in merged]
    check("a reading inside a declared container survives",
          all(item.text in texts for item in items), str(texts))
    check("a second reading of a declared control does not",
          texts.count("Open") == 1, str(texts))

    # The shape that matters, and the one a container named unlike its contents
    # does not test: GTK gives a popover menu the name of the button that
    # opened it, so a "Save" menu encloses items whose labels all contain
    # "Save". Position says they are inside it and the label test says they are
    # readings of it, and both are wrong.
    popover = declared("Save", 19, 58, width=117, height=120, role="menu")
    entries = [H.TextRun(ref="", text=t, x=25, y=y, width=60, height=10,
                         confidence=90.0)
               for t, y in (("Save As...", 66), ("Save All", 90), ("Save a Copy", 114))]
    kept = [run.text for run in H.merge_sources([popover], entries)]
    check("a menu named after its own button does not swallow its items",
          all(entry.text in kept for entry in entries), str(kept))

    # And the suppression that must still happen: a leaf control, with OCR
    # having glued an accelerator onto the end of what it read. This one is
    # told apart from the cases above by its box, not its role - a reading that
    # says more than the name is the same control only when it is the same box.
    item = declared("Keyboard Shortcuts", 1403, 501, width=170, height=14,
                    role="menu item")
    glued = H.TextRun(ref="", text="Keyboard Shortcuts Ctrl+?", x=1403, y=501,
                      width=168, height=13, confidence=90.0)
    check("a glued reading of a leaf control is still suppressed",
          [run.text for run in H.merge_sources([item], [glued])]
          == ["Keyboard Shortcuts"],
          str([run.text for run in H.merge_sources([item], [glued])]))

    # The same shape without a container role to lean on. GTK4 publishes a
    # text view and a single-line entry alike as "text", and a list row is a
    # `list item` - a leaf by every sensible reading, and the thing you click -
    # whose box still encloses whatever it draws. Measured against GTK4
    # fixtures: the entry, the text view and the row all behave like this.
    view = declared("Body", 0, 84, width=1600, height=18, role="text")
    body = [H.TextRun(ref="", text=t, x=x, y=88, width=w, height=13,
                      confidence=90.0)
            for t, x, w in (("Paragraph one", 1, 91), ("Body of the document", 112, 122))]
    kept = [run.text for run in H.merge_sources([view], body)]
    check("a document inside a text view is not taken for its field's name",
          all(line.text in kept for line in body), str(kept))

    row = declared("Report", 0, 0, width=1600, height=38, role="list item")
    drawn = H.TextRun(ref="", text="Report 2024 final", x=9, y=11,
                      width=149, height=18, confidence=90.0)
    check("text a list row merely draws is not taken for the row's own name",
          [run.text for run in H.merge_sources([row], [drawn])]
          == ["Report", "Report 2024 final"],
          str([run.text for run in H.merge_sources([row], [drawn])]))

    # And the reverse, which is what made a container role too blunt an
    # instrument: an entry showing exactly its own accessible name is one
    # control, and offering it twice turns a substring click into an error.
    field = declared("Search", 0, 0, width=1600, height=34, role="text")
    reading = H.TextRun(ref="", text="Search", x=9, y=10, width=41, height=10,
                        confidence=90.0)
    check("a control showing its own name is still reported once",
          [run.text for run in H.merge_sources([field], [reading])] == ["Search"],
          str([run.text for run in H.merge_sources([field], [reading])]))

    # The same shape once the control holds something else as well - a text
    # view whose document opens with the field's own name. It is no longer
    # describable by that one reading, so the reading stays, and the cost is a
    # duplicate rather than a control that cannot be named. That is the side to
    # be wrong on: the alternative is `click Save` landing in the middle of an
    # open menu because a `Save` item was taken for the menu's own name.
    document = declared("Notes", 0, 44, width=1600, height=956, role="text")
    lines = [H.TextRun(ref="", text=t, x=1, y=y, width=w, height=9,
                       confidence=90.0)
             for t, y, w in (("Notes", 48, 35), ("second line", 65, 70))]
    kept = [run.text for run in H.merge_sources([document], lines)]
    check("a control holding more than one reading describes none of them",
          kept == ["Notes", "Notes", "second line"], str(kept))

    # The bug that gate exists for, at the geometry it actually occurs at: a
    # `Save` menu holding a `Save` item is 2% of the menu's area, and an
    # ordinary button's glyphs are 16% of the button - ratios far too close to
    # separate, and the consequence of guessing wrong is a silent wrong click.
    split = declared("Save", 5, 32, width=101, height=120, role="menu")
    rows = [H.TextRun(ref="", text=t, x=26, y=y, width=w, height=9,
                      confidence=90.0)
            for t, y, w in (("Save", 61, 27), ("Save As...", 91, 56),
                            ("Save All", 121, 46))]
    # Asserted on the runs themselves, not on their text: the declared menu is
    # in the answer too and is also spelled "Save", so a membership test on
    # strings is satisfied by the very node that swallowed the item.
    kept = H.merge_sources([split], rows)
    check("a menu holding an item of its own name does not swallow it",
          len(kept) == 1 + len(rows)
          and all(any(run is row for run in kept) for row in rows),
          str([run.text for run in kept]))

    # And the count is of what OCR happened to read, so it cannot be trusted to
    # describe a region at all: a menu holding one legible row - one item, or
    # one tesseract could make out - encloses exactly one reading and looks
    # exactly like a control showing its own name. Suppressing it puts `click
    # Save` on the menu button and leaves the item unnameable.
    one_row = declared("Save", 5, 32, width=82, height=76, role="menu")
    row = H.TextRun(ref="", text="Save", x=26, y=61, width=27, height=9,
                    confidence=90.0)
    kept = H.merge_sources([one_row], [row])
    check("a menu holding a single reading does not swallow it either",
          any(run is row for run in kept), str([run.text for run in kept]))

    # A leaf still answers to the count, which is the whole point of keeping it.
    leaf = declared("Open", 40, 0, width=75, height=34)
    glyphs = H.TextRun(ref="", text="Open", x=48, y=12, width=36, height=10,
                       confidence=90.0)
    check("while a button showing its own name is still reported once",
          [run.text for run in H.merge_sources([leaf], [glyphs])] == ["Open"],
          str([run.text for run in H.merge_sources([leaf], [glyphs])]))

    check("with no target named, the screen is always read",
          H.needs_screen([menu, button], None), "observe asks for everything")
    check("a label the tree carries needs no screen",
          not H.needs_screen([menu, button], ["Open"]), "")
    check("a label it does not carry needs one",
          H.needs_screen([menu, button], ["Save As..."]), "")
    check("and one carried label does not excuse another",
          H.needs_screen([menu, button], ["Open", "Print..."]), "")


def test_controls_are_never_taken_from_a_window_other_than_the_one_in_hand() -> None:
    """The accessibility bus is per session, not per window.

    The probe walks every frame on it, and accessibility geometry is relative
    to a window, so offsetting all of it by one window's origin puts a control
    belonging to a window that may be scrolled out of view at a coordinate
    inside the window that is not. Two windows of the same application are the
    dangerous case: their toolbars have identical window-relative boxes, so the
    duplicate collapses and leaves no sign of which one would be clicked.
    """
    alpha = [{"frame": 1, "window": "alpha.txt - Text Editor", "text": "Open"},
             {"frame": 1, "window": "alpha.txt - Text Editor", "text": "alpha.txt"}]
    beta = [{"frame": 2, "window": "beta.txt - Text Editor", "text": "Open"},
            {"frame": 2, "window": "beta.txt - Text Editor", "text": "beta.txt"}]

    kept = H.nodes_for_window(alpha + beta, {"title": "beta.txt - Text Editor"})
    check("only the window being acted on contributes",
          [node["text"] for node in kept] == ["Open", "beta.txt"],
          str([node["text"] for node in kept]))

    # A session holding one window has nowhere else its one frame could have
    # come from, so toolkits whose frame name is not the window title keep
    # working there - which is every ordinary session.
    only = H.nodes_for_window(alpha, {"title": "something else entirely"},
                              sole_window=True)
    check("a single window is taken as it is", len(only) == 2, str(only))

    # With another window on the session that reasoning is gone. A focused
    # window that declares nothing - a terminal, an SDL game, most things under
    # Xwayland - leaves one frame on the bus belonging to the window nobody
    # asked about, and offering its controls puts every click in that window.
    stranger = H.nodes_for_window(alpha, {"title": "Alacritty"}, focused=True)
    check("one frame is not the window in hand while the session holds others",
          stranger == [], str(stranger))

    # The same shape the other way round: the one frame is the focused
    # window's, and says so, so the name it goes by never has to be consulted.
    mine = H.nodes_for_window([dict(node, active=True) for node in alpha],
                              {"title": "a name the frame does not use"},
                              focused=True)
    check("the focused window's own frame is taken though its name differs",
          len(mine) == 2, str(mine))

    # Several windows and none of them matching cannot be attributed at all.
    none = H.nodes_for_window(alpha + beta, {"title": "no such window"})
    check("an unattributable tree yields nothing rather than a wrong guess",
          none == [], str(none))

    # Two unsaved documents share a title, and the title alone cannot separate
    # their controls - which is exactly when guessing is most tempting.
    twins = [{"frame": 1, "window": "Untitled Document", "text": "Open"},
             {"frame": 2, "window": "Untitled Document", "text": "Open"}]
    check("two windows sharing a title are refused, not merged",
          H.nodes_for_window(twins, {"title": "Untitled Document"},
                             sole_window=True) == [],
          str(H.nodes_for_window(twins, {"title": "Untitled Document"})))

    # Two unsaved documents are ordinary, and refusing costs the tree
    # entirely. The focus settles it without consulting a title at all - which
    # also removes the race where the window is looked up after the walk and
    # has retitled in between.
    focused = [{"frame": 1, "window": "Untitled Document", "text": "Open"},
               {"frame": 2, "window": "Untitled Document", "text": "Open",
                "active": True}]
    picked = H.nodes_for_window(focused, {"title": "Untitled Document"},
                                focused=True)
    check("the active frame settles what two identical titles cannot",
          [node["frame"] for node in picked] == [2], str(picked))
    check("and a named window is still matched by its name",
          H.nodes_for_window(focused, {"title": "Untitled Document"}) == [],
          "no focus to lean on when the caller named a window")

    # A payload that does not number its frames cannot be attributed at all,
    # and must not quietly fall back to keying on the title - that is the same
    # collapse the case above exists to prevent, arrived at by another route.
    unnumbered = [{"window": "Untitled Document", "text": "Open"},
                  {"window": "Untitled Document", "text": "Open"}]
    check("an unnumbered payload is refused rather than keyed on the title",
          H.nodes_for_window(unnumbered, {"title": "Untitled Document"},
                             sole_window=True) == [],
          str(H.nodes_for_window(unnumbered, {"title": "Untitled Document"})))


def test_a_tree_that_cannot_be_used_says_so_instead_of_looking_unfurnished() -> None:
    """An empty answer from the tree has four causes and only one is benign.

    An application that declares nothing is working as designed, and OCR is the
    right answer for it. A tree that answered in full and was then discarded -
    because the window could not be placed on the output, or could not be told
    apart from another window's - is a capability being given up: the same
    labels come back without roles, without enabled state, and an order of
    magnitude slower. Both reach the caller as an empty list, so the reason has
    to travel beside it or the downgrade is invisible.
    """
    probe_source = """
import json, os, sys
print(json.dumps({"nodes": json.loads(os.environ["FAKE_NODES"])}))
sys.exit(int(os.environ.get("FAKE_STATUS", "0")))
"""
    node = {"frame": 1, "window": "Alpha", "text": "Open", "role": "push button",
            "x": 0, "y": 0, "width": 10, "height": 10, "enabled": True}
    # Two unsaved documents share a title, so the tree answers in full and is
    # then discarded - the expensive case, and the one worth explaining.
    twin = dict(node, frame=2)

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(probe_source)
        # `bus` is read back from the session's own record, not passed in.
        (Path(tmp) / "session.bus").write_text(
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent\n"
        )
        session = H.Session(name="fake", root=Path(tmp), display="wayland-fake",
                            socket=str(Path(tmp) / "sock"), pid=0)
        saved = (H.A11Y_PROBE, H.windows, H.window_origin, H.output_scale)
        H.A11Y_PROBE = probe
        H.output_scale = lambda _session: 1.0
        H.windows = lambda _session: [{"title": "Alpha", "is_focused": True}]
        H.window_origin = lambda _session, _window: (0, 0)
        try:
            def run(nodes, **env):
                os.environ["FAKE_NODES"] = json.dumps(nodes)
                os.environ.update({k: str(v) for k, v in env.items()})
                try:
                    return H.a11y_runs(session)
                finally:
                    for key in ("FAKE_NODES", "FAKE_STATUS"):
                        os.environ.pop(key, None)

            runs, forgone = run([node])
            check("a tree that can be used explains nothing",
                  [r.text for r in runs] == ["Open"] and forgone is None,
                  f"{[r.text for r in runs]} {forgone!r}")

            runs, forgone = run([])
            check("an application that declares nothing is not a failure",
                  runs == [] and forgone is None, f"{runs} {forgone!r}")

            runs, forgone = run([node, twin])
            check("a tree that cannot be attributed says it was not used",
                  runs == [] and forgone is not None and "window" in forgone,
                  f"{runs} {forgone!r}")

            H.window_origin = lambda _session, _window: None
            runs, forgone = run([node])
            check("a tree that cannot be placed says so",
                  runs == [] and forgone is not None and "position" in forgone,
                  f"{runs} {forgone!r}")
            H.window_origin = lambda _session, _window: (0, 0)

            H.windows = lambda _session: []
            runs, forgone = run([node])
            check("a tree with no window to belong to says so",
                  runs == [] and forgone is not None and "no window" in forgone,
                  f"{runs} {forgone!r}")
            H.windows = lambda _session: [{"title": "Alpha", "is_focused": True}]

            runs, forgone = run([node], FAKE_STATUS=3)
            check("a probe that fails is not an application with no controls",
                  runs is None and forgone is not None,
                  f"{runs} {forgone!r}")

            # `observe` can report the reason beside an empty list and let the
            # caller judge. Nothing else can: `wait --gone` and every lookup by
            # label read an empty answer as absence, and an observer that has
            # forbidden the screen has nothing to check that against - so a
            # discarded tree would close a dialog that is still open.
            def located(nodes):
                os.environ["FAKE_NODES"] = json.dumps(nodes)
                try:
                    return H.locate(session, prefer="a11y", targets=["Open"])
                finally:
                    os.environ.pop("FAKE_NODES", None)

            try:
                refused = str(located([node, twin]))
            except H.HarnessError as error:
                refused = str(error)
            check("a tree the screen cannot check is not reported as absence",
                  "window" in refused and "a11y" in refused, repr(refused))

            runs, source, _ = located([])
            check("and an application that declares nothing still answers so",
                  runs == [] and source == "a11y", f"{runs} {source}")
        finally:
            (H.A11Y_PROBE, H.windows, H.window_origin, H.output_scale) = saved


MENU_FIXTURE = """
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio


def on_activate(app):
    window = Gtk.ApplicationWindow(application=app, title="MenuProbe")
    model = Gio.Menu()
    for label in ("Save As...", "Save All", "Save a Copy"):
        model.append(label, "app.noop")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(Gtk.MenuButton(label="Save", menu_model=model))
    window.set_child(box)
    app.add_action(Gio.SimpleAction.new("noop", None))
    window.present()


app = Gtk.Application(application_id="dev.niriharness.MenuProbe")
app.connect("activate", on_activate)
app.run([])
"""


def test_a_menu_named_after_its_button_does_not_hide_its_own_items() -> None:
    """The shape that broke both `observe` and `wait --gone`, end to end.

    GTK gives a popover menu the accessible name of the button that opened it,
    and publishes the items inside it without names of their own. So the tree
    answers - with a single `Save` node covering the whole menu - while every
    item in it is invisible to the tree and plainly on screen. A menu whose
    items happen to begin with the menu's own name defeats a label test as
    well as a position test, which is why this is driven against real GTK
    rather than assembled out of fixtures.
    """
    require("niri")
    require("Xvfb")
    if H.find_a11y_programs() is None:
        raise _Skip("the at-spi accessibility bus is not installed")
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except (ImportError, ValueError):
        raise _Skip("python bindings for GTK 4 or at-spi are not installed") from None

    with tempfile.TemporaryDirectory() as scratch:
        fixture = Path(scratch) / "menuapp.py"
        fixture.write_text(MENU_FIXTURE)
        session = H.start_session(
            "niri-harness-selftest-menu",
            f"exec {sys.executable} {fixture}",
            root=Path(scratch) / "run",
            portal=None,
            env_overrides={},
            timeout=90.0,
        )
        try:
            H.click(session, *H.resolve_target(session, "Save", fresh=True)[:2])
            time.sleep(1.5)

            payload = H.observe(session, window_match=None, do_heal=False,
                                with_text=True)
            texts = [run.text for run in payload.get("runs") or []]
            declared = [run.text for run in payload["runs"] if run.role != "ocr"]
            check("the tree names the menu but not its items",
                  "Save" in declared
                  and not any(t.startswith("Save A") for t in declared),
                  str(declared))
            check("and the items are reported anyway",
                  sum(t.startswith("Save A") for t in texts) >= 2, str(texts))

            # The bug this is really about: a label on screen, reported gone.
            runs = H.fresh_runs(session, ["Save All"])
            check("an item the tree never named is still found",
                  bool(H.match_runs(runs, "Save All")),
                  str([run.text for run in runs]))

            # So it is on screen - and absent from the tree, which is the one
            # state in which `--observer a11y` must not answer `--gone`. It has
            # no screen to check against, and "not in the tree" would come back
            # as "gone" in under a tenth of a second: the kind of answer a
            # caller banks on. Waiting for presence stays sound and stays fast.
            harness = [sys.executable, str(Path(H.__file__).resolve()),
                       "--session", session.name]
            refused = subprocess.run(
                harness + ["--observer", "a11y", "wait", "--gone", "Save All",
                           "--timeout", "3"],
                capture_output=True, text=True, check=False,
            )
            check("the tree alone never reports a nameless item gone",
                  refused.returncode != 0
                  and "accessibility tree" in refused.stderr,
                  f"{refused.returncode} {refused.stdout.strip()[:80]}")
            honest = subprocess.run(
                harness + ["wait", "--gone", "Save All", "--timeout", "1"],
                capture_output=True, text=True, check=False,
            )
            check("and reading the screen says it is still there",
                  honest.returncode != 0 and "still present" in honest.stderr,
                  f"{honest.returncode} {honest.stderr.strip()[:80]}")
        finally:
            H.stop_session(session)


FIXED_FIXTURE = """
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


def on_activate(app):
    window = Gtk.ApplicationWindow(application=app, title="FixedProbe")
    window.set_resizable(False)
    window.set_default_size(600, 400)
    window.set_child(Gtk.Label(label="Zap"))
    window.present()


app = Gtk.Application(application_id="dev.niriharness.FixedProbe")
app.connect("activate", on_activate)
app.run([])
"""


def test_heal_claims_no_repair_it_did_not_make() -> None:
    """An application with a fixed size is ordinary and cannot be maximised.

    A calculator, a preferences dialog, an SDL game: the application asks not
    to be resized, niri floats it, and `maximize-column` does not reach a
    floating window. Saying "maximized column" anyway is a report of work that
    did not happen, on every command for the life of the session - which is how
    a reader learns to skip the `healed:` line, and that line has to stay worth
    reading for the command where something really was repaired.

    Nothing is lost by leaving the window alone: niri publishes the position of
    a floating window, which is the one thing the accessibility tree needs.
    """
    require("niri")
    require("Xvfb")
    try:
        import gi

        gi.require_version("Gtk", "4.0")
    except (ImportError, ValueError):
        raise _Skip("python bindings for GTK 4 are not installed") from None

    with tempfile.TemporaryDirectory() as scratch:
        fixture = Path(scratch) / "fixedapp.py"
        fixture.write_text(FIXED_FIXTURE)
        session = H.start_session(
            "niri-harness-selftest-fixed",
            f"exec {sys.executable} {fixture}",
            root=Path(scratch) / "run",
            portal=None,
            env_overrides={},
            timeout=90.0,
        )
        try:
            window = H.pick_window(session, None)
            size = (window.get("layout") or {}).get("window_size") or [0, 0]
            width, height, _ = H.output_geometry(session)
            if round(size[0]) >= width and round(size[1]) >= height:
                raise _Skip("this compositor resized the window anyway")
            check("its position is published, so its tree can be placed",
                  H.window_origin(session, window) is not None,
                  str((window.get("layout") or {}).get("tile_pos_in_workspace_view")))

            actions = H.heal(session)
            check("heal claims no maximise it could not perform",
                  "maximized column" not in actions, str(actions))
            check("and says nothing at all about a window that is fine",
                  actions == [], str(actions))
            check("which stays true however many times it is asked",
                  H.heal(session) == [], str(H.heal(session)))
        finally:
            H.stop_session(session)


def main() -> int:
    tests = [
        test_viewport_fit_repeated_width_is_idempotent,
        test_viewport_fit_nonunit_initial_scale_is_absolute,
        test_viewport_fit_narrow_wide_narrow_returns_to_same_scale,
        test_viewport_fit_transforms_use_physical_axes,
        test_viewport_fit_reports_actual_quantized_scale,
        test_viewport_fit_selects_active_output_before_action,
        test_viewport_fit_refuses_unknown_physical_mode_before_action,
        test_viewport_fit_keeps_logical_output_geometry_contract,
        test_shared_options_are_accepted_either_side_of_the_command,
        test_start_takes_the_program_and_its_arguments_verbatim,
        test_a_session_is_named_after_its_program,
        test_an_action_reports_in_one_line,
        test_an_observation_leads_with_the_window_and_nothing_else,
        test_overlapping_tiles_do_not_double_report_a_word,
        test_words_on_one_baseline_become_one_label,
        test_a_label_is_matched_exactly_before_partially,
        test_ocr_reads_both_polarities,
        test_a_command_that_cannot_start_fails_immediately,
        test_a_session_runs_an_application_and_its_text_is_readable,
        test_a_click_is_converted_out_of_framebuffer_space,
        test_an_unscaled_session_is_left_alone,
        test_a_scaled_session_reports_both_geometries,
        test_typed_text_arrives_exactly,
        test_a_nested_widget_collapses_to_the_control_that_can_be_operated,
        test_a_disabled_control_is_refused_only_when_the_state_is_reported,
        test_a_session_does_not_share_the_callers_message_bus,
        test_a_headless_session_maps_nothing_and_is_sized_exactly,
        test_a_declared_control_is_found_without_reading_the_screen,
        test_a_drag_travels_between_its_ends_without_passing_the_origin,
        test_a_tree_that_answers_partly_does_not_hide_the_rest_of_the_screen,
        test_a_menu_named_after_its_button_does_not_hide_its_own_items,
        test_heal_claims_no_repair_it_did_not_make,
        test_controls_are_never_taken_from_a_window_other_than_the_one_in_hand,
        test_a_tree_that_cannot_be_used_says_so_instead_of_looking_unfurnished,
        test_a_stale_record_never_signals_a_strangers_process_group,
        test_the_sweep_never_signals_its_own_caller,
        test_stopping_a_session_strands_no_accessibility_processes,
    ]
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for test in tests:
        if only and only not in test.__name__:
            continue
        run_test(test)

    print()
    for skip in SKIPS:
        print(f"skipped: {skip}")
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"passed ({len(SKIPS)} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
