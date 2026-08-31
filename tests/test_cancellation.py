import importlib
import importlib.util
import unittest


class CancellationTokenTest(unittest.TestCase):
    def cancellation_class(self):
        self.assertIsNotNone(
            importlib.util.find_spec("opencode_orchestrator.cancellation"),
            "cancellation module is missing",
        )
        return importlib.import_module("opencode_orchestrator.cancellation").CancellationToken

    def test_cancel_runs_registered_close_callback_once(self):
        token = self.cancellation_class()()
        calls = []
        token.add_callback(lambda: calls.append("closed"))

        self.assertTrue(token.cancel("external"))
        self.assertFalse(token.cancel("again"))

        self.assertEqual(calls, ["closed"])
        self.assertTrue(token.cancelled)
        self.assertEqual(token.reason, "external")

    def test_callback_added_after_cancellation_runs_immediately(self):
        token = self.cancellation_class()()
        token.cancel("client")
        calls = []

        token.add_callback(lambda: calls.append("late"))

        self.assertEqual(calls, ["late"])

    def test_one_callback_failure_does_not_skip_remaining_callbacks(self):
        token = self.cancellation_class()()
        calls = []

        def fail():
            raise RuntimeError("close failed")

        token.add_callback(fail)
        token.add_callback(lambda: calls.append("second"))

        self.assertTrue(token.cancel("external"))
        self.assertEqual(calls, ["second"])


if __name__ == "__main__":
    unittest.main()
