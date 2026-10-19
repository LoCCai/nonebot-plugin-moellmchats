"""Stage a solved math answer before optional plotting tools."""
import re


def solution_first_tools(tools: list, plain: str) -> list:
    names = {tool.get("function", {}).get("name", "") for tool in tools}
    solving = re.search(r"证明|解答|求解|解题|已知|求.*(?:最大|最小|面积|距离|值)|solve|prove", plain, re.I)
    if "render_markdown" not in names or not solving or not any(name.startswith("plot_") for name in names):
        return []
    return [tool for tool in tools if tool.get("function", {}).get("name", "").startswith("plot_")]


SOLUTION_PROMPT = (
    "\n本次数学解答分两阶段。本阶段仅用 Markdown 与 LaTeX 完整解答题目，"
    "包括推导、结论和用户要求的参考说明；不要等待或调用绘图，不要只写计划。"
    "系统会先把你的完整 Markdown 答案渲染并发送，再开放数学绘图工具补充辅助图。"
)
PLOT_PROMPT = (
    "完整解答已交付。现在只需基于上面的推导，按需调用数学工具绘制辅助图。"
    "表达式使用工具支持的数学语法，不传 LaTeX。不要重新排版或重复发送完整解答，"
    "图像只作示意，不能代替证明。最后仅补充简短图示说明；不需要图则直接结束。"
)
