#!/usr/bin/env python3
"""Self-test for niri-harness.

Two layers:

* Pure tests over the OCR and label-matching logic. They synthesise their own
  images, so they are fast, deterministic and need no compositor.
* One end-to-end test that starts a real nested session running a real
  application and reads its text back. It is skipped, loudly, when `niri`,
  `kitty` or a host Wayland session is unavailable.

Nothing here touches the caller's desktop: the nested session owns its own
compositor, its own output and its own pointer.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
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

        # Two identical labels are ordinary in a UI; choosing the first is fine,
        # saying nothing about it is not.
        runs.append(run_at("Changes", 10, 400))
        runs[-1].ref = "t3"
        _, _, repeated = H.resolve_target(session, "Changes", fresh=False)
        check("repeat is reported", "first of 2" in repeated, repeated)

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
    runs = H.ocr_runs(path, min_confidence=40.0)
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
# end to end


def host_windows() -> list[dict]:
    """The host compositor's windows, for non-interference assertions."""
    import json
    import subprocess

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

            # The harness must be startable while its caller keeps working, so
            # its own window on the host may neither take focus nor float.
            # Which window the host focuses instead is not ours to assert: on a
            # busy desktop something else may legitimately take it.
            host = [w for w in host_windows() if w.get("pid") == session.pid]
            check("host window exists", len(host) == 1, f"found {len(host)}")
            if host:
                check("does not steal focus", not host[0].get("is_focused"), str(host[0]))
                check("does not float", not host[0].get("is_floating"), str(host[0]))
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
    """
    require("niri")
    require("kitty")
    require("tesseract")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise _Skip("no host Wayland session")

    with tempfile.TemporaryDirectory() as scratch:
        session = H.start_session(
            "niri-harness-selftest-scale",
            "exec kitty -- sh -c 'printf \"SCALEPROBE\\n\"; sleep 600'",
            root=Path(scratch) / "run",
            portal=None,
            env_overrides={},
            timeout=60.0,
            width=1600,
        )
        try:
            payload = H.observe(session, window_match=None, do_heal=True, with_text=True)
            output = payload["output"]
            frame_w, frame_h = H.load_image(Path(payload["frame"])).size
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


def main() -> int:
    tests = [
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
