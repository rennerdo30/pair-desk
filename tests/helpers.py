import base64
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pair_desk.store import Store  # noqa: E402

# The suite may itself run inside an agent-bridge delegated job; the desk under test starts as a top-level
# session unless a test sets these (pair_desk.notify.BRIDGE_JOB_ENV).
for _name in ("AGENT_BRIDGE_JOB_ID", "AGENT_BRIDGE_PARENT_JOB"):
    os.environ.pop(_name, None)

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode("ascii")


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 9, 29, 12, 0, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += dt.timedelta(**kw)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pairdesk-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class StoreCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.store = Store(self.tmp, clock=self.clock)
        self.store.create_project("mygame", "MyGame", "MG")

    def tearDown(self):
        self.store.close()
        super().tearDown()
