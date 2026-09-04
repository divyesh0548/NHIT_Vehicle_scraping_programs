"""
Remove rows where a given column cell is empty.

Treats NaN, None, blank, whitespace, and the string "nan" as empty.

Configure via variables below (no CLI args).
"""

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configure these variables before running
# ---------------------------------------------------------------------------
INPUT_FILE = "Checkpost_Mokha_20260807_055059.xlsx"
OUTPUT_FILE = "Checkpost_Mokha_20260807_055059_cleaned.xlsx"   # Blank = overwrite INPUT_FILE
SHEET_NAME = ""               # Blank = first sheet
COLUMN_NAME = "Weight"        # Drop rows where this column is empty
# ---------------------------------------------------------------------------


def _is_empty_cell(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return not text or text.lower() in {"nan", "none", "null", "-"}


def _resolve_column(df, column_name):
    if column_name in df.columns:
        return column_name
    wanted = str(column_name).strip().lower()
    for col in df.columns:
        if str(col).strip().lower() == wanted:
            return col
    return None


def _read_table(path, sheet_name=""):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        kwargs = {"dtype": str}
        if str(sheet_name).strip():
            kwargs["sheet_name"] = sheet_name
        return pd.read_excel(path, **kwargs)
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str)
    raise ValueError(f"Unsupported file type '{suffix}' for {path}")


def _write_table(df, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        df.to_excel(path, index=False)
    elif suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported output type '{suffix}' for {path}")


def remove_empty_cell_rows(
    input_file=INPUT_FILE,
    output_file=OUTPUT_FILE,
    sheet_name=SHEET_NAME,
    column_name=COLUMN_NAME,
):
    print("=" * 70)
    print("REMOVE ROWS WITH EMPTY COLUMN CELLS")
    print("=" * 70)
    print(f"Input file : {input_file}")
    print(f"Column     : {column_name}")

    df = _read_table(input_file, sheet_name=sheet_name)
    print(f"Input rows : {len(df)}")

    col = _resolve_column(df, column_name)
    if col is None:
        raise KeyError(
            f"Column '{column_name}' not found. Available: {list(df.columns)}"
        )
    if col != column_name:
        print(f"[INFO] Column matched as '{col}'")

    empty_mask = df[col].map(_is_empty_cell)
    removed = int(empty_mask.sum())
    df_clean = df.loc[~empty_mask].copy().reset_index(drop=True)

    out_path = (output_file or "").strip() or input_file
    _write_table(df_clean, out_path)

    print("-" * 70)
    print(f"Removed    : {removed} row(s) (empty '{col}')")
    print(f"Remaining  : {len(df_clean)} row(s)")
    print(f"Saved to   : {out_path}")
    print("=" * 70)
    return df_clean


if __name__ == "__main__":
    remove_empty_cell_rows()