## 改了什么

<!-- 一句话说清这个 PR 做了什么；涉及指令/判定/处罚/存储的改动请单独标注 -->

## 为什么

<!-- 关联 issue（如有）：Closes #123 -->

## 影响面

- [ ] 指令 / 参数
- [ ] 判定与处罚（`_mod_plan` / `decide_action`）
- [ ] 撤销与记录存储（`mod_verdicts.jsonl` / `recent_msgs.json`）
- [ ] 面板（前端 / 接口）
- [ ] 文档 / 配置结构

## 怎么验证的

> 用 AstrBot 自带解释器跑（`python3` 常常缺依赖）：

```bash
rm -rf __pycache__ tests/__pycache__
python -m unittest discover -s tests        # 当前 283 例，必须全绿
python tools/check_metadata.py              # 版本/徽章/实测版本一致性
node --check pages/dashboard/app.js         # 动过面板必跑
```

- [ ] 全量测试通过（贴出结果行）
- [ ] 动了配置项：`_conf_schema.json` / `DEFAULT_SETTINGS` / 面板 `S_KEYS` / GUIDE 四处同步
- [ ] 动了用户可见文案：走模板（全局 + 按群），不硬编码
- [ ] 动了指令：同步 README / GUIDE / 插件内帮助 / issue 模板；**不要**把 1.6.0 已移除的旧指令别名加回来
- [ ] 文档三处同步（README 简洁版 / docs/GUIDE.md 详细版 / CHANGELOG）
- [ ] 需要人工验证的点（如 markdown `@` 在真实群的渲染）已在下方说明

## 需要人工验证

<!-- 例如：警告消息的 @ 是否正常渲染；真实群里撤销是否解禁成功 -->
