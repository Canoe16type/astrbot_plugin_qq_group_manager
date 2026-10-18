import json, os, shutil, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lexicon as L

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = tempfile.mkdtemp()
mgr = L.LexiconManager(d, plugin_dir=PLUGIN)
ok = lambda c, m: print(("  ✓ " if c else "  ✗ ") + m) or (c or sys.exit(1))

print("1) 内置词库加载与默认开关")
st = mgr.stats()
# 随包只带 ad / abuse 两个最小词库；其余类型可由 tools/build_lexicons.py 自行生成，因此数量断言只做下限检查
ok(st["lexicon_count"] >= 2, f"内置词库 {st['lexicon_count']} 个（随包 2 个，其余可自行生成）")
ok(st["enabled_count"] >= 1, f"默认启用 {st['enabled_count']} 个")
ok(st["pattern_count"] >= 200, f"启用词条 {st['pattern_count']} 条")
ok(st["total_pattern_count"] >= 406, f"词条总数 {st['total_pattern_count']}")
on = {e["id"] for e in mgr.list_lexicons() if e["enabled"]}
ok("builtin-abuse" in on and "builtin-ad" not in on, f"启用集合 {sorted(on)}（辱骂开、广告关）")

print("2) 命中检测")
ok(mgr.hit("你个傻逼") is not None, "辱骂词命中")
ok(mgr.hit("今晚一起看电影吧") is None, "常用词不误伤（电影库默认关）")
ok(mgr.hit("the quick brown fox") is None, "英文正常文本不命中")

print("3) 开关生效")
r_sw = mgr.upload("开关测试库", "电影", fmt="txt")
mgr.set_enabled(r_sw["id"], True)
h = mgr.hit("今晚一起看电影吧")
ok(h and h["pattern"] == "电影", f"打开词库后命中 {h}")
ok(h["source_id"] == r_sw["id"], "命中来源可追溯")

print("4) 白名单")
mgr.allow_add("电影")
ok(mgr.hit("今晚一起看电影吧") is None, "加白名单后不再命中")
ok(len(mgr.allow_words()) >= 1, f"白名单 {sorted(mgr.allow_words())}")
mgr.allow_remove("电影")
ok(mgr.hit("今晚一起看电影吧") is not None, "移除白名单后恢复命中")
mgr.set_enabled(r_sw["id"], False)

print("5) 上传 TXT（自动识别字面量/正则）")
txt = "# 注释行\n[ad]\n加我vx\n加\\s*我\nad: 私聊我领福利\n"
r = mgr.upload("测试词库", txt, fmt="txt")
ok(r["ok"], f"上传成功 {r.get('count')} 条")
e = mgr.get(r["id"])
ok(e["literal_count"] == 2 and e["regex_count"] == 1, f"字面量 {e['literal_count']} / 正则 {e['regex_count']}")
ok(mgr.hit("加我vx") is not None, "字面量命中")
ok(mgr.hit("加  我") is not None, "正则命中（\\s*）")
ok(mgr.hit("私聊我领福利") is not None, "单行类型前缀生效（ad: 私聊我领福利）")

print("6) 上传 CSV / JSON 覆盖")
r2 = mgr.upload("csv库", "类型,规则\nabuse,你个笨蛋\nad,扫码进群\n", fmt="csv")
ok(r2["ok"] and mgr.hit("你个笨蛋") is not None, "CSV 上传并命中")
r3 = mgr.upload("json库", json.dumps({"name": "json库", "match": "literal",
                                      "categories": {"ad": ["限时免费"]}}), fmt="json")
ok(r3["ok"] and mgr.hit("限时免费") is not None, "JSON 上传并命中")
r4 = mgr.upload("json库", json.dumps({"categories": {"ad": ["全新规则"]}}), fmt="json")
ok(not r4["ok"] and "已存在" in r4["error"], "同名冲突被拦下")
r5 = mgr.upload("json库", json.dumps({"categories": {"ad": ["全新规则"]}}), fmt="json",
                mode="overwrite", target_id=r3["id"])
ok(r5["ok"] and mgr.hit("全新规则") is not None, "选择覆盖后生效")
r6 = mgr.upload("追加", "首单优惠", fmt="txt")
r7 = mgr.upload("", "追加内容", fmt="txt", mode="append", target_id=r6["id"])
ok(r7["ok"] and r7["added"] == 1 and mgr.hit("追加内容") is not None, "追加模式生效")

print("7) 内置库保护 / 导出回环")
ok(not mgr.delete("builtin-abuse")["ok"], "内置库不可删")
ok(not mgr.rename("builtin-abuse", "x")["ok"], "内置库不可改名")
exp = mgr.export(r6["id"])
ok(exp["ok"], "可导出")
back = json.loads(exp["content"])
ok(back["categories"] and back["match"] in ("auto", "literal"), "导出内容结构正确")
d2 = tempfile.mkdtemp()
m2 = L.LexiconManager(d2, plugin_dir=PLUGIN)
r8 = m2.upload("回环", exp["content"], fmt="json")
ok(r8["ok"] and m2.hit("首单优惠") is not None and m2.hit("追加内容") is not None, "导出→再导入 词条完整")

print("8) 坏输入容错")
bad = mgr.upload("坏的", "{不是json", fmt="json")
ok(not bad["ok"] and bad.get("errors"), "坏 JSON 被拒并给出原因")
bad2 = mgr.upload("空", "   ", fmt="txt")
ok(not bad2["ok"], "空内容被拒")
r9 = mgr.upload("正则坏", "re: [", fmt="txt")
ok(not r9["ok"], "非法正则被拒")

print("9) 性能（全量词条编译）")
mgr.apply([e["id"] for e in mgr.list_lexicons() if e["source"] == "builtin"])
st = mgr.stats()
t0 = time.time(); mgr._build_compiled(); build_ms = (time.time() - t0) * 1000
texts = ["大家晚上好呀，今天作业写完了吗", "加我vx：abc123 进群领福利 限时秒杀",
         "这个游戏真好玩哈哈哈", "扫码进群免费领红包", "www.example.com/hahaha"]
t0 = time.time()
for _ in range(200):
    for t in texts:
        mgr.hit(t)
per = (time.time() - t0) / 1000 * 1000
ok(per < 3.0, f"{st['pattern_count']} 词条 · 编译 {build_ms:.0f}ms · 单条消息平均 {per:.2f}ms")
print("\n全部通过 ✅")
shutil.rmtree(d, ignore_errors=True); shutil.rmtree(d2, ignore_errors=True)
