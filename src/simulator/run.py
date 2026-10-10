"""
ตัวรันหลักของ simulator เฟส 3
รัน: python -m src.simulator.run   (จาก root ของโปรเจกต์)
สร้างข้อมูล 15 วัน แล้ว export CSV ลงโฟลเดอร์ data/
"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from src.simulator.fleet import generate_all, build_drones

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")

def write_csv(name, rows):
    if not rows:
        print(f"  {name}: (ว่าง)"); return
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader()
        w.writerows(rows)
    print(f"  {name}: {len(rows)} แถว")

def main():
    os.makedirs(OUT, exist_ok=True)
    telemetry, injected, missions, results = generate_all(num_days=15, seed=42)
    drones = build_drones()
    print("=== สร้างข้อมูลเสร็จ ===")
    print(f"  ภารกิจสำเร็จ {results['success']} | โดน anomaly {results['anomaly_missions']}")
    print("=== export CSV → data/ ===")
    write_csv("telemetry_logs.csv", telemetry)
    write_csv("injected_anomalies.csv", injected)
    write_csv("missions.csv", missions)
    write_csv("drones.csv", drones)

if __name__ == "__main__":
    main()
