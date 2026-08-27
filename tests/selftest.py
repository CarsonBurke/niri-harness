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


def main() -> int:
    tests = [
        test_overlapping_tiles_do_not_double_report_a_word,
        test_words_on_one_baseline_become_one_label,
        test_a_label_is_matched_exactly_before_partially,
        test_ocr_reads_both_polarities,
        test_a_command_that_cannot_start_fails_immediately,
        test_a_session_runs_an_application_and_its_text_is_readable,
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
