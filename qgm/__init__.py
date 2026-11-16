"""QQ官机群管内部包。

分层规则（重构期间逐步落地，详见 docs/ARCHITECTURE.md）：

- `kernel/`：纯工具与基础设施，禁止 import `features/`、`integrations/` 或 `main`；
- `integrations/`：QQ API、botpy 补丁、日志出口，只能依赖 kernel；
- `features/`：按功能垂直切片，切片之间不得互相 import；
- 根目录 `main.py` 只做入口与薄壳，业务逻辑逐步迁入本包。
"""
