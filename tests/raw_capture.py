#!/usr/bin/env python3
"""Offline owning-path regressions. No compositor, child or tool is executed.

For the causal baseline only: --harness-source FILE
FullOutputCaptureTests.test_fractional_frame_preserves_last_row_and_corners
"""
import copy
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

source = Path(__file__).resolve().parent.parent / "bin/niri-harness"
if "--harness-source" in sys.argv:
    index = sys.argv.index("--harness-source")
    source = Path(sys.argv[index + 1])
    del sys.argv[index:index + 2]
loader = importlib.machinery.SourceFileLoader("capture_test_harness", str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
H = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = H
loader.exec_module(H)


def output(transform="Flipped180", width=1278, height=800):
    return {"name": "winit", "make": "Smithay", "model": "Winit",
            "current_mode": 0, "modes": [{"width": width, "height": height}],
            "logical": {"x": 0, "y": 0, "width": 1002, "height": 627,
                        "scale": 1.275, "transform": transform}}


def packet(image, transform=6, flags=1, **changes):
    header = {"name": "winit", "width": image.width, "height": image.height,
              "stride": image.width * 4, "format": "XRGB8888LE",
              "transform": transform, "flags": flags}
    header.update(changes)
    return json.dumps(header).encode() + b"\n" + image.tobytes("raw", "BGRX")


class Tool:
    def __str__(self):
        return "/fake/vcap"

    def is_file(self):
        return True


class Session:
    env = {"WAYLAND_DISPLAY": "/fake/private/wayland-1", "NIRI_SOCKET": "/fake/private/niri.sock"}

    def __init__(self, state):
        self.state = state
        self.reads = 0

    def niri_json(self, *arguments):
        if arguments != ("outputs",):
            raise AssertionError("capture attempted a non-output IPC operation")
        self.reads += 1
        value = {"winit": copy.deepcopy(self.state)}
        if hasattr(self, "second_active"):
            value["other"] = copy.deepcopy(self.second_active)
        return value


class FullOutputCaptureTests(unittest.TestCase):
    def test_fractional_frame_preserves_last_row_and_corners(self):
        image = Image.new("RGB", (1278, 800), (4, 5, 6))
        for x in range(1278):
            image.putpixel((x, 799), (191, 17, 31))
        corners = {(0, 0): (255, 0, 0), (1277, 0): (0, 255, 0),
                   (0, 799): (0, 0, 255), (1277, 799): (255, 255, 0)}
        for point, color in corners.items():
            image.putpixel(point, color)
        raw = packet(image)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(kwargs["env"], Session.env)
            self.assertEqual(kwargs["timeout"], 25)
            if argv == ["/fake/vcap", "winit", "1278", "800", "6"]:
                return subprocess.CompletedProcess(argv, 0, raw, b"")
            # Exact old grim behavior: composite scale=1278/1002, truncate
            # common_height. This negative loses the distinct last row.
            if argv == ["grim", "-t", "ppm", "-"]:
                normalized = Image.new("RGB", (1278, int(627 * (1278 / 1002))))
                stream = io.BytesIO()
                normalized.save(stream, format="PPM")
                return subprocess.CompletedProcess(argv, 0, stream.getvalue(), b"")
            raise AssertionError(f"unexpected external operation {argv!r}")

        with patch.object(H, "VCAP", Tool(), create=True), patch.object(H, "run", fake_run), \
                patch.object(H, "require_tool", lambda name: "/fake/grim"):
            actual = H.capture_frame(Session(output()))
        self.assertEqual(actual.size, (1278, 800))
        self.assertEqual([actual.getpixel(point) for point in corners], list(corners.values()))
        self.assertEqual(actual.getpixel((400, 799)), (191, 17, 31))
        self.assertEqual(calls, [["/fake/vcap", "winit", "1278", "800", "6"]])

    def test_bad_stride_extent_identity_flags_and_payload_refuse(self):
        image = Image.new("RGB", (2, 2), "red")
        state = output(width=2, height=2)
        for changes in [{"stride": 7}, {"stride": 4}, {"stride": 8192},
                        {"height": 1}, {"width": 3}, {"name": "foreign"},
                        {"transform": 0}, {"transform": True}, {"flags": 2},
                        {"flags": True}, {"format": "ARGB8888LE"},
                        {"extra": 1}]:
            with self.subTest(changes=changes), self.assertRaises(H.HarnessError):
                H.decode_full_output(packet(image, **changes), state, state["modes"][0])
        for raw in [packet(image)[:-1], packet(image) + b"x", b"{}", b"not-json\n"]:
            with self.subTest(raw_length=len(raw)), self.assertRaises(H.HarnessError):
                H.decode_full_output(raw, state, state["modes"][0])

    def test_f180_and_buffer_yinvert_have_independent_corner_contracts(self):
        image = Image.new("RGB", (2, 2))
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        image.putdata(colors)
        expected = {("Normal", 0): colors, ("Normal", 1): colors[2:] + colors[:2],
                    ("Flipped180", 0): colors[2:] + colors[:2], ("Flipped180", 1): colors}
        for (name, flag), order in expected.items():
            state = output(name, 2, 2)
            with self.subTest(transform=name, flags=flag):
                actual = H.decode_full_output(packet(image, 0 if name == "Normal" else 6, flag),
                                              state, state["modes"][0])
                self.assertEqual(list(actual.getdata()), order)

    def test_capture_png_and_changed_output_do_not_mutate_clipboard_or_input(self):
        image = Image.new("RGB", (1278, 800), (21, 32, 43))
        calls = []
        session = Session(output())

        def fake_run(argv, **kwargs):
            self.assertEqual(argv, ["/fake/vcap", "winit", "1278", "800", "6"])
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, packet(image), b"")

        def forbidden(*args, **kwargs):
            raise AssertionError("capture changed clipboard, pointer, focus or compositor state")

        with patch.object(H, "VCAP", Tool()), patch.object(H, "run", fake_run), \
                patch.object(H, "pointer", forbidden), patch.object(H, "keyboard_key", forbidden):
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "native.dat"
                self.assertEqual(H.capture_png(session, target), (1278, 800))
                with Image.open(target) as saved:
                    self.assertEqual(saved.format, "PNG")
                    self.assertEqual(saved.size, (1278, 800))
                    self.assertEqual(saved.getpixel((1277, 799)), (21, 32, 43))
            self.assertEqual(len(calls), 1)

            def changed_run(argv, **kwargs):
                result = fake_run(argv, **kwargs)
                session.state["logical"]["scale"] = 1.5
                return result
            with patch.object(H, "run", changed_run), self.assertRaises(H.HarnessError):
                H.capture_frame(session)

            # Keep the original row identical while activating a second row.
            # Name-filtered before/after checks used to hide this topology change.
            session.state = output()

            def activated_run(argv, **kwargs):
                result = fake_run(argv, **kwargs)
                session.second_active = output()
                session.second_active["name"] = "other"
                return result

            with patch.object(H, "run", activated_run), self.assertRaises(H.HarnessError):
                H.capture_frame(session)

    def test_pointer_scale_and_other_capture_layouts_are_preserved(self):
        session = Session(output())
        self.assertEqual(H.pointer_point(session, 601, 400), (471, 314))
        self.assertEqual(H.pointer_point(session, 1275, 798), (1000, 626))
        self.assertEqual(H.pointer_point(session, 1277, 799), (1002, 627))
        # The physical edge above must still be refused by the owning logical
        # containment guard; capture never changes or clamps input coordinates.
        self.assertGreaterEqual(H.pointer_point(session, 1277, 799)[0], 1002)
        state = output("90")
        self.assertIsNone(H.capture_full_output(Session(state)))


if __name__ == "__main__":
    unittest.main()
