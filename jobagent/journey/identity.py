"""Cross-platform job identity: normalization, aliases, identity_key.

F1-R4 (roadmap): dedupe by ``boss:<encryptJobId>`` alone misses the same
job posted on another platform (an XHS referral post vs the Boss listing)
and Boss re-posts under a fresh encryptJobId. Deterministic merging on
raw "company+title" is equally wrong — 豆包 vs 火山引擎 are different
business lines under one company, and identical titles with multiple
headcount coexist.

Layered resolution (this module owns the deterministic layers):

- L1  same platform id                 → automatic identity (registry's
  existing job_id primary key, untouched).
- L2  normalized company + normalized
  title + business line                → deterministic identity_key; an
  exact hit merges postings under one identity automatically.
- L3  fuzzy candidates + JD evidence   → suggestion only; merging goes
  through a HITL tool (registry.merge_identities).

Business-line inference is deliberately conservative: when one source
carries a direction qualifier (「RAG & Agent 方向」) and the other does
not, the keys legitimately differ and the pair drops to L3 instead of
being force-merged.
"""

from __future__ import annotations

import re
import unicodedata

# -- company normalization ----------------------------------------------------

#: Legal-entity / filler suffixes stripped after separator squashing.
#: Applied repeatedly so 「信息技术有限公司」 loses both tails.
_COMPANY_SUFFIXES: tuple[str, ...] = (
    "有限责任公司",
    "股份有限公司",
    "有限公司",
    "信息技术",
    "信息科技",
    "网络科技",
    "集团公司",
    "集团",
    "科技公司",
    "科技",
    "软件公司",
    "软件",
    "technology",
    "tech",
    "software",
    "information",
    "network",
    "corporation",
    "company",
    "inc",
    "ltd",
    "llc",
    "corp",
    "co",
    "group",
)

#: One alias group per company: any spelling maps to the group key.
_COMPANY_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bytedance", ("字节跳动", "字节", "抖音集团", "抖音", "bytedance", "douyin")),
    ("alibaba", ("阿里巴巴", "阿里", "淘宝", "天猫", "alibaba", "taobao")),
    ("tencent", ("腾讯", "微信", "tencent", "wechat")),
    ("baidu", ("百度", "baidu")),
    ("meituan", ("美团", "meituan")),
    ("pinduoduo", ("拼多多", "pdd", "pinduoduo")),
    ("jd", ("京东", "京东商城", "jingdong", "jd.com")),
    ("huawei", ("华为", "华为技术", "huawei")),
    ("xiaomi", ("小米", "xiaomi")),
    ("netease", ("网易", "netease")),
    ("ant", ("蚂蚁集团", "蚂蚁金服", "蚂蚁", "antgroup", "antgroup", "ant")),
    ("didi", ("滴滴", "滴滴出行", "didi")),
    ("bilibili", ("哔哩哔哩", "b站", "bilibili")),
    ("dell", ("戴尔", "戴尔易安信", "dell", "dellemc", "delltechnologies")),
    ("nvidia", ("英伟达", "nvidia")),
)


def _build_alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group, aliases in _COMPANY_ALIAS_GROUPS:
        for alias in aliases:
            mapping[_squash_separators(_fold(alias))] = group
    return mapping


_PARENS_RE = re.compile(r"[（(【\[].*?[)）\]】]")
_WHITESPACE_RE = re.compile(r"[\s·、,，.。'’\-—_/]+")

# -- title normalization ------------------------------------------------------

#: Seniority qualifiers dropped so 「资深 AI Agent 工程师」 and
#: 「AI Agent 工程师」 resolve to the same title.
_TITLE_LEVEL_WORDS: tuple[str, ...] = (
    "资深",
    "高级",
    "中级",
    "初级",
    "专家级",
    "专家",
    "senior",
    "junior",
    "staff",
    "principal",
    "lead",
    "expert",
    "sr.",
    "jr.",
    "sr",
    "jr",
)

#: Role spellings folded onto one token so 开发/研发/软件工程师 merge while
#: 产品经理/测试 stay distinct.
_TITLE_EQUIVALENTS: dict[str, str] = {
    "研发工程师": "eng",
    "开发工程师": "eng",
    "软件工程师": "eng",
    "开发研发工程师": "eng",
    "engineer": "eng",
    "developer": "eng",
    "dev": "eng",
    "研发": "eng",
    "开发": "eng",
    "产品经理": "pm",
    "productmanager": "pm",
    "测试工程师": "qa",
    "测试开发工程师": "qa",
    "qaengineer": "qa",
}

# -- business line ------------------------------------------------------------

#: Keyword → business line, first match wins over lowercased text.
_BUSINESS_LINES: tuple[tuple[str, str], ...] = (
    ("agent", "agent"),
    ("智能体", "agent"),
    ("rag", "rag"),
    ("检索增强", "rag"),
    ("大模型", "llm"),
    ("llm", "llm"),
    ("aigc", "llm"),
    ("爬虫", "crawler"),
    ("spider", "crawler"),
    ("crawler", "crawler"),
    ("后端", "backend"),
    ("backend", "backend"),
    ("前端", "frontend"),
    ("frontend", "frontend"),
    ("vue", "frontend"),
    ("react", "frontend"),
    ("算法", "ml"),
    ("机器学习", "ml"),
    ("machinelearning", "ml"),
    ("全栈", "fullstack"),
    ("fullstack", "fullstack"),
    ("fullstack", "fullstack"),
    ("测试", "qa"),
    ("数据分析", "data"),
    ("data", "data"),
)

_DEFAULT_BUSINESS_LINE = "general"
_IDENTITY_PREFIX = "idn:"


def _fold(value: str) -> str:
    """NFKC-fold to unify full/half-width forms, then lowercase."""

    return unicodedata.normalize("NFKC", value).strip().lower()


def _squash_separators(value: str) -> str:
    return _WHITESPACE_RE.sub("", value)


def _strip_parenthesized(value: str) -> str:
    without = _PARENS_RE.sub("", value)
    return without if without.strip() else value


def normalize_company(name: str) -> str:
    """Canonical company token used inside identity keys.

    Pipeline: fold → drop parenthesized qualifiers → squash separators →
    strip suffix tails repeatedly → alias-group mapping. Falls back to
    the folded original when everything would be stripped.
    """

    folded = _fold(name)
    squashed = _squash_separators(_strip_parenthesized(folded))
    previous: str | None = None
    stripped = squashed
    while previous != stripped:
        previous = stripped
        for suffix in _COMPANY_SUFFIXES:
            if stripped.endswith(suffix) and len(stripped) > len(suffix):
                stripped = stripped[: -len(suffix)]
    token = stripped or folded
    return _ALIAS_TO_GROUP.get(token, token)


def canonical_company(name: str) -> str:
    """Alias-group representative for display purposes."""

    return normalize_company(name)


def normalize_title(title: str) -> str:
    """Canonical role token used inside identity keys.

    Drops parenthesized directions (「AI Agent 工程师（RAG 方向）」),
    seniority qualifiers, and folds equivalent role spellings. Keeps the
    实习 marker so intern and full-time stay distinct identities.
    """

    folded = _fold(title)
    folded = _squash_separators(_strip_parenthesized(folded))
    for word in _TITLE_LEVEL_WORDS:
        folded = folded.replace(word, "")
    for spelling, token in _TITLE_EQUIVALENTS.items():
        folded = folded.replace(spelling, token)
    return folded or _fold(title)


def derive_business_line(*texts: str) -> str:
    """First keyword hit across the given texts; ``general`` when unknown."""

    haystack = _squash_separators(" ".join(_fold(text) for text in texts if text))
    for keyword, line in _BUSINESS_LINES:
        if keyword in haystack:
            return line
    return _DEFAULT_BUSINESS_LINE


def identity_key(
    company: str,
    title: str,
    *,
    business_line: str | None = None,
) -> str:
    """Deterministic L2 identity: normalized company | title | line.

    ``business_line`` may be pre-derived from JD text; when omitted it is
    inferred from the title alone (discovery has no JD yet). Direction
    information present in one source but not the other intentionally
    produces different keys (L3 territory, never force-merged).
    """

    line = business_line or derive_business_line(title)
    return f"{_IDENTITY_PREFIX}{normalize_company(company)}|{normalize_title(title)}|{line}"


_ALIAS_TO_GROUP = _build_alias_map()
