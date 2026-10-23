# OpenClaw Drone Analytics

ระบบ monitoring และ fault analytics สำหรับฝูงโดรน โดยใช้ข้อมูลจำลอง (synthetic)
จุดขายหลัก: แยก **เฉลย** (`injected_anomalies` = ความผิดปกติที่ฉีดเข้าไปเอง) ออกจาก
**สิ่งที่ระบบตรวจเจอ** (`alerts`) เพื่อวัด precision/recall ของตัวตรวจจับได้จริง

## สถานะ

| เฟส | งาน | สถานะ |
|---|---|---|
| 3 | Simulator + ฉีด anomaly 4 แบบ | เสร็จ |
| 4 | Ingestion API (FastAPI + Pydantic) + SQLite | เสร็จ |
| 5 | Analytics: rule-based + ML (Isolation Forest) + precision/recall | ถัดไป |
| 6–9 | Dashboard, OpenClaw agent (read-only), Docker + CI, writeup | วางแผน |

## โครงสร้าง

```
openclaw-drone-analytics/
├── src/
│   ├── simulator/           # สร้างข้อมูลจำลอง 3 ลำ + สภาพรบกวน (ลม, multipath) + ฉีด anomaly
│   ├── api/                 # เฟส 4: FastAPI รับ telemetry เป็นชุด
│   │   ├── models.py            # สัญญาข้อมูล (Pydantic): กฎตรวจแต่ละคอลัมน์
│   │   └── main.py              # POST /telemetry/batch, GET /health
│   ├── db/                  # เฟส 4: SQLite
│   │   ├── schema.sql           # 5 ตาราง (drones, missions, telemetry_logs, injected_anomalies, alerts)
│   │   ├── database.py          # เชื่อมต่อ + เปิด foreign keys ทุกครั้ง
│   │   └── repository.py        # เขียน telemetry: ตรวจ mission + นับข้อมูลซ้ำ
│   └── ingest/              # เฟส 4: สคริปต์โหลดข้อมูล
│       ├── load_reference.py    # drones + missions
│       ├── load_ground_truth.py # เฉลย (แยกจาก API โดยตั้งใจ)
│       └── send_telemetry.py    # ส่ง CSV ผ่าน API เป็นชุด
├── tests/                   # pytest: API, ฐานข้อมูล, end-to-end
└── data/                    # CSV + drone.db (สร้างตอนรัน ไม่เข้า git)
```

## ติดตั้ง (ครั้งแรก)

ต้องมี Python 3.10 ขึ้นไป บน Ubuntu ต้องมีแพ็กเกจ venv ด้วย (`sudo apt install python3.10-venv`)

```bash
git clone git@github.com:ChatKh/openclaw-drone-analytics.git
cd openclaw-drone-analytics
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

## รันทั้งเส้น: สร้างข้อมูล → เก็บลงฐานข้อมูล

```bash
source .venv/bin/activate

# 1) สร้างข้อมูลจำลอง (CSV 4 ไฟล์ใน data/)
python -m src.simulator.run                                    # ชุด dev: seed 42, 15 วัน
python -m src.simulator.run --seed 7 --days 30 --out data/test  # ชุด test: เก็บไว้วัดผลรอบสุดท้าย

# 2) โหลดข้อมูลอ้างอิง แล้วโหลดเฉลย
python -m src.ingest.load_reference
python -m src.ingest.load_ground_truth

# 3) เปิด server (terminal ที่ 1)
uvicorn src.api.main:app

# 4) ส่ง telemetry ผ่าน API (terminal ที่ 2)
python -m src.ingest.send_telemetry
```

ผลที่คาด: `sent=162360 accepted=162360 duplicates=0 rejected=0 (33 batches)`
ส่งซ้ำได้อย่างปลอดภัย รอบสองจะได้ `accepted=0 duplicates=162360`

หน้าทดลอง API: http://127.0.0.1:8000/docs
ฐานข้อมูลอยู่ที่ `data/drone.db` (เปลี่ยนได้ด้วยตัวแปร `DRONE_DB_PATH`)

## หลักการออกแบบที่สำคัญ

- **schema คือสัญญา** แหล่งข้อมูลเปลี่ยนได้ (simulator วันนี้, ArduPilot SITL ทีหลัง) โดย pipeline ไม่ต้องแก้
- **ingestion ตรวจรูปแบบ ไม่ตรวจความผิดปกติ** ค่าที่ถูกฉีด (เช่น ความสูง 45 m) ยังเป็นข้อมูลที่ถูกต้อง การตรวจจับเป็นงานเฟส 5
- **แถวเสียแถวเดียวไม่ทำให้ทั้งชุดล้ม** แถวดีถูกเก็บ แถวเสียถูกส่งคืนพร้อมเหตุผล
- **ข้อสอบมีทั้งง่ายและก้ำกึ่ง** ความแรงของ anomaly ถูกสุ่มให้คร่อม threshold ตรวจจับ บางตัวจึงจับยากโดยตั้งใจ ค่าที่ฉีดจริงถูกบันทึกใน `params`
- **มีสภาพรบกวนที่ปกติแต่ดูผิดปกติ** ลมกระโชก (~35% ของเที่ยวบิน: ความสูงแกว่ง, แบตวูบชั่วคราวแล้วกลับ, VTOL หมดเร็วขึ้น) และ GPS multipath ใต้ทรงพุ่ม (~30%: hdop สูงขึ้นช่วงสั้นๆ) ไม่อยู่ในเฉลย ตัวตรวจจับที่ดีต้องไม่เตือนเพราะสิ่งเหล่านี้ บันทึกไว้ใน `conditions.csv` เพื่อวิเคราะห์ผลเท่านั้น
- **ปรับบนชุด dev วัดผลบนชุด test** ชุด test (seed อื่น) ไม่ถูกดูระหว่างปรับ threshold เพื่อไม่ให้ตัวเลขดีเกินจริง
- **ตัวตรวจจับมองไม่เห็นเฉลย** เฉลยโหลดด้วยสคริปต์แยก และตาราง missions ไม่มีคอลัมน์ที่บอกว่าภารกิจไหนโดนฉีด
