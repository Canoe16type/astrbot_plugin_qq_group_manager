# 架构说明（1.7.0 重构中）

> 目标：**精简、易维护、易加功能**；稳定优先，低配服务器可长期运行，不拖慢 AstrBot 主进程。
> 1.7.0 只做重构与缺陷修复，**不加任何新功能**；所有步骤要求 311+ 测试全程绿。

## 一、基线（1.6.0 / `431e973`）

| 指标 | 基线值 |
| --- | --- |
| `main.py` | **9452 行**，`QQGroupManager` 295 个方法 |
| 最长方法 | `_cmd_report_violation` 222 行 / `_mod_review_llm` 185 行 / `_on_group_member_remove` 178 行 |
| 测试 | 311 例，约 4 s（CI 内含全量） |
| 依赖 | 0 第三方 |
| 发布包 | 567 KB（slim zip，排除 docs/tests/tools/.github） |
| 插件市场包 | tag 归档（含 docs/screenshots/tests，约 1.5 MB） |

## 二、分层规则（硬约束，测试守护）

```
main.py            # 入口与薄壳：装饰器 + 生命周期，不写业务
qgm/
  kernel/          # 纯工具/基础设施：禁止 import features、integrations、main
  integrations/    # QQ API、botpy 补丁、日志出口：只依赖 kernel
  features/        # 垂直切片：mute / approval / audit / members / lexicon / status
  plugin.py        # 装配：把 features 接到 AstrBot（目标）
```

1. **features 之间不得互相 import**；共享能力下沉 kernel，或经 `Deps` 注入。
2. `qgm` 内禁止 `import main`；`main.py` 负责导入 `qgm.plugin`。
3. 业务落盘只走 `qgm/kernel/storage.py` 的原子写，禁止各处 `open(..., "w")`。
4. 日志统一走 `plugin_logger` + 上下文注入，不新增裸 `logging`。
5. `metadata.yaml` 的 `author` / `name` / `version` **保持不变**（市场身份），入口 `main.py`、包根结构不动。

已落地的守卫在 `tests/test_architecture.py`：
- kernel 依赖检查、qgm 禁 import main、原子写唯一实现、`main.py` 行数棘轮（只许下调）。

## 三、分阶段计划

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P0（已完成）** | 基线、ARCHITECTURE、架构守卫、kernel 初建（storage/text） | 测试 311+ 绿，main.py 不再被本地实现占用 |
| **P1（进行中）** | 叶子模块下沉：`qq_api` / `lexicon` / `variant` / `plugin_logger` / `button_kit` → `qgm/integrations`、`qgm/features/lexicon`，保留根目录兼容 shim（qq_api / plugin_logger / lexicon / variant / button_kit 已完成） | 测试绿；import 路径不变 |
| **P2（进行中）** | `audit` 切片：举报 → 决策 → 记录 → 撤销整链迁入 `qgm/features/audit/`（audit 全链 + members 事件 + 撤销 + 全部 Web API 迁出 + 指令路由表 + 指令实现全部迁出（禁言/审批/状态/成员/审核/群规），main 9452→4996（-47%）；入口必须留在 main.py（AstrBot 旧式加载器限制） | `main.py` < 5000 行；决策矩阵测试不变 |
| **P3** | 其余切片 + 指令路由表 + Web router + 配置单一来源 | `main.py` < 100 行；加指令改 2 个文件 |
| **P4（进行中）** | 前端拆分为顺序分片（6 个文件，最大 1115 行）、扩展点（Detector/Action） | 面板冒烟 + 文档示例 |

每阶段单独分支/PR，可独立回滚；打 `1.7.0-alpha.N`，正式版前不更新市场记录。

## 四、性能基线（1.7.0-dev）

在 AstrBot 已加载的环境下测得（devtools `bench_runtime.py`）：

| 指标 | 实测 | 结论 |
| --- | --- | --- |
| 插件自身导入 | **38.1 ms** | 可忽略 |
| 含框架启动导入 | 3607.6 ms | 主要是 AstrBot 自身 |
| 词库初始化 | **0.3 ms** | 无需惰性化 |
| 词库首次编译 | **1.4 ms** | 无需预构建 |
| 处罚决策 p50 / p95 / p99 | **0.008 / 0.011 / 0.02 ms** | 纯计算，可忽略 |

结论：当前架构对低配服务器与 Agent 主流程**没有可测的性能压力**；暂不做词库/拼音表惰性加载（收益 <2ms，风险更高）。

## 五、性能预算（低配服务器）

- 主消息路径**零新增落盘**；统计类走短 TTL 缓存。
- 词库 / 拼音表 / 变体索引**按需构建**，不在启动时全量加载。
- 禁止事件循环内同步阻塞（`requests`、`time.sleep`、大文件读写）；重活 `asyncio.to_thread`。
- 并发预算：模型 3 / 动作 2 / JSONL 追加 1，全部带超时与放弃路径。
- 重构每个阶段对比：启动耗时、常驻内存、单次决策 p95；**退化即停**。

## 五、易加功能（目标形态）

- 检测维度实现 `Detector`（返回 `Evidence`），pipeline 只遍历注册表；
- 处置动作实现 `Action`（warn / recall / mute / todo）；
- 用户侧指令矩阵固定为 `/群管 <子指令>`（解析见 `qgm/commands/parse.py`），子指令映射见 `qgm/commands/router.py`；
- 指令与 Web API 各有一张显式注册表（路径/权限/帮助/处理函数），帮助文案由表生成；
- 新切片模板：`features/<name>/{__init__,service,command,web}.py` + 自有测试。

## 扩展点（Detector / Action）

**新增检测维度**（不改 pipeline）：

```python
from qgm.features.audit import detectors

class SpamScore:
    name = "spamscore"
    async def detect(self, ctx):
        # ctx: group / rep / settings / verdict / host
        score = await some_check(ctx["rep"])
        if score > 0.8:
            return detectors.Evidence(name=self.name, violation=True,
                                      severity="medium", confidence=score,
                                      category="spam", detail="外部风控打分")
        return None

detectors.register(SpamScore())   # 注册即生效
```

规则：
- 注册表默认空 → 零开销、零行为变化；
- 合并**只增强不削弱**（不能把已有违规判成无违规）；
- 单个 detector 异常只记 warning，不影响本次判定。

**追加处置动作**（例如记分、外部审计工单）：

```python
from qgm.features.audit import actions

class AuditTicket:
    name = "ticket"
    async def apply(self, ctx):      # ctx: group / rep / verdict / result / settings / host
        await create_ticket(ctx["result"])
        return {"ok": True}

actions.register(AuditTicket())
```

动作在处罚执行后、落记录前运行；异常隔离，不影响主处罚。

## 六、不做的事

- 不引入 DI 框架 / ORM / 事件总线 / 微服务；
- 不为解耦把代码拆成大量 <200 行的小文件；
- 不在重构 PR 里夹带行为变更（bugfix 单独 commit）；
- 不重写前端框架，不做动态插件加载。
