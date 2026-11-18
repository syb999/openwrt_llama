#!/usr/bin/env node
/**
 * 用 pptxgenjs 生成带排版的 PPTX —— 比手搓 OOXML 多了图表、图片、表格能力。
 *
 * 支持的页面类型（slides 数组里每项一个 type）：
 *   { type:"cover",  title, subtitle }                       满屏色块封面
 *   { type:"bullets",title, bullets:[...], accent? }          标题栏 + 卡片要点
 *   { type:"chart",  title, chartType:"bar"|"pie"|"line"|"doughnut",
 *                    labels:[...], values:[...], accent? }    标题栏 + 图表
 *   { type:"table",  title, headers:[...], rows:[[...]], accent? }
 *
 * 命令行：
 *   ./pptx.js out.pptx '{"slides":[{"type":"cover","title":"标题"}]}'
 *   ./pptx.js out.pptx @spec.json
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ACCENTS = ['4472C4', 'ED7D31', '70AD47', '5B9BD5', '7030A0', 'C00000'];
const FONT = 'Microsoft YaHei';
const GREY = '7F8C9A';
const DARK = '1F2A37';

/* 主题：每次生成随机挑一套，配色/背景/文字色整体换 ——
   之前所有 PPT 都是同一套蓝色，看着就是"一个模板"。 */
const THEMES = [
  { name: '商务深蓝', accents: ['2F5597', '4472C4', '5B9BD5', '8FAADC'],
    bg: 'FFFFFF', text: '1F2A37', soft: 'F2F5FA', muted: '7F8C9A' },
  { name: '科技青', accents: ['0E7C86', '17A2B8', '38BDF8', '7DD3FC'],
    bg: 'FFFFFF', text: '12333A', soft: 'EAF6F7', muted: '6B8A90' },
  { name: '暖橙', accents: ['C55A11', 'ED7D31', 'F59E0B', 'FCD34D'],
    bg: 'FFFCF7', text: '33241A', soft: 'FDEFE3', muted: '8A7364' },
  { name: '墨紫', accents: ['5B2C83', '9B59B6', 'A78BFA', 'DDD6FE'],
    bg: 'FFFFFF', text: '2A1B36', soft: 'F4EDF9', muted: '82709A' },
  { name: '暗夜', accents: ['14B8A6', '2DD4BF', '22D3EE', '67E8F9'],
    bg: '111827', text: 'F8FAFC', soft: '1F2937', muted: '94A3B8' },
];
let THEME = THEMES[0];
function pickTheme(seed) {
  const n = THEMES.length;
  const k = (seed == null) ? Math.floor(Math.random() * n)
                           : (Math.abs(seed) % n);
  return THEMES[k];
}
function accentAt(i) {
  return THEME.accents[i % THEME.accents.length];
}

function loadPptx() {
  try {
    return require('pptxgenjs');
  } catch (e) {
    // 允许从脚本同目录的 node_modules 找
    const p = path.join(__dirname, 'node_modules', 'pptxgenjs');
    return require(p);
  }
}

function addTitleBar(slide, pptx, title, accent, idx, total) {
  slide.addShape(pptx.ShapeType.rect, {
    x: 0, y: 0, w: 10, h: 1.15, fill: { color: accent }, line: { color: accent },
  });
  slide.addShape(pptx.ShapeType.rect, {
    x: 0, y: 1.15, w: 0.09, h: 4.475, fill: { color: accent }, line: { color: accent },
  });
  slide.addText(title || '', {
    x: 0.45, y: 0.16, w: 9.0, h: 0.84,
    fontSize: 26, bold: true, color: 'FFFFFF', fontFace: FONT,
    valign: 'middle',
  });
  // 页脚
  slide.addShape(pptx.ShapeType.line, {
    x: 0.45, y: 5.18, w: 9.1, h: 0,
    line: { color: 'DCE3EC', width: 1 },
  });
  slide.addText(title || '', {
    x: 0.45, y: 5.24, w: 6.5, h: 0.3,
    fontSize: 9, color: GREY, fontFace: FONT,
  });
  slide.addText(`${idx} / ${total}`, {
    x: 8.2, y: 5.24, w: 1.35, h: 0.3,
    fontSize: 9, color: GREY, fontFace: FONT, align: 'right',
  });
}

function slideCover(pptx, spec) {
  const s = pptx.addSlide();
  const accent = spec.accent || accentAt(0);
  s.background = { color: accent };
  // 装饰圆
  s.addShape(pptx.ShapeType.ellipse, {
    x: 7.6, y: -1.3, w: 3.6, h: 3.6,
    fill: { color: 'FFFFFF', transparency: 86 }, line: { color: 'FFFFFF', transparency: 100 },
  });
  s.addShape(pptx.ShapeType.ellipse, {
    x: -1.0, y: 4.2, w: 2.8, h: 2.8,
    fill: { color: 'FFFFFF', transparency: 88 }, line: { color: 'FFFFFF', transparency: 100 },
  });
  s.addShape(pptx.ShapeType.rect, {
    x: 0.9, y: 2.72, w: 0.85, h: 0.05, fill: { color: 'FFFFFF' }, line: { color: 'FFFFFF' },
  });
  s.addText(spec.title || '', {
    x: 0.9, y: 1.55, w: 8.2, h: 1.1,
    fontSize: 34, bold: true, color: 'FFFFFF', fontFace: FONT, valign: 'bottom',
  });
  if (spec.subtitle) {
    s.addText(spec.subtitle, {
      x: 0.9, y: 2.95, w: 8.2, h: 0.6,
      fontSize: 15, color: 'FFFFFF', fontFace: FONT,
    });
  }
  return s;
}

function slideBullets(pptx, spec, idx, total) {
  const s = pptx.addSlide();
  const accent = spec.accent || accentAt(idx);
  addTitleBar(s, pptx, spec.title, accent, idx, total);

  const items = (spec.bullets || []).slice(0, 7);
  const n = Math.max(items.length, 1);
  const areaTop = 1.45, areaH = 3.55;
  const gap = 0.12;
  const h = Math.min(0.82, (areaH - (n - 1) * gap) / n);
  const totalH = n * h + (n - 1) * gap;
  const top = areaTop + Math.max(0, (areaH - totalH) / 2);

  for (let i = 0; i < n; i++) {
    const y = top + i * (h + gap);
    s.addShape(pptx.ShapeType.roundRect, {
      x: 0.45, y: y, w: 9.1, h: h,
      fill: { color: 'F4F6F9' }, line: { color: 'F4F6F9' }, rectRadius: 0.08,
    });
    s.addShape(pptx.ShapeType.rect, {
      x: 0.45, y: y, w: 0.06, h: h, fill: { color: accent }, line: { color: accent },
    });
    const d = Math.min(0.34, h - 0.16);
    s.addShape(pptx.ShapeType.ellipse, {
      x: 0.62, y: y + (h - d) / 2, w: d, h: d,
      fill: { color: accent }, line: { color: accent },
    });
    s.addText(String(i + 1), {
      x: 0.62, y: y + (h - d) / 2, w: d, h: d,
      fontSize: 10, bold: true, color: 'FFFFFF', fontFace: FONT,
      align: 'center', valign: 'middle',
    });
    s.addText(items[i] || '', {
      x: 1.12, y: y, w: 8.2, h: h,
      fontSize: 14, color: DARK, fontFace: FONT, valign: 'middle',
    });
  }
  return s;
}

function slideChart(pptx, spec, idx, total) {
  const s = pptx.addSlide();
  const accent = spec.accent || accentAt(idx);
  addTitleBar(s, pptx, spec.title, accent, idx, total);

  const types = {
    bar: pptx.ChartType.bar,
    pie: pptx.ChartType.pie,
    line: pptx.ChartType.line,
    doughnut: pptx.ChartType.doughnut,
    area: pptx.ChartType.area,
  };
  const ct = types[(spec.chartType || 'bar').toLowerCase()] || pptx.ChartType.bar;
  const labels = spec.labels || [];
  const values = (spec.values || []).map(Number);

  s.addChart(
    ct,
    [{ name: spec.seriesName || '数据', labels: labels, values: values }],
    {
      x: 0.6, y: 1.45, w: 8.8, h: 3.6,
      // 单系列统一用强调色，混色显得乱
      chartColors: [accent],
      showLegend: ct === pptx.ChartType.pie || ct === pptx.ChartType.doughnut,
      legendPos: 'b',
      showValue: ct !== pptx.ChartType.line,
      dataLabelFontSize: 10,
      dataLabelColor: '444444',
      // 保留一位小数，否则 3.7 会显示成 4、1.8 显示成 2
      dataLabelFormatCode: '0.0',
      catAxisLabelFontSize: 11,
      catAxisLabelColor: DARK,
      valAxisLabelFontSize: 10,
      valAxisLabelColor: GREY,
      barDir: 'col',            // 竖向柱
      barGapWidthPct: 80,
      lineSize: 2.5,
    }
  );
  return s;
}

function slideTable(pptx, spec, idx, total) {
  const s = pptx.addSlide();
  const accent = spec.accent || accentAt(idx);
  addTitleBar(s, pptx, spec.title, accent, idx, total);

  const headers = (spec.headers || []).map((h) => ({
    text: String(h),
    options: { bold: true, color: 'FFFFFF', fill: { color: accent }, fontSize: 12 },
  }));
  const rows = (spec.rows || []).map((r, i) =>
    r.map((c) => ({
      text: String(c),
      options: { fontSize: 11, color: DARK, fill: { color: i % 2 ? 'FFFFFF' : 'F4F6F9' } },
    }))
  );
  s.addTable([headers, ...rows], {
    x: 0.5, y: 1.5, w: 9.0,
    border: { type: 'solid', color: 'DCE3EC', pt: 1 },
    fontFace: FONT,
    valign: 'middle',
    rowH: 0.38,
  });
  return s;
}

/* ---- 新增版式：章节页 / 两栏对比 / 金句 / 结尾 ---- */

/* 章节页：整页色块 + 大号序号 + 标题 */
function slideSection(pptx, sp, idx, total) {
  const s = pptx.addSlide();
  s.background = { color: THEME.accents[0] };
  const num = sp.number != null ? String(sp.number)
                                : String(idx + 1).padStart(2, '0');
  s.addText(num, {
    x: 0.7, y: 1.35, w: 3.2, h: 1.7, fontSize: 84, bold: true,
    color: 'FFFFFF', transparency: 42, fontFace: FONT,
  });
  s.addText(sp.title || '章节', {
    x: 0.7, y: 3.05, w: 8.6, h: 1.1, fontSize: 34, bold: true,
    color: 'FFFFFF', fontFace: FONT,
  });
  if (sp.subtitle) {
    s.addText(sp.subtitle, {
      x: 0.7, y: 4.1, w: 8.6, h: 0.8, fontSize: 15, color: 'FFFFFF',
      transparency: 22, fontFace: FONT,
    });
  }
  return s;
}

/* 两栏对比：左/右各一张卡片 */
function slideTwoCol(pptx, sp, idx, total) {
  const s = pptx.addSlide();
  s.background = { color: THEME.bg };
  addTitleBar(s, pptx, sp.title || '对比', accentAt(idx), idx, total);
  const left = Array.isArray(sp.left) ? sp.left
    : (Array.isArray(sp.bullets) ? sp.bullets.slice(0, Math.ceil(sp.bullets.length / 2)) : []);
  const right = Array.isArray(sp.right) ? sp.right
    : (Array.isArray(sp.bullets) ? sp.bullets.slice(Math.ceil(sp.bullets.length / 2)) : []);
  const cols = [
    { x: 0.55, items: left, color: accentAt(idx), label: sp.leftTitle || 'A' },
    { x: 5.15, items: right, color: accentAt(idx + 1), label: sp.rightTitle || 'B' },
  ];
  cols.forEach((col) => {
    s.addShape(pptx.ShapeType.roundRect, {
      x: col.x, y: 1.55, w: 4.3, h: 4.35, fill: { color: THEME.soft },
      line: { color: THEME.soft }, rectRadius: 0.06,
    });
    s.addText(col.label, {
      x: col.x + 0.28, y: 1.75, w: 3.74, h: 0.5, fontSize: 17, bold: true,
      color: col.color, fontFace: FONT,
    });
    s.addText(col.items.length
      ? col.items.map((t) => ({ text: String(t), options: { bullet: true } }))
      : [{ text: '（空）', options: {} }], {
      x: col.x + 0.28, y: 2.35, w: 3.74, h: 3.25, fontSize: 14,
      color: THEME.text, fontFace: FONT, valign: 'top',
      lineSpacingMultiple: 1.32,
    });
  });
  return s;
}

/* 金句页：大引号 + 居中大字 + 署名 */
function slideQuote(pptx, sp) {
  const s = pptx.addSlide();
  s.background = { color: THEME.soft };
  s.addText('“', {
    x: 0.55, y: 0.35, w: 1.8, h: 1.7, fontSize: 120, bold: true,
    color: accentAt(0), fontFace: 'Georgia',
  });
  s.addText(sp.quote || sp.title || '—', {
    x: 1.1, y: 1.9, w: 7.8, h: 2.4, fontSize: 25, bold: true,
    color: THEME.text, fontFace: FONT, align: 'center', valign: 'middle',
    lineSpacingMultiple: 1.35,
  });
  if (sp.by || sp.subtitle) {
    s.addText('—— ' + (sp.by || sp.subtitle), {
      x: 1.1, y: 4.35, w: 7.8, h: 0.6, fontSize: 15, color: THEME.muted,
      fontFace: FONT, align: 'center',
    });
  }
  return s;
}

/* 结尾页：居中大字 + 副标题 + 短线 */
function slideClosing(pptx, sp) {
  const s = pptx.addSlide();
  s.background = { color: THEME.accents[0] };
  s.addText(sp.title || '谢谢', {
    x: 0.8, y: 2.25, w: 8.4, h: 1.4, fontSize: 44, bold: true,
    color: 'FFFFFF', fontFace: FONT, align: 'center',
  });
  if (sp.subtitle) {
    s.addText(sp.subtitle, {
      x: 0.8, y: 3.65, w: 8.4, h: 0.8, fontSize: 16, color: 'FFFFFF',
      transparency: 22, fontFace: FONT, align: 'center',
    });
  }
  s.addShape(pptx.ShapeType.rect, {
    x: 4.2, y: 4.55, w: 1.6, h: 0.06,
    fill: { color: 'FFFFFF' }, line: { color: 'FFFFFF' },
  });
  return s;
}

function buildPptx(outPath, spec) {
  const PptxGenJS = loadPptx();
  const pptx = new PptxGenJS();
  pptx.layout = 'LAYOUT_16x9';
  pptx.author = 'llama-agent';
  pptx.title = (spec.title || 'Presentazione');

  // 主题：默认每次随机（同一份内容连做两次也是不同配色），
  // 也可以用 spec.theme（序号或主题名）指定。
  if (typeof spec.theme === 'number') {
    THEME = THEMES[Math.abs(spec.theme) % THEMES.length];
  } else if (typeof spec.theme === 'string' && spec.theme) {
    const k = THEMES.findIndex((t) => t.name === spec.theme);
    THEME = THEMES[k >= 0 ? k : Math.floor(Math.random() * THEMES.length)];
  } else {
    THEME = pickTheme(spec.theme_seed);
  }

  const slides = (spec.slides && spec.slides.length) ? spec.slides
    : [{ type: 'cover', title: spec.title || '封面' }];
  const total = slides.length;

  slides.forEach((sp, i) => {
    const type = (sp.type || (i === 0 ? 'cover' : 'bullets')).toLowerCase();
    try {
      if (type === 'cover') slideCover(pptx, sp);
      else if (type === 'section') slideSection(pptx, sp, i, total);
      else if (type === 'two-col' || type === 'twocol' || type === 'two'
               || type === 'twocolumn') slideTwoCol(pptx, sp, i, total);
      else if (type === 'quote') slideQuote(pptx, sp);
      else if (type === 'closing' || type === 'end' || type === 'thanks') slideClosing(pptx, sp);
      else if (type === 'chart') slideChart(pptx, sp, i, total);
      else if (type === 'table') slideTable(pptx, sp, i, total);
      else slideBullets(pptx, sp, i, total);
    } catch (e) {
      // 单页失败不要毁掉整本
      const s = pptx.addSlide();
      addTitleBar(s, pptx, sp.title || ('第 ' + (i + 1) + ' 页'), accentAt(i), i, total);
      s.addText('（这一页生成失败：' + e.message + '）', {
        x: 0.6, y: 2.2, w: 8.8, h: 0.8, fontSize: 13, color: 'C00000', fontFace: FONT,
      });
    }
  });

  return pptx.writeFile({ fileName: outPath }).then(() => outPath);
}

// ------------------------------------------------------------------ CLI
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length < 2) {
    console.log('用法: ./pptx.js out.pptx \'{"slides":[...]}\'  或  ./pptx.js out.pptx @spec.json');
    process.exit(1);
  }
  const out = args[0];
  let raw = args[1];
  if (raw.startsWith('@')) raw = fs.readFileSync(raw.slice(1), 'utf8');
  let spec;
  try {
    spec = JSON.parse(raw);
  } catch (e) {
    console.error('spec 不是合法 JSON: ' + e.message);
    process.exit(1);
  }
  buildPptx(out, spec)
    .then((p) => console.log('wrote ' + p + ' (' + (spec.slides || []).length + ' slides)'))
    .catch((e) => { console.error('失败: ' + e.message); process.exit(1); });
}

module.exports = { buildPptx };
