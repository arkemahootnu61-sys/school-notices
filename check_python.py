"""Reject interpreters that cannot run this project (used by setup.bat)."""
import sys

if sys.version_info < (3, 10):
    print(f'需要 Python 3.10 或更高版本；当前是 {sys.version.split()[0]}。')
    raise SystemExit(1)
print(f'Python {sys.version.split()[0]} 可用')
