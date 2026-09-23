"""Point the database at a temporary directory for the test run.

`app.main` builds its `Database` at import time, and the production default is
`/var/lib/sagamore/sagamore.db` — correct for the systemd deployment, but not
creatable by whoever just cloned this repo. Without this, `pytest` fails with a
PermissionError that says nothing about the code.

An empty DB_PATH is treated as unset: `DB_PATH=` in the environment is a missing
value, not a deliberate choice of "" (which sqlite cannot open either).
"""

import os
import tempfile

if not os.environ.get("DB_PATH"):
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="sagamore-tests-"), "test.db"
    )
