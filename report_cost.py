#!/usr/bin/env python3
"""把一次 eval 运行的 GPU 电费折算出来（口径：只算 serving 期间 GPU 电费）。

用法：
  # 1) 先在另一个终端开着功率采样（与 bench_tiers.sh 相同格式）
  #    nvidia-smi --query-gpu=timestamp,power.draw,memory.used,utilization.gpu \
  #      --format=csv,noheader,nounits -l 1 > /tmp/eval-power.csv
  # 2) 跑 eval；3) 用采样窗口的平均功率 × 运行墙钟折算
  python3 report_cost.py --results results/run-local1.json \
      --power-csv /tmp/eval-power.csv --wall-seconds 900

成本口径与 vllm-serving-card 一致：kWh = W×s / 3.6e6，元 = kWh × 电价。
只算 GPU 电费，不含整机/折旧/机房/网络。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def cost_from_window(watts: list[float], *, wall_seconds: float, price_cny_per_kwh: float) -> dict:
    """窗口平均功率 × 墙钟 → kWh 与人民币。缺输入一律 raise（fail-closed）。"""
    if not watts:
        raise ValueError("功率窗口为空：采样器没开或窗口切错了")
    if wall_seconds <= 0:
        raise ValueError(f"墙钟时间必须为正数，收到 {wall_seconds}")
    mean_w = sum(watts) / len(watts)
    kwh = mean_w * wall_seconds / 3.6e6
    return {"mean_power_w": mean_w, "samples": len(watts), "wall_seconds": wall_seconds,
            "kwh": kwh, "cny": kwh * price_cny_per_kwh}


def read_power_csv(path: str) -> list[float]:
    """nvidia-smi csv,noheader,nounits 格式：第 2 列是 power.draw（W）。"""
    watts: list[float] = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                watts.append(float(row[1]))
            except ValueError:
                continue  # 表头或空行
    if not watts:
        raise ValueError(f"{path} 里没有可解析的功率行")
    return watts


def wall_from_results(results_path: str) -> float:
    """结果文件里没存墙钟时，退而用 run 起止 mtime 之差（粗口径，会被显式标注）。"""
    p = Path(results_path)
    if not p.exists():
        raise ValueError(f"结果文件不存在: {p}")
    return max((p.stat().st_mtime - 0) , 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="eval 运行的 GPU 电费折算")
    ap.add_argument("--results", required=True, help="run_agent 落盘的结果 JSON（含 elapsed_s 与 trace_file）")
    ap.add_argument("--power-csv", required=True)
    ap.add_argument("--wall-seconds", type=float, default=None,
                    help="eval 运行的墙钟秒数；缺省用结果 JSON 里的 elapsed_s（run_agent 记录）")
    ap.add_argument("--electricity", type=float, default=0.6, help="电价 元/kWh")
    ap.add_argument("--json-out", default=str(ROOT / "results" / "eval-cost.json"))
    args = ap.parse_args()

    d = json.loads(Path(args.results).read_text(encoding="utf-8"))
    rs = d.get("results") or []
    if not rs:
        raise SystemExit("results 文件里没有任务结果")
    wall = args.wall_seconds if args.wall_seconds is not None else d.get("elapsed_s")
    if not wall or wall <= 0:
        raise SystemExit("结果 JSON 缺 elapsed_s，必须显式传 --wall-seconds")
    watts = read_power_csv(args.power_csv)
    c = cost_from_window(watts, wall_seconds=wall, price_cny_per_kwh=args.electricity)
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rs)
    out = {
        "backend": d.get("model", "?"),
        "tasks": len(rs),
        **c,
        "electricity_cny_per_kwh": args.electricity,
        "total_tokens": total_tokens,
        "cny_per_1k_tokens": c["cny"] / max(total_tokens / 1000, 1e-12),
        "口径": "只算 GPU 电费；serving 常驻功率按采样窗口均值；不含整机/折旧/机房",
    }
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"任务 {out['tasks']} | 窗口均值 {c['mean_power_w']:.1f} W（{c['samples']} 采样） | "
          f"墙钟 {c['wall_seconds']:.0f}s | {c['kwh']:.6f} kWh | ¥{c['cny']:.4f} | "
          f"¥{out['cny_per_1k_tokens']:.4f}/1k tokens")
    print(f"已写 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
