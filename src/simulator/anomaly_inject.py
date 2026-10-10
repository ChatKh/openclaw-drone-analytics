DEFAULT={"battery_drop_pct":18.0,"gps_sat":3,"gps_hdop":8.0,"spike_alt_m":45.0,"lipo_before_landing":35.0}
def inject_battery(rows,start_idx,duration,drop_pct=DEFAULT["battery_drop_pct"]):
    for i in range(start_idx,len(rows)):
        rows[i]["battery_cruise_pct"]=round(max(0.0,rows[i]["battery_cruise_pct"]-drop_pct),2)
    end=min(start_idx+duration,len(rows)-1)
    return rows[start_idx]["timestamp"],rows[end]["timestamp"],{"drop_pct":drop_pct,"field":"battery_cruise_pct"}
def inject_gps(rows,start_idx,duration,sat=DEFAULT["gps_sat"],hdop=DEFAULT["gps_hdop"]):
    end=min(start_idx+duration,len(rows))
    for i in range(start_idx,end): rows[i]["satellite_count"]=sat; rows[i]["hdop"]=hdop
    return rows[start_idx]["timestamp"],rows[end-1]["timestamp"],{"satellite_count":sat,"hdop":hdop}
def inject_sensor(rows,spike_idx,field="altitude_m",value=DEFAULT["spike_alt_m"]):
    rows[spike_idx][field]=value
    return rows[spike_idx]["timestamp"],rows[spike_idx]["timestamp"],{"field":field,"value":value}
def inject_lipo(rows,level=DEFAULT["lipo_before_landing"]):
    start=next((i for i,r in enumerate(rows) if r["flight_mode"]=="transit_back"),None)
    if start is None: return None
    for i in range(start,len(rows)): rows[i]["battery_vtol_pct"]=min(rows[i]["battery_vtol_pct"],level)
    return rows[start]["timestamp"],rows[-1]["timestamp"],{"vtol_level":level}
