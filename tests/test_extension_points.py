"""扩展点测试：Detector / Action 注册表与合并规则。"""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.features.audit import actions, detectors  # noqa: E402


class _Logger:
    def __init__(self):
        self.lines = []

    def warning(self, msg):
        self.lines.append(msg)


class _Host:
    def __init__(self):
        self.logger = _Logger()


class DetectorTest(unittest.TestCase):
    def tearDown(self):
        detectors.clear()
        actions.clear()

    def test_empty_registry_is_noop(self):
        v = {"violation": False}
        self.assertEqual(detectors.count(), 0)
        self.assertIs(detectors.merge(v, []), v)

    def test_merge_only_escalates(self):
        base = {"violation": False, "severity": "", "confidence": 0.1}
        ev = detectors.Evidence(name="extra", violation=True, severity="high",
                                category="fraud", confidence=0.9)
        out = detectors.merge(base, [ev])
        self.assertTrue(out["violation"])
        self.assertEqual(out["severity"], "high")
        self.assertEqual(out["category"], "fraud")
        self.assertEqual(out["confidence"], 0.9)

    def test_merge_never_downgrades(self):
        base = {"violation": True, "severity": "high", "confidence": 0.95}
        ev = detectors.Evidence(name="weak", violation=False, severity="low", confidence=0.2)
        out = detectors.merge(base, [ev])
        self.assertTrue(out["violation"])
        self.assertEqual(out["severity"], "high")
        self.assertEqual(out["confidence"], 0.95)

    def test_detector_exception_is_isolated(self):
        class Bad:
            name = "bad"

            async def detect(self, ctx):
                raise RuntimeError("boom")

        detectors.register(Bad())
        host = _Host()
        evs = asyncio.run(detectors.collect(host, {}))
        self.assertEqual(evs, [])
        self.assertTrue(any("bad" in x for x in host.logger.lines))


class ActionTest(unittest.TestCase):
    def tearDown(self):
        actions.clear()

    def test_extra_action_runs_and_reports(self):
        class Notify:
            name = "notify"

            async def apply(self, ctx):
                return {"group": ctx["group"]}

        actions.register(Notify())
        host = _Host()
        out = asyncio.run(actions.run(host, {"group": "g1"}))
        self.assertEqual(out[0]["name"], "notify")
        self.assertEqual(out[0]["result"]["group"], "g1")


if __name__ == "__main__":
    unittest.main()
