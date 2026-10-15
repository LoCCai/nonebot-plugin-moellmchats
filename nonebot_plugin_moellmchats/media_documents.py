"""Extract document contents locally before involving a model."""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
from pathlib import PurePath
import sys
import xml.etree.ElementTree as ET
import zipfile

from .media_context import MediaAsset, MediaError

_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".css", ".js", ".ts",
    ".py", ".rs", ".go", ".java", ".c", ".cpp", ".sql", ".log", ".yaml", ".yml", ".toml", ".rtf",
})
_PDF_WORKER = r"""
import base64, io, json, resource, sys
resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
from pypdf import PdfReader
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read(20 * 1024 * 1024 + 1)))
if reader.is_encrypted and not reader.decrypt(""):
    raise ValueError("encrypted")
text, images, warnings = [], [], []
budget = 120000
for index, page in enumerate(reader.pages[:20]):
    current = page.extract_text() or ""
    text.append("[第%d页]\n%s" % (index + 1, current[:budget]))
    budget -= min(budget, len(current))
    if not current.strip():
        added = 0
        for image in page.images:
            if len(images) >= 6:
                break
            if len(image.data) <= 2 * 1024 * 1024:
                images.append(base64.b64encode(image.data).decode("ascii"))
                added += 1
        if not added:
            warnings.append("第%d页未提取到文字或可读图片。" % (index + 1))
    if budget <= 0:
        warnings.append("文档文字超出12万字读取上限。")
        break
if len(reader.pages) > 20:
    warnings.append("文档超过20页，本次只读取前20页。")
print(json.dumps({"text":"\n".join(text), "images":images, "warnings":warnings}))
"""


async def extract_document(asset: MediaAsset) -> tuple[str, list[bytes], list[str]]:
    data = asset.payload or b""
    suffix = PurePath(asset.name).suffix.lower()
    if data.startswith(b"%PDF-") or suffix == ".pdf":
        try:
            import pypdf  # noqa: F401 — optional media extra; isolate parsing in a subprocess
        except ImportError:
            raise MediaError("PDF 读取需要安装 moellmchats 的 media 可选依赖。") from None
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", _PDF_WORKER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            result, _ = await asyncio.wait_for(process.communicate(data), 18)
            if process.returncode or len(result) > 20 * 1024 * 1024:
                raise MediaError("PDF 读取失败，可能已加密、损坏或超过处理上限。")
            parsed = json.loads(result)
            return parsed["text"], [base64.b64decode(image) for image in parsed["images"]], parsed["warnings"]
        except asyncio.TimeoutError:
            raise MediaError("PDF 读取超时，已停止处理。") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 8 * 1024 * 1024:
                    raise MediaError("文档解压后过大。")
                raw = archive.read(info)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise MediaError("文档包含不支持的 XML 声明。")
            root = ET.fromstring(raw)
            text = "\n".join("".join(node.itertext()) for node in root.iter()
                             if node.tag.endswith("}p"))
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            raise MediaError("Word 文档无法读取。") from None
    elif suffix in _TEXT_EXTENSIONS or asset.mime.startswith("text/"):
        text = None
        encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        for encoding in encodings:
            try:
                text = data.decode(encoding)
                break
            except UnicodeError:
                continue
        if text is None or "\x00" in text[:1000]:
            raise MediaError("文件不是可识别的文本编码。")
    else:
        raise MediaError("暂不支持该文件类型；支持 PDF、DOCX、文本和代码文件。")
    warnings = ["文档超出12万字，本次已截断。"] if len(text) > 120_000 else []
    return text[:120_000], [], warnings
