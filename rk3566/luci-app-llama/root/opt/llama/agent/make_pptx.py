#!/usr/bin/env python3
"""生成带排版的 .pptx（Office Open XML），只用 python 标准库。

python-pptx 要 lxml，OpenWrt 装不了；PPTX 本身就是 zip + XML，所以这里直接
拼 OOXML。视觉上做了：

  · 封面页      满屏主色块 + 居中大标题 + 副标题 + 装饰圆
  · 内容页      顶部彩色标题栏（白字）+ 项目符号正文 + 左侧装饰条 + 页码
  · 配色轮换    每页用不同强调色（accent1..6），整本不单调

用法（库）：
    from make_pptx import build_pptx
    build_pptx("out.pptx", [{"title": "标题", "bullets": ["a", "b"]}])

命令行：
    ./make_pptx.py out.pptx "封面标题|副标题" -- "页2标题" "要点a" "要点b"
"""
import sys
import zipfile

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

# 16:9 画布（EMU）
SLIDE_W, SLIDE_H = 12192000, 6858000
MAX_BULLETS = 7

ACCENTS = ["4472C4", "ED7D31", "70AD47", "5B9BD5", "7030A0", "C00000"]
DARK = "1F2A37"          # 正文文字色
GREY = "7F8C9A"          # 页码/次要文字


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _sp_round(sid, name, x, y, cx, cy, fill, radius=8000):
    """圆角矩形（做卡片底）。"""
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/>'
            '</p:nvSpPr><p:spPr><a:xfrm><a:off x="%d" y="%d"/>'
            '<a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="roundRect"><a:avLst>'
            '<a:gd name="adj" fmla="val %d"/></a:avLst></a:prstGeom>'
            '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
            '<a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody>'
            '</p:sp>' % (sid, name, x, y, cx, cy, radius, fill))


def _card_bullets(sid0, x, y, w, h_avail, items, accent):
    """每条要点一张浅灰卡片：左边 accent 竖条 + 序号圆 + 文字。

    比裸项目符号更"成块"，要点少时也不会显得空。
    """
    n = max(1, min(len(items), MAX_BULLETS))
    gap = 130000
    h = min(1050000, int((h_avail - (n - 1) * gap) / n))
    total = n * h + (n - 1) * gap
    top = y + max(0, int((h_avail - total) / 2))
    shapes = []
    for i in range(n):
        cy = top + i * (h + gap)
        # 卡片底
        shapes.append(_sp_round(sid0 + i * 4, "Card%d" % i, x, cy, w, h,
                                "F4F6F9"))
        # 左侧 accent 竖条
        shapes.append(_sp_rect(sid0 + i * 4 + 1, "CardBar%d" % i,
                               x, cy, 70000, h, accent))
        # 序号圆
        d = min(330000, h - 200000)
        shapes.append(_sp_ellipse(sid0 + i * 4 + 2, "Num%d" % i,
                                  x + 220000, cy + int((h - d) / 2), d, d, accent))
        shapes.append(_sp_text(sid0 + i * 4 + 3, "NumTxt%d" % i,
                               x + 220000, cy + int((h - d) / 2), d, d,
                               [("p", [(str(i + 1), 1300, True, "FFFFFF")])],
                               anchor="ctr", align="ctr", space_after=0))
        # 正文
        txt = items[i] if i < len(items) else ""
        shapes.append(_sp_text(sid0 + i * 4 + 3 + 1, "CardTxt%d" % i,
                               x + 620000, cy, w - 760000, h,
                               [("p", [(txt, 1900, False, DARK)])],
                               anchor="ctr", align="l", space_after=0))
    return shapes


def _sp_rect(sid, name, x, y, cx, cy, fill, line=None):
    """一个纯色矩形（用于色块、装饰条）。"""
    ln = ('<a:ln w="12700"><a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:ln>'
          % line) if line else '<a:ln><a:noFill/></a:ln>'
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/>'
            '</p:nvSpPr><p:spPr><a:xfrm><a:off x="%d" y="%d"/>'
            '<a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>%s</p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody>'
            '</p:sp>' % (sid, name, x, y, cx, cy, fill, ln))


def _sp_ellipse(sid, name, x, y, cx, cy, fill, alpha=None):
    a = ('<a:alpha val="%d"/>' % alpha) if alpha else ""
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/>'
            '</p:nvSpPr><p:spPr><a:xfrm><a:off x="%d" y="%d"/>'
            '<a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
            '<a:solidFill><a:srgbClr val="%s">%s</a:srgbClr></a:solidFill>'
            '<a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody>'
            '</p:sp>' % (sid, name, x, y, cx, cy, fill, a))


def _sp_text(sid, name, x, y, cx, cy, runs, anchor="t", align="l",
             space_after=600):
    """一个文本框。runs = [(文字, 字号, 粗体, 颜色)] 或
              [("p", [(run), ...])] 形式的多段落列表。"""
    body = []
    for item in runs:
        # 支持两种写法：
        #   ("文字", 字号, 粗体, 颜色)                    单段落单 run
        #   ("p", [("文字", 字号, 粗体, 颜色), ...])      一个段落里的多个 run
        if (isinstance(item, tuple) and len(item) == 2 and item[0] == "p"
                and isinstance(item[1], list)):
            paras = item[1]
        else:
            paras = [item]
        ppr = ('<a:pPr algn="%s">' % align) + \
              ('<a:spcAft><a:spcPts val="%d"/></a:spcAft>' % space_after) + '</a:pPr>'
        rs = []
        for (txt, sz, bold, color) in paras:
            rs.append('<a:r><a:rPr lang="zh-CN" altLang="en-US" sz="%d"%s>'
                      '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
                      '<a:latin typeface="Microsoft YaHei"/>'
                      '<a:ea typeface="Microsoft YaHei"/></a:rPr>'
                      '<a:t>%s</a:t></a:r>'
                      % (sz, ' b="1"' if bold else "", color, _esc(txt)))
        body.append('<a:p>%s%s</a:p>' % (ppr, "".join(rs)))
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
            '</p:cNvSpPr><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '<a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr wrap="square" lIns="45720" tIns="0" rIns="45720" '
            'bIns="0" anchor="%s"><a:normAutofit/></a:bodyPr><a:lstStyle/>%s</p:txBody>'
            '</p:sp>' % (sid, name, x, y, cx, cy, anchor, "".join(body)))


def _bullets_text(sid, x, y, cx, cy, bullets, accent):
    """带 • 项目符号的要点块（每条一个段落，方形符号用强调色）。"""
    paras = []
    for b in bullets[:MAX_BULLETS]:
        paras.append(
            '<a:p><a:pPr marL="428625" indent="-428625" algn="l">'
            '<a:buClr><a:srgbClr val="%s"/></a:buClr>'
            '<a:buSzPct val="110000"/>'
            '<a:buFont typeface="Arial"/><a:buChar char="&#8226;"/>'
            '<a:spcAft><a:spcPts val="1000"/></a:spcAft></a:pPr>'
            '<a:r><a:rPr lang="zh-CN" altLang="en-US" sz="2200">'
            '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
            '<a:latin typeface="Microsoft YaHei"/>'
            '<a:ea typeface="Microsoft YaHei"/></a:rPr>'
            '<a:t>%s</a:t></a:r></a:p>' % (accent, DARK, _esc(b)))
    if not paras:
        paras.append('<a:p><a:endParaRPr/></a:p>')
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="Bullets"/>'
            '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '<a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr wrap="square" lIns="45720" tIns="45720" '
            'rIns="45720" bIns="45720" anchor="ctr"><a:normAutofit/></a:bodyPr>'
            '<a:lstStyle/>%s</p:txBody></p:sp>' % (sid, x, y, cx, cy, "".join(paras)))


def _cover_slide(title, subtitle):
    """封面：满屏主色 + 装饰圆 + 居中标题。"""
    accent = ACCENTS[0]
    shapes = [
        _sp_rect(2, "Bg", 0, 0, SLIDE_W, SLIDE_H, accent),
        _sp_ellipse(3, "Deco1", SLIDE_W - 2600000, -1200000, 3600000, 3600000,
                    "FFFFFF", alpha=14000),
        _sp_ellipse(4, "Deco2", -900000, SLIDE_H - 1400000, 2600000, 2600000,
                    "FFFFFF", alpha=10000),
        _sp_rect(5, "Rule", 1100000, 3380000, 900000, 36000, "FFFFFF"),
        _sp_text(6, "Title", 1100000, 2100000, 9900000, 1200000,
                 [("p", [(title, 4400, True, "FFFFFF")])], anchor="b", align="l"),
    ]
    if subtitle:
        shapes.append(_sp_text(7, "Sub", 1100000, 3520000, 9900000, 900000,
                               [("p", [(subtitle, 2000, False, "FFFFFF")])],
                               align="l"))
    return _wrap_slide(shapes)


def _content_slide(idx, total, title, bullets, subtitle=None):
    """内容页：彩色标题栏 + 卡片式要点 + 页脚（细线 + 页名 + 页码）。"""
    accent = ACCENTS[idx % len(ACCENTS)]
    bar_h = 1320000
    footer_y = SLIDE_H - 600000
    body_y = bar_h + 240000
    body_h = footer_y - body_y - 200000
    shapes = [
        _sp_rect(2, "TitleBar", 0, 0, SLIDE_W, bar_h, accent),
        _sp_text(4, "Title", 640000, 250000, 9600000, 820000,
                 [("p", [(title, 3200, True, "FFFFFF")])], anchor="ctr", align="l"),
        _sp_rect(5, "SideBar", 0, bar_h, 150000, SLIDE_H - bar_h, accent),
    ]
    shapes += _card_bullets(10, 640000, body_y, SLIDE_W - 1280000, body_h,
                            bullets, accent)
    # 页脚：细线 + 页名 + 页码
    shapes += [
        _sp_rect(90, "FootLine", 640000, footer_y - 90000,
                 SLIDE_W - 1280000, 12700, "DCE3EC"),
        _sp_text(91, "FootTitle", 640000, footer_y, 8000000, 340000,
                 [("p", [(title, 1100, False, GREY)])], align="l", space_after=0),
        _sp_text(92, "PageNo", SLIDE_W - 1600000, footer_y, 1100000, 340000,
                 [("p", [("%d / %d" % (idx + 1, total), 1100, False, GREY)])],
                 align="r", space_after=0),
    ]
    return _wrap_slide(shapes)


def _wrap_slide(shape_xml):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<p:sld xmlns:a="%s" xmlns:r="%s" xmlns:p="%s"><p:cSld><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
            '</p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
            '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
            '</a:xfrm></p:grpSpPr>%s</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
            % (NS_A, NS_R, NS_P, "".join(shape_xml)))


# --------------------------------------------------------------- 固定部件
CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
{extra}
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""

THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="%s" name="Office">
<a:themeElements>
<a:clrScheme name="Office">
<a:dk1><a:sysClr val="windowText" lastClr="1F2A37"/></a:dk1>
<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>
<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>
<a:accent3><a:srgbClr val="70AD47"/></a:accent3><a:accent4><a:srgbClr val="5B9BD5"/></a:accent4>
<a:accent5><a:srgbClr val="7030A0"/></a:accent5><a:accent6><a:srgbClr val="C00000"/></a:accent6>
<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
</a:clrScheme>
<a:fontScheme name="Office">
<a:majorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:majorFont>
<a:minorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:minorFont>
</a:fontScheme>
<a:fmtScheme name="Office">
<a:fillStyleLst>
<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
<a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"><a:lumMod val="110000"/><a:satMod val="105000"/><a:tint val="67000"/></a:schemeClr></a:gs><a:gs pos="50000"><a:schemeClr val="phClr"><a:lumMod val="105000"/><a:satMod val="103000"/><a:tint val="73000"/></a:schemeClr></a:gs><a:gs pos="100000"><a:schemeClr val="phClr"><a:lumMod val="105000"/><a:satMod val="109000"/><a:tint val="81000"/></a:schemeClr></a:gs></a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill>
<a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"><a:satMod val="103000"/><a:lumMod val="102000"/><a:tint val="94000"/></a:schemeClr></a:gs><a:gs pos="50000"><a:schemeClr val="phClr"><a:satMod val="110000"/><a:lumMod val="100000"/><a:shade val="100000"/></a:schemeClr></a:gs><a:gs pos="100000"><a:schemeClr val="phClr"><a:lumMod val="99000"/><a:satMod val="120000"/><a:shade val="78000"/></a:schemeClr></a:gs></a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill>
</a:fillStyleLst>
<a:lnStyleLst>
<a:ln w="6350" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>
<a:ln w="12700" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>
<a:ln w="19050" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>
</a:lnStyleLst>
<a:effectStyleLst>
<a:effectStyle><a:effectLst/></a:effectStyle>
<a:effectStyle><a:effectLst/></a:effectStyle>
<a:effectStyle><a:effectLst/></a:effectStyle>
</a:effectStyleLst>
<a:bgFillStyleLst>
<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
<a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/><a:satMod val="170000"/></a:schemeClr></a:solidFill>
<a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"><a:tint val="93000"/><a:satMod val="150000"/><a:shade val="98000"/><a:lumMod val="102000"/></a:schemeClr></a:gs><a:gs pos="50000"><a:schemeClr val="phClr"><a:tint val="98000"/><a:satMod val="130000"/><a:shade val="90000"/><a:lumMod val="103000"/></a:schemeClr></a:gs><a:gs pos="100000"><a:schemeClr val="phClr"><a:shade val="63000"/><a:satMod val="120000"/></a:schemeClr></a:gs></a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill>
</a:bgFillStyleLst>
</a:fmtScheme>
</a:themeElements>
</a:theme>""" % NS_A

MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">
<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>
<p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
<p:txStyles><p:titleStyle><a:lvl1pPr algn="l"><a:defRPr sz="3200" b="1"/></a:lvl1pPr></p:titleStyle><p:bodyStyle><a:lvl1pPr marL="0" indent="0"><a:defRPr sz="2200"/></a:lvl1pPr></p:bodyStyle><p:otherStyle><a:defRPr sz="1800"/></p:otherStyle></p:txStyles>
</p:sldMaster>""" % (NS_A, NS_R, NS_P)

MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""

LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="%s" xmlns:r="%s" xmlns:p="%s" type="blank" preserve="1">
<p:cSld name="Blank"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>""" % (NS_A, NS_R, NS_P)

LAYOUT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""


def _slide_rels():
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/></Relationships>')


def build_pptx(path, slides):
    """slides: [{"title": str, "subtitle": str(可选), "bullets": [str, ...]}, ...]
    第 1 页自动渲染成封面（满屏色块），其余是内容页（彩色标题栏 + 要点）。"""
    slides = slides or [{"title": "封面", "bullets": []}]
    n = len(slides)

    extra = "\n".join(
        '<Override PartName="/ppt/slides/slide%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        % (i + 1) for i in range(n))

    pres = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<p:presentation xmlns:a="%s" xmlns:r="%s" xmlns:p="%s" saveSubsetFonts="1">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst>%s</p:sldIdLst><p:sldSz cx="%d" cy="%d"/>'
            '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
            % (NS_A, NS_R, NS_P,
               "".join('<p:sldId id="%d" r:id="rId%d"/>' % (256 + i, i + 2)
                       for i in range(n)), SLIDE_W, SLIDE_H))

    pres_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
        'Target="slideMasters/slideMaster1.xml"/>%s</Relationships>'
        % "".join(
            '<Relationship Id="rId%d" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            'Target="slides/slide%d.xml"/>' % (i + 2, i + 1) for i in range(n)))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES.format(extra=extra))
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("ppt/presentation.xml", pres)
        z.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        z.writestr("ppt/theme/theme1.xml", THEME)
        z.writestr("ppt/slideMasters/slideMaster1.xml", MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", MASTER_RELS)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", LAYOUT_RELS)
        for i, s in enumerate(slides):
            title = s.get("title", "")
            bullets = s.get("bullets") or []
            sub = s.get("subtitle") or ""
            if i == 0:
                xml = _cover_slide(title, sub or (bullets[0] if bullets else ""))
            else:
                xml = _content_slide(i, n, title, bullets, sub or None)
            z.writestr("ppt/slides/slide%d.xml" % (i + 1), xml)
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % (i + 1), _slide_rels())
    return path


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    out = args[0]
    groups, cur = [], None
    for a in args[1:]:
        if a == "--":
            cur = None
            continue
        if cur is None:
            title, _, sub = a.partition("|")
            cur = {"title": title, "subtitle": sub, "bullets": []}
            groups.append(cur)
        else:
            cur["bullets"].append(a)
    build_pptx(out, groups)
    print("wrote %s (%d slides)" % (out, len(groups)))
