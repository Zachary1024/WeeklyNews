#!/usr/bin/env python3
"""
项目周报自动化脚本
==================
功能：
  1. 读取 4 个人的个人周报（纯文本，每条带 [标签]）
  2. 按标签匹配到项目周报的各个分项
  3. 读取上周项目周报
  4. 生成结构化数据供 LLM 归纳汇总，产出本周项目周报

用法：
  python weekly_report.py                    # 自动计算当前周，输出 JSON + prompt
  python weekly_report.py --week W19         # 指定周号
  python weekly_report.py --dry-run          # 只显示解析结果，不生成
  python weekly_report.py --output json      # 输出结构化 JSON
  python weekly_report.py --output prompt    # 输出 LLM prompt
  python weekly_report.py --output md        # 直接调用 DeepSeek API 生成 .md 周报
  python weekly_report.py --output all       # 输出 JSON + prompt + .md
  python weekly_report.py --output md --save-prompt  # 生成 .md 同时保存 prompt
  python weekly_report.py --review           # 扫描上周周报已完成条目，生成审核文件
  python weekly_report.py --diff             # 对比本周与上周周报，列出未变更条目
  python weekly_report.py --prune ID1,ID2    # 删除指定条目，自动清理空节并重新编号
"""

import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Windows GBK 环境下强制 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# ─── 工具函数 ────────────────────────────────────────────

def get_week_label(date_obj: date) -> str:
    """返回 ISO 周标签，如 '2026W17'"""
    iso = date_obj.isocalendar()
    return f"{iso[0]}W{iso[1]:02d}"


def parse_week_label(label: str) -> tuple[int, int]:
    """解析 '2026W17' -> (2026, 17)"""
    m = re.match(r'(\d{4})W(\d{2})', label)
    if not m:
        raise ValueError(f"无效的周标签: {label}")
    return int(m.group(1)), int(m.group(2))


def prev_week_label(label: str) -> str:
    """返回上一周的标签，如 '2026W17' -> '2026W16'"""
    year, week = parse_week_label(label)
    # 用该周周四推算该周日期，再减7天
    d = date.fromisocalendar(year, week, 4)
    return get_week_label(d - timedelta(days=7))


def get_report_path(config: dict, week_label: str) -> str:
    """根据配置和周标签，返回项目周报文件路径"""
    pattern = config["project_report_pattern"].replace("{week_label}", week_label)
    return os.path.join(config["project_report_dir"], pattern)


# ─── 周报结构解析与重构 ────────────────────────────────────

def parse_report_to_structure(text: str) -> list[dict]:
    """解析周报 Markdown 为结构化数据。

    返回: [{num, title, subsections: [{num, title, items: [{letter, text}]}]}]

    两种格式:
      - 标题+子条目: （1）标题 下有 - a. / - b. 子条目
      - 内联条目: （1）条目正文（无子条目，标题即内容）
    """
    sections = []
    section_blocks = re.split(r'\n(?=### \d+\.)', text)

    for block in section_blocks:
        if not block.strip():
            continue

        sm = re.match(r'###\s+(\d+)\.\s*(.+)', block)
        if not sm:
            continue

        sec_num = int(sm.group(1))
        sec_title = sm.group(2).strip()
        body = block[sm.end():].strip()

        sub_blocks = re.split(r'\n(?=（\d+）)', body) if body else []

        subsections = []
        for sub_block in sub_blocks:
            sub_block = sub_block.strip()
            if not sub_block:
                continue

            sm = re.match(r'（(\d+)）(.+)', sub_block)
            if not sm:
                continue

            sub_num = int(sm.group(1))
            sub_rest = sm.group(2).strip()
            sub_body = sub_block[sm.end():].strip()

            item_pattern = re.compile(r'(?:^|\n)\s*(?:-\s*)?([a-z])\.\s+')
            item_matches = list(item_pattern.finditer(sub_body))

            if item_matches:
                items = []
                for j, m in enumerate(item_matches):
                    letter = m.group(1)
                    start = m.end()
                    end = item_matches[j + 1].start() if j + 1 < len(item_matches) else len(sub_body)
                    item_text = sub_body[start:end].strip()
                    item_lines = item_text.split('\n')
                    item_text = ' '.join(l.strip() for l in item_lines if l.strip())
                    items.append({'letter': letter, 'text': item_text})

                subsections.append({
                    'num': sub_num,
                    'title': sub_rest,
                    'items': items
                })
            else:
                subsections.append({
                    'num': sub_num,
                    'title': None,
                    'items': [{'letter': None, 'text': sub_rest}]
                })

        sections.append({
            'num': sec_num,
            'title': sec_title,
            'subsections': subsections
        })

    return sections


def structure_to_markdown(structure: list[dict]) -> str:
    """将结构化数据还原为 Markdown 周报。"""
    lines = []

    for i, sec in enumerate(structure):
        if i > 0:
            lines.append('')
        lines.append(f"### {sec['num']}. {sec['title']}")
        lines.append('')

        for sub in sec['subsections']:
            items = sub['items']
            if not items:
                continue

            if sub['title'] is not None:
                lines.append(f"（{sub['num']}）{sub['title']}")
                lines.append('')
                for item in items:
                    lines.append(f"   - {item['letter']}. {item['text']}")
                    lines.append('')
            else:
                for item in items:
                    lines.append(f"（{sub['num']}）{item['text']}")
                    lines.append('')

    return '\n'.join(lines)


def get_item_map(structure: list[dict]) -> dict[str, str]:
    """将结构化数据展平为 {item_id: item_text} 映射。"""
    items = {}
    for sec in structure:
        for sub in sec['subsections']:
            for item in sub['items']:
                if item['letter']:
                    item_id = f"{sec['num']}.{sub['num']}.{item['letter']}"
                else:
                    item_id = f"{sec['num']}.{sub['num']}"
                items[item_id] = item['text']
    return items


def _normalize_text(text: str) -> str:
    """规范化文本用于对比：压缩空白字符。"""
    return re.sub(r'\s+', ' ', text).strip()


def diff_report_items(new_struct: list[dict], old_struct: list[dict]) -> list[dict]:
    """对比新旧周报结构，返回内容未变更的条目列表。

    返回: [{id, text, full_text}, ...]
    """
    new_items = get_item_map(new_struct)
    old_items = get_item_map(old_struct)

    unchanged = []
    for item_id, new_text in new_items.items():
        if item_id in old_items:
            if _normalize_text(new_text) == _normalize_text(old_items[item_id]):
                preview = new_text[:100] + ('...' if len(new_text) > 100 else '')
                unchanged.append({
                    'id': item_id,
                    'text': preview,
                    'full_text': new_text
                })

    return unchanged


def prune_report_structure(structure: list[dict], remove_ids: set[str]) -> list[dict]:
    """从结构中删除指定条目，清理空子节/大节，重新编号。"""
    for sec in structure:
        new_subs = []
        for sub in sec['subsections']:
            new_items = []
            for item in sub['items']:
                if item['letter']:
                    item_id = f"{sec['num']}.{sub['num']}.{item['letter']}"
                else:
                    item_id = f"{sec['num']}.{sub['num']}"

                if item_id not in remove_ids:
                    new_items.append(item)

            if new_items:
                sub['items'] = new_items
                new_subs.append(sub)

        sec['subsections'] = new_subs

    # 移除空大节
    structure = [s for s in structure if s['subsections']]

    # 重新编号大节
    for i, sec in enumerate(structure):
        sec['num'] = i + 1

    # 重新编号子节
    for sec in structure:
        for j, sub in enumerate(sec['subsections']):
            sub['num'] = j + 1

    # 重新编号子条目字母
    for sec in structure:
        for sub in sec['subsections']:
            for k, item in enumerate(sub['items']):
                if item['letter'] is not None:
                    item['letter'] = chr(ord('a') + k)

    return structure


# ─── 配置加载 ────────────────────────────────────────────

def load_config(config_path: str = "config.json") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_alias_map(sections: list[dict]) -> dict[str, tuple[str, str, str]]:
    """
    构建 别名 -> (section_id, section_title, subsection_id_or_None) 映射表。
    返回 dict: lowercase_alias -> (section_id, title, sub_id_or_None)
    """
    alias_map = {}
    for sec in sections:
        sid = sec["id"]
        stitle = sec["title"]
        # 主 section 别名
        for a in sec.get("aliases", []):
            alias_map[a.lower()] = (sid, stitle, None)
        # 也直接用 id 匹配
        alias_map[sid.lower()] = (sid, stitle, None)
        alias_map[stitle.lower()] = (sid, stitle, None)

        # 子 section 别名
        for sub in sec.get("subsections", []):
            subid = sub["id"]
            subtitle = sub["title"]
            for a in sub.get("aliases", []):
                alias_map[a.lower()] = (sid, stitle, subid)
            alias_map[subid.lower()] = (sid, stitle, subid)
            alias_map[subtitle.lower()] = (sid, stitle, subid)
    return alias_map


# ─── 个人周报解析 ────────────────────────────────────────

def read_individual_reports(config: dict) -> dict[str, str]:
    """读取所有人的个人周报，返回 {name: text}"""
    reports = {}
    for person in config.get("people", []):
        path = person["file"]
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                reports[person["name"]] = f.read()
        else:
            print(f"⚠ 未找到 {person['name']} 的周报: {path}")
    return reports


# ─── 个人周报一级标题提取 ─────────────────────────────────

# 一级标题正则：匹配 [可选编号] 标题文本 [可选冒号]
# 编号格式: "1." "1、" "一、" "（1）" "1)"
# 为避免误匹配长内容行，加约束：
#   - 纯标题（无编号、无冒号）限 2-15 字符
#   - 有编号或有冒号的标题限 2-40 字符
HEADING_RE_NAMED = re.compile(
    r'^\s*'
    r'(?:'
    r'(?:\d+|[一二三四五六七八九十]+)[\.、．）)]\s*'  # 编号如 "1." "一、"
    r'|（\d+）\s*'                                       # 中文括号编号 "（1）"
    r')+'
    r'([^：:\n]{2,40})'                                   # 有编号的标题（2-40字符）
    r'[：:]?\s*$'
)

HEADING_RE_COLON = re.compile(
    r'^\s*'
    r'([^：:\n]{2,40})'                                   # 标题文本（2-40字符）
    r'[：:]\\s*$'                                          # 必须以冒号结尾
)

HEADING_RE_SHORT = re.compile(
    r'^\s*'
    r'([^：:\n]{2,15})'                                   # 短标题（2-15字符，无冒号）
    r'\s*$'
)


def _is_heading_line(stripped: str) -> str | None:
    """判断一行是否为一级标题，返回标题文本或 None。

    三级匹配（优先级从高到低）：
      1. 有编号的标题: "1. Slt bug支持" / "一、外部支持："
      2. 以冒号结尾的标题: "同步：" / "其他："
      3. 短标题（2-15字符，无冒号无编号）: "定位支持"
    """
    m = HEADING_RE_NAMED.match(stripped)
    if m:
        return m.group(1).strip()

    m = HEADING_RE_COLON.match(stripped)
    if m:
        return m.group(1).strip()

    m = HEADING_RE_SHORT.match(stripped)
    if m:
        return m.group(1).strip()

    return None


def extract_headings(text: str) -> list[dict]:
    """从个人周报文本中提取一级标题及其内容块。

    返回: [{heading: str, lines: [str, ...]}, ...]

    规则：
      - 用 _is_heading_line() 识别标题行
      - 短标题（无编号无冒号）只有前面是空行或文件开头时才识别，避免误匹配内容行
      - 标题后的行属于该标题，直到遇到下一个标题
      - 第一个标题之前的文本被忽略
    """
    raw_lines = text.split('\n')
    blocks = []
    current_heading = None
    current_lines = []
    prev_blank = True  # 文件开头视为前面有空行

    for line in raw_lines:
        stripped = line.strip()
        heading_text = _is_heading_line(stripped)

        # 短标题需要前面有空行
        if heading_text is not None:
            is_short = (HEADING_RE_SHORT.match(stripped) is not None
                        and HEADING_RE_NAMED.match(stripped) is None
                        and HEADING_RE_COLON.match(stripped) is None)
            if is_short and not prev_blank:
                # 短标题但前面没有空行 → 视为内容
                heading_text = None

        if heading_text is not None:
            # 保存上一个 block
            if current_heading is not None:
                blocks.append({
                    'heading': current_heading,
                    'lines': current_lines
                })
            current_heading = heading_text
            current_lines = []
            prev_blank = False
        else:
            if stripped:
                if current_heading is not None:
                    current_lines.append(stripped)
                prev_blank = False
            else:
                prev_blank = True

    # 最后一个 block
    if current_heading is not None:
        blocks.append({
            'heading': current_heading,
            'lines': current_lines
        })

    return blocks


def _tokenize_cjk(text: str) -> list[str]:
    """从中文文本中提取关键词token（用于匹配）。"""
    tokens = []
    # 完整文本作为一个token
    tokens.append(text)
    # 按分隔符拆分
    parts = re.split(r'[、，,./\-—–\s]+', text)
    for p in parts:
        p = p.strip()
        if len(p) >= 2:
            tokens.append(p)
    # CJK 2-gram 和 3-gram（用于部分匹配）
    cjk_runs = re.findall(r'[\u4e00-\u9fff]+', text)
    for run in cjk_runs:
        for i in range(len(run) - 1):
            tokens.append(run[i:i+2])
        for i in range(len(run) - 2):
            tokens.append(run[i:i+3])
    # 英文/数字词
    eng = re.findall(r'[a-zA-Z0-9]+', text)
    tokens.extend(eng)
    # 去重，按长度降序（长token匹配优先）
    return sorted(set(tokens), key=len, reverse=True)


def match_heading(heading: str, sections_config: list[dict]) -> tuple[str, str, str | None, str]:
    """将个人周报的一级标题匹配到项目周报的二级标题（subsection）或大标题（section）。

    匹配原则：标题中的"部分字段"（关键词token）是否被目标标题"完全匹配"（作为子串出现）。

    参数:
        heading: 个人周报一级标题（如 "同步"、"定位支持"、"Slt bug支持"）
        sections_config: config.json 中的 sections 数组

    返回:
        (section_id, section_title, subsection_id_or_None, matched_title)
        未匹配时 section_id 为 "?"
    """
    tokens = _tokenize_cjk(heading)

    # 停用词列表：过于通用的词降权（得分减半）
    STOP_WORDS = {'支持', '开发', '测试', '验证', '版本', '维护', '问题', '功能', '方案', '设计', '实现', '进行', '相关', '更新'}

    # 构建目标列表: [(section_id, section_title, subsection_id_or_None, target_title), ...]
    # 同时包含 subsection 和 section 大标题
    targets = []
    for sec in sections_config:
        sid = sec['id']
        stitle = sec['title']
        # section 大标题（subsection_id=None 表示匹配到大节）
        targets.append((sid, stitle, None, stitle))
        # subsection 标题
        for sub in sec.get('subsections', []):
            targets.append((sid, stitle, sub['id'], sub['title']))

    # 对每个 target 计算匹配得分
    best_score = 0
    best_match = ("?", "", None, heading)

    for sid, stitle, subid, target_title in targets:
        score = 0
        target_lower = target_title.lower()
        for token in tokens:
            token_lower = token.lower()
            if len(token) < 2:
                continue

            # 英文/数字token：要求词边界匹配，避免 "LS" 误匹配 "slss"
            is_eng = bool(re.match(r'^[a-zA-Z0-9]+$', token) and len(token) >= 2)
            if is_eng:
                # 用词边界正则匹配（前后为非字母数字或字符串边界）
                pattern = re.compile(r'(?:^|[^a-zA-Z0-9])' + re.escape(token_lower) + r'(?:$|[^a-zA-Z0-9])')
                if not pattern.search(target_lower):
                    continue

            if token_lower in target_lower:
                weight = len(token)
                # 停用词降权
                if token in STOP_WORDS:
                    weight = max(1, weight // 2)
                # 英文/数字token加权（更具体）
                if is_eng:
                    weight = weight * 2
                score += weight

        # 完整标题作为子串匹配时大幅加分
        heading_lower = heading.lower()
        if heading_lower in target_lower:
            score += 10

        if score > best_score:
            best_score = score
            if subid is None:
                # 匹配的是 section 大标题
                best_match = (sid, stitle, None, target_title)
            else:
                best_match = (sid, stitle, subid, target_title)

    # 至少要有一个 >=2 字符的token命中，才算有效匹配
    if best_score >= 2:
        return best_match
    else:
        return ("?", "", None, heading)


TAG_RE = re.compile(r'\[([^\]]+)\]')  # [标签]
SECTION_TAG_RE = re.compile(r'\[(\d+(?:\.\d+)?)\s*[-—–\s]+([^\]]+)\]')  # [1.1 标题]

# 无标签文本的分条正则：数字编号 或 中文编号
ITEM_START_RE = re.compile(
    r'^\s*(?:\d+[\.、）)]|（\d+）|[一二三四五六七八九十]+[、．])\s*'
)
# 用于去除编号前缀
STRIP_NUM_RE = re.compile(
    r'^\s*(?:\d+[\.、）)]\s*|（\d+）\s*|[一二三四五六七八九十]+[、．]\s*)'
)


def parse_tagged_items(text: str, sections_config: list[dict]) -> list[dict]:
    """
    解析个人周报文本：先按一级标题切分，再将每个标题块匹配到项目周报的二级标题。

    新逻辑（v2）：
      1. 用 extract_headings() 提取一级标题及内容
      2. 用 match_heading() 将标题匹配到最相关的 subsection
      3. 对内容行较多的块，按子编号（a/b/c 或 1/2/3）拆分为多条
      4. 若无标题 → 回退到关键词匹配

    返回:
      [{section_id, section_title, subsection_id, tag, content, lines}, ...]
    """
    all_items = []

    # 第一步：尝试标题提取
    heading_blocks = extract_headings(text)

    if heading_blocks:
        for block in heading_blocks:
            heading = block['heading']
            lines = block['lines']

            # 允许空内容的标题（标题本身即内容）
            if not lines:
                lines = [heading]

            # 将标题匹配到 subsection
            sid, stitle, subid, matched_subtitle = match_heading(heading, sections_config)

            if sid == "?":
                # 未匹配 → section_id 保持 "?"，后续由 build_structure 归入「支持工作」
                tag = heading
            else:
                tag = heading

            # 如果内容行有子编号（a/b/c 或数字），拆分为独立条目
            sub_items = _split_numbered_lines(lines)
            if sub_items:
                for sub_item in sub_items:
                    all_items.append({
                        "section_id": sid,
                        "section_title": stitle,
                        "subsection_id": subid,
                        "tag": tag,
                        "content": sub_item[0],
                        "lines": sub_item,
                        "matched_subtitle": matched_subtitle  # 新增字段：匹配到的二级标题
                    })
            else:
                all_items.append({
                    "section_id": sid,
                    "section_title": stitle,
                    "subsection_id": subid,
                    "tag": tag,
                    "content": lines[0] if lines else "",
                    "lines": lines,
                    "matched_subtitle": matched_subtitle
                })

    # 第二步：如果没有提取到标题 → 回退到旧版关键词匹配
    if not all_items and text.strip():
        # 构建 alias_map 用于回退
        alias_map = build_alias_map(sections_config)
        return _parse_keyword_items(text, alias_map)

    return all_items


def _split_numbered_lines(lines: list[str]) -> list[list[str]]:
    """将内容行按子编号（如 a. / b. / 1. / bug1：）拆分为多个条目块。"""
    sub_pattern = re.compile(
        r'^\s*'
        r'(?:'
        r'[a-z][\.、）)]\s*'          # a.  b)
        r'|\d+[\.、）)]\s*'            # 1.  2)
        r'|bug\d+[：:]\s*'            # bug1：
        r'|[①②③④⑤⑥⑦⑧⑨⑩][、．]\s*'    # ①
        r')'
    )

    blocks = []
    current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue
        if sub_pattern.match(stripped):
            if current:
                blocks.append(current)
            current = [stripped]
        else:
            if current:
                current.append(stripped)
            else:
                current = [stripped]

    if current:
        blocks.append(current)

    return blocks if len(blocks) > 1 else []


# ─── 无标签关键词匹配 ─────────────────────────────────────

def _split_plain_items(text: str) -> list[list[str]]:
    """将无标签纯文本按编号 / 空行拆分为条目块。"""
    lines = text.split("\n")
    blocks = []
    current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue

        if ITEM_START_RE.match(stripped):
            if current:
                blocks.append(current)
            current = [stripped]
        else:
            current.append(stripped)

    if current:
        blocks.append(current)

    return blocks


def _find_best_section(text: str, alias_map: dict[str, tuple[str, str, str]]) -> tuple[str, str, str | None, list[str]]:
    """
    通过关键词在 alias_map 中匹配最佳 section。
    返回 (section_id, section_title, subsection_id, 命中关键词列表)。
    使用关键词长度加权：长关键词权重更高。
    """
    text_lower = text.lower()

    # 收集命中的 alias: {alias -> (sid, title, subid)}
    hits: dict[str, tuple[str, str, str | None]] = {}
    for alias, (sid, stitle, subid) in alias_map.items():
        if len(alias) >= 2 and alias in text_lower:
            hits[alias] = (sid, stitle, subid)

    if not hits:
        return ("?", "", None, [])

    # 按 section_id 聚合得分
    section_scores: dict[str, tuple[float, str, str | None, list[str]]] = {}
    for alias, (sid, stitle, subid) in hits.items():
        weight = len(alias)  # 长关键词权重高
        prev = section_scores.get(sid, (0, stitle, None, []))
        new_score = prev[0] + weight
        # 优先保留 subsection 匹配
        best_subid = subid or prev[2]
        hit_aliases = prev[3] + [alias]
        section_scores[sid] = (new_score, stitle, best_subid, hit_aliases)

    # 取最高分
    best_sid = max(section_scores, key=lambda k: section_scores[k][0])
    score, stitle, subid, hit_aliases = section_scores[best_sid]
    return (best_sid, stitle, subid, hit_aliases)


def _parse_keyword_items(text: str, alias_map: dict[str, tuple[str, str, str]]) -> list[dict]:
    """无标签回退：将文本拆分为条目块，关键词匹配到分项。"""
    blocks = _split_plain_items(text)
    items = []

    for block in blocks:
        if not block:
            continue
        # 去掉首行编号前缀
        first = STRIP_NUM_RE.sub("", block[0]).strip()

        # 跳过纯标题块：单行且以冒号结尾
        if len(block) == 1 and (first.endswith("：") or first.endswith(":")):
            continue

        full_text = first + " " + " ".join(block[1:])

        sid, stitle, subid, hit_aliases = _find_best_section(full_text, alias_map)
        tag = hit_aliases[0] if hit_aliases else "(未匹配)"

        items.append({
            "section_id": sid,
            "section_title": stitle,
            "subsection_id": subid,
            "tag": tag,
            "content": first,
            "lines": [first] + block[1:]
        })

    return items


# ─── 项目周报读取 ────────────────────────────────────────

def read_previous_report(config: dict, week_label: str) -> str | None:
    """读取上周项目周报"""
    prev_label = prev_week_label(week_label)
    pattern = config["project_report_pattern"].replace("{week_label}", prev_label)
    path = os.path.join(config["project_report_dir"], pattern)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    print(f"⚠ 未找到上周项目周报: {path}")
    return None


# ─── 完成标记扫描 ──────────────────────────────────────────

def scan_completion_markers(report_text: str, markers: list[str]) -> list[dict]:
    """扫描上周周报，找出包含完成标记的行。
    返回 [{line_num, line_text, marker, section}, ...]"""
    if not report_text:
        return []
    lines = report_text.split("\n")
    matches = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for marker in markers:
            if marker in stripped:
                # 向上查找所属分组
                section = ""
                for j in range(i, -1, -1):
                    if lines[j].strip().startswith("###"):
                        section = lines[j].strip("# ").strip()
                        break
                matches.append({
                    "line_num": i + 1,
                    "line_text": stripped,
                    "marker": marker,
                    "section": section,
                })
                break  # 一行只匹配一个 marker

    return matches


def mark_report_for_review(report_text: str, markers: list[str]) -> str:
    """在包含完成标记的行前添加 [待确认删除] 标记。"""
    if not report_text:
        return report_text
    lines = report_text.split("\n")
    result = []
    for line in lines:
        marked = False
        for marker in markers:
            if marker in line:
                # 匹配 "  1. text" 格式
                m = re.match(r'^(\s+)(\d+)\.\s', line)
                if m:
                    result.append(f"{m.group(1)}**[待确认删除]** {m.group(2)}. {line[m.end():]}")
                else:
                    result.append(f"**[待确认删除]** {line}")
                marked = True
                break
        if not marked:
            result.append(line)
    return "\n".join(result)


# ─── 结构体生成 ──────────────────────────────────────────

def build_structure(prev_report: str | None, all_items: list[dict], people: list[dict],
                    sections: list[dict], week_label: str) -> dict:
    """
    构建结构化数据。

    新逻辑（v2）：
      - 未匹配项（section_id="?"）不再单独列为 unmatched，
        而是自动归入 section 3「支持工作」作为新增子节。
      - 每个独特的未匹配 heading 成为 section 3 下的一个子节标题。

    返回:
      {
        week: "2026W18",
        previous_report: "上周全文",
        sections: [
          {id, title, items: [{person, tag, content_lines}, ...], auto_subs: [...]},
          ...
        ],
        unmatched: [],    # v2 中始终为空（已归入 section 3）
        people: [...]
      }
    """
    # 按 section_id 分组
    grouped: dict[str, list[dict]] = {}
    unmatched = []

    for item in all_items:
        sid = item["section_id"]
        if sid == "?":
            unmatched.append(item)
        else:
            grouped.setdefault(sid, []).append(item)

    # 将未匹配项按 heading(tag) 分组
    auto_sub_groups: dict[str, list[dict]] = {}
    for item in unmatched:
        heading = item.get("tag", "(其他)")
        auto_sub_groups.setdefault(heading, []).append(item)

    result_sections = []
    for sec in sections:
        sid = sec["id"]
        sec_items = grouped.get(sid, [])
        auto_subs = []

        # 只对 section 3「支持工作」追加自动子节
        if sid == "3" and auto_sub_groups:
            for heading, items in auto_sub_groups.items():
                # 将多条合并为一条，保留每个人的贡献
                merged_lines = []
                for it in items:
                    person = it.get("person", "?")
                    content = " ".join(it.get("lines", []))
                    if content:
                        merged_lines.append(f"[{person}] {content}")
                auto_subs.append({
                    "heading": heading,
                    "items": items,
                    "merged_lines": merged_lines
                })
            # 将自动子节的内容也加入 sec_items（以特殊标记）
            for auto_sub in auto_subs:
                for it in auto_sub["items"]:
                    it["section_id"] = "3"
                    it["auto_sub_heading"] = auto_sub["heading"]
                sec_items.extend(auto_sub["items"])

        result_sections.append({
            "id": sid,
            "title": sec["title"],
            "items": sec_items,
            "auto_subs": auto_subs if sid == "3" else []
        })

    return {
        "week": week_label,
        "previous_report": prev_report,
        "sections": result_sections,
        "unmatched": [],  # v2: 不再有独立 unmatched
        "people": [p["name"] for p in people]
    }


# ─── 输出 ────────────────────────────────────────────────

def output_json(struct: dict, week_label: str):
    """输出 JSON 到文件和控制台"""
    out_path = f"project_reports/_merge_{week_label}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(struct, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结构化数据已保存到: {out_path}")
    print(f"\n--- JSON 摘要 ---")
    for sec in struct["sections"]:
        if sec["items"]:
            print(f"  [{sec['id']}] {sec['title']}: {len(sec['items'])} 条")
    if struct["unmatched"]:
        print(f"  [未匹配]: {len(struct['unmatched'])} 条")


def output_prompt(struct: dict, week_label: str):
    """生成 LLM prompt 文本并保存到文件"""
    out_path = f"project_reports/_prompt_{week_label}.txt"
    prompt_text = build_prompt_text(struct)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    print(f"\n✅ LLM Prompt 已保存到: {out_path}")
    print(f"   将以下文件内容发送给 LLM 即可获得周报:\n   {out_path}")


def output_dry_run(all_reports: dict, sections_config: list[dict]):
    """仅展示解析结果"""
    for name, text in all_reports.items():
        print(f"\n{'='*60}")
        print(f"📄 {name}")
        print(f"{'='*60}")
        items = parse_tagged_items(text, sections_config)
        for item in items:
            sid = item['section_id']
            subid = item['subsection_id']
            tag = item['tag']
            content = "\n    ".join(item.get("lines", []))
            loc = f"[{sid}]" if subid is None else f"[{sid} → {subid}]"
            match_info = ""
            if sid == "?":
                match_info = " → [支持工作]"
            elif item.get("matched_subtitle"):
                match_info = f" → {item['matched_subtitle']}"
            print(f"  {loc} 标题=[{tag}]{match_info}")
            print(f"    {content}")
        if not items:
            print("  (无标签条目)")


# ─── Prompt 构建 ──────────────────────────────────────────

def build_prompt_text(struct: dict) -> str:
    """构建 LLM prompt 文本（不含文件写入），返回完整 prompt 字符串。"""
    lines = []

    lines.append(f"## 任务：生成 {struct['week']} 项目周报\n")
    lines.append("请根据以下资料，在**上周项目周报**的基础上，更新生成本周项目周报。\n")
    lines.append("### 要求")
    lines.append("1. 保持项目周报的原有结构和编号，以上周周报为模板进行更新")
    lines.append("2. 每个分项下，将多人的同类工作归纳汇总为项目级的进度描述，**不要逐人罗列**")
    lines.append("3. 对每一大项更新完成百分比（如 99%），根据实际进展调整\n")
    lines.append("### 条目合并与精简规则（重要）")
    lines.append("4. **已完成项精简**：已完成的条目保留在结构中，但只写「已完成」或「已交付」，**删除所有历史细节和过程描述**")
    lines.append("5. **同主题合并**：多人/多条涉及同一主题时，合并为一条概括描述（以最完整/最新的那条为准），**禁止一人一条罗列**")
    lines.append("6. **不再活跃的条目**：标记为「暂无新增」，不再重复抄写上期内容；连续多周无进展可删除")
    lines.append("7. **版本号只保留最新**：同时出现新旧版本号时，只保留最新版本，旧版本号及旧描述去掉")
    lines.append("8. **只保留最新进展**：同一问题的多轮跟踪，去掉旧结论和历史过程，只保留当前状态和最新处理方案")
    lines.append("9. **会议/讨论归入相关技术子项**：如 DRX/LS/DS讨论归入低功耗，SLT讨论归入版本维护，不要单独列为「XX会议」条目\n")
    lines.append("### 编号与归属规则")
    lines.append("10. 条目精确归属到最相关的**子标题**下，不要笼统归到大类或创建独立子项")
    lines.append("11. 保留长期任务的标题和编号不变")
    lines.append("12. 语言风格：简洁、技术性、每项以动词开头（如「定位至」「完成」「支持」「发布」）")
    lines.append("13. **自动归类子节**：本周汇总中标注「（自动归类）」的条目，在周报中作为「支持工作」下的新子节（如（5）XXX），标题使用自动归类的 heading 名称，内容归纳汇总后写入\n")

    lines.append("---\n")
    lines.append("## 上周项目周报（作为模板基础）\n")
    if struct["previous_report"]:
        lines.append(struct["previous_report"])
    else:
        lines.append("（无上周周报，请按结构新建）")

    lines.append("---\n")
    lines.append("## 本周个人周报汇总（已按分项归类）\n")

    for sec in struct["sections"]:
        if sec["items"]:
            lines.append(f"\n### [{sec['id']}] {sec['title']}")
            # 如果有自动生成的子节，先列出
            auto_subs = sec.get("auto_subs", [])
            if auto_subs:
                for auto_sub in auto_subs:
                    lines.append(f"\n（自动归类）**{auto_sub['heading']}**：")
                    for it in auto_sub["items"]:
                        person = it.get("person", "?")
                        content = "\n    ".join(it.get("lines", []))
                        lines.append(f"  - **{person}**: {content}")

            # 原有匹配项
            regular_items = [it for it in sec["items"] if not it.get("auto_sub_heading")]
            for item in regular_items:
                person = item.get("person", "?")
                content = "\n    ".join(item.get("lines", []))
                lines.append(f"- **{person}** [{item['tag']}]: {content}")

    if struct["unmatched"]:
        lines.append(f"\n### [未匹配] 以下条目未能自动匹配到分项")
        for item in struct["unmatched"]:
            person = item.get("person", "?")
            content = "\n    ".join(item.get("lines", []))
            lines.append(f"- **{person}** [{item['tag']}]: {content}")

    lines.append("\n---\n")
    lines.append(f"请基于以上内容生成 {struct['week']} 的项目周报。只输出周报的 markdown 正文，不要输出任何解释性文字。\n")

    return "\n".join(lines)


# ─── LLM API 调用 ─────────────────────────────────────────

DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_RETRIES = 3


def call_deepseek_api(prompt: str, api_key: str, api_base: str = DEFAULT_API_BASE,
                      model: str = DEFAULT_MODEL) -> str:
    """调用 DeepSeek API 生成周报，含指数退避重试。"""
    if OpenAI is None:
        print("❌ 请先安装 openai SDK: pip install openai")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=api_base)

    for attempt in range(MAX_RETRIES):
        try:
            print(f"🤖 正在调用 DeepSeek API (第 {attempt + 1}/{MAX_RETRIES} 次)...")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个专业的技术周报撰写助手。请严格按照模板格式输出，保持技术语言的准确性和简洁性。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=8192,
            )
            content = response.choices[0].message.content
            print(f"✅ API 调用成功 (tokens: {response.usage.total_tokens})")
            return content

        except Exception as e:
            print(f"⚠ API 调用失败 (第 {attempt + 1} 次): {e}")
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                print(f"   {wait} 秒后重试...")
                time.sleep(wait)
            else:
                print("❌ 已达最大重试次数，生成失败。")
                raise


def output_md(struct: dict, week_label: str, api_key: str,
              api_base: str = DEFAULT_API_BASE, model: str = DEFAULT_MODEL,
              save_prompt: bool = False, completion_markers: list[str] | None = None):
    """调用 LLM 生成本周项目周报 .md 文件。"""
    prompt = build_prompt_text(struct)

    # 扫描上周报告中的已完成条目，追加到 prompt
    if completion_markers and struct.get("previous_report"):
        matches = scan_completion_markers(struct["previous_report"], completion_markers)
        if matches:
            lines = ["\n---\n", "## ⚠ 上周周报已完成条目识别\n"]
            lines.append("以下条目在上周报告中标记为已完成，请在新周报中酌情删除或标记为「已完成归档」（勿保留原文）：\n")
            for m in matches:
                lines.append(f"- [{m['section']}] {m['line_text'][:80]}")
            lines.append(f"\n共 {len(matches)} 条待处理。")
            prompt += "\n".join(lines)
            print(f"🔍 上周报告中识别到 {len(matches)} 条已完成条目，已写入 prompt。")

    if save_prompt:
        prompt_path = f"project_reports/_prompt_{week_label}.txt"
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        print(f"📝 Prompt 已保存到: {prompt_path}")

    md_content = call_deepseek_api(prompt, api_key, api_base, model)

    out_path = f"project_reports/小组周报_{week_label}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"\n✅ 本周项目周报已生成: {out_path}")


# ─── 主入口 ──────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="项目周报自动化脚本")
    parser.add_argument("--week", "-w", help="指定周标签，如 2026W18，默认自动计算")
    parser.add_argument("--config", "-c", default="config.json", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅解析，不生成")
    parser.add_argument("--output", "-o", choices=["json", "prompt", "md", "both", "all"],
                        default="both", help="输出格式: json/prompt/md/both/all")
    parser.add_argument("--api-key", help="DeepSeek API key (默认从 DEEPSEEK_API_KEY 环境变量读取)")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="API base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型名称")
    parser.add_argument("--save-prompt", action="store_true",
                        help="生成 md 时同时保存 prompt 文件")
    parser.add_argument("--review", action="store_true",
                        help="扫描上周周报中的已完成条目，标记并生成 _review.md 供审核")
    parser.add_argument("--diff", action="store_true",
                        help="对比本周与上周周报，列出内容未变更的条目")
    parser.add_argument("--prune", metavar="IDS",
                        help="删除指定条目，多个ID用逗号分隔，如 1.2.b,1.4.b,4.1")
    args = parser.parse_args()

    # 确定周标签
    if args.week:
        week_label = args.week
    else:
        week_label = get_week_label(date.today())

    print(f"📅 当前周: {week_label}")

    # 加载配置
    if not os.path.exists(args.config):
        print(f"❌ 配置文件不存在: {args.config}")
        sys.exit(1)

    config = load_config(args.config)

    # 读取个人周报
    all_reports = read_individual_reports(config)
    if not all_reports:
        print("❌ 没有找到任何个人周报文件")
        sys.exit(1)

    print(f"👥 已读取 {len(all_reports)} 份个人周报")

    # 解析：按一级标题匹配到项目周报二级标题
    all_items = []
    for name, text in all_reports.items():
        items = parse_tagged_items(text, config["sections"])
        for item in items:
            item["person"] = name
        all_items.extend(items)
        match_count = sum(1 for it in items if it["section_id"] != "?")
        total = len(items)
        print(f"   {name}: {match_count}/{total} 条匹配成功", end="")
        if match_count < total:
            unmatched_headings = set(
                it["tag"] for it in items if it["section_id"] == "?"
            )
            print(f"  ⚠ 未匹配: {', '.join(unmatched_headings)}", end="")
        print()

    if args.dry_run:
        output_dry_run(all_reports, config["sections"])
        return

    # 读取上周项目周报
    prev_report = read_previous_report(config, week_label)

    # --review 模式：扫描已完成条目
    markers = config.get("completion_markers", [])
    if args.review:
        if prev_report and markers:
            matches = scan_completion_markers(prev_report, markers)
            if matches:
                print(f"\n{'='*60}")
                print(f"🔍 上周周报已完成条目扫描 — 共 {len(matches)} 处匹配")
                print(f"{'='*60}")
                for m in matches:
                    ctx = m["line_text"][:70]
                    print(f"  L{m['line_num']:3d}  [{m['marker']}]  {m['section'][:35]}")
                    print(f"         {ctx}")
                # 保存标记版
                marked = mark_report_for_review(prev_report, markers)
                prev_label = prev_week_label(week_label)
                review_path = f"project_reports/小组周报_{prev_label}_review.md"
                with open(review_path, "w", encoding="utf-8") as f:
                    f.write(marked)
                print(f"\n📝 标记版已保存: {review_path}")
                print(f"   请打开该文件确认，删除已完成条目后重新运行脚本。")
            else:
                print("✅ 未发现明显已完成条目。")
        else:
            print("⚠ 无上周周报或未配置 completion_markers，跳过扫描。")
        return

    # --diff / --prune 模式：对比与筛选（独立模式，不触发生成）
    if args.diff or args.prune:
        report_path = get_report_path(config, week_label)
        new_text = None

        if args.diff:
            if not os.path.exists(report_path):
                print(f"❌ 未找到本周周报: {report_path}")
                print(f"   请先运行 --output md 生成本周周报")
                sys.exit(1)
            with open(report_path, 'r', encoding='utf-8') as f:
                new_text = f.read()

            prev_text = read_previous_report(config, week_label)
            if not prev_text:
                print("❌ 未找到上周周报，无法对比")
                sys.exit(1)

            new_struct = parse_report_to_structure(new_text)
            old_struct = parse_report_to_structure(prev_text)
            unchanged = diff_report_items(new_struct, old_struct)

            if unchanged:
                print(f"\n{'='*60}")
                print(f"📋 与上周周报对比 — {len(unchanged)} 条内容未变更")
                print(f"{'='*60}\n")
                for item in unchanged:
                    print(f"  [{item['id']}] {item['text']}")
                print(f"\n💡 使用 --prune ID1,ID2,... 删除指定条目")
            else:
                print("✅ 所有条目均有更新。")

        if args.prune:
            if new_text is None:
                if not os.path.exists(report_path):
                    print(f"❌ 未找到本周周报: {report_path}")
                    sys.exit(1)
                with open(report_path, 'r', encoding='utf-8') as f:
                    new_text = f.read()

            remove_ids = set(x.strip() for x in args.prune.split(','))

            # 备份原文件
            backup_path = report_path.replace('.md', '_backup.md')
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(new_text)

            new_struct = parse_report_to_structure(new_text)
            pruned = prune_report_structure(new_struct, remove_ids)
            new_text = structure_to_markdown(pruned)

            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(new_text)

            print(f"\n✅ 已删除 {len(remove_ids)} 个条目，更新至: {report_path}")
            print(f"   备份保存至: {backup_path}")

        return

    # 构建结构化数据
    struct = build_structure(prev_report, all_items, config["people"],
                             config["sections"], week_label)

    # 输出 JSON
    if args.output in ("json", "both", "all"):
        output_json(struct, week_label)

    # 输出 Prompt
    if args.output in ("prompt", "both", "all"):
        output_prompt(struct, week_label)

    # 输出 md（调用 LLM）
    if args.output in ("md", "all"):
        api_cfg = config.get("api", {})
        api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or api_cfg.get("key", "")
        api_base = args.api_base or api_cfg.get("base", DEFAULT_API_BASE)
        api_model = args.model or api_cfg.get("model", DEFAULT_MODEL)
        if not api_key:
            print("❌ 未设置 DeepSeek API key。请通过以下方式之一设置:")
            print("   - config.json: 添加 \"api\": {\"key\": \"sk-xxx\"}")
            print("   - 环境变量: set DEEPSEEK_API_KEY=sk-xxx")
            print("   - 命令行参数: --api-key sk-xxx")
            sys.exit(1)
        output_md(struct, week_label, api_key, api_base, api_model,
                  save_prompt=args.save_prompt,
                  completion_markers=config.get("completion_markers", []))

    if args.output not in ("md", "all"):
        print(f"\n📋 下一步:")
        print(f"   将生成的 prompt 文件或 JSON 交给 LLM，即可获得 {week_label} 的项目周报。")
        print(f"   或使用 --output md 直接调用 DeepSeek API 生成最终周报。")


if __name__ == "__main__":
    main()


