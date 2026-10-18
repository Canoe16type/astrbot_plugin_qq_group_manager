# 内置词库来源与数据用途

## 数据用途声明（给云服务商 / 安全审计看的）

本目录下的 `*.json` 是**内容审核用的黑名单词表**，用途只有一个：**在本地把违法违规内容拦下来**。

- **来源公开、可复现**：全部由 `tools/build_lexicons.py` 从下表三个开源项目生成，不含任何自行采集或人工编写的词条；
- **只做本地过滤**：词表仅在本机内存中用于字符串匹配，**不对外发布、不对外提供、不生成任何内容、不上传任何地方**；
- **数据方向相反**：文件里出现这些词，是为了匹配到就撤回 / 禁言，处理动作是**拦截**而不是传播；
- **不随插件提供词条预览的公开接口**：查看词条的接口位于 AstrBot 管理面板内部路由（需登录面板），
  单次最多返回每类 300 条预览；
- **可自行精简**：不需要哪一类，直接删除对应的 `*.json` 文件即可，插件不会重新生成、也不会报错
  （只保留你需要的库，文件体积与数据面同步变小）。

> 同类词表是社区/IM/游戏平台的常规审核组件。若云服务商或安全审计问询，出示本文件即可说明来源与用途。
>
> **完整免责声明见同目录 [`DISCLAIMER.md`](./DISCLAIMER.md)**（用途边界 5 条 + 免责条款 5 条 + 立场声明）。
> 面板出于同一考虑**不展示词条内容**：详情只给类型/条数/词长分布，命中测试只给类型/来源/词长/位置，
> 判定理由与日志不写入命中的具体词。

---

# 内置词库来源

本目录下的 `*.json` **不是手写的**，全部由 `tools/build_lexicons.py` 从公开开源词库生成：

```bash
python3 tools/build_lexicons.py        # 重新生成（联网，缓存 7 天）
python3 tools/build_lexicons.py --offline
```

## 上游项目

| 项目 | 协议 | 地址 |
| --- | --- | --- |
| konsheng/Sensitive-lexicon | MIT | https://github.com/konsheng/Sensitive-lexicon |
| houbb/sensitive-word-data | Apache-2.0 | https://github.com/houbb/sensitive-word-data |
| censor-text/profanity-list | Unlicense (public domain) | https://github.com/censor-text/profanity-list |

## 生成了什么

**随插件分发的只有下面 2 个**（其余类型一律不内置、也不内置任何下载链接）：

| 文件 | 名称 | 违规类型 | 词条 | 默认 | 上游文件 |
| --- | --- | --- | --- | --- | --- |
| `ad.json` | 广告引流 | ad | 120 | 关 | konsheng/Sensitive-lexicon · Vocabulary/广告类型.txt |
| `abuse.json` | 辱骂攻击 | abuse | 286 | 开 | censor-text/profanity-list · list/zh.txt |

选择标准：**误伤最低 + 群管理最常用**。其余的（色情、涉政、涉毒、违法犯罪、赌博、涉枪涉爆、暴恐）
体积大、宽泛词多、合规边界更敏感，不随插件分发，需要时自行生成：

```bash
python3 tools/build_lexicons.py      # 生成全部 9 类到 lexicons/（联网，缓存 7 天）
```

其余 7 类（`porn` / `crime` / `drugs` / `politics` / `gambling` / `weapon` / `violence`）同样由该脚本生成，
默认不在仓库里；生成后在本地查看条数与来源，或直接删掉不想用的文件。

> 自己生成、自己上传的词表由使用者自行负责：插件只做本地匹配与拦截，不对任何第三方词表的内容、准确性、合规性作保证。

## 其他随包数据（非词库）

变体检测依赖两份数据文件，随插件分发，与词库无关：

| 文件 | 内容 | 来源 | 许可 |
| --- | --- | --- | --- |
| `data/pinyin_map.json` | 1.2 万常用汉字的无声调拼音（多音字用 `\|` 分隔） | 由 `tools/build_pinyin_map.py` 用 [pypinyin](https://github.com/mozillazg/python-pinyin) 0.55.0 生成，范围 U+4E00–U+9FFF | pypinyin · MIT |
| `data/variant_confusables.json` | 78 组常见形近字（同组互为近似字形） | 人工整理的通用汉字形近知识，**不含任何违规词** | 随本插件 MIT |

两者都只做字形 / 读音比对，不含词表内容；可按需增删，重新生成用 `python3 tools/build_pinyin_map.py`。

## 使用与合规说明

- 词表仅用于**拦截、审核、过滤**本群违规内容，插件界面不展示词条内容；
- 上游词库由社区维护，可能包含误伤词或漏词：可在管理面板「词典」页启用/停用，
  或上传自己的词库覆盖补充（同名词库按「自定义优先」合并）；
- **除 `ad.json` / `abuse.json` 外的 7 个库都不随仓库分发**（占全部词条 94% 以上，体积与合规风险集中在这里）；需要时运行 `python3 tools/build_lexicons.py` 生成，生成后可正常加载（缺失的词库自动跳过）；
- 协议各不相同（MIT / Apache-2.0 / Unlicense），二次分发时请保留本文件与本表与上游许可证原文。
- 涉毒、暴恐、涉政、违法犯罪等词表默认关闭或按需开启，开启后请在群里先跑试运行。

> 生成时间：2026-10-01 20:12:35
