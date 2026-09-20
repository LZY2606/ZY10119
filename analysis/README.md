# charset-normalizer 决策链追踪材料

- `ANALYSIS.zh-CN.md` — 完整分析文档（函数/条件引用、fixture 十六进制与预期分支、实现现状标注）
- `diagnose.py` — 可复现诊断脚本（公开 API + 受控只读 instrumentation）
  - `uv run python analysis/diagnose.py`
  - `uv run python analysis/diagnose.py --json analysis/trace.json --huge`
- `trace_tools.py` — 被动包装 `cut_sequence_chunks` / `mess_ratio` 的观察器（原样透传，
  额外以 `threshold=1e9` 复算完整 mess 值，运行后恢复 monkeypatch）
- `fixtures/` — 全部最小输入（小文件，十六进制头见分析文档第 7 节）
- `trace.json` — 最近一次诊断的完整 JSON 快照（生成物，可用脚本随时再生成）

测试：`uv run pytest -q tests/test_decision_trace.py`
