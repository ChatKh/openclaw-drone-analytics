ANOMALY_TYPES=["battery","gps","sensor","lipo"]
def make_anomaly_record(anomaly_id,drone_id,mission_id,anomaly_type,subtype,start_time,end_time,params):
    assert anomaly_type in ANOMALY_TYPES
    return {"anomaly_id":anomaly_id,"drone_id":drone_id,"mission_id":mission_id,
            "anomaly_type":anomaly_type,"subtype":subtype,"start_time":start_time,"end_time":end_time,"params":params}
