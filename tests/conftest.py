"""Ensure the pinned pytest basetemp parent exists before tests run.

pyproject.toml pins `--basetemp=temp/pytest` (to dodge sandbox/AV
denials on the user temp dir — see the comment there), but pytest only
creates the basetemp directory itself, NOT its parents. On the operator
machine `temp/` always exists (run.bat creates it at startup); on a
fresh checkout (CI, new clone) it doesn't, and every tmp_path-using
test errors with FileNotFoundError.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
(_REPO / "temp").mkdir(exist_ok=True)
