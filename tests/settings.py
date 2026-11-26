import os

os.environ["OMRON_SECRET_KEY"] = "test-only-secret-key-never-for-runtime-000000000000"

from omron_data.settings import *  # noqa: E402,F403
