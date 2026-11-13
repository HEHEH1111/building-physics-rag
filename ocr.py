"""扫描页 / 图片页的文字提取与页面图片导出。

后端优先级：Windows 内置 OCR（零第三方依赖，支持 zh-Hans-CN / en-US）
          → rapidocr_onnxruntime / paddleocr（可选增强）
          → 明确降级（返回空文本，但仍导出页面图片供人工核对）。

设计约定：
- OCR 文本统一标记为 source='ocr'，与原生可提取文字区分，便于评测时单独统计准确率。
- 任何失败都不静默吞掉：返回 ('', 'none', png) 并记录到 page_sources 表。
- 只处理「原生无文字页」，不改动已有 65,804 个正常切块。
"""

from functools import lru_cache
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PAGE_IMAGES = ROOT / 'page_images'
PS_SCRIPT = ROOT / 'ocr_windows.ps1'
MIN_TEXT_CHARS = 8          # 少于此长度视为识别失败，不写入索引
OCR_TIMEOUT = 180           # 单页 OCR 超时（秒）
RENDER_SCALE = 2.0          # 渲染倍率，约 A4 -> 1190x1684


def powershell_exe():
    """项目 Python 的 PATH 常不含 System32，显式定位 Windows PowerShell 5.1。"""
    windir = os.environ.get('WINDIR', r'C:\Windows')
    exe = Path(windir) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    return str(exe) if exe.is_file() else 'powershell'


def normalize_ocr_text(text):
    """Windows OCR 把中文按字输出并插入空格，直接入库会让中文双字词切分失效。"""
    text = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text)
    text = re.sub(r'(?<=\d)\s*[．.]\s*(?=\d)', '.', text)
    text = re.sub(r'(?<=\d)\s+(?=\d)', '', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


@lru_cache(maxsize=1)
def backend():
    """探测可用 OCR 后端，结果缓存。"""
    try:
        probe = subprocess.run(
            [powershell_exe(), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
             '$t=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime];'
             'if($t -and [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages){Write-Output "ok"}'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25)
        if 'ok' in (probe.stdout or '').lower():
            return 'windows'
    except (OSError, subprocess.SubprocessError):
        pass
    for module, name in (('rapidocr_onnxruntime', 'rapidocr'), ('paddleocr', 'paddleocr')):
        try:
            __import__(module)
            return name
        except ImportError:
            continue
    return 'none'


def render_page(pdf_path, page_no, out_png=None, scale=RENDER_SCALE):
    """用 PDFium 把指定页渲染为 PNG，返回图片路径。页码按 PDF 阅读器从 1 起算。"""
    import pypdfium2 as pdfium
    PAGE_IMAGES.mkdir(exist_ok=True)
    if out_png is None:
        out_png = PAGE_IMAGES / ('p%03d.png' % page_no)
    out_png = Path(out_png)
    with pdfium.PdfDocument(str(pdf_path)) as doc:
        page = doc[page_no - 1]
        bitmap = page.render(scale=scale)
        bitmap.to_pil().save(out_png)
        page.close()
    return out_png


def recognize_image(png_path, language='zh-Hans-CN'):
    """识别单张图片文字，失败返回空串。"""
    engine = backend()
    if engine == 'windows':
        out = Path(str(png_path) + '.ocr.txt')
        try:
            result = subprocess.run(
                [powershell_exe(), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(PS_SCRIPT),
                 '-ImagePath', str(png_path), '-OutputPath', str(out), '-Language', language],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=OCR_TIMEOUT)
            if result.returncode == 0 and out.exists():
                text = normalize_ocr_text(out.read_text(encoding='utf-8-sig', errors='replace'))
                return text if len(text) >= MIN_TEXT_CHARS else ''
        except (OSError, subprocess.SubprocessError) as exc:
            print('[ocr] windows backend failed: %s' % exc, file=sys.stderr)
        return ''
    if engine == 'rapidocr':
        try:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
            result, _ = ocr(str(png_path))
            if result:
                return '\n'.join(item[1] for item in result).strip()
        except Exception as exc:
            print('[ocr] rapidocr backend failed: %s' % exc, file=sys.stderr)
        return ''
    if engine == 'paddleocr':
        try:
            from paddleocr import PaddleOCR
            ocr = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
            result = ocr.ocr(str(png_path), cls=True)
            if result and result[0]:
                return '\n'.join(line[1][0] for line in result[0]).strip()
        except Exception as exc:
            print('[ocr] paddleocr backend failed: %s' % exc, file=sys.stderr)
        return ''
    return ''


def extract_page_text(pdf_path, page_no, language='zh-Hans-CN', doc_id=None):
    """返回 (text, source, png_path)。source 为 'ocr' 或 'none'。

    无论 OCR 是否成功都会导出页面图片，保证图片页至少「可见、可定位、可打开原版版面」。
    图片按 doc_id + 页码命名，供 /read 页直接展示。
    """
    name = ('doc%s_p%03d.png' % (doc_id, page_no)) if doc_id is not None else ('p%03d.png' % page_no)
    png = render_page(pdf_path, page_no, out_png=PAGE_IMAGES / name)
    text = recognize_image(png, language=language)
    if text and len(text) >= MIN_TEXT_CHARS:
        return text, 'ocr', png
    return '', 'none', png


if __name__ == '__main__':
    print('OCR backend:', backend())
