"""
Stop keys from ever reaching GitHub (the repo is public).

Runs before every commit (.githooks/pre-commit) and on GitHub for every push.
Usage: python check_secrets.py            → checks files staged for commit
       python check_secrets.py --all      → checks every tracked file
Exits 1 and names the file if anything looks like a secret.
"""
import re
import subprocess
import sys

PATTERNS = {
    "Google API key": r"AIza[0-9A-Za-z_\-]{30,}",
    "Groq key": r"gsk_[0-9A-Za-z]{20,}",
    "OpenRouter/OpenAI key": r"sk-(or-v1-)?[0-9A-Za-z_\-]{30,}",
    "Telegram bot token": r"\b\d{8,10}:AA[0-9A-Za-z_\-]{30,}",
    "Private key": r"-----BEGIN (RSA |EC )?PRIVATE KEY-----",
    "Service account file": r'"type":\s*"service_account"',
}
BLOCKED_FILES = (".env", "credentials.json")


def files(all_files: bool) -> list[str]:
    cmd = ["git", "ls-files"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    return [f for f in subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines() if f]


def main() -> int:
    problems = []
    for path in files("--all" in sys.argv):
        if path.split("/")[-1] in BLOCKED_FILES:
            problems.append(f"{path}: this file must never be uploaded")
            continue
        if path == "check_secrets.py":
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for name, pattern in PATTERNS.items():
            if re.search(pattern, text):
                problems.append(f"{path}: looks like a {name}")
    if problems:
        print("🚫 Possible secret found, NOT committing:\n  " + "\n  ".join(problems))
        print("Keep keys in .env on the server only.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
