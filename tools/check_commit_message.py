"""Checks commit messages, and pull request titles and descriptions.

Used by the commit-messages GitHub Actions job (every pull request) and, if you
install it, a local commit-msg hook. Standard library only.

    python3 tools/check_commit_message.py FILE      # a commit message in a file
    python3 tools/check_commit_message.py --range BASE..HEAD   # every commit in a range
    python3 tools/check_commit_message.py --pr      # PR_TITLE and PR_BODY from the environment

Rules:
- the subject (first line) is not empty and at most 72 characters;
- a message with a body has a blank second line, so the subject stays one line;
- no AI attribution lines ("Co-Authored-By: Claude", "Generated with Claude Code", ...).

Exits 1 and prints every problem if anything fails.
"""

import os
import re
import subprocess
import sys

MAX_SUBJECT = 72
ATTRIBUTION_RE = re.compile(
    r"co-authored-by:.*(claude|anthropic|openai|copilot|gpt|gemini)"
    r"|generated (with|by) .*(claude|ai\b|copilot|chatgpt)"
    r"|noreply@anthropic\.com",
    re.IGNORECASE)


def message_lines(text):
    # Lines starting with "#" are git's editor comments, removed before the commit is made.
    return [line.rstrip() for line in text.splitlines() if not line.startswith("#")]


def check_subject(subject, label):
    problems = []
    if not subject.strip():
        problems.append(f"{label}: the subject (first line) is empty")
    elif len(subject) > MAX_SUBJECT:
        problems.append(f"{label}: the subject is {len(subject)} characters (at most {MAX_SUBJECT}): {subject[:60]!r}...")
    return problems


def check_attribution(lines, label):
    return [f"{label}: AI attribution line: {line.strip()!r}" for line in lines if ATTRIBUTION_RE.search(line)]


def check_message(text, label="commit message"):
    lines = message_lines(text)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return [f"{label}: empty message"]
    problems = check_subject(lines[0], label)
    if len(lines) > 1 and lines[1]:
        problems.append(f"{label}: the second line must be blank (otherwise the subject runs on into the body)")
    return problems + check_attribution(lines, label)


def check_pr(title, body):
    problems = check_subject(title, "pull request title")
    if "\n" in title or "\r" in title:
        problems.append("pull request title: contains a line break")
    return problems + check_attribution(body.splitlines(), "pull request description")


def check_range(rev_range):
    shas = subprocess.run(["git", "rev-list", "--no-merges", rev_range],
                          check=True, capture_output=True, text=True).stdout.split()
    problems = []
    for sha in shas:
        text = subprocess.run(["git", "log", "-1", "--format=%B", sha],
                              check=True, capture_output=True, text=True).stdout
        problems += check_message(text, f"commit {sha[:7]}")
    return problems


def main(argv):
    if argv[1:] == ["--pr"]:
        problems = check_pr(os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""))
    elif len(argv) == 3 and argv[1] == "--range":
        problems = check_range(argv[2])
    elif len(argv) == 2:
        with open(argv[1], encoding="utf-8") as f:
            problems = check_message(f.read())
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
