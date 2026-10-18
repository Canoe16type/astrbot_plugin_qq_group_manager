## 改了什么

<!-- 一句话说清这个 PR 做了什么 -->

## 为什么

<!-- 关联 issue（如有）：Closes #123 -->

## 怎么验证的

- [ ] `python3 tools/check_metadata.py`
- [ ] `python3 tools/selftest_lexicon.py`
- [ ] `python3 tools/selftest_variant.py`
- [ ] `python3 tools/selftest_mod_integration.py`
- [ ] 在真实 AstrBot 环境里跑过（请注明版本与平台）

## 自查清单

- [ ] 没有提交大词库（只允许 `lexicons/ad.json` / `lexicons/abuse.json`）
- [ ] 新增 / 更换词库时已更新 `lexicons/SOURCES.md`
- [ ] 新增配置项已写 `description` + `default`，并在 README 配置说明补了说明
- [ ] 面向用户的话术走 `_conf_schema.json` 的 `templates`，没有硬编码
- [ ] 行为变更已在 `CHANGELOG.md` 记录
- [ ] 没有引入第三方依赖或构建步骤
- [ ] 提交内容里没有本机绝对路径、AppID / AppSecret / openid
