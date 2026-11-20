import datetime
import math
from functools import reduce
from operator import and_

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# ==========================================
# 1. PAGE CONFIGURATION & STYLE
# ==========================================
st.set_page_config(
    page_title="SPK Table Dashboard",
    page_icon="📊",
    layout="wide",
)

TABLE_NAME = "view_penerimaan_LSN"
CACHE_TTL_SECONDS = 600
DATE_COLS = ["tglspk", "tglterima", "tglinputspk"]

# Yellow highlight for Status Pembebanan = "Belum" (dark text keeps it readable)
HIGHLIGHT_BELUM = "background-color: #FFD43B; color: #1F1F1F; font-weight: 600;"

# Pandas Styler refuses to render more cells than this (hard limit is 262,144).
STYLER_MAX_CELLS = 200_000

APP_CSS = """
<style>
/* Pull everything up: less empty space above the title */
.block-container, [data-testid="stMainBlockContainer"] {
    padding-top: 2.5rem !important;
    padding-bottom: 1.5rem !important;
}
/* Slightly tighter spacing between elements */
[data-testid="stMain"] [data-testid="stVerticalBlock"] { gap: 0.8rem; }
/* Compact headings */
.dash-title { font-size: 1.7rem; font-weight: 700; line-height: 1.2; margin: 0; padding: 0.2rem 0 0; }
.table-title { font-size: 1.15rem; font-weight: 600; margin: 0; padding-top: 0.4rem; }
/* Smaller metric numbers so the cards stay compact */
[data-testid="stMetricValue"] { font-size: 1.9rem; }
</style>
"""


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

    # Real datetime64 columns (fast, vectorised); normalize() drops time-of-day.
    for col in DATE_COLS:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()

    # Low-cardinality text -> category: far less memory, faster isin / ==.
    df["namabengkel"] = df["namabengkel"].astype("category")
    df["status_pembebanan"] = df["status_pembebanan"].astype("category")
    df["no_spk"] = df["no_spk"].astype("string")

    # Sort once here (cached) instead of ORDER BY on the database server.
    return df.sort_values("tglinputspk", ascending=False, ignore_index=True)


# ==========================================
# 4. HEADER ROW: title (left) + refresh button (right)
# ==========================================
# The CSS lives inside the title's markdown so it adds no extra blank element.
title_col, refresh_col = st.columns([5, 1.2])
with title_col:
    st.markdown(
        APP_CSS + '<div class="dash-title">📊 Database Records</div>',
        unsafe_allow_html=True,
    )
with refresh_col:
    if st.button("🔄 Refresh Data", use_container_width=True):
        load_data.clear()
        st.rerun()

try:
    today = datetime.date.today()
    df = load_data(today.year)
except Exception as e:
    st.error(f"Database Connection Failed: {e}")
    st.stop()

st.sidebar.caption("🟢 MySQL connected")

# ==========================================
# 5. SIDEBAR FILTERS
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
# 6. DASHBOARD METRICS (compact cards, right under the title)
# ==========================================
status_counts = filtered_df["status_pembebanan"].value_counts()
sudah_count = int(status_counts.get("Sudah", 0))
belum_count = int(status_counts.get("Belum", 0))
total_count = len(filtered_df)

card1, card2, card3 = st.columns(3)

with card1:
    with st.container(border=True):
        st.metric(label="✅ Status: Sudah", value=f"{sudah_count:,}")

with card2:
    with st.container(border=True):
        st.metric(label="🟡 Status: Belum", value=f"{belum_count:,}")

with card3:
    with st.container(border=True):
        st.metric(label="Total Records", value=f"{total_count:,}")

# ==========================================
# 7. DISPLAY DATABASE TABLE (PAGINATED + HIGHLIGHT)
# ==========================================
# Sending every row to the browser is the slowest part of a big dashboard.
# Only the rows of the current page are serialised and sent.
PAGE_SIZE_OPTIONS = [500, 1000, 5000, "All"]

table_title_col, size_col, page_col = st.columns([3, 1, 1])

with size_col:
    page_size_choice = st.selectbox(
        "Rows per page",
        PAGE_SIZE_OPTIONS,
        index=1,
        format_func=lambda n: "All rows" if n == "All" else f"{n:,} rows per page",
        label_visibility="collapsed",
    )

page_size = max(total_count, 1) if page_size_choice == "All" else page_size_choice
total_pages = max(1, math.ceil(total_count / page_size))

page = 1
with page_col:
    if total_pages > 1:
        page = st.selectbox(
            "Page",
            list(range(1, total_pages + 1)),
            format_func=lambda p: f"Page {p:,} of {total_pages:,}",
            label_visibility="collapsed",
        )

with table_title_col:
    st.markdown(
        f'<div class="table-title">Penerimaan LSN ({total_count:,} results)</div>',
        unsafe_allow_html=True,
    )

start_row = (page - 1) * page_size
page_df = filtered_df.iloc[start_row:start_row + page_size]

# Small copy of just this page: dates become "YYYY-MM-DD" text (ISO text still
# sorts correctly) so they display cleanly next to the highlighted column.
view = page_df.copy()
for col in DATE_COLS:
    view[col] = view[col].dt.strftime("%Y-%m-%d").fillna("")


def highlight_belum(column):
    return [HIGHLIGHT_BELUM if value == "Belum" else "" for value in column]


table = view
if not view.empty and view.size <= STYLER_MAX_CELLS:
    try:
        table = view.style.apply(highlight_belum, subset=["status_pembebanan"])
    except Exception:
        table = view  # highlighting is cosmetic: never break the table over it
elif view.size > STYLER_MAX_CELLS:
    st.caption("⚠️ Yellow highlight is off for this many rows. Choose a smaller page size.")

# Fit the table height to the rows (no empty space for short results).
table_height = max(120, min(640, 35 * (len(view) + 1) + 3))

st.dataframe(
    table,
    use_container_width=True,
    hide_index=True,
    height=table_height,
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
        "tglspk": "tgl SPK",
        "tglterima": "tgl terima",
        "tglinputspk": "tgl input SPK",
    },
)

# ==========================================
# 8. FOOTER
# ==========================================
st.caption("SPK Table Dashboard | MySQL Database")
