# -*- coding: utf-8 -*-
"""wb_prompt.py — 网关自有系统提示词模式（panel prompt.go / degrade.go 语义）。

客户端（Codex / Claude Code 等）在 system prompt 里注入固定模板句时，上游内容
审核可能按逐字匹配误杀合法流量。本模组让网关在出站前改写 system/developer
讯息，从源头消灭 system 来源的指纹误报；user/assistant/tool 讯息不动（既有
sanitize 指纹清洗照旧，两层互不替代）。

模式（settings.json 的 prompt.mode，预设 passthrough，五者互斥、只选一个）：
  - passthrough 透传：客户端 system/developer 原样送出（hub 原本的行为）；
  - append 追加到 system：在开头连续 system/developer 区块之后插入一条网关
    提示词，客户端既有讯息逐字保留；
  - custom 全部替换：删除所有 system/developer，只留一条网关提示词；
  - replace_exact 精确替换：保留客户端讯息，把 system/developer 正文里的 A
    逐字替换成 B（多条规则按顺序叠加）；
  - replace_paragraph 整段替换：保留客户端讯息，把 system/developer 正文里
    包含 A 的那个段落整段换成 B（B 留空则删除该段落；每段只认第一条命中规则，
    不做级联）。

「追加 / 全部替换」用上方的提示词正文（prompt.text / prompt.file），
「精确替换 / 整段替换」用替换规则（prompt.replace_rules），互不干涉。

降级（passthrough / append）：内容审核拦截（403）多半是 system 来源的指纹
误杀。此时切到最小中性提示词，直到次日 00:00 CST 重置，并把同一个请求用
中性提示词重试一次（panel degrade.go）。custom / replace_exact /
replace_paragraph 不进降级路径：前者的正文本就是网关自己写的，后两者保留
客户端 system 是用户选它的理由，换成中性提示词与意图相反。
"""

import re
import threading
import time

# 内置默认提示词：用于 custom / append 模式（可用 prompt.file 覆盖）。
DEFAULT_PROMPT = """你是一名工程助手，幫助用戶完成軟件工程任務。

- 先理解代碼與上下文再動手，遵循既有模式與約定。
- 最小改動：只改必要部分，不做無關重構。
- 改動後要驗證：跑測試或構建確認結果，不假設「應該沒問題」。
- 對不確定的事保持誠實：說明不確定並給出驗證路徑，不編造。
- 跟隨用戶語言；簡潔直接，技術術語精確。
- 如實報告失敗與邊界，不掩蓋、不粉飾。
"""

# 降级提示词：刻意极简中性，只用于绕开 system 来源的审核误报。
DEGRADED_PROMPT = ("You are a helpful assistant. Respond in the user's language, "
                   "follow the user's instructions, and be direct and concise.")

VALID_MODES = ("passthrough", "custom", "append",
               "replace_exact", "replace_paragraph")
#: 用「提示词正文」的模式（注入网关自己的 system）。
INJECT_MODES = ("custom", "append")
#: 用「替换规则」的模式（就地改写客户端 system/developer，不注入任何正文）。
REPLACE_MODES = ("replace_exact", "replace_paragraph")

#: 替换规则的条数与单字段长度上限：规则是面板手写的，得有界但不必苛刻。
REPLACE_RULES_MAX = 50
REPLACE_FIELD_MAX = 4096

#: 面板文本框里「旧文本 => 新文本」的分隔符（按首个出现处切分）。
REPLACE_SEPARATOR = "=>"


def normalize_rules(raw):
    """把任意来源的规则归一成 [{"old", "new"}]；坏条目直接丢弃（宽松读）。

    兼容 {"from"/"to"} 与 {"old"/"new"} 两种键名；old 为空/非字符串、或整条
    不是对象的，静默跳过——读取路径要对手改 settings.json 宽容。
    """
    out = []
    for item in (raw or []):
        if not isinstance(item, dict):
            continue
        old = item.get("old", item.get("from"))
        new = item.get("new", item.get("to"))
        if not isinstance(old, str) or not old:
            continue
        if not isinstance(new, str):
            continue
        if len(out) >= REPLACE_RULES_MAX:
            break
        out.append({"old": old, "new": new})
    return out


def parse_rules_text(text):
    """把面板 textarea 的多行文本解析成规则列表。

    一行一条「旧文本 => 新文本」，按首个 => 切分；替换为空串时省略 => 之后
    的内容（"删除模式"）。行内没有 => 的（除首尾空白外为空）忽略。旧文本两端
    的空格会被剥掉——多行文本框里精确表达前后空格不现实，真要匹配空格可用
    settings.json 直接写规则。
    """
    rules = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or REPLACE_SEPARATOR not in line:
            continue
        old, new = line.split(REPLACE_SEPARATOR, 1)
        rules.append({"old": old.strip(), "new": new.strip()})
    return rules


def rules_to_text(rules):
    """规则列表还原成面板 textarea 的多行文本（与 parse_rules_text 互逆）。"""
    return "\n".join("%s %s %s" % (r["old"], REPLACE_SEPARATOR, r["new"])
                     for r in (rules or []))


#: 段落边界：空行（只含空白也算）。带捕获组，split 才把分隔符原文留在结果里。
PARAGRAPH_SPLIT_RE = re.compile(r"(\n[ \t]*\n)")


def replace_text(text, rules):
    """精确替换：按顺序对字符串叠加应用规则；未改动时返回原对象（引用不变）。"""
    if not rules or not isinstance(text, str) or not text:
        return text
    out = text
    for rule in rules:
        if rule["old"] in out:
            out = out.replace(rule["old"], rule["new"])
    return out


def replace_paragraph_text(text, rules):
    """整段替换：命中 A 的那个段落整体换成 B；B 为空串则删掉该段。

    段落以空行分隔。一个段落只认第一条命中的规则，替换结果不会再被后续规则
    二次命中（不做级联，行为可预测）。原文的换行风格逐字保留：只有真的命中
    规则时才重建，未命中返回原对象。
    """
    if not rules or not isinstance(text, str) or not text:
        return text
    parts = PARAGRAPH_SPLIT_RE.split(text)
    seps = parts[1::2]          # 段落之间的空行原文
    paras = parts[0::2]
    matched = []
    for para in paras:
        hit = None
        for rule in rules:
            if rule["old"] in para:
                hit = rule["new"]
                break
        matched.append(hit)
    if all(hit is None for hit in matched):
        return text
    out = []
    for idx, para in enumerate(paras):
        hit = matched[idx]
        if hit == "":           # 删除整段：连同它前面那个分隔符一起消失
            continue
        if out:
            out.append(seps[idx - 1])
        out.append(para if hit is None else hit)
    return "".join(out)


def _map_content(content, fn):
    """对 str 或 OpenAI content part 数组逐段应用 fn；其余类型原样返回。"""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        out = []
        changed = False
        for part in content:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                new_text = fn(part["text"])
                if new_text != part["text"]:
                    p = dict(part)
                    p["text"] = new_text
                    out.append(p)
                    changed = True
                    continue
            out.append(part)
        return out if changed else content
    return content


def apply_replace_content(content, rules):
    """（兼容入口）对 content 做精确替换。"""
    return _map_content(content, lambda t: replace_text(t, rules))


def _map_system_content(messages, rules, fn):
    """对所有 system/developer 讯息的正文应用 fn，其余讯息不动。

    未命中任何规则时返回原列表对象（引用不变），让调用方的 passthrough
    「零改写」语义在测试与日志层面可判别。
    """
    if not rules or not messages:
        return messages
    out = []
    changed = False
    for m in messages:
        if (isinstance(m, dict) and m.get("role") in ("system", "developer")
                and "content" in m):
            new_content = _map_content(m["content"], fn)
            if new_content is not m["content"]:
                m = dict(m)
                m["content"] = new_content
                changed = True
        out.append(m)
    return out if changed else messages


def apply_replacements(messages, rules):
    """精确替换（replace_exact）：A => B 逐字替换 system/developer 正文。"""
    return _map_system_content(messages, rules,
                               lambda t: replace_text(t, rules))


def apply_paragraph_replacements(messages, rules):
    """整段替换（replace_paragraph）：含 A 的段落整段换成 B。"""
    return _map_system_content(messages, rules,
                               lambda t: replace_paragraph_text(t, rules))


def normalize_mode(value):
    """未知模式回退 passthrough（保守默认，不意外改写客户端提示词）。"""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_MODES else "passthrough"


def load_prompt(mode, file_path="", text=""):
    """读取网关提示词：在线正文优先，其次 file，最后内置默认。

    在线正文来自设置页的全局提示词输入框（settings.json 的 prompt.text）。
    它比文件路径更适合容器部署：不必把文件挂进容器，改完即时生效。file 仍
    保留给已有的部署；两者都为空时用内置默认。读档失败抛出，调用方 fail
    open（与从前一致）。
    """
    inline = str(text or "")
    if inline.strip():
        return inline
    path = str(file_path or "").strip()
    if not path:
        return DEFAULT_PROMPT
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def rewrite(messages, system_prompt):
    """删除所有 system/developer，头部插入一条网关 system 讯息。"""
    if not system_prompt:
        return messages
    kept = [m for m in (messages or [])
            if not (isinstance(m, dict)
                    and m.get("role") in ("system", "developer"))]
    return [{"role": "system", "content": system_prompt}] + kept


def append(messages, system_prompt):
    """在开头连续 system/developer 区块之后插入网关提示词，其余讯息不动。"""
    if not system_prompt:
        return messages
    msgs = list(messages or [])
    insert_at = 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("system", "developer"):
            break
        insert_at += 1
    return (msgs[:insert_at]
            + [{"role": "system", "content": system_prompt}]
            + msgs[insert_at:])


def apply_mode(messages, mode, text, degraded=False, replace_rules=None):
    """一次请求只走一条分支：五个模式并列、互斥、各用自己的输入。

    - passthrough：什么都不做（degraded 时换成中性提示词）。
    - append / custom：用「提示词正文」text，不碰客户端已有 system 的字（append）。
    - replace_exact / replace_paragraph：用「替换规则」replace_rules 就地改写
      客户端 system/developer，不注入任何正文；两者都不进降级路径。

    正文与规则不再互相叠加：选了哪个模式，就只有它那一套输入生效，避免
    「规则把网关正文又改了一遍」这种看不见的二次改写。
    """
    mode = normalize_mode(mode)
    if mode == "custom":
        return rewrite(messages, text)
    if mode == "append":
        if degraded:
            return rewrite(messages, DEGRADED_PROMPT)
        return append(messages, text)
    if mode == "replace_exact":
        return apply_replacements(messages, replace_rules)
    if mode == "replace_paragraph":
        return apply_paragraph_replacements(messages, replace_rules)
    if degraded:
        return rewrite(messages, DEGRADED_PROMPT)
    return messages


def next_midnight_cst(now=None):
    """now 之后最近的 Asia/Shanghai 00:00（epoch 秒）。

    用固定 +08:00 计算，不依赖宿主机时区（容器/宿主时区不确定）。
    00:00 整点 → 次日 00:00；23:59 → 几秒后的次日 00:00。
    """
    now = time.time() if now is None else float(now)
    offset = 8 * 3600
    day = int((now + offset) // 86400)
    return float((day + 1) * 86400 - offset)


class DegradeGate(object):
    """进程内存降级窗口：触发后到次日 00:00 CST 为止（不续期）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0

    def active(self, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            return now < self._until

    def trigger(self, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            if now >= self._until:
                self._until = next_midnight_cst(now)

    def reset(self):
        with self._lock:
            self._until = 0.0
