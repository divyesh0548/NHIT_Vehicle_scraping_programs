import os
import pandas as pd


def _read_table(input_path, sheet_name=0):
    """Read Excel or CSV based on file extension."""
    ext = os.path.splitext(input_path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(input_path, dtype=str)
    if ext in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return pd.read_excel(
            input_path, sheet_name=sheet_name, header=0, engine="openpyxl", dtype=str
        )
    if ext in {".xls"}:
        return pd.read_excel(input_path, sheet_name=sheet_name, header=0, dtype=str)
    raise ValueError(
        f"Unsupported file type '{ext}'. Use .xlsx / .xls / .csv — got: {input_path}"
    )


def _safe_keyword(keyword):
    """Normalize keyword for use in filenames. Empty means no keyword in the name."""
    if keyword is None:
        return ""
    cleaned = str(keyword).strip()
    if not cleaned:
        return ""
    for ch in '<>:"/\\|?*':
        cleaned = cleaned.replace(ch, "_")
    cleaned = "_".join(cleaned.split())
    return cleaned


def divide_excel_file(input_path, num_parts, keyword, output_dir=None, sheet_name=0):
    if num_parts < 1:
        raise ValueError("num_parts must be at least 1")
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Not found: {input_path}")

    keyword = _safe_keyword(keyword)
    abs_in = os.path.abspath(input_path)
    base_dir = os.path.dirname(abs_in)
    stem, _ = os.path.splitext(os.path.basename(abs_in))
    out_root = os.path.abspath(output_dir) if output_dir else base_dir
    os.makedirs(out_root, exist_ok=True)

    def part_name(part_idx):
        if keyword:
            return f"{stem}_{keyword}_part_{part_idx}.xlsx"
        return f"{stem}_part_{part_idx}.xlsx"

    df = _read_table(abs_in, sheet_name=sheet_name)
    n_data = len(df)
    print(f"Loaded {n_data} rows from {abs_in}")

    if num_parts == 1:
        out_path = os.path.join(out_root, part_name(1))
        df.to_excel(out_path, index=False, engine="openpyxl")
        return [out_path]

    if n_data == 0:
        out_path = os.path.join(out_root, part_name(1))
        df.to_excel(out_path, index=False, engine="openpyxl")
        for i in range(2, num_parts + 1):
            empty = pd.DataFrame(columns=df.columns)
            p = os.path.join(out_root, part_name(i))
            empty.to_excel(p, index=False, engine="openpyxl")
        return [os.path.join(out_root, part_name(i)) for i in range(1, num_parts + 1)]

    # As-even-as-possible chunk sizes (larger chunks first if remainder)
    base = n_data // num_parts
    remainder = n_data % num_parts
    sizes = [base + (1 if k < remainder else 0) for k in range(num_parts)]

    written = []
    start = 0
    for part_idx, size in enumerate(sizes, start=1):
        chunk = df.iloc[start : start + size]
        start += size
        out_path = os.path.join(out_root, part_name(part_idx))
        chunk.to_excel(out_path, index=False, engine="openpyxl")
        written.append(out_path)

    return written


if __name__ == "__main__":
    # Edit these, then run: python divide_excel_file.py
    INPUT_PATH = r"C:\Divyesh\S_T_Vehicle_processing\Usaka JAN-MAR 26.xlsx"
    NUM_PARTS = 60
    KEYWORD = ""  # optional; empty leaves it out of the filename
    OUTPUT_DIR = None  # None = same folder as INPUT_PATH; else set a folder path string

    paths = divide_excel_file(
        INPUT_PATH, NUM_PARTS, keyword=KEYWORD, output_dir=OUTPUT_DIR
    )
    for p in paths:
        print(p)
