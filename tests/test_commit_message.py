"""Tests for tools/check_commit_message.py (commit message and pull request checks)."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "check_commit_message.py"


def load():
    spec = importlib.util.spec_from_file_location("check_commit_message", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load()


class MessageTests(unittest.TestCase):
    def test_good_messages(self):
        for text in ("Add profile photos", "Add profile photos\n",
                     "Add profile photos\n\nAnyone can upload a photo.\nSecond line.\n",
                     "Fix it\n\nBody\n\n# Please enter the commit message\n"):
            with self.subTest(text=text):
                self.assertEqual(check.check_message(text), [])

    def test_body_straight_after_subject(self):
        problems = check.check_message("Add profile photos\nAnyone can upload one profile photo.\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("second line must be blank", problems[0])

    def test_subject_length(self):
        self.assertEqual(check.check_message("x" * 72), [])
        self.assertIn("73 characters", check.check_message("x" * 73)[0])

    def test_empty(self):
        for text in ("", "\n\n", "# only a comment\n", "\n\nBody after blank subject"):
            with self.subTest(text=text):
                self.assertTrue(check.check_message(text))



class PullRequestTests(unittest.TestCase):
    def test_good(self):
        self.assertEqual(check.check_pr("Add profile photos"), [])

    def test_title(self):
        self.assertTrue(check.check_pr(""))
        self.assertTrue(check.check_pr("x" * 73))
        self.assertTrue(check.check_pr("Add\nphotos"))


class MainTests(unittest.TestCase):
    def test_usage(self):
        self.assertEqual(check.main(["x"]), 2)

    def test_pr_from_environment(self):
        import os
        old = os.environ.get("PR_TITLE")
        try:
            os.environ["PR_TITLE"] = "Add photos"
            self.assertEqual(check.main(["x", "--pr"]), 0)
            os.environ["PR_TITLE"] = "y" * 80
            self.assertEqual(check.main(["x", "--pr"]), 1)
        finally:
            if old is None:
                os.environ.pop("PR_TITLE", None)
            else:
                os.environ["PR_TITLE"] = old


if __name__ == "__main__":
    unittest.main()
