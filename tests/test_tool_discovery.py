from __future__ import annotations

import sys
from types import SimpleNamespace

import nonebot

from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.tool_discovery import (
    DISCOVERY_FEATURES_KEY,
    MAX_DISCOVERY_CATALOG_CHARS,
    MAX_DISCOVERY_FEATURES,
    PicMenuProjectionSnapshot,
    build_intent_owner_index,
    build_plugin_catalog_entries,
    discovery_directory_identity,
    finalize_discovery_catalog,
    normalize_llm_intent,
    normalize_menu_data,
    resolve_business_intent,
    with_menu_discovery,
)
from nonebot_plugin_moellmchats.tool_manager import ToolManager, ToolSnapshot


def _like_menu() -> list[dict[str, object]]:
    return [
        {
            "func": "点赞",
            "trigger_method": "命令触发",
            "trigger_condition": "<命令前缀>点赞 或 赞我 或 七七赞我",
            "brief_des": "<ft color=green>请求机器人给自己点赞</ft>",
            "detail_des": ("<ft size=40 color=green>每天可请求一次，随机获得1-10个赞</ft>"),
            "pmn_llm_intents": ["给我点个赞", "给我点赞"],
        },
        {
            "func": "资料卡点赞通知",
            "trigger_method": "被动事件触发",
            "trigger_condition": "收到 profile_like 通知",
            "brief_des": "记录真实点赞通知",
        },
    ]


def test_menu_metadata_becomes_bounded_clean_discovery_features() -> None:
    raw = _like_menu()
    raw.extend(
        [
            {"func": object(), "brief_des": "错误标题类型"},
            "不是映射",
            {
                "func": "QWeb 命令",
                "brief_des": "结构化触发",
                "trigger_method": "- **命令**：`点赞`",
                "trigger_condition": "普通用户",
                "pmn_triggers": [
                    {"type": "command", "value": "点赞"},
                    {"type": "command", "value": "赞我"},
                    {"type": "command", "value": 123},
                    object(),
                ],
            },
        ]
    )

    features = normalize_menu_data(raw)

    assert [feature["name"] for feature in features] == [
        "点赞",
        "资料卡点赞通知",
        "QWeb 命令",
    ]
    assert features[0]["summary"] == "请求机器人给自己点赞"
    assert features[0]["triggers"] == (
        {
            "type": "command",
            "value": "<命令前缀>点赞 或 赞我 或 七七赞我",
        },
    )
    assert features[0]["invocable"] is True
    assert features[0]["llm_intents"] == ("给我点个赞", "给我点赞")
    assert features[1]["invocable"] is False
    assert features[2]["permission"] == "普通用户"
    assert features[2]["triggers"] == (
        {"type": "command", "value": "点赞"},
        {"type": "command", "value": "赞我"},
    )
    assert "<ft" not in repr(features)


def test_menu_discovery_limits_feature_count_and_does_not_coerce_objects() -> None:
    raw = [
        {
            "func": f"功能{index}",
            "brief_des": "说明",
            "trigger_method": "命令触发",
            "trigger_condition": f"命令{index}",
        }
        for index in range(MAX_DISCOVERY_FEATURES + 10)
    ]
    raw.insert(0, {"func": "x" * 500, "brief_des": object()})

    features = normalize_menu_data(raw)

    assert len(features) == MAX_DISCOVERY_FEATURES
    assert len(features[0]["name"]) == 80
    assert features[0]["summary"] == features[0]["name"]
    assert "object at" not in repr(features)


def test_complete_catalog_has_a_global_fail_safe_budget() -> None:
    entries = [f"- tool_{index} | 工具 | " + "x" * 1000 for index in range(200)]

    catalog = finalize_discovery_catalog(entries)

    assert len(catalog) <= MAX_DISCOVERY_CATALOG_CHARS + 80
    assert catalog.startswith("- tool_0")
    assert "工具目录达到安全上限" in catalog
    assert "tool_199" not in catalog


def test_feature_catalog_and_selected_schema_share_the_same_menu_projection() -> None:
    info = with_menu_discovery(
        {
            "name": "群管理",
            "description": "群管理、监控与实用工具",
            "usage": "需使用命令前缀触发",
        },
        _like_menu(),
    )
    entries = build_plugin_catalog_entries("qi_group_admin", info)
    plugin_info, specs = build_nonebot_plugin_candidate({"qi_group_admin": info})

    assert entries[0].startswith("- qi_group_admin | 群管理 > 点赞 | 请求机器人给自己点赞")
    assert "点赞 或 赞我 或 七七赞我" in entries[0]
    assert "资料卡点赞通知" in entries[1]
    assert "菜单功能提示" in specs[0].description
    assert "每天可请求一次" in specs[0].description
    assert "不得通过 command 伪造：资料卡点赞通知" in specs[0].description
    assert "直接调用 Bot/NapCat API" in specs[0].description

    snapshot = ToolSnapshot(
        generation=7,
        plugin_info=plugin_info,
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    assert isinstance(
        snapshot.plugin_info["qi_group_admin"][DISCOVERY_FEATURES_KEY],
        tuple,
    )
    assert snapshot.plugin_info["qi_group_admin"][DISCOVERY_FEATURES_KEY][0]["name"] == "点赞"


def test_hidden_picmenu_features_are_only_classified_for_superusers() -> None:
    info = with_menu_discovery(
        {"name": "管理插件", "description": "管理"},
        [
            {
                "func": "公开查询",
                "brief_des": "普通用户可查看",
                "trigger_method": "命令",
                "trigger_condition": "查询",
                "pmn_hidden": False,
            },
            {
                "func": "后台维护",
                "brief_des": "仅管理员维护",
                "trigger_method": "命令",
                "trigger_condition": "维护",
                "pmn_hidden": True,
            },
        ],
    )

    user_catalog = build_plugin_catalog_entries("admin_plugin", info)
    admin_catalog = build_plugin_catalog_entries(
        "admin_plugin",
        info,
        is_superuser=True,
    )

    assert len(user_catalog) == 1
    assert "公开查询" in user_catalog[0]
    assert all("后台维护" not in line for line in user_catalog)
    assert any("后台维护" in line for line in admin_catalog)

    plugin_info, _specs = build_nonebot_plugin_candidate({"admin_plugin": info})
    user_schema = ToolManager.build_tool_schema(
        ["admin_plugin"],
        plugin_info=plugin_info,
        custom_tools={},
        is_superuser=False,
    )
    admin_schema = ToolManager.build_tool_schema(
        ["admin_plugin"],
        plugin_info=plugin_info,
        custom_tools={},
        is_superuser=True,
    )
    user_description = user_schema[0]["function"]["description"]
    admin_description = admin_schema[0]["function"]["description"]
    assert "公开查询" in user_description
    assert "后台维护" not in user_description
    assert "后台维护" in admin_description


def test_loaded_plugin_menu_is_automatic_but_custom_info_remains_authoritative(
    monkeypatch,
) -> None:
    plugin = SimpleNamespace(
        name="qi_group_admin",
        metadata=SimpleNamespace(
            name="群管理",
            description="元数据描述",
            usage="元数据用法",
            extra={"menu_data": _like_menu()},
        ),
    )
    monkeypatch.setattr(
        nonebot.plugin,
        "get_loaded_plugins",
        lambda: [plugin],
    )
    manager = object.__new__(ToolManager)
    monkeypatch.setattr(manager, "_load_custom_plugin_info", lambda: {})

    automatic = manager.build_plugin_info()["qi_group_admin"]
    assert automatic[DISCOVERY_FEATURES_KEY][0]["name"] == "点赞"

    monkeypatch.setattr(
        manager,
        "_load_custom_plugin_info",
        lambda: {
            "qi_group_admin": {
                "name": "人工覆写",
                "description": "人工描述",
                "usage": "人工精确命令",
            }
        },
    )
    overridden = manager.build_plugin_info()["qi_group_admin"]
    assert overridden == {
        "name": "人工覆写",
        "description": "人工描述",
        "usage": "人工精确命令",
    }


def test_custom_info_can_explicitly_supply_menu_data() -> None:
    info = with_menu_discovery(
        {
            "name": "人工覆写",
            "menu_data": _like_menu(),
        },
        _like_menu(),
    )

    assert "menu_data" not in info
    assert info[DISCOVERY_FEATURES_KEY][0]["name"] == "点赞"


def test_loaded_picmenu_memory_catalog_overrides_metadata_without_file_coupling(
    monkeypatch,
) -> None:
    module_name = "src.plugins.nonebot_plugin_picmenu_next"
    picmenu = SimpleNamespace(
        name="nonebot_plugin_picmenu_next",
        module_name=module_name,
        metadata=None,
    )
    target = SimpleNamespace(
        name="qi_group_admin",
        module_name="src.plugins.qi_group_admin",
        metadata=SimpleNamespace(
            name="旧群管理",
            description="旧描述",
            usage="旧用法",
            extra={"menu_data": []},
        ),
    )
    qweb_feature = SimpleNamespace(
        func="点赞与禅定",
        trigger_method="命令触发",
        trigger_condition="群成员；操作受平台频率和插件状态限制",
        brief_des="请求 Bot 点赞，或临时进入和解除禅定状态。",
        detail_des="示例：赞我",
        triggers=[
            SimpleNamespace(type="command", value="<命令前缀>点赞"),
            SimpleNamespace(type="command", value="<命令前缀>给我点赞"),
        ],
        llm_intents=["给我点个赞", "给我点赞"],
    )
    installed_catalog = [
        SimpleNamespace(
            plugin_id="qi_group_admin",
            name="七七群管理",
            description="QWeb 权威描述",
            usage="权限：群成员",
            pm_data=[qweb_feature],
        )
    ]
    monkeypatch.setitem(
        sys.modules,
        f"{module_name}.data_source",
        SimpleNamespace(get_infos=lambda: installed_catalog),
    )
    monkeypatch.setattr(
        nonebot.plugin,
        "get_loaded_plugins",
        lambda: [picmenu, target],
    )
    manager = object.__new__(ToolManager)
    monkeypatch.setattr(manager, "_load_custom_plugin_info", lambda: {})

    info = manager.build_plugin_info()["qi_group_admin"]

    assert info["name"] == "七七群管理"
    assert info["description"] == "QWeb 权威描述"
    assert info["discovery_source"] == "picmenu"
    assert info[DISCOVERY_FEATURES_KEY][0]["name"] == "点赞与禅定"
    assert info[DISCOVERY_FEATURES_KEY][0]["triggers"] == (
        {"type": "command", "value": "<命令前缀>点赞"},
        {"type": "command", "value": "<命令前缀>给我点赞"},
    )
    assert info[DISCOVERY_FEATURES_KEY][0]["llm_intents"] == (
        "给我点个赞",
        "给我点赞",
    )


def test_business_intent_aliases_are_exact_normalized_and_fail_closed() -> None:
    qi_post = with_menu_discovery(
        {"name": "群报告"},
        [
            {
                "func": "今日发言排行",
                "brief_des": "今日排行",
                "trigger_method": "命令",
                "trigger_condition": "B话榜 今日",
                "pmn_llm_intents": [
                    "今天谁发言最多",
                    "今日发言排行",
                ],
            }
        ],
    )
    owners = build_intent_owner_index({"qi_post": qi_post})
    assert normalize_llm_intent("  今天，谁发言最多？！ ") == (
        normalize_llm_intent("今天谁发言最多")
    )
    unique = resolve_business_intent(
        "今天，谁发言最多？！",
        owners=owners,
        loaded_plugins={"qi_post": qi_post},
        is_superuser=False,
        is_blacklisted=lambda _name: False,
    )
    assert unique.status == "unique"
    assert unique.owner == "qi_post"
    assert resolve_business_intent(
        "今天谁发言比较多",
        owners=owners,
        loaded_plugins={"qi_post": qi_post},
        is_superuser=False,
        is_blacklisted=lambda _name: False,
    ).status == "no_match"
    assert resolve_business_intent(
        "今天谁发言最多",
        owners=owners,
        loaded_plugins={},
        is_superuser=False,
        is_blacklisted=lambda _name: False,
    ).status == "unavailable"
    assert resolve_business_intent(
        "今天谁发言最多",
        owners=owners,
        loaded_plugins={"qi_post": qi_post},
        is_superuser=False,
        is_blacklisted=lambda _name: True,
    ).status == "unavailable"

    duplicate = build_intent_owner_index(
        {
            "qi_post": qi_post,
            "wrong_owner": with_menu_discovery(
                {"name": "错误所有者"},
                [
                    {
                        "func": "错误排行",
                        "brief_des": "不得猜测",
                        "trigger_method": "命令",
                        "trigger_condition": "错误排行",
                        "pmn_llm_intents": ["今天谁发言最多"],
                    }
                ],
            ),
        }
    )
    assert resolve_business_intent(
        "今天谁发言最多",
        owners=duplicate,
        loaded_plugins={"qi_post": qi_post, "wrong_owner": {}},
        is_superuser=True,
        is_blacklisted=lambda _name: False,
    ).status == "ambiguous"


def test_hidden_intent_owner_requires_superuser_visibility() -> None:
    info = with_menu_discovery(
        {"name": "后台"},
        [
            {
                "func": "隐藏功能",
                "brief_des": "隐藏",
                "trigger_method": "命令",
                "trigger_condition": "隐藏功能",
                "pmn_hidden": True,
                "pmn_llm_intents": ["执行隐藏维护"],
            }
        ],
    )
    owners = build_intent_owner_index({"admin_plugin": info})
    normal = resolve_business_intent(
        "执行隐藏维护",
        owners=owners,
        loaded_plugins={"admin_plugin": info},
        is_superuser=False,
        is_blacklisted=lambda _name: False,
    )
    superuser = resolve_business_intent(
        "执行隐藏维护",
        owners=owners,
        loaded_plugins={"admin_plugin": info},
        is_superuser=True,
        is_blacklisted=lambda _name: False,
    )
    assert normal.status == "unavailable"
    assert superuser.status == "unique"


def test_picmenu_projection_is_deeply_immutable_and_digest_bound() -> None:
    feature = SimpleNamespace(
        func="排行",
        trigger_method="命令",
        trigger_condition="B话榜 今日",
        brief_des="今日排行",
        detail_des="今日排行详情",
        hidden=False,
        triggers=[SimpleNamespace(type="command", value="B话榜 今日")],
        llm_intents=["今天谁发言最多"],
    )
    infos = [
        SimpleNamespace(
            plugin_id="qi_post",
            name="群报告",
            description="排行",
            usage="所有群成员",
            pm_data=[feature],
        )
    ]
    snapshot = PicMenuProjectionSnapshot.from_infos(infos)
    assert snapshot.plugin_count == 1
    assert snapshot.feature_count == 1
    assert len(snapshot.digest) == 64
    menu = snapshot.plugins["qi_post"]["menu_data"]
    assert isinstance(menu, tuple)
    try:
        menu[0]["func"] = "漂移"
    except TypeError:
        pass
    else:
        raise AssertionError("PicMenu projection must be deeply immutable")

    plugin_info = {
        "qi_post": with_menu_discovery(
            {"name": "群报告"},
            snapshot.plugins["qi_post"]["menu_data"],
        )
    }
    owners = build_intent_owner_index(plugin_info)
    count, digest = discovery_directory_identity(plugin_info, owners)
    assert count == 1
    assert len(digest) == 64
