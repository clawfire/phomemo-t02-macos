"""Keep every test away from the real ~/Library: point the app dirs at a temp folder."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="t02-tests-")
os.environ.setdefault("T02_HOME", _tmp)
os.environ.setdefault("T02_LOG_DIR", os.path.join(_tmp, "logs"))

from t02bridge.config import ensure_dirs  # noqa: E402  (after the env is set)

ensure_dirs()
