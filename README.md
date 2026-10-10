# OpenClaw Drone Analytics

ระบบ monitoring & analytics สำหรับ drone fleet โดยใช้ข้อมูลจำลอง (synthetic)
เป้าหมาย: portfolio + ต่อยอดใช้จริงกับสวนไม้มีค่า

## สถานะ: เฟส 3 เสร็จ (Simulator + Anomaly)

## โครงสร้าง
```
openclaw-drone-analytics/
├── src/simulator/       # เฟส 3: ตัวสร้างข้อมูลจำลอง
│   ├── simulator.py         # state machine + step_state + generate_mission
│   ├── fleet.py             # rotation 3 ลำ 15 วัน + generate_all
│   ├── anomaly_schema.py    # โครง injected_anomalies (ground truth)
│   ├── anomaly_inject.py    # ฟังก์ชันฉีด 4 ชนิด
│   ├── anomaly_orchestrate.py  # กลไกฉีด + สุ่มภารกิจ
│   └── run.py               # ตัวรันหลัก
└── data/                # ผลลัพธ์ CSV (สร้างตอนรัน)
```

## วิธีรัน
```bash
cd openclaw-drone-analytics
python3 -m src.simulator.run
```
จะได้ CSV 4 ไฟล์ใน `data/`: telemetry_logs, injected_anomalies, missions, drones

## ผลที่คาด
- ~162,360 แถว telemetry | 45 missions | 3 drones | ~14 anomalies
- FK integrity ครบ (พร้อมเข้า ingestion เฟส 4)
