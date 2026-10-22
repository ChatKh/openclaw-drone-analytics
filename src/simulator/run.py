"""
ตัวรันหลักของ simulator
รัน (จาก root ของโปรเจกต์):
    python -m src.simulator.run                                   # ชุด dev: seed 42, 15 วัน -> data/
    python -m src.simulator.run --seed 7 --days 30 --out data/test # ชุด test: ไว้วัดผลครั้งสุดท้าย

ชุด dev ใช้ปรับ threshold ได้ ชุด test ห้ามดูระหว่างปรับ ใช้วัดผลรอบสุดท้ายเท่านั้น
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from src.simulator.fleet import build_drones, generate_all


def write_csv(out_dir, name, rows):
    if not rows:
        print(f"  {name}: (ว่าง)")
        return
    with open(os.path.join(out_dir, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  {name}: {len(rows)} แถว")


def main():
    ap = argparse.ArgumentParser(description="สร้างข้อมูลจำลองฝูงโดรน")
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    telemetry, injected, missions, results = generate_all(num_days=args.days, seed=args.seed)
    drones = build_drones()
    print(f"=== สร้างข้อมูลเสร็จ (seed={args.seed}, {args.days} วัน) ===")
    print(f"  ภารกิจสำเร็จ {results['success']} | โดน anomaly {results['anomaly_missions']}")
    print(f"=== export CSV → {args.out}/ ===")
    write_csv(args.out, "telemetry_logs.csv", telemetry)
    write_csv(args.out, "injected_anomalies.csv", injected)
    write_csv(args.out, "missions.csv", missions)
    write_csv(args.out, "drones.csv", drones)


if __name__ == "__main__":
    main()
