"""Windows service entry point - the counterpart to run.sh's final exec.

Run under pythonw.exe so there is no console window to accidentally close,
which means there is also no stdout to inherit: everything, including a
traceback from a library that writes to stderr directly, is pointed at
data/server.log here rather than through a uvicorn --log-config, so nothing
can be lost to a None stdout.

    pythonw.exe serve_win.py

Host and port come from the same AUDIOBOOK_* variables run.sh uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import SETTINGS  # noqa: E402


def main() -> None:
    log_path = SETTINGS.data_dir / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # line buffered so a tail of the log is current, and errors="replace" so a
    # stray byte in a book title can never take the server down mid-job.
    stream = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = stream
    sys.stderr = stream

    import uvicorn

    uvicorn.run("app.main:app", host=SETTINGS.host, port=SETTINGS.port)


if __name__ == "__main__":
    main()
