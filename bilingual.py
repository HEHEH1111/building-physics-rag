"""Offline English/Chinese translation with a persistent, content-keyed cache.

v1.3：翻译前先把公式/符号/〔公式N〕标记切出来原样保留，只翻译普通文本。
翻译模型会把 α=4+4v 之类的式子改写成完全不同的字符串，这是公式失真的
主要来源之一，必须在链路层阻断。缓存键同步升级到 v5，避免复用旧译文。
"""
import hashlib
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from functools import lru_cache
from contextlib import closing
from indexer import ROOT
import formula


lock = threading.RLock()
CACHE = ROOT/'中文阅读缓存.sqlite'
with closing(sqlite3.connect(CACHE)) as c:
    c.execute('CREATE TABLE IF NOT EXISTS translations(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    c.commit()

@lru_cache(maxsize=2)
def engine(direction):
    import ctranslate2
    import sentencepiece
    root=ROOT/'translation-model'/('translate-'+direction+'-1_9')
    return (sentencepiece.SentencePieceProcessor(model_proto=(root/'sentencepiece.model').read_bytes()),
            ctranslate2.Translator(str(root/'model'),compute_type='int8',intra_threads=3))

def _translate_plain(text,direction):
    if not text.strip(): return text
    lead=text[:len(text)-len(text.lstrip())]
    trail=text[len(text.rstrip()):]
    body=text.strip()
    sp,model=engine(direction)
    # Keep software object identifiers verbatim; bound sentence lengths for PDF tables.
    keep=r'(\b[A-Z]{2,}\b|[A-Za-z][A-Za-z0-9]*(?::[A-Za-z][A-Za-z0-9]*)+|\b[A-Z][A-Za-z0-9]+[A-Z][A-Za-z0-9]+\b)'
    parts=re.split(keep,body) if direction=='en_zh' else [body]
    out=[]
    for i,part in enumerate(parts):
        if i%2 or not re.search(r'[A-Za-z\u3400-\u9fff]',part):
            out.append(part);continue
        inputs=[]
        for sentence in re.split(r'(?<=[.!?。！？])\s+|\n+',part):
            pieces=sp.encode(sentence,out_type=str)
            inputs.extend(pieces[j:j+150] for j in range(0,len(pieces),150))
        if inputs:
            translated=model.translate_batch(inputs,beam_size=2,max_decoding_length=260,repetition_penalty=1.05)
            out.extend(sp.decode(item.hypotheses[0]) for item in translated)
    return lead+' '.join(out).replace('▁',' ').strip()+trail

def translate(text,direction='en_zh'):
    if not text.strip(): return text
    if direction=='en_zh' and len(re.findall(r'[\u3400-\u9fff]',text))*3 > len(re.findall('[a-zA-Z]',text)): return text
    if not (ROOT/'translation-model'/('translate-'+direction+'-1_9')/'model').is_dir():
        return text
    key=hashlib.sha256(('v5'+direction+text).encode()).hexdigest()
    with lock, closing(sqlite3.connect(CACHE)) as c:
        row=c.execute('SELECT value FROM translations WHERE key=?',(key,)).fetchone()
        if row:return row[0]
        segs=formula.segments(text) if direction=='en_zh' else [('text',text)]
        result=''.join(s if kind=='literal' else _translate_plain(s,direction)
                       for kind,s in segs).replace('▁',' ').strip()
        c.execute('INSERT OR REPLACE INTO translations VALUES (?,?)',(key,result));c.commit()
        return result
