# 参与贡献

感谢你愿意帮忙。这个插件不复杂，但有几个**硬规矩**，先看完再动手能省双方很多时间。

## 环境准备

```bash
git clone https://github.com/Canoe16type/astrbot_plugin_qq_group_manager.git
cd astrbot_plugin_qq_group_manager
python3 -m pip install pyyaml        # 仅校验脚本需要，插件本体零第三方依赖
```

把目录放进 AstrBot 的 `plugins/` 下，或直接在仓库里跑离线自检（不需要 AstrBot 实例）：

```bash
python3 tools/check_metadata.py          # 元数据 / 配置 / 版本号一致性
python3 tools/selftest_lexicon.py        # 词库编译与匹配
python3 tools/selftest_variant.py        # 变体检测（谐音 / 形近 / 插符号）
python3 tools/selftest_mod_integration.py  # 审核链路（无 AstrBot 时自动跳过 B 层）
python3 tools/selftest_admin_register.py   # /管理登记 指令链路
```

**提 PR 前至少跑通前两个**，CI 会跑全部。

## 目录速览

| 路径 | 作用 |
| --- | --- |
| `main.py` | 插件入口：指令、事件、WebAPI 路由 |
| `moderation.py` | 群规审核：判定、待办队列、复犯累进、存储 |
| `lexicon.py` / `lexicons/` | 关键词层与内置词库 |
| `variant.py` / `data/` | 变体检测（拼音表 + 形近字组） |
| `qq_api.py` | QQ 官方 API 封装 |
| `pages/dashboard/` | 管理面板（单页 HTML，无构建步骤） |
| `tools/` | 构建脚本与离线自检（`_plugin_import.py` 负责按目录名导入）；`bench_*.py` 是误杀率 / 绕过率基准，跑一次几秒，用于验证默认值没被调坏 |
| `tests/` | 行为探针（改动欢迎链路时用它对比前后 trace） |

## 词库相关（重点）

- **不要提交大词库**。仓库只随包 `lexicons/ad.json` 与 `lexicons/abuse.json` 两个最小集，
  色情 / 涉政 / 涉毒 / 违法犯罪 / 赌博 / 涉枪涉爆 / 暴恐七类已在 `.gitignore` 里，
  需要的人自己跑 `python3 tools/build_lexicons.py` 生成。
- 新增内置词库或换上游，**必须同步更新 `lexicons/SOURCES.md`**（来源 + 协议 + 生成方式），否则 PR 不合并。
- 词条内容只做审核用途，PR 里不要贴出具体词条清单。

## 代码约定

- Python 3.11+，只用标准库与 AstrBot 自带依赖，**不新增第三方包**（需要的话先开 issue 讨论）。
- 中文注释、中文用户话术；面向用户的话术走 `_conf_schema.json` 的 `templates`，别硬编码在逻辑里。
- 新增配置项要写 `description` 与 `default`，并在 README 的配置说明里补一行。
- 涉及审核判定 / 变体检测的改动，**必须**补对应 `tools/selftest_*.py` 的用例。
- 不引入构建步骤（面板保持单文件 HTML 可直接打开）。

## 提交与 PR

- 提交信息用 `type: 摘要` 形式（`feat` / `fix` / `docs` / `chore` / `refactor`），一条提交做一件事。
- PR 描述里写清：**改了什么、为什么、怎么验证的**（跑了哪些自检）。
- 涉及行为的改动请在 `CHANGELOG.md` 顶部对应版本里加一行（发版前该版本号即「未发布」）。

## 合规红线

插件用于群管理和内容拦截，不接受以下方向的贡献：

- 采集、上传、外传群成员消息或凭据；
- 绕过平台风控、伪造事件、批量操作他人账号；
- 把词库用于生成或传播违规内容。

详见 [SECURITY.md](SECURITY.md) 与 [lexicons/DISCLAIMER.md](lexicons/DISCLAIMER.md)。
