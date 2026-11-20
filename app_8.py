import datetime
import math
from functools import reduce
from operator import and_

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# ==========================================
# 1. PAGE CONFIGURATION
# ==========================================
st.set_page_config(
    page_title="SPK Table Dashboard",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Database Records")
st.markdown("---")

TABLE_NAME = "view_penerimaan_LSN"
CACHE_TTL_SECONDS = 600
DATE_COLS = ["tglspk", "tglterima", "tglinputspk"]


# ==========================================
# 2. MYSQL ENGINE (created once, shared by all sessions)
# ==========================================
@st.cache_resource
def get_engine():
    try:
        db_config = st.secrets["mysql"]
        connection_url = URL.create(
            drivername="mysql+pymysql",
            username=db_config["DB_USER"],
            password=db_config["DB_PASSWORD"],
            host=db_config["DB_HOST"],
            port=int(db_config["DB_PORT"]),
            database=db_config["DB_NAME"],
        )
    except KeyError as e:
        raise KeyError(
            f"Missing secret key: {e}. Check your .streamlit/secrets.toml configuration."
        )

    return create_engine(
        connection_url,
        pool_pre_ping=True,   # silently replaces dead connections
        pool_recycle=1800,
        connect_args={"connect_timeout": 10},
    )


# ==========================================
# 3. LOAD DATA FROM MYSQL
# ==========================================
# NOTE: cache_resource returns the SAME DataFrame object to every rerun/session
# (no pickle/copy like cache_data). That is much faster for big tables, but the
# cached DataFrame must be treated as READ-ONLY. Never modify `df` in place;
# filtering/slicing below always creates new objects, which is safe.
@st.cache_resource(ttl=CACHE_TTL_SECONDS, show_spinner="Loading data from MySQL...")
def load_data(year: int) -> pd.DataFrame:
    engine = get_engine()
    year_start = datetime.date(year, 1, 1)
    next_year_start = datetime.date(year + 1, 1, 1)

    # Range comparisons (col >= start AND col < next_start) can use an index on
    # the column. YEAR(col) = 2026 cannot: it forces MySQL to scan every row.
    # Same result as the original YEAR() filters, including the NULL rules.
    query = text(f"""
        SELECT namabengkel, no_spk, status_pembebanan, tglspk, tglterima, tglinputspk
        FROM `{TABLE_NAME}`
        WHERE tglinputspk >= :start AND tglinputspk < :next_start
          AND (tglspk IS NULL OR (tglspk >= :start AND tglspk < :next_start))
          AND (tglterima IS NULL OR (tglterima >= :start AND tglterima < :next_start))
    """)

    with engine.connect() as conn:
        df = pd.read_sql(
            query,
            conn,
            params={"start": year_start, "next_start": next_year_start},
        )

    # Dates: keep real datetime64 columns (fast, vectorised comparisons).
    # The old code turned them into Python `date` objects, which is slow to
    # filter and uses much more memory. normalize() drops any time-of-day.
    for col in DATE_COLS:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()

    # Low-cardinality text -> category: far less memory, faster isin / ==.
    df["namabengkel"] = df["namabengkel"].astype("category")
    df["status_pembebanan"] = df["status_pembebanan"].astype("category")
    df["no_spk"] = df["no_spk"].astype("string")

    # Sort once here (cached) instead of ORDER BY on the database server.
    return df.sort_values("tglinputspk", ascending=False, ignore_index=True)


try:
    today = datetime.date.today()
    df = load_data(today.year)
except Exception as e:
    st.error(f"Database Connection Failed: {e}")
    st.stop()

st.sidebar.success("✅ MySQL Connected")

# ==========================================
# 4. SIDEBAR FILTERS
# ==========================================
# Every filter produces a boolean mask. All masks are combined and applied to
# the DataFrame ONCE at the end (no repeated df.copy() / re-filtering).
masks = []

year_start = datetime.date(today.year, 1, 1)
year_end = datetime.date(today.year, 12, 31)


def date_range_mask(col_name: str, label: str):
    """Render a date slider and return a mask, or None if not narrowed."""
    series = df[col_name]
    if series.notna().sum() == 0:
        return None

    # Defaults come from the full dataset so the widget stays stable between
    # reruns (changing a slider's default would silently reset it).
    default_start = max(series.min().date(), year_start)
    default_end = min(series.max().date(), year_end)
    if default_start > default_end:
        return None

    picked = st.slider(
        label,
        min_value=year_start,
        max_value=year_end,
        value=(default_start, default_end),
    )

    # Only filter when the user actually narrowed the range. This keeps rows
    # with an empty date (NULL) visible until a date range is chosen.
    if picked == (default_start, default_end):
        return None
    return (series >= pd.Timestamp(picked[0])) & (series <= pd.Timestamp(picked[1]))


with st.sidebar.form(key="filter_form"):
    st.header("🔍 Filter Data")

    # A. Dropdown for "namabengkel"
    selected_bengkel = st.multiselect(
        "Nama Bengkel", options=df["namabengkel"].cat.categories.tolist()
    )
    if selected_bengkel:
        masks.append(df["namabengkel"].isin(selected_bengkel))

    # B. Search for "no_spk"
    search_spk = st.text_input("Search No SPK").strip()
    if search_spk:
        masks.append(
            df["no_spk"].str.contains(search_spk, case=False, regex=False, na=False)
        )

    # C. Select for "status_pembebanan"
    status_list = ["All"] + df["status_pembebanan"].cat.categories.tolist()
    selected_status = st.selectbox("Status Pembebanan", options=status_list)
    if selected_status != "All":
        masks.append(df["status_pembebanan"] == selected_status)

    # D. Date sliders
    st.markdown("### 📅 Date Filters")
    for col_name, label in [
        ("tglspk", "Tanggal SPK"),
        ("tglterima", "Tanggal Terima"),
        ("tglinputspk", "Tanggal Input SPK"),
    ]:
        mask = date_range_mask(col_name, label)
        if mask is not None:
            masks.append(mask)

    st.form_submit_button(label="Apply Filters", use_container_width=True)

filtered_df = df[reduce(and_, masks)] if masks else df

# ==========================================
# 5. DASHBOARD METRICS (SCORE CARDS)
# ==========================================
status_counts = filtered_df["status_pembebanan"].value_counts()
sudah_count = int(status_counts.get("Sudah", 0))
belum_count = int(status_counts.get("Belum", 0))
total_count = len(filtered_df)

col1, col2, col3, col4 = st.columns([1, 1, 1, 1.5])

with col1:
    st.metric(label="✅ Status: Sudah", value=f"{sudah_count:,}")

with col2:
    st.metric(label="⏳ Status: Belum", value=f"{belum_count:,}")

with col3:
    st.metric(label="Total Records", value=f"{total_count:,}")

with col4:
    st.write("")
    if st.button("🔄 Refresh / Reset Data", use_container_width=True):
        load_data.clear()
        st.rerun()

st.markdown("---")

# ==========================================
# 6. DISPLAY DATABASE TABLE (PAGINATED)
# ==========================================
# Sending every row to the browser is the slowest part of a big dashboard.
# Only the rows of the current page are serialised and sent.
st.subheader(f"Penerimaan LSN ({total_count:,} results)")

PAGE_SIZE_OPTIONS = [500, 1000, 5000, "All"]

ctrl_left, ctrl_right, _ = st.columns([1, 1, 4])
with ctrl_left:
    page_size_choice = st.selectbox("Rows per page", PAGE_SIZE_OPTIONS, index=1)

page_size = max(total_count, 1) if page_size_choice == "All" else page_size_choice
total_pages = max(1, math.ceil(total_count / page_size))

page = 1
if total_pages > 1:
    with ctrl_right:
        page = int(
            st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)
        )
    st.caption(f"Page {page:,} of {total_pages:,}")

start_row = (page - 1) * page_size
page_df = filtered_df.iloc[start_row:start_row + page_size]

st.dataframe(
    page_df,
    use_container_width=True,
    hide_index=True,
    height=600,
    column_order=[
        "namabengkel",
        "no_spk",
        "status_pembebanan",
        "tglspk",
        "tglterima",
        "tglinputspk",
    ],
    column_config={
        "namabengkel": "Nama Bengkel",
        "no_spk": "No SPK",
        "status_pembebanan": "Status Pembebanan",
        "tglspk": st.column_config.DatetimeColumn("tgl SPK", format="YYYY-MM-DD"),
        "tglterima": st.column_config.DatetimeColumn("tgl terima", format="YYYY-MM-DD"),
        "tglinputspk": st.column_config.DatetimeColumn("tgl input SPK", format="YYYY-MM-DD"),
    },
)

# ==========================================
# 7. FOOTER
# ==========================================
st.markdown("---")
st.caption("SPK Table Dashboard | MySQL Database")
