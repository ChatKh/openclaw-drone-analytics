from datetime import datetime, timedelta
import random
from src.simulator.simulator import generate_mission, generate_idle, get_daily_roles, DRONES
from src.simulator.anomaly_orchestrate import plan_anomaly, apply_anomaly

def build_drones():
    """ตาราง dimension: drones (นิ่ง อ้างอิง)"""
    return [{"drone_id":d, "model":"VTOL-QuadPlane-X8", "firmware":"v1.0",
             "battery_health_pct":100, "payload_type":"camera",
             "max_flight_time_min":90, "status":"active"} for d in DRONES]

def generate_all(num_days=15, base_date=datetime(2026,6,1), seed=42):
    rng = random.Random(seed); random.seed(seed)
    all_rows, injected, missions = [], [], []
    results = {"success":0,"failed":0,"anomaly_missions":0,"errors":[]}
    aid = 0
    for day in range(num_days):
        day_num = day+1
        date = base_date + timedelta(days=day)
        morning, evening, standby = get_daily_roles(day)
        flights = [(morning,"AM",date.replace(hour=8)), (evening,"PM",date.replace(hour=15))]
        for drone_id, slot, start in flights:
            mid = f"D{day_num:04d}_{drone_id}_{slot}"
            try:
                rows = generate_mission(drone_id, mid, start)
                has_anom = False
                spec = plan_anomaly(rows, rng)
                if spec:
                    aid += 1
                    mag_rng = random.Random(f"{seed}:{mid}")  # magnitude only; same missions get hit
                    rec = apply_anomaly(rows, spec, aid, drone_id, mid, mag_rng)
                    if rec: injected.append(rec); results["anomaly_missions"]+=1; has_anom=True
                all_rows.extend(rows); results["success"]+=1
                missions.append({"mission_id":mid,"drone_id":drone_id,"role":slot,
                    "start_time":rows[0]["timestamp"],"end_time":rows[-1]["timestamp"],
                    "planned_duration_min":90,"actual_duration_min":round(len(rows)/60,1),
                    "mission_status":"completed","has_injected_anomaly":has_anom})
            except Exception as e:
                results["failed"]+=1; results["errors"].append({"mission":mid,"error":str(e)})
                print(f"[FAIL] {mid}: {e}")
        # ลำสำรอง
        idle_mid = f"D{day_num:04d}_{standby}_IDLE"
        all_rows.extend(generate_idle(standby, date, idle_mid))
        missions.append({"mission_id":idle_mid,"drone_id":standby,"role":"standby",
            "start_time":date.replace(hour=6),"end_time":date.replace(hour=18),
            "planned_duration_min":0,"actual_duration_min":0,
            "mission_status":"standby","has_injected_anomaly":False})
    return all_rows, injected, missions, results
