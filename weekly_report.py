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


def parse_tagged_items(text: str, alias_map: dict[str, tuple[str, str, str]]) -> list[dict]:
    """
    解析文本中的带标签条目。
    规则：
      - 以 [标签] 开头的行标记一个新条目的开始
      - 后续行属于该条目，直到遇到下一个 [标签] 或空行分隔
      - 标签可以是 section alias 或 "1.1 标题" 格式
      - 若无标签，回退到关键词匹配模式

    返回:
      [{section_id, section_title, subsection_id, tag, content}, ...]
    """
    items = []
    lines = text.split("\n")
    current = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if current:
                items.append(current)
                current = None
            continue

        # 检查行首是否有标签
        tag_match = TAG_RE.search(stripped)
        if tag_match and tag_match.start() <= 1:  # 行首或仅前面有空白/序号
            # 保存前一个条目
            if current:
                items.append(current)

            tag = tag_match.group(1).strip()
            # 去掉标签部分，剩余为内容
            content_start = tag_match.end()
            rest = stripped[content_start:].strip("：: -—–").strip()

            # 尝试匹配
            key = tag.lower()
            if key in alias_map:
                sid, stitle, subid = alias_map[key]
            else:
                # 尝试 "ID 标题" 格式
                m = SECTION_TAG_RE.match(stripped)
                if m:
                    key2 = m.group(1).lower()
                    if key2 in alias_map:
                        sid, stitle, subid = alias_map[key2]
                    else:
                        sid, stitle, subid = "?", "", None
                else:
                    sid, stitle, subid = "?", "", None

            current = {
                "section_id": sid,
                "section_title": stitle,
                "subsection_id": subid,
                "tag": tag,
                "content": rest,
                "lines": [rest] if rest else []
            }
        else:
            if current:
                if stripped:
                    current["lines"].append(stripped)

    if current:
        items.append(current)

    # 没有标签 → 回退到关键词匹配
    if not items and text.strip():
        return _parse_keyword_items(text, alias_map)

    return items


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


# ─── 结构体生成 ──────────────────────────────────────────

def build_structure(prev_report: str | None, all_items: list[dict], people: list[dict],
                    sections: list[dict], week_label: str) -> dict:
    """
    构建结构化数据：
    {
      week: "2026W18",
      previous_report: "上周全文",
      sections: [
        {id, title, items: [{person, tag, content_lines}, ...]},
        ...
      ],
      unmatched: [{person, content, raw}, ...]
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

    result_sections = []
    for sec in sections:
        sid = sec["id"]
        sec_items = grouped.get(sid, [])
        result_sections.append({
            "id": sid,
            "title": sec["title"],
            "items": sec_items
        })

    return {
        "week": week_label,
        "previous_report": prev_report,
        "sections": result_sections,
        "unmatched": unmatched,
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


def output_dry_run(all_reports: dict, all_items: list[dict], alias_map: dict):
    """仅展示解析结果"""
    for name, text in all_reports.items():
        print(f"\n{'='*60}")
        print(f"📄 {name}")
        print(f"{'='*60}")
        items = parse_tagged_items(text, alias_map)
        for item in items:
            sid = item['section_id']
            subid = item['subsection_id']
            tag = item['tag']
            content = "\n    ".join(item.get("lines", []))
            loc = f"[{sid}]" if subid is None else f"[{sid} → {subid}]"
            print(f"  {loc} 标签=[{tag}]")
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
    lines.append("1. 保持项目周报的原有结构和编号")
    lines.append("2. 每个分项下，将个人周报内容归纳汇总为项目级的进度描述")
    lines.append("3. 对每一大项更新完成百分比（如 99%）")
    lines.append("4. 删除已完成的条目，标记不再活跃的事项为「暂无新增」")
    lines.append("5. 保留长期任务的标题不变")
    lines.append("6. 语言风格与上周周报保持一致：简洁、技术性、每项以动词开头\n")

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
            for item in sec["items"]:
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
              save_prompt: bool = False):
    """调用 LLM 生成本周项目周报 .md 文件。"""
    prompt = build_prompt_text(struct)

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
    alias_map = build_alias_map(config["sections"])

    # 读取个人周报
    all_reports = read_individual_reports(config)
    if not all_reports:
        print("❌ 没有找到任何个人周报文件")
        sys.exit(1)

    print(f"👥 已读取 {len(all_reports)} 份个人周报")

    # 解析带标签条目
    all_items = []
    for name, text in all_reports.items():
        items = parse_tagged_items(text, alias_map)
        for item in items:
            item["person"] = name
        all_items.extend(items)
        match_count = sum(1 for it in items if it["section_id"] != "?")
        print(f"   {name}: {match_count}/{len(items)} 条匹配成功")

    if args.dry_run:
        output_dry_run(all_reports, all_items, alias_map)
        return

    # 读取上周项目周报
    prev_report = read_previous_report(config, week_label)

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
                  save_prompt=args.save_prompt)

    if args.output not in ("md", "all"):
        print(f"\n📋 下一步:")
        print(f"   将生成的 prompt 文件或 JSON 交给 LLM，即可获得 {week_label} 的项目周报。")
        print(f"   或使用 --output md 直接调用 DeepSeek API 生成最终周报。")


if __name__ == "__main__":
    main()
