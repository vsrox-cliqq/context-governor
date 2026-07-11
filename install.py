#!/usr/bin/env python3
"""Back-compat shim — install logic now lives in `governor.py install`.

Old:  python3 install.py --claude
New:  python3 governor.py install --claude   (or just: governor install)

Flags are identical (--claude, --cursor, --cursor-project DIR, --all);
they are passed straight through.
"""

import os
import sys
from pathlib import Path

GOVERNOR = Path(__file__).resolve().parent / "governor.py"

args = sys.argv[1:] or ["--claude"]
os.execv(sys.executable, [sys.executable, str(GOVERNOR), "install"] + args)
