import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from billcheck_collector import state


class StateTest(unittest.TestCase):
    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            st = state.load(Path(d) / "s.json")
        self.assertEqual(st, state.State(None, 0, {}))

    def test_state_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            first = state.State("9", 5, {"3": 1.0})
            state.save(p, first)
            self.assertEqual(state.load(p), first)
            with mock.patch("os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    state.save(p, state.State("9", 99, {}))
            self.assertEqual(state.load(p), first)
            self.assertEqual(os.listdir(d), ["s.json"])
