"""Remove rows from file 2 when their vehicle number appears in file 1.

Source files are never deleted. Output is written to a new Excel file.
"""

from pathlib import Path

import pandas as pd

from vehicle_number_utils import normalize_vehicle_number


def remove_vehicles_found_in_lookup(
    lookup_file,
    target_file,
    lookup_vehicle_col,
    target_vehicle_col,
    output_path=None,
    lookup_sheet=0,
    target_sheet=0,
):
    """
    Take all vehicle numbers from lookup_file and drop matching rows
    from target_file. Returns the output path.
    """
    lookup_path = Path(lookup_file)
    target_path = Path(target_file)

    if not lookup_path.is_file():
        raise FileNotFoundError(f"Lookup file not found: {lookup_path}")
    if not target_path.is_file():
        raise FileNotFoundError(f"Target file not found: {target_path}")

    if output_path is None:
        output_path = target_path.with_name(
            f"{target_path.stem}_after_lookup{target_path.suffix}"
        )
    else:
        output_path = Path(output_path)

    lookup_df = pd.read_excel(lookup_path, sheet_name=lookup_sheet, dtype=str)
    target_df = pd.read_excel(target_path, sheet_name=target_sheet, dtype=str)

    if lookup_vehicle_col not in lookup_df.columns:
        raise KeyError(
            f"Column '{lookup_vehicle_col}' not found in lookup file. "
            f"Columns: {list(lookup_df.columns)}"
        )
    if target_vehicle_col not in target_df.columns:
        raise KeyError(
            f"Column '{target_vehicle_col}' not found in target file. "
            f"Columns: {list(target_df.columns)}"
        )

    lookup_numbers = {
        normalize_vehicle_number(v)
        for v in lookup_df[lookup_vehicle_col]
        if normalize_vehicle_number(v)
    }

    before = len(target_df)
    keep_mask = ~target_df[target_vehicle_col].apply(normalize_vehicle_number).isin(
        lookup_numbers
    )
    result_df = target_df[keep_mask].copy()
    removed = before - len(result_df)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result_df.to_excel(output_path, index=False, engine="openpyxl")

    print(f"Lookup file: {lookup_path}")
    print(f"  Vehicle column: {lookup_vehicle_col}")
    print(f"  Unique vehicle numbers: {len(lookup_numbers)}")
    print(f"Target file: {target_path}")
    print(f"  Vehicle column: {target_vehicle_col}")
    print(f"  Rows before: {before}")
    print(f"  Rows removed: {removed}")
    print(f"  Rows remaining: {len(result_df)}")
    print(f"[OK] Wrote: {output_path}")
    return str(output_path)


if __name__ == "__main__":
    # Edit these, then run: python temp-output-lookup.py
    LOOKUP_FILE = r"C:\Divyesh\S_T_Vehicle_processing\Final Processed - Raksha\Final Processed - Raksha.xlsx"
    TARGET_FILE = r"C:\Divyesh\S_T_Vehicle_processing\Usaka - base input.xlsx"

    # Column names in each file (can be different)
    LOOKUP_VEHICLE_COL = "VEH REG NO"
    TARGET_VEHICLE_COL = "Veh Reg No."

    OUTPUT_PATH = None  # None = <target>_after_lookup.xlsx next to target file

    remove_vehicles_found_in_lookup(
        LOOKUP_FILE,
        TARGET_FILE,
        lookup_vehicle_col=LOOKUP_VEHICLE_COL,
        target_vehicle_col=TARGET_VEHICLE_COL,
        output_path=OUTPUT_PATH,
    )
