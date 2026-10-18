# Phase8第一批设计证据（2026-10-17）

本目录是当前v6的只读诊断和设计证据，尚无v7修复/真实调用/浏览器成绩。设计及实施计划见[45号](../../45-Phase8统一证据契约设计与实施计划.md)。

- site-audit.json：现场HEAD、指纹/118证据核对、迁移/配置存在性边界。
- replay-v6.py、replay-results.json：重新执行43号纯函数回放；只读公开冻结快照，无HTTP/LLM/用户库调用。
- scoped-baseline.log：既有五份专项80通过，不是全量工程或质量PASS。
- jobs.json：本轮任务账本；尚无写入Worker。

复现：在项目根目录运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend backend/.venv/bin/python docs/evaluations/phase8-v7-design/replay-v6.py --output-dir /tmp/insightflow-phase8-design-replay-unique`；输出目录必须不存在旧replay-results.json。此脚本断言当前v6缺陷，后续修复后不要求恢复缺陷来通过。

本机work保存243文件基线、Git完整差异及私有保护清单；不复制密钥或其内容到本目录。旧42号成绩保留历史，当前Phase8 PARTIAL/Research质量FAIL，不提交不push。
