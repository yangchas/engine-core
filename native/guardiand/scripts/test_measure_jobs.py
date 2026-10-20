import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("measure_jobs.py")
SPEC = importlib.util.spec_from_file_location("measure_jobs", SCRIPT)
measure_jobs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure_jobs)


class MeasureJobsTests(unittest.TestCase):
    def test_timeout_and_exit_code_are_recorded(self):
        timed = measure_jobs.run_command(["{python}", "-c", "import time; time.sleep(2)"],
                                         timeout=0.05)
        failed = measure_jobs.run_command(["{python}", "-c", "raise SystemExit(7)"], timeout=2)
        self.assertEqual(timed["status"], "TIMEOUT")
        self.assertEqual(failed["returncode"], 7)

    def test_outside_write_target_is_unsafe(self):
        with tempfile.TemporaryDirectory(prefix="c366-sandbox-") as tmp:
            root = Path(tmp) / "artifact"
            root.mkdir()
            self.assertEqual(measure_jobs.audit_targets([str(root / "x")], root), ("SAFE", []))
            status, reasons = measure_jobs.audit_targets(["/home/exedev/core-data/elsewhere"], root)
            self.assertEqual(status, "UNSAFE")
            self.assertTrue(reasons)

    def test_cleanup_requires_owned_temp_prefix(self):
        with tempfile.TemporaryDirectory(prefix="c366-sandbox-") as tmp:
            candidate = Path(tmp)
            self.assertTrue(measure_jobs.assert_owned_sandbox(candidate))
            with self.assertRaises(ValueError):
                measure_jobs.assert_owned_sandbox(Path("/home/exedev/core-data/guardian-native"))

    def test_empty_partial_fetch_is_not_measured_as_success(self):
        result = {"status": "COMPLETED", "returncode": 0, "stderr": ""}
        self.assertEqual(measure_jobs.classify_result(result, "PARTIAL", 0)[0],
                         "SOURCE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
