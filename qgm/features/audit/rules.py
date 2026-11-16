"""群规文本解析（纯函数）。"""

from __future__ import annotations

from typing import Optional

from ...kernel.constants import _RULE_SEP_RE
from ...kernel.timefmt import parse_duration

RULE_USAGE_ADD = (
    "用法：/群管 群规 添加 规则名 | 说明 | 时长" + chr(10) +
    "示例：/群管 群规 添加 刷屏 | 连续刷无意义内容 | 10分钟" + chr(10) +
    "竖线用半角「|」或全角「｜」都行；时长可省略，默认 1 小时。"
)
RULE_USAGE_SET = (
    "用法：/群管 群规 设置 规则名 | 说明 | 时长" + chr(10) +
    "示例：/群管 群规 设置 刷屏 | 连续刷无意义内容 | 10分钟" + chr(10) +
    "多条用「;」隔开，规则会追加到本群群规里。"
)

def parse_rule_spec(text: str) -> tuple[str, str, int, Optional[str]]:
    """解析群规的一行式写法，返回 (name, desc, seconds, error)。

    error 非 None 时调用方应回用法提示，而不是硬建一条残缺群规。

    三种写法都认（都是用户真实会发出来的）：
      名｜说明｜时长（全角竖线，帮助里的标准写法）
      名 | 说明 | 时长（半角竖线，手机输入法/英文键盘最常见）
      名 说明 时长（纯空格）
    时长可省略 → 默认 1 小时；只写一段 → 只当规则名。
    """
    raw = (text or "").strip()
    if not raw:
        return "", "", 3600, "empty"
    norm = _RULE_SEP_RE.sub("｜", raw)
    if "｜" in norm:
        seg = [x.strip() for x in norm.split("｜") if x.strip()]
    else:
        seg = [x.strip() for x in norm.split() if x.strip()]
    seconds = 3600
    if len(seg) >= 2:
        tail = parse_duration(seg[-1])
        if tail:
            seconds = int(tail)
            seg = seg[:-1]
    if not seg:
        return "", "", 3600, "empty"
    name = seg[0]
    desc = " ".join(seg[1:]).strip()
    if len(name) > 24:
        return name, desc, seconds, "name_too_long"
    return name, desc, seconds, None
