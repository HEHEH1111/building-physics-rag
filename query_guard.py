"""查询门控与同义词扩展。

解决两类问题：
1) 领域外 / 闲聊类问题（如「今天天气如何」）在进入检索与模型之前被拦截，
   返回统一话术，不消耗算力、不生成貌似合理的答案。
2) 词法检索（FTS5）无法处理同义表述：按「查询词典.json」的同义词组扩展
   中文双字词与英文短语，把「含湿量 / 含水率 / moisture content」等召回对齐。

词典为外部 JSON，可由产品/领域同学维护，改完重启服务即可生效。
"""

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DICT_FILE = ROOT / '查询词典.json'

INVALID_MESSAGE = ('该问题无效：本知识库仅收录建筑热湿领域手册'
                   '（COMSOL / EnergyPlus / 实验资料），请提出与手册内容相关的问题。')

# 词典缺失时的最小兜底，保证服务仍可启动（不静默放行领域外问题）
_FALLBACK = {
    'synonym_groups': [],
    'domain_terms': ['建筑', '热湿', '手册', '实验', '材料', '温度', '湿度'],
    'english_domain_terms': ['comsol', 'energyplus', 'hamt', 'sorption', 'moisture'],
    'software_terms': ['comsol', 'energyplus', 'hamt'],
    'strong_domain_terms': ['comsol', 'energyplus', 'hamt', 'sorption'],
    'invalid_patterns': ['今天天气', '天气预报', '你是谁', '讲个笑话'],
    'chitchat_patterns': [r'^(你好|hi|hello|谢谢|thanks)[\s!！?？。]*$'],
}

HAN = r'[\u3400-\u9fff]'
OBJECT_RE = re.compile(r'[a-z][a-z0-9]*(?::[a-z][a-z0-9]*)+')
UNIT_RE = re.compile(r'(W/\(|w/\(|kg/\(|°C|℃|m²|m2|Pa\b|%\b|kg/|J/\()')


def _load():
    try:
        data = json.loads(DICT_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = _FALLBACK
    # 同义词组里的中文词条自动成为领域锚点，避免「含水率 / 热导率」这类
    # 合法专业术语因不在手工词表而被误判为领域外问题。
    zh_terms = [t.lower() for g in data.get('synonym_groups', [])
                for t in g if re.search(HAN, t)]
    domain = tuple(dict.fromkeys(
        [t.lower() for t in data.get('domain_terms', [])] + zh_terms))
    strong = tuple(dict.fromkeys(
        [t.lower() for t in data.get('strong_domain_terms', [])] + zh_terms))
    return {
        'synonyms': [tuple(g) for g in data.get('synonym_groups', [])],
        'domain': domain,
        'en': tuple(t.lower() for t in data.get('english_domain_terms', [])),
        'software': tuple(t.lower() for t in data.get('software_terms', [])),
        'strong': strong,
        'invalid': tuple(re.compile(p) for p in data.get('invalid_patterns', [])),
        'chitchat': tuple(re.compile(p) for p in data.get('chitchat_patterns', [])),
        'version': data.get('version', 'fallback'),
    }


_CFG = _load()


def reload_dict():
    """热加载词典（改完 查询词典.json 后调用，或重启服务）。"""
    global _CFG
    _CFG = _load()
    return _CFG['version']


def normalize(q):
    return unicodedata.normalize('NFKC', q or '').strip()


def _has_domain_anchor(plain):
    cfg = _CFG
    if any(t in plain for t in cfg['domain']):
        return True
    if any(t in plain for t in cfg['en']):
        return True
    if any(t in plain for t in cfg['software']):
        return True
    if OBJECT_RE.search(plain):
        return True
    if UNIT_RE.search(plain):
        return True
    return False


def validate_question(q):
    """返回 (is_valid, reason, message)。

    reason 取值：domain / empty / out_of_domain / chitchat，供埋点归因。
    判定顺序：空问题 → 领域外句式（除非命中强领域锚点）→ 闲聊 → 领域锚点白名单。
    """
    text = normalize(q)
    plain = text.lower()
    if len(text) < 2 or not re.search(r'[0-9a-z\u3400-\u9fff]', plain):
        return False, 'empty', INVALID_MESSAGE

    strong = any(t in plain for t in _CFG['strong'])
    if not strong:
        if any(p.search(plain) for p in _CFG['invalid']):
            return False, 'out_of_domain', INVALID_MESSAGE
        if any(p.search(plain) for p in _CFG['chitchat']):
            return False, 'chitchat', INVALID_MESSAGE
    if _has_domain_anchor(plain):
        return True, 'domain', ''
    return False, 'out_of_domain', INVALID_MESSAGE


def expand_terms(q):
    """返回 (英文短语列表, 中文同义词列表, 命中的同义词组)。

    中文同义词交回检索层做双字词切分，英文短语作为 FTS5 短语查询参与 OR 召回。
    """
    plain = normalize(q).lower()
    en, cn, groups = [], [], []
    for group in _CFG['synonyms']:
        if any(term.lower() in plain for term in group):
            groups.append(group)
            for term in group:
                if re.search(r'[a-zA-Z]', term):
                    en.append(term.lower())
                elif re.search(HAN, term):
                    cn.append(term)
    dedupe = lambda seq: list(dict.fromkeys(seq))
    return dedupe(en), dedupe(cn), groups


def describe():
    """自检用：返回词典规模，便于确认配置已加载。"""
    return {
        'version': _CFG['version'],
        'synonym_groups': len(_CFG['synonyms']),
        'domain_terms': len(_CFG['domain']),
        'english_terms': len(_CFG['en']),
        'invalid_patterns': len(_CFG['invalid']),
    }


if __name__ == '__main__':
    print(describe())
    for sample in ('今天天气如何', '吸水系数怎么测？', 'HAMT sorption isotherm',
                   '你好', '帮我写一首诗', '含湿量和含水率的区别',
                   'Site:GroundTemperature:BuildingSurface', '今天天气对围护结构传热的影响'):
        print(sample, '->', validate_question(sample)[:2])
