import datetime
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
import pypdfium2 as pdf

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'knowledge.sqlite'
CONFIG = ROOT / '资料路径.json'
HAN = r'[\u3400-\u9fff]'
def tokenize(text):
    text = unicodedata.normalize('NFKC', text).lower()
    for word in re.findall(HAN + r'+|[a-z0-9]+', text):
        if re.match(HAN, word) and len(word) > 1:
            yield from (word[i:i+2] for i in range(len(word)-1))
        else:
            yield word

def chunks(text):
    text = re.sub(r'(?<=[a-z])-\r?\n(?=[a-z])', '', text)
    text = re.sub(r'(?<='+HAN+r')\s+(?='+HAN+r')','',text)
    text = ' '.join(text.split())
    start = 0
    while start < len(text):
        end = min(len(text), start+1150)
        if end < len(text):
            cut = max(text.rfind(sep, start+800, end) for sep in ['。','. ',' '])
            if cut >= 0: end = cut+1
        yield text[start:end]
        if end == len(text): break
        start = end-120

def build():
    cfg = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    files = {}
    for group, locations in cfg.items():
        for location in locations:
            root = ROOT / location
            if not root.exists(): raise FileNotFoundError(root)
            for path in ([root] if root.is_file() else sorted(root.rglob('*.pdf'))):
                files[str(path.resolve())] = group
    con = sqlite3.connect(DB, timeout=60)
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript("""
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY,path TEXT UNIQUE,groupname TEXT,stamp TEXT,pages INTEGER,blank INTEGER);
    CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(terms,body UNINDEXED,doc UNINDEXED,page UNINDEXED);
    CREATE TABLE IF NOT EXISTS page_sources(doc INTEGER,page INTEGER,source TEXT,chars INTEGER,image TEXT,updated TEXT,PRIMARY KEY(doc,page));
    """)
    for path, group in files.items():
        stat=Path(path).stat()
        stamp=f'{stat.st_mtime_ns}-{stat.st_size}'
        old=con.execute('SELECT id,stamp,groupname FROM docs WHERE path=?',(path,)).fetchone()
        if old and old[1:] == (stamp,group): continue
        with pdf.PdfDocument(path) as source, con:
            if old:
                doc=old[0]
                con.execute('DELETE FROM passages WHERE doc=?',(doc,))
            else:
                doc=con.execute('INSERT INTO docs(path) VALUES (?)',(path,)).lastrowid
            total=len(source)
            blank=0
            # 原生无文字页：走 OCR 管道；仍无文字才计为空白页。
            # 无论识别结果如何都导出页面图片，并在 page_sources 留痕，便于评测与追溯。
            lang='zh-Hans-CN' if group=='实验' else 'en-US'
            for n in range(len(source)):
                page=source[n]
                tp=page.get_textpage()
                txt=tp.get_text_bounded()
                tp.close()
                page.close()
                if not txt.strip():
                    from ocr import extract_page_text
                    try:
                        ocr_txt,ocr_src,png=extract_page_text(path,n+1,language=lang,doc_id=doc)
                        status='ocr' if ocr_txt else 'blank'
                    except Exception as exc:
                        print('[ocr] doc=%s page=%s failed: %s'%(doc,n+1,exc))
                        ocr_txt,ocr_src,png,status='','none',None,'error'
                    con.execute('INSERT OR REPLACE INTO page_sources VALUES (?,?,?,?,?,?)',
                        (doc,n+1,status,len(ocr_txt),png.name if png else '',datetime.date.today().isoformat()))
                    if ocr_txt:
                        con.executemany('INSERT INTO passages VALUES (?,?,?,?)', [(' '.join(tokenize(part)),part,doc,n+1) for part in chunks(ocr_txt)])
                    else: blank+=1
                    continue
                con.executemany('INSERT INTO passages VALUES (?,?,?,?)', [(' '.join(tokenize(part)),part,doc,n+1) for part in chunks(txt)])
            con.execute('UPDATE docs SET groupname=?,stamp=?,pages=?,blank=? WHERE id=?',(group,stamp,len(source),blank,doc))
        print(group,Path(path).name,total,flush=True)
    with con:
        for doc,path in con.execute('SELECT id,path FROM docs').fetchall():
            if path not in files:
                con.execute('DELETE FROM passages WHERE doc=?',(doc,))
                con.execute('DELETE FROM docs WHERE id=?',(doc,))
    con.close()
if __name__=='__main__': build()


