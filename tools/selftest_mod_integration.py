#!/usr/bin/env python3
"""变体检测 + 待办 + 复犯累进 的集成自测（离线）。

跑法:
    python3 tools/selftest_mod_integration.py

分两层：
  A 层只依赖插件自身模块（词库/审核存储），任何环境都能跑；
  B 层要 import main.py（会 import astrbot），拿不到 astrbot 时自动跳过 B 层。
"""

from __future__ import annotations

import glob
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(BASE))

from _plugin_import import load  # noqa: E402

lex_lib = load("lexicon")
moderation = load("moderation")
variant_lib = load("variant")

FAILED: list[str] = []
SKIPPED: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILED.append(msg)


def skip(msg: str) -> None:
    print("  ⏭ 跳过：" + msg)
    SKIPPED.append(msg)


def find_astrbot_site() -> str:
    """定位 AstrBot 站点包目录（可移植）。

    顺序：已能 import astrbot → 环境变量 ASTRBOT_SITE → 当前 sys.path →
    本机 site-packages → 常见虚拟环境路径。找不到返回空串，调用方自行跳过。
    """
    try:
        import astrbot  # noqa: F401

        return os.path.dirname(os.path.dirname(os.path.abspath(astrbot.__file__)))
    except Exception:
        pass
    env = os.environ.get("ASTRBOT_SITE", "").strip()
    cands = [env] if env else []
    cands += [p for p in sys.path if p]
    try:
        import site as _site

        cands += list(_site.getsitepackages())
        cands.append(_site.getusersitepackages())
    except Exception:
        pass
    cands += [
        os.path.expanduser("~/.local/share/uv/tools/*/lib/python*/site-packages"),
        os.path.expanduser("~/.venv/lib/python*/site-packages"),
        "/usr/local/lib/python*/site-packages",
        "/usr/lib/python3/dist-packages",
    ]
    pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for p in cands:
        for hit in glob.glob(p):
            if not os.path.isdir(os.path.join(hit, "astrbot")):
                continue
            # 版本不匹配的站点包（如 uv 装的 3.12 包 + 3.11 解释器）import 必炸，跳过
            if "python3." in hit and pyver not in hit:
                continue
            return hit
    return ""


# ---------------------------------------------------------------- A 层
def test_store() -> None:
    print("\n[A1] 复犯前科统计")
    tmp = tempfile.mkdtemp(prefix="mod_int_")
    store = moderation.ModerationStore(tmp)
    now = int(time.time())
    store.verdicts = [
        # 真执行的前科（应计入）
        {"id": 1, "group": "g1", "target": "u1", "violation": True, "executed": True,
         "dry_run": False, "created_at": now - 3600, "category": "ad"},
        # 试运行（不计入）
        {"id": 2, "group": "g1", "target": "u1", "violation": True, "executed": True,
         "dry_run": True, "created_at": now - 3600},
        # 超窗口（不计入）
        {"id": 3, "group": "g1", "target": "u1", "violation": True, "executed": True,
         "dry_run": False, "created_at": now - 40 * 86400},
        # 人工标注误判（不计入）
        {"id": 4, "group": "g1", "target": "u1", "violation": True, "executed": True,
         "dry_run": False, "created_at": now - 100, "feedback": "false_positive"},
        # 别人（不计入）
        {"id": 5, "group": "g1", "target": "u2", "violation": True, "executed": True, "dry_run": False,
         "created_at": now - 100},
        # 同类别的额外前科（按类别统计时才计入）
        {"id": 6, "group": "g1", "target": "u1", "violation": True, "executed": True, "dry_run": False,
         "created_at": now - 200, "category": "abuse"},
    ]
    ok(store.repeat_count("g1", "u1", window_days=30) == 2, "窗口内真执行的前科 = 2（排除试运行/超窗/误判）")
    ok(store.repeat_count("g1", "u1", window_days=30, category="ad") == 1, "按类别过滤 = 1")
    ok(store.repeat_count("g1", "u3", window_days=30) == 0, "陌生成员 = 0")
    ok(store.repeat_count("", "u1") == 0, "缺参数时安全返回 0")

    print("\n[A2] 复犯累进倍率")
    ok(moderation.escalate_seconds(600, 0) == (600, 1.0), "0 前科 -> 原样")
    ok(moderation.escalate_seconds(600, 1) == (1200, 2.0), "1 前科 -> 翻倍")
    ok(moderation.escalate_seconds(600, 2) == (1800, 3.0), "2 前科 -> 三倍")
    ok(moderation.escalate_seconds(600, 99)[0] == 2400, "倍率封顶（默认 4 倍）")
    ok(moderation.escalate_seconds(600, 5, max_multiplier=2.0)[0] == 1200, "自定义上限生效")
    ok(moderation.escalate_seconds(600, 3, cap=700)[0] == 700, "被 max_mute_seconds 截断")
    ok(moderation.escalate_seconds(600, "x") == (600, 1.0), "脏参数不炸")
    ok(moderation.escalate_seconds(600, 1, step=0) == (600, 1.0), "step=0 等于不累进")

    print("\n[A3] 待办聚合")
    store2 = moderation.ModerationStore(tempfile.mkdtemp(prefix="mod_int2_"))
    store2.verdicts = [
        {"id": 11, "group": "g", "violation": True, "executed": False, "dry_run": False,
         "skipped_reason": "置信度 0.5 < 阈值 0.75", "created_at": now, "text": "x", "target": "u"},
        {"id": 12, "group": "g", "violation": False, "executed": False, "dry_run": False,
         "variant_hit": True, "variant": {"method": "pinyin", "strength": "high", "count": 1},
         "variant_recalled": True, "created_at": now, "text": "y", "target": "u"},
        {"id": 13, "group": "g", "violation": True, "executed": True, "dry_run": False,
         "created_at": now, "text": "z"},
        {"id": 14, "group": "g", "violation": True, "executed": False, "dry_run": True,
         "skipped_reason": "试运行模式，未实际执行", "created_at": now},
    ]
    items = store2.todo_items("g")
    kinds = sorted({i["kind"] for i in items})
    ok({i["id"] for i in items} == {11, 12}, f"只收「低置信未处理」与「变体疑似」-> {[i['id'] for i in items]}")
    ok(kinds == ["low_confidence", "variant"], f"两类都在 -> {kinds}")
    ok(store2.todo_count("g") == 2, "待办计数 = 2")
    ok(store2.mark_todo([11, 12], "done", operator="tester") == 2, "标记已处理 2 条")
    ok(store2.todo_count("g") == 0, "标记后待办清零")
    store2.verdicts.append({"id": 15, "group": "g", "violation": True, "executed": False,
                            "dry_run": False, "skipped_reason": "置信度不足", "created_at": now,
                            "todo_state": "disabled"})
    ok(store2.todo_count("g") == 0, "本群关闭待办（todo_state=disabled）时不进队列")

    store3 = moderation.ModerationStore(tmp)
    old_ts = now - 30 * 86400
    store3.verdicts.append({"id": 16, "group": "g", "violation": True, "executed": False,
                            "dry_run": False, "skipped_reason": "置信度不足", "created_at": old_ts,
                            "text": "一个月前的低置信记录"})
    ok(store3.todo_count("g") == 0, "超窗口（30 天前）的历史账不进待办")
    ok(store3.todo_count("g", within_days=0) == 1, "within_days=0 表示不限，老记录可见")
    ok(store3.todo_count("g", within_days=60) == 1, "放宽窗口即能看到")
    ok(store2.mark_todo([999], "done") == 0, "不存在的 id 安全返回 0")

    print("\n[A4] 统计字段")
    st = store2.stats("g")
    ok("todo_open" in st and "variant_hits" in st, f"统计包含新增字段 -> {[k for k in st if 'todo' in k or 'variant' in k]}")


# ---------------------------------------------------------------- B 层
def test_main_shim() -> None:
    print("\n[B1] main.py 的变体扫描接线（无需 AstrBot 运行实例）")
    site = find_astrbot_site()
    if site:
        sys.path.insert(0, site)
    try:
        main_lib = load("main")
    except Exception as e:                                          # noqa: BLE001
        skip(f"拿不到 astrbot 依赖（{e}），B 层跳过")
        return
    import types

    class _Log:
        def info(self, m):      print(f"      [log] {m}")
        def warning(self, m):   print(f"      [warn] {m}")
        def error(self, m):     print(f"      [err] {m}")
        def debug(self, m):     pass

    tmp = tempfile.mkdtemp(prefix="mod_int3_")
    lex = lex_lib.LexiconManager(tmp, plugin_dir=BASE, logger=_Log())
    obj = types.SimpleNamespace(
        logger=_Log(),
        lexicons=lex,
        variant=variant_lib.VariantMatcher(BASE, _Log()),
        _variant_words=None,
        mod=moderation.ModerationStore(tmp),
    )
    scan = main_lib.QQGroupManager._mod_variant_scan
    # 把真实的索引同步方法绑到 shim 上，这样测的是真实接线（含词库字面量 + 内置正则片段）
    obj._variant_sync = types.MethodType(main_lib.QQGroupManager._variant_sync, obj)
    obj._mod_extra_variant_words = main_lib.QQGroupManager._mod_extra_variant_words
    obj._variant_method_name = staticmethod(main_lib.QQGroupManager._variant_method_name)

    # 开关关着 -> 不扫（零开销）
    ok(scan(obj, "g", {"text": "加·微·信 详聊"}, {"variant_check": False}) is None,
       "开关关闭时不产生任何结果")
    # 开关打开 -> 用真实词库扫一遍（写一条自定义关键词，避免依赖词库具体内容）
    settings = {"variant_check": True, "variant_min_chars": 3, "variant_confusable": True,
                "keywords": {"ad": ["代刷单"]}}
    hit = scan(obj, "g", {"text": "前排接袋刷单，需要的私我"}, settings)
    ok(bool(hit) and hit["method"] == "pinyin" and hit["strength"] == "high",
       f"自定义关键词的谐音写法应命中 -> {hit}")
    ok(hit is not None and "match" not in hit and "pattern" not in hit,
       "返回摘要里不含命中的具体词（沿用不展示词条的约定）")
    ok(scan(obj, "g", {"text": "今天天气不错，出去吃饭吧"}, settings) is None,
       "正常句子不命中")
    ok(scan(obj, "g", {"text": ""}, settings) is None, "空文本安全返回 None")


def main() -> int:
    test_store()
    test_main_shim()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   -", f)
        return 1
    print(f"✅ 全部通过（跳过 {len(SKIPPED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
