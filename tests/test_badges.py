# SPDX-FileCopyrightText: 2026 Erwan Gouriou
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path

from collector import badges

NOW = "2026-02-01T00:00:00Z"


def pr(number, areas, labeled, state="open"):
    return {"number": number, "areas": areas, "labeled_at": labeled, "state": state}


def corpus(prs):
    return {"schema": 1, "generated_at": NOW, "watermark": NOW, "prs": prs}


PRS = [
    pr(1, ["SPI"], "2026-01-22T00:00:00Z"),  # 10 days
    pr(2, ["SPI", "Boards/SoCs"], "2026-01-02T00:00:00Z"),  # 30 days
    pr(3, ["Boards/SoCs"], "2025-12-03T00:00:00Z"),  # 60 days
    pr(4, ["I2C"], "2025-11-01T00:00:00Z", state="merged"),
]


class SlugTest(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(badges.slugify("Bluetooth Host"), "bluetooth-host")
        self.assertEqual(badges.slugify("Boards/SoCs"), "boards-socs")
        self.assertEqual(badges.slugify("  I2C  "), "i2c")

    def test_empty_slug(self):
        with self.assertRaises(ValueError):
            badges.slugify("//")

    def test_collision_fails(self):
        with self.assertRaises(badges.SlugCollisionError):
            badges.area_slugs({"Boards/SoCs", "Boards SoCs"})


class PayloadTest(unittest.TestCase):
    def test_payload(self):
        doc = badges.payload("SPI", [10.0, 30.0])
        self.assertEqual(doc["message"], "2 open, median 20 d")
        self.assertEqual(doc["color"], "001E91")
        self.assertEqual(doc["labelColor"], "B0B7B9")
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["cacheSeconds"], 300)

    def test_zero_open(self):
        doc = badges.payload("I2C", [])
        self.assertEqual(doc["message"], "0 open")
        self.assertEqual(doc["color"], "001E91")

    def test_colors(self):
        self.assertEqual(badges.color_for(None), "001E91")
        self.assertEqual(badges.color_for(30), "001E91")
        self.assertEqual(badges.color_for(45), "FAD200")
        self.assertEqual(badges.color_for(500), "FAD200")


class BuildTest(unittest.TestCase):
    def test_files(self):
        files = badges.build_badges(corpus(PRS), {})
        self.assertEqual(
            sorted(files),
            [
                "area/boards-socs.json",
                "area/i2c.json",
                "area/spi.json",
                "index.json",
                "overall.json",
            ],
        )
        self.assertEqual(files["overall.json"]["message"], "3 open, median 30 d")
        self.assertEqual(files["area/spi.json"]["message"], "2 open, median 20 d")
        self.assertEqual(files["area/boards-socs.json"]["message"], "2 open, median 45 d")
        # Only a merged PR ever had this area: it renders 0 open.
        self.assertEqual(files["area/i2c.json"]["message"], "0 open")
        self.assertEqual(
            files["index.json"]["areas"],
            {"Boards/SoCs": "boards-socs", "I2C": "i2c", "SPI": "spi"},
        )

    def test_existing_area_missing_from_corpus_is_kept(self):
        files = badges.build_badges(corpus(PRS[:1]), {"uart": "UART"})
        self.assertEqual(files["area/uart.json"]["label"], "UART")
        self.assertEqual(files["area/uart.json"]["message"], "0 open")
        self.assertEqual(files["index.json"]["areas"]["UART"], "uart")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "badges"

    def test_never_deletes_and_zero_not_404(self):
        badges.update_badges(corpus(PRS), self.dir)
        before = sorted(p.name for p in (self.dir / "area").iterdir())
        self.assertEqual(before, ["boards-socs.json", "i2c.json", "spi.json"])

        # Everything closes and the corpus loses its area history entirely.
        empty = corpus([pr(9, ["Clock"], "2026-01-31T00:00:00Z")])
        badges.update_badges(empty, self.dir)
        after = sorted(p.name for p in (self.dir / "area").iterdir())
        self.assertEqual(after, ["boards-socs.json", "clock.json", "i2c.json", "spi.json"])
        spi = json.loads((self.dir / "area" / "spi.json").read_text())
        self.assertEqual(spi["message"], "0 open")
        self.assertEqual(spi["label"], "SPI")

    def test_unchanged_run_rewrites_nothing(self):
        self.assertEqual(len(badges.update_badges(corpus(PRS), self.dir)), 5)
        self.assertEqual(badges.update_badges(corpus(PRS), self.dir), [])
        self.assertFalse(any(p.name.startswith(".") for p in self.dir.rglob("*")))


if __name__ == "__main__":
    unittest.main()
