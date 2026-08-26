import traceback

from nonebot.log import logger

from .config import config_parser
from .tool_manager import SearchExtractorView, ToolSnapshot, tool_manager
from .utils import get_session


class Search:
    def __init__(
        self,
        plain,
        tool_snapshot=None,
        *,
        is_superuser: bool = False,
    ):
        if type(is_superuser) is not bool:
            raise TypeError("Search is_superuser 必须是布尔值")
        self.plain = plain
        self.tool_snapshot = tool_snapshot
        self.is_superuser = is_superuser

    def _has_selectable_extractor(self) -> bool:
        snapshot = self.tool_snapshot
        if isinstance(snapshot, ToolSnapshot):
            view = snapshot.resolve_search_extractor(
                is_superuser=self.is_superuser,
            )
            if view is None:
                return False
            if not isinstance(view, SearchExtractorView):
                raise TypeError("Search extractor view 非法")
            if not view.provider_authoritative:
                # The rollback switch intentionally preserves the historical
                # membership-only behavior, including its permission and
                # blacklist semantics.
                return True
            decision = view.trust_decision
            assert decision is not None
            return decision.allowed and not tool_manager.is_tool_blacklisted(
                view.tool_name
            )

        # Bootstrap and old integrations retain the bounded legacy membership
        # check until a complete transaction snapshot is available.
        tools = (
            snapshot.custom_tools
            if snapshot is not None
            else tool_manager.custom_tools
        )
        return "extract_webpage" in tools

    async def get_search(self) -> str:
        url = "https://api.tavily.com/search"
        headers = {
            "Content-Type": "application/json",
            "Authorization": config_parser.get_config("search_api"),
            "Accept-Encoding": "identity",
        }
        data = {
            "query": self.plain,
            "include_answer": True,
        }

        try:
            has_extractor = self._has_selectable_extractor()
            async with get_session().post(
                url, headers=headers, json=data, ssl=False
            ) as response:
                response_data = await response.json()

                answer = response_data.get("answer", "")
                results = response_data.get("results", [])

                if answer or results:
                    final_res = answer

                    # 只有在有搜索结果且用户安装了提取工具的情况下，才暴露 URL 和诱导提示词
                    if results and has_extractor:
                        source_list = [
                            f"- {result.get('title', '未知')}: {result.get('url', '')}"
                            for result in results[:3]
                            if result.get("url")
                        ]
                        if source_list:
                            final_res += (
                                "\n\n参考来源URL(需要详情时可调用 extract_webpage)：\n"
                                + "\n".join(source_list)
                            )
                    elif results and not has_extractor:
                        # 如果没有安装提取工具，但你想让大模型知道有这些来源，可以仅暴露标题不诱导调用工具
                        source_titles = [
                            f"- {result.get('title', '未知')}"
                            for result in results[:3]
                            if result.get("title")
                        ]
                        if source_titles:
                            final_res += "\n\n参考来源：\n" + "\n".join(source_titles)

                    return final_res if final_res else "搜索成功，但未返回直接摘要。"
                else:
                    return False  # 没有相关内容
        except Exception:
            logger.warning(traceback.format_exc())
            return None  # 错误
