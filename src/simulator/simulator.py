import math, random
from datetime import timedelta, datetime
CRUISE_ALT_M=20.0; VTOL_DRAIN_PER_SEC=0.67; CRUISE_DRAIN_PER_SEC=0.0136; CLIMB_SECS=60.0
BASE_LAT,BASE_LON=8.4304,99.9631
PHASES={
 "rest":{"next":"takeoff","duration":None,"alt":"ground","speed":0.0,"drain":None},
 "takeoff":{"next":"transit_out","duration":60,"alt":"climb","speed":0.5,"drain":"vtol"},
 "transit_out":{"next":"survey","duration":240,"alt":"hold","speed":18.0,"drain":"cruise"},
 "survey":{"next":"transit_back","duration":4800,"alt":"hold","speed":10.0,"drain":"cruise"},
 "transit_back":{"next":"landing","duration":240,"alt":"hold","speed":18.0,"drain":"cruise"},
 "landing":{"next":"rest","duration":60,"alt":"descend","speed":0.5,"drain":"vtol"},
}
def noise(m): return random.gauss(0,m)
def move_position(lat,lon,sp,hd,dt):
    d=sp*dt; dlat=(d*math.cos(math.radians(hd)))/111320.0
    dlon=(d*math.sin(math.radians(hd)))/(111320.0*math.cos(math.radians(lat)))
    return lat+dlat,lon+dlon
def done_by_time(s,cfg): return cfg["duration"] is not None and s["elapsed_in_phase"]>=cfg["duration"]
def step_state(state,dt=1.0):
    s=dict(state); cfg=PHASES[s["phase"]]
    if cfg["drain"]=="vtol": s["battery_vtol_pct"]=max(0.0,s["battery_vtol_pct"]-VTOL_DRAIN_PER_SEC*dt)
    elif cfg["drain"]=="cruise": s["battery_cruise_pct"]=max(0.0,s["battery_cruise_pct"]-CRUISE_DRAIN_PER_SEC*dt)
    p=min(1.0,s["elapsed_in_phase"]/CLIMB_SECS)
    if cfg["alt"]=="climb": s["altitude_m"]=CRUISE_ALT_M*p
    elif cfg["alt"]=="descend": s["altitude_m"]=CRUISE_ALT_M*(1.0-p)
    elif cfg["alt"]=="hold": s["altitude_m"]=CRUISE_ALT_M+noise(0.3)
    else: s["altitude_m"]=0.0
    s["ground_speed_mps"]=max(0.0,cfg["speed"]+noise(0.3))
    s["lat"],s["lon"]=move_position(s["lat"],s["lon"],s["ground_speed_mps"],s["heading_deg"],dt)
    s["satellite_count"]=max(0,10+int(noise(1))); s["hdop"]=0.9+abs(noise(0.2))
    s["elapsed_in_phase"]+=dt; s["t"]+=dt
    if done_by_time(s,cfg): s["phase"]=cfg["next"]; s["elapsed_in_phase"]=0.0
    return s
def state_to_row(s,drone_id,mission_id,ts):
    return {"timestamp":ts,"drone_id":drone_id,"mission_id":mission_id,"flight_mode":s["phase"],
     "battery_vtol_pct":round(s["battery_vtol_pct"],2),"battery_cruise_pct":round(s["battery_cruise_pct"],2),
     "lat":round(s["lat"],6),"lon":round(s["lon"],6),"altitude_m":round(s["altitude_m"],2),
     "ground_speed_mps":round(s["ground_speed_mps"],2),"satellite_count":s["satellite_count"],"hdop":round(s["hdop"],2)}
def generate_mission(drone_id,mission_id,start_time,initial_state=None):
    state=initial_state or {"phase":"takeoff","battery_vtol_pct":100.0,"battery_cruise_pct":100.0,
     "elapsed_in_phase":0.0,"t":0,"lat":BASE_LAT,"lon":BASE_LON,"heading_deg":45,
     "altitude_m":0.0,"ground_speed_mps":0.0,"satellite_count":10,"hdop":0.9}
    rows=[]; t=start_time; steps=0; MAX=6000
    while state["phase"]!="rest":
        rows.append(state_to_row(state,drone_id,mission_id,t))
        state=step_state(state); t=t+timedelta(seconds=1); steps+=1
        if steps>=MAX: break
    return rows
IDLE_START_HOUR,IDLE_END_HOUR,IDLE_INTERVAL_MIN=6,18,30
def generate_idle(drone_id,date,mission_id=None):
    rows=[]; slots=int((IDLE_END_HOUR-IDLE_START_HOUR)*60/IDLE_INTERVAL_MIN)
    for i in range(slots):
        t=date.replace(hour=IDLE_START_HOUR,minute=0,second=0,microsecond=0)+timedelta(minutes=i*IDLE_INTERVAL_MIN)
        s={"phase":"rest","battery_vtol_pct":100.0,"battery_cruise_pct":100.0,
           "lat":BASE_LAT+noise(0.00001),"lon":BASE_LON+noise(0.00001),"altitude_m":0.0,
           "ground_speed_mps":0.0,"satellite_count":max(0,10+int(noise(1))),"hdop":0.9+abs(noise(0.2))}
        rows.append(state_to_row(s,drone_id,mission_id or f"IDLE_{drone_id}",t))
    return rows
DRONES=["A","B","C"]
def get_daily_roles(di):
    sh=di%3; r=DRONES[sh:]+DRONES[:sh]; return r[0],r[1],r[2]
