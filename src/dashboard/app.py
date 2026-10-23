"""
Fleet dashboard (Phase 6). Read-only view of the drone database.

Run:  streamlit run src/dashboard/app.py
      (database: DRONE_DB_PATH or data/drone.db; switch to the test set in the sidebar)
"""
import sys
from pathlib import Path

# `streamlit run` puts src/dashboard on sys.path, not the project root
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.dashboard import views as V  # noqa: E402

st.set_page_config(page_title="OpenClaw Drone Analytics", layout="wide")
V.sidebar()
st.navigation([
    st.Page(V.page_overview, title="Overview", default=True),
    st.Page(V.page_missions, title="Missions", url_path="missions"),
    st.Page(V.page_alerts, title="Alerts", url_path="alerts"),
    st.Page(V.page_energy, title="Energy", url_path="energy"),
    st.Page(V.page_evaluation, title="Evaluation", url_path="evaluation"),
]).run()
