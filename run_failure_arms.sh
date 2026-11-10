#!/usr/bin/env bash
# 失败驱动闭环（README「失败驱动的修复闭环」节）的复现脚本。
# 需要本机 vLLM 服务（见 README 本地部署命令）+ 网关默认配置。
# 三个臂各自独立，可只跑其一：ARMS="baseline armA armB" bash run_failure_arms.sh
set -euo pipefail
cd "$(dirname "$0")"
ARMS="${ARMS:-baseline armA armB}"
OUT=results/failure-loop
mkdir -p "$OUT"

MODEL_ARGS=(--model Qwen3-4B-AWQ --base-url http://127.0.0.1:8123/v1 --api-key sk-local)

for arm in $ARMS; do
  case "$arm" in
    baseline) TOKENS=2048; PROMPT_PATCH="" ;;
    armA)     TOKENS=4096; PROMPT_PATCH="" ;;
    armB)     TOKENS=2048; PROMPT_PATCH="armB" ;;
    *) echo "unknown arm: $arm"; exit 2 ;;
  esac
  if [ "$PROMPT_PATCH" = "armB" ]; then
    # arm-B：SYSTEM_PROMPT 第 2 条换成「一句反思立即重试」，跑完恢复基线
    python3 - <<'EOF'
import pathlib
p = pathlib.Path("evalkit/agent.py"); s = p.read_text()
s = s.replace('    "2) 工具返回错误文本时，读取错误信息并自行纠正后重试。\\n"',
              '    "2) 工具返回错误文本时，先用一句话说明哪里错了，然后立即用修正后的参数重试；"\n    "不要在思考里重新推导整个表达式。\\n"')
p.write_text(s)
EOF
  fi
  for i in 1 2 3; do
    python3 run_agent.py --task adv-05 "${MODEL_ARGS[@]}" --max-tokens "$TOKENS" \
      --out "$OUT/${arm}-adv05-$i.json" --traces-dir "$OUT" >/dev/null
    python3 -c "import json; r=json.load(open('$OUT/${arm}-adv05-$i.json'))['results'][0]\
; print('${arm} r${i}:', r['ok'], r['final_answer'], 'turns', r['turns'])"
  done
  if [ "$PROMPT_PATCH" = "armB" ]; then
    python3 - <<'EOF'
import pathlib
p = pathlib.Path("evalkit/agent.py"); s = p.read_text()
s = s.replace('    "2) 工具返回错误文本时，先用一句话说明哪里错了，然后立即用修正后的参数重试；"\n    "不要在思考里重新推导整个表达式。\\n"',
              '    "2) 工具返回错误文本时，读取错误信息并自行纠正后重试。\\n"')
p.write_text(s)
EOF
  fi
done
python3 - <<'EOF'
# 自证：三臂 ×3 全部 0/3（负结果成立）；基线与已落盘证据一致
import json, glob
for arm in ("baseline", "armA", "armB"):
    files = sorted(glob.glob(f"results/failure-loop/{arm}-adv05-*.json"))
    if not files:
        continue
    oks = [json.load(open(f))["results"][0]["ok"] for f in files]
    assert not any(oks), f"{arm} 出现通过，与 README 负结果矛盾，需复核"
    print(f"{arm}: {sum(oks)}/{len(oks)} —— 负结果复现")
EOF
