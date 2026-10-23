"""
Dashboard pages (Phase 6). app.py wires them into a multipage app; tests run each page alone.
"""
import os

import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard import queries as Q

DETECTOR_COLORS = alt.Scale(domain=["rule", "ml"], range=["#2a78d6", "#eb6834"])
SETUP_HELP = """
```bash
python -m src.simulator.run
python -m src.ingest.load_reference && python -m src.ingest.load_ground_truth
uvicorn src.api.main:app            # terminal 1
python -m src.ingest.send_telemetry # terminal 2
python -m src.analytics.rule_detector
python -m src.analytics.ml_detector train && python -m src.analytics.ml_detector detect
```"""

# ---------- data access: one read-only connection per query, cached until the file changes ----------

def _mtime(path: str) -> float:
    return max((os.path.getmtime(p) for p in (path, path + "-wal") if os.path.exists(p)), default=0.0)


@st.cache_data(show_spinner="Reading database...")
def load(name: str, path: str, mtime: float, *args):
    conn = Q.connect_ro(path)
    try:
        return getattr(Q, name)(conn, *args)
    finally:
        conn.close()


def get(name: str, *args):
    path = st.session_state["db_path"]
    return load(name, path, _mtime(path), *args)


# ---------- sidebar ----------

def sidebar() -> None:
    default = os.environ.get("DRONE_DB_PATH", "data/drone.db")
    options = list(dict.fromkeys([default, "data/drone.db", "data/test.db"]))
    labels = {"data/drone.db": "dev set (data/drone.db)", "data/test.db": "test set (data/test.db)"}
    st.sidebar.selectbox("Database", options, key="db_path", format_func=lambda p: labels.get(p, p))
    st.sidebar.caption("Read-only. The dashboard never writes to the database.")


def guard() -> dict | None:
    """Stop the page with a helpful message when there is nothing to show."""
    path = st.session_state["db_path"]
    try:
        c = get("counts")
    except Q.NoDatabase:
        st.info(f"No database at `{path}` yet. Build one first:" + SETUP_HELP)
        return None
    if not c["telemetry_logs"]:
        st.info(f"`{path}` has no telemetry yet. Load it first:" + SETUP_HELP)
        return None
    return c


# ---------- pages ----------

def page_overview() -> None:
    st.title("Fleet overview")
    if not guard():
        return
    o = get("overview")
    cols = st.columns(4)
    cols[0].metric("Flights", o["flights"])
    cols[1].metric("Flight hours", o["flight_hours"])
    cols[2].metric("Rule alerts", o["rule_alerts"])
    cols[3].metric("ML alerts", o["ml_alerts"])
    st.caption(f"Missions from {o['first_day'][:10]} to {o['last_day'][:10]}")

    st.subheader("Health per drone")
    st.dataframe(get("fleet_table"), hide_index=True, use_container_width=True)

    st.subheader("Alerts per drone")
    a = get("alerts_per_drone")
    if a.empty:
        st.info("No alerts yet. Run the detectors (rule_detector, ml_detector detect).")
        return
    chart = alt.Chart(a).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("drone_id:N", title="drone", axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("detector:N", sort=["rule", "ml"]),
        y=alt.Y("sum(alerts):Q", title="alerts"),
        color=alt.Color("detector:N", scale=DETECTOR_COLORS),
        tooltip=["drone_id", "detector", "anomaly_type", "alerts"],
    ).properties(height=260)
    st.altair_chart(chart, use_container_width=True)


def _alert_bands(alerts: pd.DataFrame, legend: bool = True) -> alt.Chart:
    """Shaded alert spans. Each is drawn at least one chart point wide so a 1 s alert is still visible."""
    pad = pd.Timedelta(seconds=Q.BUCKET_S / 2)
    shown = alerts.assign(band_start=alerts["start_time"] - pad, band_end=alerts["end_time"] + pad)
    return alt.Chart(shown).mark_rect(opacity=0.25).encode(
        x="band_start:T", x2="band_end:T",
        color=alt.Color("detector:N", scale=DETECTOR_COLORS,
                        legend=alt.Legend(orient="top", title="alert") if legend else None),
        tooltip=["detector", "anomaly_type", "start_time:T", "end_time:T", "score"],
    )


def page_missions() -> None:
    st.title("Missions")
    if not guard():
        return
    ml = get("mission_list")
    label = {r.mission_id: f"{r.mission_id}   (rule {int(r.rule_alerts)}, ml {int(r.ml_alerts)})"
             for r in ml.itertuples()}
    with_alerts = [r.mission_id for r in ml.itertuples() if r.rule_alerts or r.ml_alerts]
    only_alerts = st.toggle("Only missions with alerts", value=bool(with_alerts))
    ids = with_alerts if only_alerts else list(label)
    if not ids:
        st.info("No mission has alerts yet. Turn the switch off to see every mission.")
        return
    mid = st.selectbox("Mission", ids, format_func=label.get)

    tel = get("mission_telemetry", mid)
    alerts = get("mission_alerts", mid)
    st.caption(f"{len(tel)} points, one per {Q.BUCKET_S} s. Altitude shows the highest reading in each "
               "point so one-second spikes stay visible. Shaded bands are alerts.")

    def panel(y: str, title: str, legend: bool) -> None:
        base = alt.Chart(tel).encode(x=alt.X("time:T", title=None))
        line = base.mark_line(strokeWidth=2, color="#52514e").encode(
            y=alt.Y(f"{y}:Q", title=title, scale=alt.Scale(zero=False)),
            tooltip=["time:T", y, "flight_mode"])
        layers = [_alert_bands(alerts, legend)] if not alerts.empty else []
        layers.append(line)
        st.altair_chart(alt.layer(*layers).properties(height=200), use_container_width=True)

    panel("altitude_max_m", "altitude (m)", legend=True)
    batt = tel.rename(columns={"battery_cruise_pct": "cruise", "battery_vtol_pct": "vtol"}).melt(
        id_vars=["time"], value_vars=["cruise", "vtol"], var_name="battery", value_name="pct")
    bchart = alt.Chart(batt).mark_line(strokeWidth=2).encode(
        x=alt.X("time:T", title=None), y=alt.Y("pct:Q", title="battery (%)"),
        color=alt.Color("battery:N", scale=alt.Scale(domain=["cruise", "vtol"], range=["#1baf7a", "#6250d6"]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=["time:T", "battery", "pct"])
    layers = [_alert_bands(alerts, legend=False), bchart] if not alerts.empty else [bchart]
    st.altair_chart(alt.layer(*layers).resolve_scale(color="independent").properties(height=200),
                    use_container_width=True)
    panel("hdop_max", "hdop (worst)", legend=False)

    st.subheader("Alerts in this mission")
    if alerts.empty:
        st.write("None.")
    else:
        st.dataframe(alerts, hide_index=True, use_container_width=True)


def page_alerts() -> None:
    st.title("Alerts")
    if not guard():
        return
    df = get("alerts_with_agreement")
    if df.empty:
        st.info("No alerts yet. Run the detectors (rule_detector, ml_detector detect).")
        return
    c1, c2, c3 = st.columns(3)
    dets = c1.multiselect("Detector", sorted(df["detector"].unique()), default=sorted(df["detector"].unique()))
    types = c2.multiselect("Type", sorted(df["anomaly_type"].unique()), default=sorted(df["anomaly_type"].unique()))
    drones = c3.multiselect("Drone", sorted(df["drone_id"].unique()), default=sorted(df["drone_id"].unique()))
    view = df[df["detector"].isin(dets) & df["anomaly_type"].isin(types) & df["drone_id"].isin(drones)]
    if st.toggle("Only alerts that one detector raised alone", value=False):
        view = view[~view["other_detector_agrees"]]

    m = st.columns(3)
    both = view[view["other_detector_agrees"]]
    m[0].metric("Alerts shown", len(view))
    m[1].metric("Other detector agrees", len(both))
    m[2].metric("Only one detector", len(view) - len(both))
    st.caption(f"Agree = the other detector raised the same type in the same mission within ±{Q.MATCH_TOL_S} s. "
               "Alerts only one detector raised are where to look first. This page does not use the answer key.")
    st.dataframe(view, hide_index=True, use_container_width=True)


def page_energy() -> None:
    st.title("Energy per flight phase")
    if not guard():
        return
    e = get("energy_by_phase")
    fleet = (e.groupby("flight_mode", observed=True, as_index=False)
             .agg(minutes=("minutes", "mean"), vtol=("vtol_used_pct", "mean"), cruise=("cruise_used_pct", "mean")))
    vt = fleet[fleet["flight_mode"].isin(["takeoff", "landing"])]
    cols = st.columns(3)
    cols[0].metric("VTOL battery used, takeoff + landing", f"{vt['vtol'].sum():.0f} %")
    cols[1].metric("in minutes", f"{vt['minutes'].sum():.1f}")
    cols[2].metric("Cruise battery used, whole flight", f"{fleet['cruise'].sum():.0f} %")
    st.caption("Fleet average per flight. The VTOL pack drains fast in a few minutes of hover; "
               "the cruise pack drains slowly over the long legs. That is why low VTOL at the return leg is a fault.")

    long = fleet.melt(id_vars=["flight_mode", "minutes"], value_vars=["vtol", "cruise"],
                      var_name="battery", value_name="used_pct")
    chart = alt.Chart(long).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("flight_mode:N", sort=Q.PHASES, title="phase", axis=alt.Axis(labelAngle=0)),
        xOffset="battery:N",
        y=alt.Y("used_pct:Q", title="battery used (%)"),
        color=alt.Color("battery:N", scale=alt.Scale(domain=["cruise", "vtol"], range=["#1baf7a", "#6250d6"])),
        tooltip=["flight_mode", "battery", alt.Tooltip("used_pct:Q", format=".1f"),
                 alt.Tooltip("minutes:Q", format=".1f")],
    ).properties(height=280)
    st.altair_chart(chart, use_container_width=True)

    st.subheader("Per drone")
    st.dataframe(e, hide_index=True, use_container_width=True)


def page_evaluation() -> None:
    st.title("Detector evaluation")
    if not guard():
        return
    st.warning("This page reads the answer key (injected_anomalies) to score the detectors. "
               "The detectors themselves never see it.")
    ev = get("evaluation")
    if ev.empty:
        st.info("Nothing to score: this database has no answer key or no alerts.")
        return
    total = ev[ev["type"] == "all"].set_index("detector")
    cols = st.columns(len(total))
    for col, (det, r) in zip(cols, total.iterrows()):
        col.metric(f"{det}: precision / recall",
                   f"{r.precision:.0%} / {r.recall:.0%}" if pd.notna(r.precision) else f"n/a / {r.recall:.0%}")
        col.caption(f"{int(r.caught)} of {int(r.injected)} faults caught, {int(r.false_alarms)} false alarms")
    show = ev.copy()
    for c in ("precision", "recall"):
        show[c] = show[c].map(lambda x: "n/a" if pd.isna(x) else f"{x:.0%}")
    st.dataframe(show, hide_index=True, use_container_width=True)
    st.caption("Tune on the dev set only. Look at the test set, don't tune on it.")


