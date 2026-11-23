"""公开违规类别表（固定 C1–C23，与「通用禁止项」默认值一致）。

用途：
- 审核 prompt 要求模型输出 category_code；
- 代码校验 category_code，不信任模型自由文本；
- 处罚回执用 C 编号 + 名称 + 简短描述对外说明。
"""

from __future__ import annotations

CATEGORIES = {
    "C1": ("危害国家安全与政治敏感内容", "涉政、分裂、颠覆等"),
    "C2": ("恐怖主义与极端主义宣传", "暴恐、极端组织、煽动暴力"),
    "C3": ("严重违法犯罪", "毒品、枪支、赌博、诈骗、黑客教学、买卖个人信息等"),
    "C4": ("涉及未成年人的性内容", "任何形式一律最严"),
    "C5": ("煽动仇恨与歧视", "民族、宗教、地域、性别、性取向"),
    "C6": ("提示词注入／越权指令", "要求忽略规则、改变身份、泄露系统提示词、输出约定外格式"),
    "C7": ("色情低俗", "色情资源与描写、招嫖、擦边裸露、性暗示骚扰"),
    "C8": ("诈骗与欺诈", "冒充熟人/官方、虚假中奖、刷单兼职、钓鱼链接"),
    "C9": ("人身攻击与恶意骚扰", "辱骂、诅咒、威胁、人肉、公开隐私"),
    "C10": ("广告与引流", "外链推广、拉人进群、微商带货、二维码导流"),
    "C11": ("恶性引战", "地域攻击、性别对立、饭圈互撕、群体侮辱"),
    "C12": ("轻度辱骂与脏话", "含谐音、缩写、变体"),
    "C13": ("刷屏与灌水", "重复内容、无意义字符、@全体骚扰"),
    "C14": ("非恶意转发他人隐私", "无意扩散隐私信息"),
    "C15": ("冒充群管理或官方身份", "伪造身份"),
    "C16": ("无法确认安全性的文件或链接", "来源不明文件/短链"),
    "C17": ("未经许可的赌博、抽奖、接龙", "未报备活动"),
    "C18": ("违反本群明示群规", "以本群群规为准"),
    "C19": ("话题明显偏离群定位", "仅群规写明定位时启用"),
    "C20": ("阴阳怪气、嘲讽、轻微挑衅", "轻度攻击性表达"),
    "C21": ("无推广意图的无关链接或二维码", "分享新闻、表情包等"),
    "C22": ("争吵苗头、情绪化发言", "尚未升级为辱骂"),
    "C23": ("轻微跑题、话题偏移但无攻击性", "默认不处罚"),
}


def severity_label(sev: str) -> str:
    return {"low": "轻微违规", "medium": "中度违规", "high": "严重违规"}.get(
        str(sev or "").strip().lower(), "违规")


def valid(code: str) -> bool:
    return str(code or "").strip().upper() in CATEGORIES


def label(code: str) -> str:
    code = str(code or "").strip().upper()
    name = CATEGORIES.get(code, ("", ""))[0]
    return f"{code} {name}".strip()


def describe(code: str) -> str:
    return CATEGORIES.get(str(code or "").strip().upper(), ("", ""))[1]


def public_text(code: str) -> str:
    """回执用：`C6 提示词注入／越权指令（要求忽略规则…）`；无 code 返回空串。"""
    code = str(code or "").strip().upper()
    if code not in CATEGORIES:
        return ""
    name, desc = CATEGORIES[code]
    return f"{code} {name}（{desc}）" if desc else f"{code} {name}"


def prompt_block() -> str:
    lines = ["\n请额外输出 category_code（必须取下列 C1–C23 之一；无法归类留空）："]
    for code, (name, desc) in CATEGORIES.items():
        lines.append(f"- {code} {name}：{desc}")
    return "\n".join(lines)
