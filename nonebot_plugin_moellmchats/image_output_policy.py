"""Guidance shared by classification and the answering model's tool view."""

DOCUMENT_IMAGE_GUIDANCE = (
    "攻略图、资料卡、表格、清单、流程图、思维导图、代码截图等以文字和数据为主体的图片，"
    "优先整理准确内容，再用 render_markdown 或 render_html 排版成图；这些工具面向普通用户，"
    "无需绘图授权。思维导图和流程图可用自包含 HTML/CSS/内联 SVG 绘制，代码可用 Markdown 代码块；"
    "不依赖在线脚本或外部绘图库。数据查询与排版可连续调用各自工具。"
    "用户使用‘画、绘制、生成图片’等措辞不代表必须调用生图模型。"
    "只有调用 generate_image 或图像/视频/3D 生成模型时才核验相应手动授权；"
    "群管理员身份和绘图手动授权不能互相替代，以工具核验结果为准。"
    "生成模型拒绝授权时，仍可继续完成文档排版类需求。只调用当前工具列表中实际提供的工具，"
    "渲染工具不可用时如实说明并提供整理好的文字，不要虚构‘只有管理员才能画攻略图’。"
)


def image_output_hint(tool_names: set[str]) -> str:
    if not tool_names.intersection({"render_html", "render_markdown", "generate_image", "call_model_capability"}):
        return ""
    available = sorted(tool_names.intersection({"get_render_theme", "render_html", "render_markdown"}))
    return "\n图片输出工具选择：" + DOCUMENT_IMAGE_GUIDANCE + "本轮可用排版工具：" + ("、".join(available) or "无") + "。"
