from __future__ import annotations

import getpass
import os
import re
import sys


def default_endpoint() -> str:
	"""The service's endpoint: SBUD_SOCKET if set, else the named pipe or socket the service uses by default."""
	override = os.environ.get("SBUD_SOCKET")
	if override:
		return override
	if sys.platform == "win32":
		user = re.sub(r"[^A-Za-z0-9._-]", "_", getpass.getuser())
		return rf"\\.\pipe\sbud-{user}"
	runtime = os.environ.get("XDG_RUNTIME_DIR")
	if runtime:
		return os.path.join(runtime, "sbud.sock")
	home = os.environ.get("SBUD_HOME") or os.path.join(os.path.expanduser("~"), ".sbud")
	return os.path.join(home, "sbud.sock")
