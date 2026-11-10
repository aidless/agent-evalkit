.PHONY: test selftest gate demo clean

# 单元测试（含门禁与成本口径的纯函数测试）
test:
	python3 -m unittest discover -s tests -v

# 评测门禁自检（6 类注入必须被阻断）
selftest:
	python3 run_eval.py --selftest

# 离线回放门禁：不调 LLM，直接对已落盘结果评分
gate:
	python3 run_eval.py --replay results/latest.json --gate 0.80

# 一键演示：测试 → 门禁自检 → 回放门禁 → 本 run 的 trace 摘要
# trace_file 由 run_agent 写进结果 JSON，避免把 traces/ 下别的 run（如回归反证）混进来
demo: test selftest gate
	python3 trace_summary.py "$$(python3 -c "import json;print(json.load(open('results/latest.json'))['trace_file'])")" --results results/latest.json | tail -20

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
