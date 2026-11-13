"""公式保真模块（v1.3 新增）

需求：答案中出现的公式必须与文档里的一模一样。

背景（实测 2026-11-12）：PDF 的**文本层本身会把公式打散**。例如实验书
PDF 第 115 页式(6-13) 的文本层是 "waer =A·τ+k(6-13)"，而原版版面是
"m_water / A = A_cap · √τ + k"。因此「让模型把公式重打一遍」无论怎么调
Prompt 都不可能逐字一致，翻译模型同样会改写符号。

本模块用三层保证替代「模型复述公式」：
  1. 抽取层 page_formulas()：用 pypdfium2 的字符坐标定位公式行，
     从原版 PDF 裁切页面图片（像素级一致，含上下标/分数线/编号），
     同时保留 PDF 文本层的逐字字符串（仅供复制与检索，不作为版式依据）。
  2. 链路层 segments() / inventory()：翻译时公式片段原样跳过；
     生成时模型只允许使用 〔公式N〕 引用，禁止自行书写任何公式。
  3. 校验层 verify_answer()：生成结果中任何公式样文本若不是原文逐字副本，
     一律替换为 〔公式N〕 或删除，绝不把模型自造公式交给用户。

对外主要接口：
  cards_for_refs(con, refs)  -> 公式卡片清单（带 marker / image / text）
  segments(text)            -> [(kind, text)]，供 bilingual.translate 跳过公式
  inventory(cards)          -> 注入 Prompt 的「可用原文公式清单」
  verify_answer(ans, cards) -> (修复后的答案, 修复记录)
  cards_html(cards, used)   -> 答案页的原文公式卡片 HTML
"""

from __future__ import annotations

import difflib
import hashlib
import html
import os
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE_IMAGES = ROOT / 'page_images'
SCALE = 2.6          # 原版页面渲染倍率（pt -> px）
BAND = 13.0          # 公式带纵向半高（pt），覆盖上下标与分数线
MAX_PER_PAGE = 4
MAX_PER_ANSWER = 8

MARKER = re.compile(r'〔公式\s*(\d+)〕')
_MARKER_FULL = re.compile(r'〔公式\s*\d+〕')
_STRONG = '=＝≈≠≡∝∑∫∂√≤≥∇∈→∼'
_SUBSUP = '\u2070-\u209f\u2080-\u209f\u00b2\u00b3\u00b9\u2212\u2013'
_CJK_CLASS = r'\u3400-\u9fff\u3000-\u303f\uff01-\uff65'

_MATH_CHARS = (
    r'A-Za-z0-9\u0370-\u03ff'
    r'\\\.\,\(\)\[\]\{\}:;<>\+\-\*\/\^_=＝≈≠≡∝∑∫∂√≤≥±·×÷∇∈→∼°%'
    r'\u2070-\u209f\u2080-\u209f\u2212\u2013\u221e'
)
_TOKEN_OK = re.compile('^[' + _MATH_CHARS + ']+$')
_CJK_ONLY = re.compile('^[' + _CJK_CLASS + ']+$')
_SPLIT = re.compile(r'〔公式\s*\d+〕|[' + _CJK_CLASS + r']+|\S+')
_LATEX_BLOCK = re.compile(r'\\\[.*?\\\]|\\\(.*?\\\)|\$\$.*?\$\$', re.S)
_FORMULA_SPAN = re.compile(
    r'\\\[.*?\\\]|\\\(.*?\\\)|\$\$.*?\$\$'
    r'|[^' + _CJK_CLASS + r'\r\n]{2,200}',
    re.S,
)


# --------------------------------------------------------------------------
# 1. 链路保护：把文本切成「可翻译」与「必须原样保留」两类片段
# --------------------------------------------------------------------------

_UNIT_LIKE = re.compile(
    r'^[A-Za-z\u00b5\u03bc\u00b0]{1,6}'
    r'(?:[\u00b7\u2219/\^\-−()]|\d|\u2070-\u209f|\u2080-\u209f|[A-Za-z]{1,3}){1,20}$')


def _is_unit_token(tok):
    """kg·m−2·s−1 / W/(m·K) 一类单位串，翻译模型会把 · 和 − 改成连字符。"""
    tok = tok.strip('.,;:，。；：')
    return bool(_UNIT_LIKE.fullmatch(tok)) and bool(
        re.search(r'[\d\u00b7\u2219/\^\-−\u2070-\u209f\u2080-\u209f]', tok))


def _math_value(tok):
    """0=普通文本，1=符号/数值/单位类，2=含强数学运算符（可起始一个公式片段）。"""
    if _is_unit_token(tok):
        return 2
    if not _TOKEN_OK.match(tok):
        return 0
    core = tok.strip('.,;:')
    if not core:
        return 0
    if any(c in core for c in _STRONG):
        return 2
    if re.search('[' + _SUBSUP + ']', core):
        return 1
    if re.search(r'\d', core) and re.search(r'[·/\^×÷]', core):
        return 1
    if re.fullmatch(r'[\+\-\*/\^=≈≠±·×÷√∑∫∂∇]+', core):
        return 1
    if re.fullmatch(r'[A-Za-z0-9\.\u0370-\u03ff_\u2070-\u209f\u2080-\u209f]{1,4}', core) and (
        re.search(r'\d', core) or re.search(r'[\u0370-\u03ff_\u2070-\u209f\u2080-\u209f]', core)
        or len(core) <= 2
    ):
        return 1
    if re.fullmatch(r'[()\[\]\{\}\d\.\-\u2212]+', core):
        return 1
    return 0


def _segments_plain(text):
    items = []
    for m in _SPLIT.finditer(text):
        s = m.group()
        if _MARKER_FULL.fullmatch(s):
            kind, val = 'marker', 2
        elif _CJK_ONLY.fullmatch(s):
            kind, val = 'cjk', 0
        else:
            kind, val = 'tok', _math_value(s)
        items.append((m.start(), m.end(), s, kind, val))

    out = []
    pos = 0
    i = 0
    while i < len(items):
        if items[i][4] == 0 or items[i][3] == 'cjk':
            i += 1
            continue
        j = i
        while (j + 1 < len(items)
               and items[j + 1][4] > 0
               and items[j + 1][3] != 'cjk'
               and items[j + 1][0] - items[j][1] <= 2
               and items[j + 1][1] - items[i][0] <= 180):
            j += 1
        # 去掉公式前误并入的英文虚词（is / the / and 之类），不误伤真正的符号
        while i < j and items[i][4] == 1 and re.fullmatch(r'[A-Za-z]{1,3}', items[i][2]):
            i += 1
        # 若片段从等号开始，把紧邻左侧的变量名一起纳入（alpha = ... 不能只保护半截）
        if (i > 0 and items[i][3] == 'tok'
                and items[i][2][:1] in '=＝≈≠≡∝≤≥'
                and items[i][0] - items[i - 1][1] <= 2
                and re.fullmatch(r'[A-Za-z\u0370-\u03ff]{1,8}(?:_\{?[A-Za-z0-9]+\}?)?',
                                 items[i - 1][2])):
            i -= 1
        start, end = items[i][0], items[j][1]
        chunk = text[start:end]
        has_strong = any(items[k][4] == 2 for k in range(i, j + 1))
        signal = (any(c in chunk for c in _STRONG)
                  or _MARKER_FULL.search(chunk)
                  or re.search('[' + _SUBSUP + ']', chunk)
                  or any(_is_unit_token(items[k][2]) for k in range(i, j + 1)))
        if has_strong and signal:
            if start > pos:
                out.append(('text', text[pos:start]))
            out.append(('literal', chunk))
            pos = end
            i = j + 1
        else:
            i += 1
    if pos < len(text):
        out.append(('text', text[pos:]))
    return out


def segments(text):
    """返回 [(kind, 文本)]，kind='literal' 表示公式/符号/标记，必须原样保留。

    覆盖整个 text（含空白），调用方可用 ''.join 无损拼回。
    LaTeX 公式块整体作为 literal，避免被切碎后漏保护。
    """
    out = []
    for part in re.split(r'(\\\[.*?\\\]|\\\(.*?\\\)|\$\$.*?\$\$)', text, flags=re.S):
        if not part:
            continue
        if _LATEX_BLOCK.fullmatch(part):
            out.append(('literal', part))
        else:
            out.extend(_segments_plain(part))
    return out


# --------------------------------------------------------------------------
# 2. 抽取层：从原版 PDF 定位公式行并裁切原版页面图片
# --------------------------------------------------------------------------

def _row_score(s):
    score = 0
    if re.search(r'[=＝≈≠≡∝≤≥]', s):
        score += 3
    if re.search(r'[∑∫∂√∇±×÷·]', s):
        score += 2
    if re.search(r'[\u0370-\u03ff\u2070-\u209f\u2080-\u209f\u2212]', s):
        score += 2
    if re.search(r'\(\s*\d+\s*[-–]\s*\d+\s*\)', s):
        score += 2
    if re.search(r'[A-Za-z\u0370-\u03ff]{1,4}\s*[=＝]\s*[^=＝]', s):
        score += 2
    if not re.search(r'\d', s):
        score -= 3          # 纯符号行（如 "w = ·-"）多为排版碎片，不是完整公式
    if s.count('=') > 3:
        score -= 2
    if len(s) > 160:
        score -= 3
    han = len(re.findall(r'[\u3400-\u9fff]', s))
    if han > 0 and not re.search(r'\(\s*\d+\s*[-–]\s*\d+\s*\)', s):
        score -= 4          # 带中文说明的句子不算公式行
    if han > max(3, len(s) * 0.35):
        score -= 3
    if re.search(r'https?|www\.|\.pdf\b|Table \d|Figure \d', s, re.I):
        score -= 3
    return score


def _rows_of(textpage):
    chars = []
    for i in range(textpage.count_chars()):
        try:
            left, bottom, right, top = textpage.get_charbox(i)
            ch = textpage.get_text_range(i, 1)
        except Exception:
            continue
        chars.append(((bottom + top) / 2, left, bottom, right, top, ch))
    chars.sort(key=lambda x: (-x[0], x[1]))
    rows = []
    for c in chars:
        if rows and abs(rows[-1][0] - c[0]) <= 3.2:
            rows[-1][1].append(c)
        else:
            rows.append((c[0], [c]))
    out = []
    for y, members in rows:
        s = re.sub(r'\s+', ' ', ''.join(x[5] for x in sorted(members, key=lambda z: z[1])).strip())
        out.append({
            'y': y,
            'text': s,
            'l': min(x[1] for x in members),
            'b': min(x[2] for x in members),
            'r': max(x[3] for x in members),
            't': max(x[4] for x in members),
            'score': _row_score(s) if s and len(s) <= 180 else 0,
        })
    return out


@lru_cache(maxsize=128)
def _page_formulas_cached(path, page_no, doc_id, max_items, stamp):
    try:
        import pypdfium2 as pdf
    except ImportError:
        return ()
    try:
        doc = pdf.PdfDocument(path)
    except Exception:
        return ()
    try:
        if not 1 <= page_no <= len(doc):
            return ()
        page = doc[page_no - 1]
        width, height = page.get_size()
        tp = page.get_textpage()
        rows = _rows_of(tp)
        tp.close()
        candidates = [r for r in rows if r['score'] >= 5]
        if not candidates:
            page.close()
            return ()
        bands = []
        for cand in sorted(candidates, key=lambda x: -x['score']):
            members = [r for r in rows
                       if abs(r['y'] - cand['y']) <= BAND
                       and r['l'] <= cand['r'] + 90
                       and r['r'] >= cand['l'] - 90
                       and r['text']]
            box = (min(r['l'] for r in members) - 8,
                   min(r['b'] for r in members) - 5,
                   max(r['r'] for r in members) + 8,
                   max(r['t'] for r in members) + 5)
            if any(abs((b[0][1] + b[0][3]) - (box[1] + box[3])) < 16 for b in bands):
                continue
            bands.append((box, cand))
            if len(bands) >= max_items:
                break
        if not bands:
            page.close()
            return ()
        image = page.render(scale=SCALE).to_pil()
        page.close()
        PAGE_IMAGES.mkdir(parents=True, exist_ok=True)
        out = []
        for idx, (box, cand) in enumerate(bands, 1):
            # 文件名绑定裁切区域与算法版本：规则或 PDF 变化时自动重新生成，不会复用旧图
            digest = hashlib.sha256(('v2|%s|%s|%s' % (path, box, cand['text'])).encode()).hexdigest()[:10]
            name = 'f%d_%d_%d_%s.png' % (doc_id, page_no, idx, digest)
            target = PAGE_IMAGES / name
            if not target.is_file():
                px = (max(0, int(box[0] * SCALE)),
                      max(0, int((height - box[3]) * SCALE)),
                      min(image.width, int(box[2] * SCALE)),
                      min(image.height, int((height - box[1]) * SCALE)))
                if px[2] > px[0] and px[3] > px[1]:
                    tmp = target.with_name(name + '.tmp')
                    image.crop(px).save(tmp, format='PNG')
                    os.replace(tmp, target)
            out.append({
                'text': cand['text'],
                'image': name if target.is_file() else None,
                'bbox': tuple(round(v, 1) for v in box),
                'score': cand['score'],
            })
        return tuple(out)
    except Exception as exc:
        # 原版 PDF 不可用 / 加密 / 渲染失败：不中断问答，只退化成无图片
        print('[formula] doc=%s page=%s: %s' % (doc_id, page_no, exc))
        return ()
    finally:
        try:
            doc.close()
        except Exception:
            pass


def page_formulas(pdf_path, page_no, doc_id, max_items=MAX_PER_PAGE):
    """返回该页的公式候选（text / image / bbox / score），失败返回空列表。"""
    path = Path(pdf_path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    return [dict(x) for x in _page_formulas_cached(
        str(path), int(page_no), int(doc_id), int(max_items), stamp)]


def cards_for_refs(con, refs, limit=MAX_PER_ANSWER):
    """按引用页顺序生成 〔公式N〕 卡片清单。refs=[(doc,page,name), ...]。"""
    cards = []
    for ref_index, item in enumerate(refs[:3], 1):
        doc, page, name = item[0], item[1], item[2]
        row = con.execute('SELECT path FROM docs WHERE id=?', (doc,)).fetchone()
        if not row:
            continue
        for f in page_formulas(row[0], page, doc, MAX_PER_PAGE):
            if len(cards) >= limit:
                break
            n = len(cards) + 1
            card = dict(f)
            card.update(n=n, marker='〔公式%d〕' % n, doc=doc, page=page,
                        name=Path(name).name if name else '资料', ref=ref_index)
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


# --------------------------------------------------------------------------
# 3. 生成层：清单、标记规范化与答案校验
# --------------------------------------------------------------------------

def inventory(cards):
    """只给模型「编号 + 来源 + 公式编号」，不暴露被打散的文本层。

    实测：把 'β=(1-59)' 这类文本层原样塞给 1.5B 模型，它会把公式编号
    (1-59) 当成算式并算出「1 减 59」。因此清单里只保留可定位信息。
    """
    if not cards:
        return ''
    lines = ['可用原文公式清单（原文公式以原版图片单独呈现，只准用编号引用，'
             '严禁抄写、改写、转写或补全）：']
    for c in cards:
        eq = re.search(r'\(\s*(\d+\s*[-–]\s*\d+)\s*\)', c['text'])
        tag = '式(%s)' % re.sub(r'\s+', '', eq.group(1)) if eq else '含等号的计算式'
        lines.append('%s 来源：材料 [%d]，PDF 第 %d 页，%s'
                     % (c['marker'], c['ref'], c['page'], tag))
    return '\n'.join(lines)


def normalize_markers(text, n):
    """去掉越界编号并统一空格写法，避免渲染出指向不存在卡片的引用。"""
    def repl(m):
        i = int(m.group(1))
        return '〔公式%d〕' % i if 1 <= i <= n else ''
    return MARKER.sub(repl, text)


def markers_used(text):
    return {int(m.group(1)) for m in MARKER.finditer(text)}


def _norm(s):
    """逐字比较用的最小归一：只折叠空白，不做 NFKC（不把 ² 悄悄变成 2）。"""
    return re.sub(r'\s+', '', s)


def _is_formula_text(s):
    if _LATEX_BLOCK.fullmatch(s.strip()):
        return True
    if re.search(r'[A-Za-z]{5,}\s+[A-Za-z]{3,}', s) and not re.search(r'[=＝≈≠≡∝≤≥]', s):
        return False
    return bool(re.search(r'[A-Za-z\u0370-\u03ff\^_\\]', s)) and (
        any(c in s for c in '=＝≈≠≡∝≤≥') or re.search(r'[∑∫∂]', s)
    )


def _best_card(s, cards):
    best, ratio = None, 0.0
    a = _norm(s)
    for c in cards:
        r = difflib.SequenceMatcher(None, a, _norm(c['text'])).ratio()
        if r > ratio:
            best, ratio = c, r
    return best if ratio >= 0.30 else None


def verify_answer(answer, cards):
    """(修复后的答案, 修复记录)。任何非原文逐字的公式样文本都不允许留在答案里。"""
    repairs = []
    out = []
    pos = 0
    for m in _FORMULA_SPAN.finditer(answer):
        s = m.group()
        if not _is_formula_text(s):
            continue
        if any(_norm(s) == _norm(c['text']) for c in cards):
            continue  # 原文逐字副本，允许保留
        best = _best_card(s, cards)
        repl = best['marker'] if best else '（公式见下方原文卡片）'
        out.append(answer[pos:m.start()])
        out.append(repl)
        pos = m.end()
        repairs.append({'span': s[:120], 'replacement': repl})
    out.append(answer[pos:])
    return ''.join(out), repairs


# --------------------------------------------------------------------------
# 4. 展示层：标记链接与原文公式卡片
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


def link_markers(escaped_text, cards):
    """把答案正文里的 〔公式N〕 变成指向卡片的锚点（输入已 HTML 转义）。"""
    valid = {c['n'] for c in cards}

    def repl(m):
        n = int(m.group(1))
        if n not in valid:
            return ''
        return '<a class="fref" href="#f%d">〔公式%d〕</a>' % (n, n)

    return MARKER.sub(repl, escaped_text)


def cards_html(cards, used=None, title='原文公式（原版 PDF 原样，未经模型重排）'):
    """默认展示全部命中公式；used 只用于标注哪些被答案引用，不做过滤。"""
    if not cards:
        return ''
    out = ['<section class="fblock"><h3>' + esc(title) + '</h3>',
           '<p><small>下列公式直接截取自原版 PDF 页面，含上下标、分数线与公式编号；'
           '文本层逐字副本仅供复制与检索，若与图片不一致，以图片及「原版版面」为准。</small></p>']
    for c in cards:
        tag = ' · <b>本回答引用</b>' if used and c['n'] in used else ''
        out.append('<figure class="fcard" id="f%d"><figcaption>%s · %s · PDF 第 %d 页%s · '
                   '<a href="/read/%d/%d" target="_blank">原版版面</a></figcaption>'
                   % (c['n'], esc(c['marker']), esc(c['name']), c['page'], tag,
                      c['doc'], c['page']))
        if c.get('image'):
            out.append('<img src="/pageimg/%s" alt="%s 原文公式" loading="lazy">'
                       % (esc(c['image']), esc(c['marker'])))
        else:
            out.append('<p><small>原版 PDF 当前不可用，仅显示文本层逐字副本，请打开原版版面核对。</small></p>')
        out.append('<pre>' + esc(c['text']) + '</pre></figure>')
    out.append('</section>')
    return ''.join(out)
