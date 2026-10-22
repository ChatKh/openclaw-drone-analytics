"""ข้อ 3: กลไกฉีดระหว่างปั่น  |  ข้อ 4: สุ่มว่าภารกิจไหนโดนอะไร"""
import src.simulator.anomaly_inject as A
from src.simulator.anomaly_schema import make_anomaly_record

FLIGHT_ANOMALY_RATE = 0.30   # decision: 30% ของภารกิจบินโดน anomaly

def plan_anomaly(rows, rng):
    """ข้อ 4: ตัดสินว่าภารกิจนี้โดนไหม + ชนิดอะไร + ตรงไหน (1 ตัว/ภารกิจ)"""
    if rng.random() > FLIGHT_ANOMALY_RATE:
        return None
    atype = rng.choice(["battery", "gps", "sensor", "lipo"])
    n = len(rows)
    lo, hi = 400, max(401, n - 400)          # ฉีดในช่วง survey เป็นหลัก
    if atype == "battery":
        return {"type":"battery","subtype":"drop","start":rng.randint(lo,hi),"dur":rng.randint(120,600)}
    if atype == "gps":
        return {"type":"gps","subtype":"drift","start":rng.randint(lo,hi),"dur":rng.randint(60,300)}
    if atype == "sensor":
        return {"type":"sensor","subtype":"spike","start":rng.randint(lo,hi),"dur":1}
    return {"type":"lipo","subtype":"insufficient","start":None,"dur":None}

def apply_anomaly(rows, spec, anomaly_id, drone_id, mission_id, mag_rng):
    """ข้อ 3: ลงมือบิดข้อมูลตาม spec + คืน ground truth record
    mag_rng: RNG สำหรับความแรงของ anomaly แยกจาก RNG หลัก
    (เปลี่ยนความแรงได้โดยไม่เปลี่ยนว่าภารกิจไหนโดนฉีด)"""
    if spec["type"] == "battery":
        st, et, p = A.inject_battery(rows, spec["start"], spec["dur"], mag_rng)
    elif spec["type"] == "gps":
        st, et, p = A.inject_gps(rows, spec["start"], spec["dur"], mag_rng)
    elif spec["type"] == "sensor":
        st, et, p = A.inject_sensor(rows, spec["start"], mag_rng)
    else:
        res = A.inject_lipo(rows, mag_rng)
        if res is None: return None
        st, et, p = res
    return make_anomaly_record(anomaly_id, drone_id, mission_id,
                               spec["type"], spec["subtype"], st, et, p)
