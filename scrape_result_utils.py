"""Shared scrape completeness helpers for Grid / local vehicle scrapers.

After parallel sessions finish, some vehicles may still be empty or marked
Error* because a Chrome session failed. Callers can:
  1. Detect incomplete rows
  2. Retry those vehicles
  3. Treat explicit site-miss markers (no-data / N/A / none) as complete
"""

from __future__ import annotations

NO_DATA_MARKER = "no-data"

# Explicit values that mean "looked up; nothing useful on the site" (or legacy).
SITE_MISS_MARKERS = {
    NO_DATA_MARKER,
    "n/a",
    "na",
    "none",
    "-",
}

ERROR_PREFIXES = ("error",)


def cell_text(value):
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def is_site_miss_value(value):
    text = cell_text(value)
    return bool(text) and text.lower() in SITE_MISS_MARKERS


def is_error_value(value):
    text = cell_text(value).lower()
    return any(text.startswith(prefix) for prefix in ERROR_PREFIXES)


def is_complete_detail_value(value):
    """
    True when the cell shows a finished attempt:
    real scraped data, or an explicit site-miss marker.
    Empty and Error* are incomplete.
    """
    text = cell_text(value)
    if not text:
        return False
    if is_error_value(text):
        return False
    return True


def ensure_columns(df, columns):
    for col in columns:
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("").astype(str).replace({"nan": ""})
    return df


def incomplete_index(df, columns):
    """Index labels for rows that still need scraping."""
    if df is None or df.empty:
        return []
    ensure_columns(df, columns)

    incomplete = []
    for idx, row in df.iterrows():
        if not all(is_complete_detail_value(row.get(col, "")) for col in columns):
            incomplete.append(idx)
    return incomplete


def mark_row_no_data(df, idx, columns):
    ensure_columns(df, columns)
    for col in columns:
        df.at[idx, col] = NO_DATA_MARKER


def clear_incomplete_cells(df, columns):
    """Blank Error*/empty cells so a retry pass re-scrapes them."""
    ensure_columns(df, columns)
    for idx in incomplete_index(df, columns):
        for col in columns:
            value = df.at[idx, col]
            if not is_complete_detail_value(value):
                df.at[idx, col] = ""
    return df


def merge_result_columns(base_df, update_df, columns):
    """Copy result columns from update_df onto base_df by index."""
    if update_df is None or update_df.empty:
        return base_df
    ensure_columns(base_df, columns)
    ensure_columns(update_df, columns)
    common = base_df.index.intersection(update_df.index)
    for col in columns:
        if col in update_df.columns:
            base_df.loc[common, col] = update_df.loc[common, col]
    return base_df


def ensure_scrape_complete(
    df,
    scrape_fn,
    result_columns,
    scrape_label="SCRAPE",
    remote_url=None,
    max_retry_passes=2,
):
    """
    Retry vehicles that are still empty or Error* after a Grid/local run.

    scrape_fn(chunk_df, remote_url) must return a dataframe with the same index.
    Pass 1 retries on the same remote_url (Grid if set).
    Later passes force local Chrome (remote_url=None).

    Site-miss markers (no-data / N/A / none) are treated as complete and are
    not retried. Scrapers themselves should write NO_DATA_MARKER when the
    portal has no record.
    """
    if df is None or df.empty:
        return df

    ensure_columns(df, result_columns)
    working = df.copy()

    for pass_num in range(1, max_retry_passes + 1):
        missing = incomplete_index(working, result_columns)
        if not missing:
            print(
                f"  [COMPLETE] {scrape_label}: all {len(working)} row(s) have results",
                flush=True,
            )
            return working

        print(
            f"  [RETRY {pass_num}/{max_retry_passes}] {scrape_label}: "
            f"{len(missing)}/{len(working)} vehicle(s) still incomplete "
            f"(empty or Error*)",
            flush=True,
        )

        subset = working.loc[missing].copy()
        clear_incomplete_cells(subset, result_columns)
        retry_url = remote_url if pass_num == 1 else None
        mode = "Grid" if retry_url else "local Chrome"
        print(f"  [RETRY {pass_num}] Re-scraping on {mode}...", flush=True)

        try:
            updated = scrape_fn(subset, retry_url)
        except Exception as exc:
            print(f"  [RETRY {pass_num}] failed: {exc}", flush=True)
            continue

        working = merge_result_columns(working, updated, result_columns)

    still_missing = incomplete_index(working, result_columns)
    if still_missing:
        print(
            f"  [WARN] {scrape_label}: {len(still_missing)} vehicle(s) still "
            f"incomplete after retries (left as Error/empty for next run)",
            flush=True,
        )
    else:
        print(f"  [COMPLETE] {scrape_label}: all vehicles filled after retries", flush=True)

    return working
