"""Merge Excel files from a folder into one workbook.

All source files are expected to share the same column names.
Source files are never deleted or modified.
"""

import os
from pathlib import Path

import pandas as pd

SUPPORTED_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}


def list_excel_files(folder_path):
    folder = Path(folder_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"Folder not found: {folder}")

    files = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.name.startswith("~$") or path.name.startswith("."):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return files


def merge_excel_folder(folder_path, output_path=None, sheet_name=0):
    """
    Merge every Excel file in folder_path into one Excel file.

    Returns the output path. Input files are left unchanged.
    """
    folder = Path(folder_path).resolve()
    files = list_excel_files(folder)
    if not files:
        raise FileNotFoundError(f"No Excel files found in: {folder}")

    if output_path is None:
        output_path = folder / f"{folder.name}_merged.xlsx"
    else:
        output_path = Path(output_path)
        if not output_path.is_absolute():
            output_path = folder / output_path

    frames = []
    for path in files:
        if path.resolve() == output_path.resolve():
            print(f"[SKIP] Output file itself: {path.name}")
            continue
        print(f"[READ] {path.name}")
        df = pd.read_excel(path, sheet_name=sheet_name, dtype=str)
        frames.append(df)

    if not frames:
        raise ValueError("No Excel data to merge after skipping output file.")

    merged = pd.concat(frames, ignore_index=True)
    os.makedirs(output_path.parent, exist_ok=True)
    merged.to_excel(output_path, index=False, engine="openpyxl")

    print(f"[OK] Merged {len(frames)} file(s), {len(merged)} rows -> {output_path}")
    return str(output_path)


if __name__ == "__main__":
    # Edit these, then run: python temp-merge-output.py
    FOLDER_PATH = r"C:\Divyesh\S_T_Vehicle_processing\Usaka"
    OUTPUT_PATH = None  # None = <folder_name>_merged.xlsx inside the folder

    merge_excel_folder(FOLDER_PATH, output_path=OUTPUT_PATH)
