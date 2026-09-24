"""Server-owned, conservative extraction of editable journey summaries."""

import re
from datetime import date

from pydantic import BaseModel, Field


class JourneyDraft(BaseModel):
    company: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)
    department: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=200)
    salary: str = Field(default="", max_length=100)
    cycle: str = Field(default_factory=lambda: date.today().isoformat(), max_length=100)
    description: str = ""


LABELS = {
    "company": "公司名称|企业名称|公司|企业|雇主|Employer",
    "role": "岗位名称|职位名称|岗位|职位|Role|Position",
    "department": "部门名称|部门|业务线|事业部|Department",
    "location": "工作地点|地点|Location",
    "salary": "薪资范围|薪资|薪酬|Salary",
    "cycle": "招聘周期",
}
SECTION = r"(?:职位描述|【\s*(?:核心职责|岗位职责|任职要求)\s*】)"


def _explicit(text: str) -> dict[str, str]:
    # Bare words such as 企业级 / 职位描述 are never labels. Only a full
    # 名称 label may omit punctuation. Do not consume the next OCR line.
    names = [name for group in LABELS.values() for name in group.split("|")]
    patterns = [
        re.escape(name) + (
            r"[ \t]*(?:(?:改为|是|为)[ \t]*)?[:：]?[ \t]*"
            if name.endswith("名称") else r"[ \t]*(?:[:：]|改为|是|为)[ \t]*"
        ) for name in names
    ]
    marker = re.compile(r"(?:^|[\s，,。；;])(" + "|".join(patterns) + r")", re.I)
    matches = list(marker.finditer(text))
    result = {}
    for index, match in enumerate(matches):
        label = next(name for name in names if match[1].lower().startswith(name.lower()))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = re.split(r"[\r\n，,。；;]", text[match.end():end], maxsplit=1)[0].strip()
        if value and len(value) <= 100:
            key = next(key for key, group in LABELS.items() if label in group.split("|"))
            result[key] = value
    return result


def extract_draft(text: str, current: JourneyDraft | None = None) -> JourneyDraft:
    """Extract a new JD or apply only explicit corrections to an existing draft."""
    fields = _explicit(text)
    draft = current.model_copy(deep=True) if current else JourneyDraft(description=text)
    if current is None:
        # Preserve raw OCR; normalize only a working copy, including Chinese
        # character spacing from Tesseract. Field delimiters remain explicit.
        lines = [re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", line).strip()
                 for line in text.splitlines()]
        normalized = "\n".join(lines)
        header = re.split(SECTION, normalized, maxsplit=1)[0]
        if "company" not in fields:
            # A footer HR signature identifies the employer without requiring
            # a legal company suffix or relying on webpage button labels.
            footer = text[-300:]
            # Keep the activity label as a boundary before joining OCR-split
            # Chinese characters; otherwise it becomes part of the employer.
            footer = re.split(r"刚\s*刚\s*活\s*跃|最\s*近\s*活\s*跃", footer)[-1]
            footer = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", footer)
            signatures = re.findall(
                r"(?:^|\s)([\u4e00-\u9fffA-Za-z0-9&.-]{2,40})\s*[·•\"'“”]\s*HRM?\b",
                footer, re.I,
            )
            if signatures:
                fields["company"] = signatures[-1]
            else:
                companies = [line for line in lines if re.fullmatch(
                    r"[\u4e00-\u9fffA-Za-z0-9（）()·& .-]{2,60}"
                    r"(?:有限公司|股份公司|集团|科技)", line
                ) and not re.search(r"负责|熟悉|要求|服务|对接", line)]
                if len(set(companies)) == 1:
                    fields["company"] = companies[0]
        if "role" not in fields:
            header = re.split(SECTION, normalized, maxsplit=1)[0]
            title_pattern = (
                r"[\u4e00-\u9fffA-Za-z0-9（）()+#./-]{2,60}"
                r"(?:工程师|开发|经理|专员|主管|总监|架构师|设计师|负责人)"
            )
            # Keep complete multiline titles; for a flattened card use the
            # title token before page chrome. Never search responsibilities.
            titles = [line for line in header.splitlines() if re.fullmatch(
                r"(?:AI[ \t]+)?" + title_pattern, line
            ) and not re.search(r"负责|熟悉|要求|经验", line)]
            if not titles:
                raw_header = header
                titles = [token for token in raw_header.split()
                          if re.fullmatch(title_pattern, token)
                          and not re.search(r"负责|熟悉|要求|经验", token)]
            if titles:
                fields["role"] = titles[0]
        # Read the dedicated location label, not the first city on the page.
        location = re.search(r"工作地点\s*[:：]\s*([^\r\n，,。；;]+)", normalized)
        if location:
            fields["location"] = location[1].strip()
        elif "location" not in fields:
            city = re.search(
                r"北京|上海|广州|深圳|杭州|南京|苏州|成都|武汉|西安|重庆|天津|厦门", header
            )
            if city:
                fields["location"] = city[0]
        if "salary" not in fields:
            salary = re.search(
                r"\d+(?:\.\d+)?\s*[kK万千]?\s*[-~至到—–]\s*"
                r"\d+(?:\.\d+)?\s*[kK万千]", text
            )
            if salary:
                fields["salary"] = re.sub(r"\s+", "", salary[0])
    if "location" in fields:
        value = re.split(
            r"\s+(?:[A-Za-z]+\b|刚刚活跃|最近活跃)", fields["location"], maxsplit=1
        )[0].strip()
        fields["location"] = "上海临港" if re.sub(r"\s+", "", value) == "临港" else value
    return JourneyDraft.model_validate({**draft.model_dump(), **fields})


def draft_response(text: str, current: JourneyDraft | None = None) -> dict:
    draft = extract_draft(text, current)
    missing = [label for field, label in (("company", "公司名称"), ("role", "岗位名称"))
               if not getattr(draft, field)]
    return {
        "draft": draft.model_dump(), "missing_fields": missing, "method": "rules",
        "message": (
            "还需确认：" + "、".join(missing) if missing
            else "已提取公司和岗位，请检查右侧摘要后确认创建。"
        ),
    }
