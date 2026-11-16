## 改动说明

<!-- 一句话说明改了什么、为什么 -->

## 类型

- [ ] 缺陷修复
- [ ] 文档 / 模板
- [ ] 测试补充
- [ ] 重构（无行为变化）

## 验证

- [ ] `python -m unittest discover -s tests`（当前 363 例）
- [ ] `python -m unittest tests.test_loader_harness`（真实构造插件）
- [ ] `python -m pyflakes main.py qgm`（无 undefined name / redefinition）
- [ ] 涉及面板时手动走过对应页签

## 检查项

- [ ] 未提交密钥、openid、群号、成员昵称等敏感信息
- [ ] `metadata.yaml` 的 `author / name / version` 变更符合预期
- [ ] 行为变化已写入 `CHANGELOG.md`
- [ ] 无 `except Exception: pass`（或已按规范标注原因）
