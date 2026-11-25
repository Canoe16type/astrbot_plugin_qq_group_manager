# 参与贡献

感谢你愿意帮忙。这个插件不复杂，但有几个**硬规矩**，先看完再动手能省双方很多时间。

## 环境准备

> 插件运行在 **AstrBot 的解释器**（官方发行版为 Python 3.12），不是你本机的 Python。
> 本机是 3.11 只影响编辑器/工具，不要用它跑插件测试或部署。


> **当前版本（1.10.0）**：**只接受缺陷修复与测试补充**（重点：消息模板迁移、判定类别、记录存储、许可证/来源标注）。
> 新功能建议先开 issue 讨论，不直接提 PR。

```bash
git clone https://github.com/Canoe16type/astrbot_plugin_qq_group_manager.git
cd astrbot_plugin_qq_group_manager
# 用 AstrBot 自带解释器跑测试（缺 astrbot 依赖时导入会失败）：
#   Linux/macOS 常见路径：~/.local/share/uv/tools/astrbot/bin/python
python -m pip install pyyaml         # 仅元数据校验脚本需要；插件本体零第三方依赖
```

把目录放进 AstrBot 的 `plugins/` 下，或直接在仓库里跑离线自检（不需要 AstrBot 实例）：

```bash
python3 tools/check_metadata.py          # 元数据 / 配置 / 版本号一致性
# 开发工具已拆到独立仓库（https://github.com/Canoe16type/astrbot_plugin_qq_group_manager_devtools）：
#   git clone 后设置 QGM_PLUGIN_DIR 指向本插件目录，再跑 selftest_lexicon / selftest_variant / ...
# 本仓库只保留 tools/check_metadata.py（版本/徽章/实测版本校验）
python -m unittest discover -s tests -v     # 当前 419 例：指令别名 / 审核加固 / 撤销 / 存储 / 智能分级 / 话术 / 缓存
node --check pages/dashboard/app.js         # 动过面板必跑
```

测试文件速查：

| 文件 | 覆盖 |
| --- | --- |
| `test_no_legacy_alias.py` | 1.6.0 起旧指令别名确实已移除（防止兼容层被加回来） |
| `test_mod_hardening.py` | 原文留存、并发闸门、超时、反馈样本 |
| `test_mod_image_review.py` | 引用消息的图片维度、拆条投递、纯图片兜底 |
| `test_quote_nested.py` | 嵌套引用 / 同分歧义 / 超窗降级 |
| `test_revoke.py` | 撤销权限、幂等、解禁三态、按 @用户撤销、历史查询 |
| `test_verdict_store.py` | JSONL 存储、迁移、条数上限、摊还裁剪、序号规则、警告 0.5 权重 |
| `test_smart_penalty.py` | 档位动作、前科升档/加刑、硬阈值、低置信警告带、试跑一致性 |
| `test_recent_cache.py` | 最近消息缓存落盘与恢复 |
| `test_variant_sync.py` | 词库开关后变体索引重建、撤销致歉模板 |
| `test_common_ban_default.py` | 通用禁止项默认值与输出契约一致性 |
| `test_rules_transfer.py` | 群规导出/转移的存储行为 |
| `test_member_tombstone.py` | 退群档案删除后不被重复事件重建（墓碑 + 事件去重） |
| `test_log_context_lint.py` | 核心链路日志必须带 `_ctx()` 上下文（防回退） |
| `test_log_concise.py` | 简洁日志模式：过程日志降 DEBUG、后果日志保留 |
| `test_log_coverage.py` | 日志上下文覆盖率：入口/指令/面板接口必须绑定上下文 |
| `test_panel_html_structure.py` | 面板 DOM 结构：div 闭合、页签 section 同级（防止删 DOM 把页面弄坏） |

**提 PR 前至少跑通前两个**，CI 会跑全部。

## 目录速览

| 路径 | 作用 |
| --- | --- |
| `main.py` | 插件入口：指令、事件、WebAPI 路由 |
| `moderation.py` | 审核核心：判定、处罚计划、累犯、待办、存储（`mod_verdicts.jsonl` / `recent_msgs.json`） |
| `lexicon.py` / `lexicons/` | 关键词层与内置词库 |
| `variant.py` / `data/` | 变体检测（拼音表 + 形近字组） |
| `qq_api.py` | QQ 官方 API 封装 |
| `pages/dashboard/` | 管理面板（单页 HTML，无构建步骤） |
| `docs/` | `GUIDE.md` 完整手册（README 只留快速上手，细节都在这里） |
| `tools/` | 仅保留 `check_metadata.py`（版本/徽章/实测版本校验）；自检/基准/构建/预览脚本在 [devtools 仓库](https://github.com/Canoe16type/astrbot_plugin_qq_group_manager_devtools) |
| `tests/` | 行为探针（改动欢迎链路时用它对比前后 trace）；`test_join_approval.py` 覆盖入群审批的判定与去重，`test_join_request_buttons.py` 覆盖申请事件、通知合并与按钮审批 |

## 词库相关（重点）

- **不要提交大词库**。仓库只随包 `lexicons/ad.json` 与 `lexicons/abuse.json` 两个最小集，
  色情 / 涉政 / 涉毒 / 违法犯罪 / 赌博 / 涉枪涉爆 / 暴恐七类已在 `.gitignore` 里，
  需要的人用 [devtools](https://github.com/Canoe16type/astrbot_plugin_qq_group_manager_devtools) 的 `build_lexicons.py` 生成。
- 新增内置词库或换上游，**必须同步更新 `lexicons/SOURCES.md`**（来源 + 协议 + 生成方式），否则 PR 不合并。
- 词条内容只做审核用途，PR 里不要贴出具体词条清单。

## 代码约定

- Python 3.11+，只用标准库与 AstrBot 自带依赖，**不新增第三方包**（需要的话先开 issue 讨论）。
- 中文注释、中文用户话术；面向用户的话术走 `_conf_schema.json` 的 `templates`，别硬编码在逻辑里。
- **判定与处罚必须走 `_mod_plan()`**：它是真实执行与试跑的唯一决策来源，不要在别处再拼一份参数
  （历史上两份逻辑漂移导致过「试跑说警告、实际不处罚」）。
- **用户可见文案一律走模板**：全局默认在 `qgm/kernel/template_defaults.py`，
  `_conf_schema.json` 与面板「设置 → 消息模板」使用同一来源；按群覆盖已移除。不要硬编码在逻辑里；
  带 `@` 的模板默认走 markdown，否则 `<qqbot-at-user>` 不会被解析。
- **新增群级配置**要同时改四处：`moderation.DEFAULT_SETTINGS`、面板 `index.html` 的 `s-<key>`、
  `app.js` 的 `S_KEYS` 与数值裁剪、`docs/GUIDE.md` 配置说明；插件级配置改 `_conf_schema.json`。
- 涉及审核判定 / 变体检测 / 撤销 / 存储的改动，**必须**补 `tests/` 用例（见上表），并跑全量 419 例。
- 改存储层时注意：记录**不常驻内存**、写盘要节流/摊还、序号在「清空后归零、有记录时单调」。
- 不引入构建步骤（面板保持单文件 HTML 可直接打开）。

## 提交与 PR

- 提交信息用 `type: 摘要` 形式（`feat` / `fix` / `docs` / `chore` / `refactor`），一条提交做一件事。
- PR 描述里写清：**改了什么、为什么、怎么验证的**（跑了哪些自检）。
- 涉及行为的改动请在 `CHANGELOG.md` 对应版本（当前为 `1.10.0`）里按「新增 / 修复 / 调整」加条目；
  文档改动同步 README（简洁）、`docs/GUIDE.md`（详细）、`CHANGELOG`（变更记录）三处。
- PR 描述请附：变更摘要、影响面（判定/处罚/面板/存储）、跑过的测试与结果、需要人工验证的点
  （例如 markdown `@` 在真实群的渲染）。

## 合规红线

插件用于群管理和内容拦截，不接受以下方向的贡献：

- 采集、上传、外传群成员消息或凭据；
- 绕过平台风控、伪造事件、批量操作他人账号；
- 把词库用于生成或传播违规内容。

详见 [SECURITY.md](SECURITY.md) 与 [lexicons/DISCLAIMER.md](lexicons/DISCLAIMER.md)。
