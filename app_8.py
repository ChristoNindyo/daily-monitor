import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# ==========================================
# 1. PAGE CONFIG + SMALL STYLE TWEAKS
# ==========================================
st.set_page_config(
    page_title="SPK Table Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        .block-container { padding-top: 2rem; padding-bottom: 2rem; }
        [data-testid="stMetricValue"] { font-size: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Newer Streamlit versions replace use_container_width with width="stretch"
try:
    _major, _minor = (int(x) for x in st.__version__.split(".")[:2])
    STRETCH = {"width": "stretch"} if (_major, _minor) >= (1, 50) else {"use_container_width": True}
except Exception:
    STRETCH = {"use_container_width": True}

# ==========================================
# 2. CONSTANTS
# ==========================================
TABLE_NAME = "view_penerimaan_LSN"
YEAR = datetime.date.today().year
YEAR_START = datetime.date(YEAR, 1, 1)
YEAR_END = datetime.date(YEAR, 12, 31)

DATE_COLS = {
    "tglspk": "Tgl SPK",
    "tglterima": "Tgl Terima",
    "tglinputspk": "Tgl Input SPK",
}
STATUS_ICON = {"Sudah": "✅ Sudah", "Belum": "⏳ Belum"}

FILTER_KEYS = [
    "f_bengkel", "f_spk", "f_status",
    "f_tglspk", "f_tglterima", "f_tglinputspk", "f_keep_null",
]


# ==========================================
# 3. DATABASE (cached engine + cached data)
# ==========================================
@st.cache_resource
def get_engine():
    try:
        cfg = st.secrets["mysql"]
        url = URL.create(
            drivername="mysql+pymysql",
            username=cfg["DB_USER"],
            password=cfg["DB_PASSWORD"],
            host=cfg["DB_HOST"],
            port=int(cfg["DB_PORT"]),
            database=cfg["DB_NAME"],
        )
    except KeyError as e:
        raise KeyError(f"Missing secret key: {e}. Check .streamlit/secrets.toml.")

    return create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,   # replaces the SELECT 1 check on every rerun
        pool_recycle=1800,
        connect_args={"connect_timeout": 10},
    )


@st.cache_data(ttl=600, show_spinner="Loading records…")
def load_data(_engine):
    # Plain range comparisons (instead of YEAR(col) = ...) let MySQL use an
    # index on these columns, and sorting is left to pandas.
    query = text(f"""
        SELECT namabengkel, no_spk, status_pembebanan, tglspk, tglterima, tglinputspk
        FROM `{TABLE_NAME}`
        WHERE tglinputspk >= :start AND tglinputspk < :end
          AND (tglspk    IS NULL OR (tglspk    >= :start AND tglspk    < :end))
          AND (tglterima IS NULL OR (tglterima >= :start AND tglterima < :end))
    """)
    params = {"start": YEAR_START, "end": datetime.date(YEAR + 1, 1, 1)}

    with _engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params)

    # Real datetime columns allow fast vectorized filtering; bad dates become NaT
    for col in DATE_COLS:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    # Categories use less memory and make filtering faster
    df["namabengkel"] = df["namabengkel"].astype("category")
    df["status_pembebanan"] = df["status_pembebanan"].astype("category")

    df = df.sort_values("tglinputspk", ascending=False, ignore_index=True)

    try:
        from zoneinfo import ZoneInfo
        loaded_at = datetime.datetime.now(ZoneInfo("Asia/Jakarta"))
    except Exception:
        loaded_at = datetime.datetime.now()

    return df, loaded_at


try:
    df, loaded_at = load_data(get_engine())
except Exception as e:
    st.error(f"Could not load data: {e}")
    if st.button("Try again"):
        load_data.clear()
        st.rerun()
    st.stop()


# ==========================================
# 4. HEADER
# ==========================================
head_left, head_right = st.columns([4, 1], vertical_alignment="center")
with head_left:
    st.title("📊 Penerimaan LSN")
    st.caption(
        f"{TABLE_NAME} • {YEAR} • updated {loaded_at:%d %b %Y, %H:%M} "
        f"(refreshes automatically every 10 minutes)"
    )
with head_right:
    if st.button("🔄 Refresh data", **STRETCH):
        load_data.clear()
        st.rerun()

if df.empty:
    st.info(f"No records found for {YEAR}.")
    st.stop()

# ==========================================
# 5. SIDEBAR FILTERS (live, no Apply button)
# ==========================================
def reset_filters():
    for key in FILTER_KEYS:
        st.session_state.pop(key, None)


def date_bounds(series):
    valid = series.dropna()
    lo = max(valid.min().date(), YEAR_START) if not valid.empty else YEAR_START
    hi = min(valid.max().date(), YEAR_END) if not valid.empty else YEAR_END
    return lo, hi


with st.sidebar:
    st.header("🔍 Filters")
    st.button("Reset filters", on_click=reset_filters, **STRETCH)

    bengkel_options = list(df["namabengkel"].cat.categories)
    selected_bengkel = st.multiselect(
        "Nama Bengkel", bengkel_options, key="f_bengkel", placeholder="All workshops"
    )

    search_spk = st.text_input(
        "No SPK", key="f_spk", placeholder="Type part of a number, then press Enter"
    ).strip()

    status_options = ["All"] + list(df["status_pembebanan"].cat.categories)
    selected_status = st.radio(
        "Status Pembebanan", status_options, key="f_status", horizontal=True
    )

    date_ranges = {}
    with st.expander("📅 Date filters"):
        for col, label in DATE_COLS.items():
            lo, hi = date_bounds(df[col])
            chosen = st.date_input(
                label,
                value=(lo, hi),
                min_value=YEAR_START,
                max_value=YEAR_END,
                key=f"f_{col}",
            )
            date_ranges[col] = (chosen, (lo, hi))
        keep_null = st.checkbox(
            "Keep rows with empty dates",
            value=True,
            key="f_keep_null",
            help="When a date range is narrowed, keep records that have no date in that column.",
        )

# ==========================================
# 6. APPLY FILTERS (vectorized, single mask)
# ==========================================
mask = pd.Series(True, index=df.index)
active = []

if selected_bengkel:
    mask &= df["namabengkel"].isin(selected_bengkel)
    active.append(f"{len(selected_bengkel)} workshop(s)")

if search_spk:
    mask &= df["no_spk"].astype(str).str.contains(search_spk, case=False, na=False, regex=False)
    active.append(f"SPK contains “{search_spk}”")

if selected_status != "All":
    mask &= df["status_pembebanan"] == selected_status
    active.append(selected_status)

for col, (chosen, default) in date_ranges.items():
    # date_input returns a single date while the user is still picking the end date
    if not (isinstance(chosen, tuple) and len(chosen) == 2) or tuple(chosen) == default:
        continue
    start = pd.Timestamp(chosen[0])
    end = pd.Timestamp(chosen[1]) + pd.Timedelta(days=1)
    in_range = (df[col] >= start) & (df[col] < end)
    if keep_null:
        in_range |= df[col].isna()
    mask &= in_range
    active.append(DATE_COLS[col])

view = df[mask]

with st.sidebar:
    st.caption(f"Showing {len(view):,} of {len(df):,} records")

# ==========================================
# 7. METRICS
# ==========================================
total = len(view)
sudah = int((view["status_pembebanan"] == "Sudah").sum())
belum = int((view["status_pembebanan"] == "Belum").sum())
rate = sudah / total if total else 0.0

m1, m2, m3, m4 = st.columns(4)
for box, label, value in (
    (m1, "Total records", f"{total:,}"),
    (m2, "✅ Sudah", f"{sudah:,}"),
    (m3, "⏳ Belum", f"{belum:,}"),
    (m4, "Completion", f"{rate:.0%}"),
):
    with box.container(border=True):
        st.metric(label, value)

st.progress(rate, text=f"{sudah:,} of {total:,} records are Sudah")

if active:
    st.caption("Active filters: " + " • ".join(active))

if view.empty:
    st.info("No records match these filters. Remove a filter or click **Reset filters** in the sidebar.")
    st.stop()

# ==========================================
# 8. TABS: RECORDS + INSIGHTS
# ==========================================
tab_table, tab_insights = st.tabs(["📋 Records", "📈 Insights"])

with tab_table:
    table = pd.DataFrame(
        {
            "Nama Bengkel": view["namabengkel"].astype("object"),
            "No SPK": view["no_spk"],
            "Status Pembebanan": view["status_pembebanan"].astype("object").map(STATUS_ICON).fillna(
                view["status_pembebanan"].astype("object")
            ),
            **{label: view[col] for col, label in DATE_COLS.items()},
        }
    )

    csv_bytes = table.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Download filtered data (CSV)",
        data=csv_bytes,
        file_name=f"penerimaan_lsn_{YEAR}.csv",
        mime="text/csv",
    )

    st.dataframe(
        table,
        hide_index=True,
        height=560,
        column_config={
            "Nama Bengkel": st.column_config.TextColumn(width="large"),
            "No SPK": st.column_config.TextColumn(width="medium"),
            "Status Pembebanan": st.column_config.TextColumn(width="small"),
            "Tgl SPK": st.column_config.DateColumn(format="DD MMM YYYY", width="small"),
            "Tgl Terima": st.column_config.DateColumn(format="DD MMM YYYY", width="small"),
            "Tgl Input SPK": st.column_config.DatetimeColumn(
                format="DD MMM YYYY, HH:mm", width="medium"
            ),
        },
        **STRETCH,
    )

with tab_insights:
    chart_left, chart_right = st.columns(2)

    with chart_left:
        st.subheader("Top 10 workshops")
        by_bengkel = (
            view.groupby(["namabengkel", "status_pembebanan"], observed=True)
            .size()
            .unstack(fill_value=0)
        )
        top_index = by_bengkel.sum(axis=1).sort_values(ascending=False).head(10).index
        st.bar_chart(by_bengkel.loc[top_index])

    with chart_right:
        st.subheader("SPK input per week")
        weekly = (
            view.dropna(subset=["tglinputspk"])
            .set_index("tglinputspk")
            .resample("W")
            .size()
            .rename("SPK inputs")
        )
        st.bar_chart(weekly)

    st.subheader("Latest Tgl Input SPK per workshop")
    latest = (
        view.dropna(subset=["tglinputspk"])
        .groupby("namabengkel", observed=True)
        .agg(
            latest_input=("tglinputspk", "max"),
            total_records=("no_spk", "size"),
        )
        .sort_values("latest_input", ascending=False)
        .reset_index()
        .rename(
            columns={
                "namabengkel": "Nama Bengkel",
                "latest_input": "Latest Tgl Input SPK",
                "total_records": "Total Records",
            }
        )
    )
    st.dataframe(
        latest,
        hide_index=True,
        height=min(600, 38 + 35 * len(latest)),
        column_config={
            "Nama Bengkel": st.column_config.TextColumn(width="large"),
            "Latest Tgl Input SPK": st.column_config.DatetimeColumn(
                format="DD MMM YYYY, HH:mm", width="medium"
            ),
            "Total Records": st.column_config.NumberColumn(format="%d", width="small"),
        },
        **STRETCH,
    )
