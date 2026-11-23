"""处罚回执的“依据句”：把判定结果翻译成可公示的群规描述。"""

from __future__ import annotations


def basis_detail(host, group: str, result: dict) -> str:
    """返回 `群规第X条「名称」` 或维度名（语义/关键词/多重维度）。

    规则条号按当前群规顺序实时计算，不写入规则本身，拖拽排序后仍然正确。
    """
    rule_name = str(result.get("rule_name") or "").strip()
    if not rule_name:
        dim = str(result.get("basis") or "")
        try:
            return host.mod.get_dim_names(group).get(dim, dim or "多重维度")
        except Exception:
            return dim or "多重维度"

    rule_id = str(result.get("rule_id") or "")
    idx = 0
    try:
        for i, rule in enumerate(host.mod.get_rules(group), 1):
            if rule_id and str(rule.get("id") or "") == rule_id:
                idx = i
                break
            if not rule_id and str(rule.get("name") or "") == rule_name:
                idx = i
                break
    except Exception:
        idx = 0
    return f"群规第{idx}条「{rule_name}」" if idx else f"群规「{rule_name}」"
