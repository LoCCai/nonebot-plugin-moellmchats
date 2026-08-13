from __future__ import annotations

import os
from pathlib import Path

import nonebot

os.environ.setdefault("LOCALSTORE_USE_CWD", "true")
os.environ.setdefault("NICKNAME", '["TestBot"]')
os.environ.setdefault("SUPERUSERS", '["1"]')
os.environ.setdefault("DRIVER", "~none")

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
