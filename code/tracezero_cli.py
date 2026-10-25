"""`tracezero` console command: runs code/main.py exactly as `python code/main.py` does.

The engine resolves its defaults (./dataset, ./output.csv, code/cache/) from its own location in the
source tree, so TraceZero is installed from a checkout with `pip install -e .`; this wrapper refuses to
run from anywhere else instead of silently reading or writing inside the Python environment.
"""
import runpy
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent


def main():
    if not (CODE / "main.py").is_file() or not (CODE / "audit").is_dir():
        sys.exit(f"tracezero: engine sources not found next to {__file__}; "
                 "install from a source checkout with `pip install -e .`")
    sys.path.insert(0, str(CODE))  # what `python code/main.py` puts first on sys.path
    runpy.run_path(str(CODE / "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
