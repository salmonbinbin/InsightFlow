# Phase8 v6只读根因证据

本目录是43号交接的诊断证据，不是修复或新一轮真实AI/工程验收。42号最后代码和118份原归档保持不变。

- `original-phase8-task.txt`：原详细任务逐字归档；与后续授权冲突时以后续授权和真实证据为准。
- `replay.py` / `replay-results.json` / `replay.log`：读取冻结官方快照及实际纯函数，复现逐句数量门、Windows范围漏检、契约不一致、analysis/版本支持缺口、#9候选回退、来源政策误判、中文查询丢词、下游known风险只校验ID。PASS表示这些诊断断言成立，不表示产品修复通过。无HTTP/LLM/数据库调用。
- `failure-distribution.json`：冻结18题的错误事件分类；一轮可同时触发多类，不能加成失败Run数量。
- `readonly-audit.json`：233文件前后不变、旧归档SHA一致、Git暂存为空；本轮未重新全量测试或真实AI。
- `sanitization.json` / `manifest.json`：已知密钥扫描及本目录SHA256。

从项目根目录独立回放，使用一个不存在结果文件的新输出目录：

```bash
PYTHONPATH=backend UV_OFFLINE=1 uv run --project backend python docs/evaluations/phase8-v6-diagnosis/replay.py --output-dir work/phase8-supervision/next-diagnosis-replay
```

不要把输出目录设为已冻结归档，脚本会拒绝覆盖已有replay-results.json。纯函数重放不能替代最后代码后的SQLite/隔离PG/迁移/前端/真实模型/官方原文/浏览器门槛。
