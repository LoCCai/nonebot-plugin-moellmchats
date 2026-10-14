from __future__ import annotations

import os
from pathlib import Path

import nonebot

os.environ.setdefault("LOCALSTORE_USE_CWD", "true")
os.environ.setdefault("NICKNAME", '["TestBot"]')
os.environ.setdefault("SUPERUSERS", '["1"]')
os.environ.setdefault("DRIVER", "~none")
# The render/meme tools read these at import time to decide registration;
# scrub them so the suite is deterministic even on a configured host.
for _moellm_env in (
    "MOELLM_RENDER_ENABLED",
    "MOELLM_RENDER_BROWSER_EXECUTABLE",
    "MOELLM_RENDER_BROWSER_ENDPOINT",
    "MOELLM_RENDER_DEFAULT_WIDTH",
    "MOELLM_MEME_API_BASE",
):
    os.environ.pop(_moellm_env, None)

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

nonebot.load_plugin("nonebot_plugin_localstore")

import nonebot_plugin_localstore as localstore

_test_data = Path(__file__).parent / ".data"
localstore.get_plugin_config_dir = lambda *args, **kwargs: _test_data / "config"
localstore.get_plugin_data_dir = lambda *args, **kwargs: _test_data / "data"
localstore.get_plugin_cache_dir = lambda *args, **kwargs: _test_data / "cache"
