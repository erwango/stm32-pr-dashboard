# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path

from collector import areas

MAINTAINERS = """\
# File format comment
#   - looks like a list item but is a comment
"Drivers: SPI":
  status: odd fixes
  collaborators:
    - someone
  files:
    - drivers/spi/
    - include/zephyr/drivers/spi.h
  labels:
    - "area: SPI"

"Boards/SoCs":
  status: maintained
  files:
    - boards/
    - soc/
  labels:
    - "area: Boards/SoCs"

USB:
  description: >-
    Plain text with a bullet list:
    - drivers/fake/
  files:
    - drivers/usb/
    - subsys/usb/
  labels:
    - "area: USB"

Bluetooth HCI:
  files:
    - drivers/bluetooth/
  labels:
    - "area: Bluetooth"

Bluetooth Host:
  files:
    - subsys/bluetooth/host/
  labels:
    - "area: Bluetooth"

Bluetooth Mesh:
  files:
    - subsys/bluetooth/mesh/
  labels:
    - "area: Bluetooth"

"Networking: Ethernet":
  files:
    - subsys/net/l2/ethernet/
  labels:
    - "area: Ethernet"

"Drivers: Ethernet":
  files:
    - drivers/ethernet/
  labels:
    - "area: ethernet"

Native_sim and POSIX arch:
  files:
    - drivers/*/*posix*
    - drivers/interrupt_controller/intc_plic.c
  labels:
    - "area: native port"

Shields:
  files:
    - boards/shields/
  labels:
    - "area: Shields"

"West project: foo":
  files: []
  labels:
    - "area: Audio"

"Drivers: Audio":
  files:
    - drivers/audio/
  labels:
    - "area: Audio"
    - "plain label"

Tests:
  files:
    - tests/
  labels:
    - "area: Tests"
"""


class ParseTest(unittest.TestCase):
    def test_parse(self):
        parsed = {a.title: a for a in areas.parse_maintainers(MAINTAINERS)}
        spi = parsed["Drivers: SPI"]
        self.assertEqual(spi.files, ["drivers/spi/", "include/zephyr/drivers/spi.h"])
        self.assertEqual(parsed["Drivers: SPI"].labels, ["area: SPI"])
        self.assertEqual(parsed["West project: foo"].files, [])

    def test_description_and_people_are_ignored(self):
        usb = {a.title: a for a in areas.parse_maintainers(MAINTAINERS)}["USB"]
        self.assertEqual(usb.files, ["drivers/usb/", "subsys/usb/"])
        self.assertNotIn("someone", repr(areas.parse_maintainers(MAINTAINERS)))


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.hardware = areas.hardware_labels(areas.parse_maintainers(MAINTAINERS))

    def test_driver_boards_and_whole_directory_areas(self):
        for name in ("spi", "boards/socs", "usb", "audio"):
            self.assertIn(name, self.hardware)

    def test_label_needs_half_of_its_areas(self):
        self.assertNotIn("bluetooth", self.hardware)  # 1 of 3
        self.assertIn("ethernet", self.hardware)  # 1 of 2, case-insensitive

    def test_not_hardware(self):
        for name in ("shields", "tests", "native port", "plain label"):
            self.assertNotIn(name, self.hardware)

    def test_sorted_lowercase(self):
        self.assertEqual(self.hardware, sorted(self.hardware))
        self.assertTrue(all(name == name.lower() for name in self.hardware))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.output = Path(self._dir.name) / "areas.json"

    def test_writes_and_is_stable(self):
        self.assertTrue(areas.update(MAINTAINERS, self.output))
        first = self.output.read_text()
        self.assertTrue(areas.update(MAINTAINERS, self.output))
        self.assertEqual(self.output.read_text(), first)
        self.assertIn("spi", json.loads(first)["hardware"])

    def test_empty_result_keeps_previous(self):
        areas.update(MAINTAINERS, self.output)
        before = self.output.read_text()
        self.assertFalse(areas.update("garbage: 1\n", self.output))
        self.assertEqual(self.output.read_text(), before)

    def test_large_shrink_keeps_previous(self):
        areas.update(MAINTAINERS, self.output)
        before = self.output.read_text()
        tiny = '"Drivers: SPI":\n  files:\n    - drivers/spi/\n  labels:\n    - "area: SPI"\n'
        self.assertFalse(areas.update(tiny, self.output))
        self.assertEqual(self.output.read_text(), before)

    def test_main_never_fails(self):
        missing = Path(self._dir.name) / "nope.yml"
        self.assertEqual(areas.main(["--source", str(missing), "--output", str(self.output)]), 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
