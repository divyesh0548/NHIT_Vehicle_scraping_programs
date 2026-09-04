"""
Output lookup / input cleanup.

Removes rows from an INPUT file when values from its column already appear
in an OUTPUT (lookup) file column. Each file has its own column name.

Configure paths and columns via the variables below (no CLI args).
"""

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configure these variables before running
# ---------------------------------------------------------------------------
INPUT_FILE = "Checkpost_Mokha_20260807_055059.csv"              # File to clean (rows may be removed)
OUTPUT_FILE = "Checkpost_Mokha_20260807_055059_cleaned.xlsx"            # Lookup file (values already processed)
INPUT_COLUMN_NAME = "Veh Reg No."      # Column in INPUT_FILE
OUTPUT_COLUMN_NAME = "Veh Reg No."     # Column in OUTPUT_FILE
RESULT_FILE = ""                       # Optional; blank = overwrite INPUT_FILE
# ---------------------------------------------------------------------------


def _normalize_cell(value):
    """String form used for set membership (strip; treat blank/nan as empty)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def _resolve_column(df, column_name):
    """
    Resolve column_name against df.columns (exact, then case-insensitive).
    Returns the actual column name in df, or None.
    """
    if column_name in df.columns:
        return column_name

    wanted = str(column_name).strip().lower()
    for col in df.columns:
        if str(col).strip().lower() == wanted:
            return col
    return None


def _read_table(path):
    """Read Excel or CSV based on file extension."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(path, dtype=str)
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str)
    raise ValueError(f"Unsupported file type '{suffix}' for {path}")


def _write_table(df, path):
    """Write DataFrame as Excel or CSV based on file extension."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        df.to_excel(path, index=False)
    elif suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported output type '{suffix}' for {path}")


def remove_rows_found_in_output(
    input_file=INPUT_FILE,
    output_file=OUTPUT_FILE,
    input_column_name=INPUT_COLUMN_NAME,
    output_column_name=OUTPUT_COLUMN_NAME,
    result_file=RESULT_FILE,
    ):
    """
    Drop rows from input_file whose input_column_name value exists in
    output_file's output_column_name.

    Returns the cleaned DataFrame. Writes to result_file, or overwrites
    input_file when result_file is blank.
    """
    print("=" * 70)
    print("OUTPUT LOOKUP — remove input rows already present in output")
    print("=" * 70)
    print(f"Input file   : {input_file}")
    print(f"Output file  : {output_file}")
    print(f"Input column : {input_column_name}")
    print(f"Output column: {output_column_name}")

    df_input = _read_table(input_file)
    df_output = _read_table(output_file)
    print(f"Input rows : {len(df_input)}")
    print(f"Output rows: {len(df_output)}")

    input_col = _resolve_column(df_input, input_column_name)
    output_col = _resolve_column(df_output, output_column_name)

    if input_col is None:
        raise KeyError(
            f"Column '{input_column_name}' not found in input file. "
            f"Available: {list(df_input.columns)}"
        )
    if output_col is None:
        raise KeyError(
            f"Column '{output_column_name}' not found in output file. "
            f"Available: {list(df_output.columns)}"
        )

    if input_col != input_column_name:
        print(f"[INFO] Input column matched as '{input_col}'")
    if output_col != output_column_name:
        print(f"[INFO] Output column matched as '{output_col}'")

    lookup_values = {
        _normalize_cell(v)
        for v in df_output[output_col].tolist()
        if _normalize_cell(v)
    }
    print(f"Unique non-empty lookup values: {len(lookup_values)}")

    before = len(df_input)
    normalized_input = df_input[input_col].map(_normalize_cell)
    keep_mask = ~normalized_input.isin(lookup_values)
    # Keep blank input values (they are not "found" in output)
    keep_mask = keep_mask | (normalized_input == "")

    df_clean = df_input.loc[keep_mask].copy().reset_index(drop=True)
    removed = before - len(df_clean)

    out_path = (result_file or "").strip() or input_file
    _write_table(df_clean, out_path)

    print("-" * 70)
    print(f"Removed    : {removed} row(s) (value found in output file)")
    print(f"Remaining  : {len(df_clean)} row(s)")
    print(f"Saved to   : {out_path}")
    print("=" * 70)
    return df_clean


if __name__ == "__main__":
    remove_rows_found_in_output(
        input_file=INPUT_FILE,
        output_file=OUTPUT_FILE,
        input_column_name=INPUT_COLUMN_NAME,
        output_column_name=OUTPUT_COLUMN_NAME,
        result_file=RESULT_FILE,
    )
