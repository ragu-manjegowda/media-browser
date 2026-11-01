"""Fail on namcap errors; preserve its full dependency/ABI diagnostics."""

import subprocess
import sys

result = subprocess.run(["namcap", *sys.argv[1:]], text=True, capture_output=True, check=False)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
if result.returncode or any(" E: " in line for line in result.stdout.splitlines()):
    raise SystemExit(1)
