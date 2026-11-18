"""把文档转成纯文本，给纯文本模型读。

只用 python3 / pip 包（OpenWrt 上没有 pandoc、libreoffice、poppler）：
  PDF   pypdf
  DOCX  python-docx
  XLSX  openpyxl
  PPTX  python-pptx
  文本  .txt/.md/.csv/.json/.py/... 直接读

不支持 .doc（老二进制格式，没有纯 python 解析器）→ 提示另存为 .docx。
"""

import os

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
            ".py", ".js", ".c", ".h", ".sh", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".ini", ".conf", ".sql", ".rs", ".go", ".java"}

SUPPORTED = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}

# 转换后最多保留多少字。40 万字的 PDF 全塞给 3B 会直接撑爆 ctx(4096 token)。
MAX_CHARS = 120000


def supported(filename):
    return os.path.splitext(str(filename or ""))[1].lower() in SUPPORTED


def extract(path, max_chars=MAX_CHARS):
    """返回 (text, info)。失败抛 ValueError，消息直接给用户看。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        info = "文本文件"
    elif ext == ".pdf":
        text, info = _pdf(path)
    elif ext == ".docx":
        text, info = _docx(path)
    elif ext in (".xlsx", ".xlsm"):
        text, info = _xlsx(path)
    elif ext == ".pptx":
        text, info = _pptx(path)
    elif ext == ".doc":
        raise ValueError(".doc 是老二进制格式，本机没有解析器。"
                         "请用 Word / WPS 另存为 .docx 再上传。")
    else:
        raise ValueError("不支持 %s。支持：PDF / DOCX / XLSX / PPTX / 纯文本"
                         % (ext or "无扩展名"))

    text = (text or "").strip()
    if not text:
        raise ValueError("文件里没提取到文字（可能是扫描件或纯图片）。"
                         "扫描版 PDF 需要 OCR，当前板子上没有 OCR 引擎。")
    if len(text) > max_chars:
        text = (text[:max_chars] +
                "\n\n…（原文 %d 字，已截断到 %d 字）" % (len(text), max_chars))
    return text, info


def _pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:                                      # noqa: BLE001
            t = ""
        if t.strip():
            pages.append("── 第 %d 页 ──\n%s" % (i + 1, t.strip()))
    return "\n\n".join(pages), "PDF，共 %d 页" % len(reader.pages)


def _docx(path):
    import docx
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for ti, table in enumerate(d.tables):
        rows = []
        for row in table.rows:
            rows.append(" | ".join(c.text.strip().replace("|", "｜")
                                   for c in row.cells))
        if rows:
            parts.append("── 表 %d ──\n%s" % (ti + 1, "\n".join(rows)))
    n_para = len([p for p in d.paragraphs if p.text.strip()])
    return "\n".join(parts), "DOCX，%d 段 %d 表" % (n_para, len(d.tables))


def _xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    names = list(wb.sheetnames)
    # 跨工作表去重：很多表的"汇总""首页推荐"是同一批数据的另一种排序，
    # 全拼上去就变成每条数据出现 2-3 次（用户看到就是"忽悠人"）。
    seen = set()
    dup = 0
    for si, ws in enumerate(wb.worksheets):
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            # 原来限制 200 行，300 行的表直接被腰斩 —— 实测 BOTTOM10 全错
            # （真正的最小值在第 301 行，压根没进文本）。
            # 放宽到 2000 行；超长文件靠 MAX_CHARS 兜底截断。
            if i >= 2000:
                rows.append("…（超过 2000 行，其余省略）")
                break
            if row is None:
                continue
            cells = ["" if c is None else str(c) for c in row]
            if not any(c.strip() for c in cells):
                continue
            # 单元格内容里的半角竖线会和列分隔符冲突（"《篡改正史 | xx》"
            # 会被切成两列，名称丢一半）→ 统一换成全角
            cells = [c.replace("|", "｜") for c in cells]
            key = " | ".join(cells)
            if key in seen:
                dup += 1
                continue
            seen.add(key)
            rows.append(key)
        if rows:
            # 第一个工作表不加 "── 工作表 ──" 标题行：
            # 原文件第一行就是表头，插个标题会把表头顶到第二行，
            # 下游按"第一行=表头"解析就全错位（用户直接问"为什么读出来的不是表头"）。
            if si == 0:
                parts.append("\n".join(rows))
            else:
                parts.append("── 工作表 %s ──\n%s" % (ws.title, "\n".join(rows)))
    wb.close()
    info = "XLSX，%d 个工作表" % len(names)
    if dup:
        info += "（跨表重复 %d 行已省略）" % dup
    return "\n\n".join(parts), info


def _pptx(path):
    from pptx import Presentation
    pr = Presentation(path)
    parts = []
    for i, slide in enumerate(pr.slides):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and str(shape.text).strip():
                texts.append(str(shape.text).strip())
        if texts:
            parts.append("── 第 %d 页 ──\n%s" % (i + 1, "\n".join(texts)))
    return "\n\n".join(parts), "PPTX，共 %d 页" % len(pr.slides)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        t, info = extract(p)
        print("=== %s（%s，%d 字）===" % (p, info, len(t)))
        print(t[:500])
