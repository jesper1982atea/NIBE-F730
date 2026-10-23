import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_controller import config, entity, fixtures
from controller import ControlError, Controller, RESTORE_CONFIRM_SECONDS, TEST_SECONDS


class PumpTestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.offset = 0
        self.ha = Mock()
        self.ha.states.side_effect = lambda: [*fixtures()[:-1], entity("number.offset", self.offset, min=-10, max=10, step=1)]
        self.ha.integration_registry.return_value = []
        self.controller = Controller(self.ha, self.temp.name)
        self.controller.save(config(enabled=True, target_temperature=23))
        self.controller.last_write = time.time() - 3600

    def kinds(self):
        return [e["kind"] for e in self.controller.events.recent()]

    def tick_at(self, offset):
        with patch("controller.time.time", return_value=time.time() + offset):
            self.controller.tick()

    def test_start_writes_one_step_and_blocks_automation(self):
        test = self.controller.start_test()
        self.ha.set_offset.assert_called_once_with("number.offset", 1)
        self.assertEqual((test["original"], test["value"], test["phase"]), (0, 1, "running"))
        self.controller.tick()
        self.ha.set_offset.assert_called_once()
        self.assertIn("Test av styrningen", self.controller.snapshot()["message"])
        self.assertIn("test_sent", self.kinds())

    def test_steps_down_at_upper_limit(self):
        self.controller.save(config(enabled=True, target_temperature=23, max_offset=0))
        self.assertEqual(self.controller.start_test()["value"], -1)

    def test_refused_without_room_or_while_pending(self):
        self.controller.save(config(min_offset=0, max_offset=0))
        with self.assertRaisesRegex(ControlError, "utrymme"):
            self.controller.start_test()
        self.controller.save(config())
        self.controller.pending = {"value": 1}
        with self.assertRaisesRegex(ControlError, "väntar"):
            self.controller.start_test()
        self.ha.set_offset.assert_not_called()

    def test_refused_when_already_running(self):
        self.controller.start_test()
        with self.assertRaisesRegex(ControlError, "pågår"):
            self.controller.start_test()

    def test_full_cycle_is_confirmed(self):
        self.controller.start_test()
        self.offset = 1
        self.controller.tick()
        self.assertIn("test_observed", self.kinds())
        self.tick_at(TEST_SECONDS + 1)
        self.ha.set_offset.assert_called_with("number.offset", 0)
        self.assertEqual(self.controller.test["phase"], "restoring")
        self.offset = 0
        self.tick_at(TEST_SECONDS + 70)
        self.assertEqual((self.controller.test["phase"], self.controller.test["result"]), ("done", "confirmed"))
        self.assertEqual(self.ha.set_offset.call_count, 2)

    def test_never_read_back_is_unconfirmed(self):
        self.controller.start_test()
        self.tick_at(TEST_SECONDS + 1)
        self.tick_at(TEST_SECONDS + 70)
        self.assertEqual(self.controller.test["result"], "unconfirmed")

    def test_restore_not_read_back_times_out(self):
        self.controller.start_test()
        self.offset = 1
        self.tick_at(TEST_SECONDS + 1)
        self.tick_at(TEST_SECONDS + RESTORE_CONFIRM_SECONDS + 2)
        self.assertEqual(self.controller.test["result"], "restore_unconfirmed")

    def test_value_changed_by_someone_else_is_left_alone(self):
        self.controller.start_test()
        self.offset = 3
        self.tick_at(TEST_SECONDS + 1)
        self.assertEqual(self.controller.test["result"], "changed")
        self.ha.set_offset.assert_called_once()

    def test_stop_restores_now(self):
        self.controller.start_test()
        self.offset = 1
        self.controller.stop_test()
        self.ha.set_offset.assert_called_with("number.offset", 0)
        self.assertEqual(self.controller.test["phase"], "restoring")
        with self.assertRaises(ControlError):
            self.controller.stop_test()

    def test_survives_restart(self):
        self.controller.start_test()
        restored = Controller(self.ha, self.temp.name)
        self.assertEqual(restored.test["phase"], "running")
        self.assertEqual(restored.test_status()["test"]["value"], 1)
        self.assertTrue((Path(self.temp.name) / "test-state.json").exists())

    def test_write_failure_is_reported(self):
        self.ha.set_offset.side_effect = ControlError("Nekad")
        with self.assertRaisesRegex(ControlError, "Nekad"):
            self.controller.start_test()
        self.assertEqual(self.controller.test["result"], "failed")
        self.assertIn("test_failed", self.kinds())

    def test_restore_failure_retries(self):
        self.controller.start_test()
        self.offset = 1
        self.ha.set_offset.side_effect = ControlError("Timeout")
        with self.assertRaises(ControlError):
            self.tick_at(TEST_SECONDS + 1)
        self.assertEqual(self.controller.test["phase"], "running")
        self.ha.set_offset.side_effect = None
        self.tick_at(TEST_SECONDS + 70)
        self.assertEqual(self.controller.test["phase"], "restoring")


if __name__ == "__main__":
    unittest.main()
