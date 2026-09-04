import os
import pandas as pd


def divide_excel_file(input_path, num_parts, output_dir=None, sheet_name=0):
    if num_parts < 1:
        raise ValueError("num_parts must be at least 1")
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Not found: {input_path}")

    abs_in = os.path.abspath(input_path)
    base_dir = os.path.dirname(abs_in)
    stem, _ = os.path.splitext(os.path.basename(abs_in))
    out_root = os.path.abspath(output_dir) if output_dir else base_dir
    os.makedirs(out_root, exist_ok=True)

    df = pd.read_excel(abs_in, sheet_name=sheet_name, header=0, engine="openpyxl")
    n_data = len(df)

    if num_parts == 1:
        out_path = os.path.join(out_root, f"{stem}_part_1.xlsx")
        df.to_excel(out_path, index=False, engine="openpyxl")
        return [out_path]

    if n_data == 0:
        out_path = os.path.join(out_root, f"{stem}_part_1.xlsx")
        df.to_excel(out_path, index=False, engine="openpyxl")
        for i in range(2, num_parts + 1):
            empty = pd.DataFrame(columns=df.columns)
            p = os.path.join(out_root, f"{stem}_part_{i}.xlsx")
            empty.to_excel(p, index=False, engine="openpyxl")
        return [os.path.join(out_root, f"{stem}_part_{i}.xlsx") for i in range(1, num_parts + 1)]

    # As-even-as-possible chunk sizes (larger chunks first if remainder)
    base = n_data // num_parts
    remainder = n_data % num_parts
    sizes = [base + (1 if k < remainder else 0) for k in range(num_parts)]

    written = []
    start = 0
    for part_idx, size in enumerate(sizes, start=1):
        chunk = df.iloc[start : start + size]
        start += size
        out_path = os.path.join(out_root, f"{stem}_part_{part_idx}.xlsx")
        chunk.to_excel(out_path, index=False, engine="openpyxl")
        written.append(out_path)

    return written


if __name__ == "__main__":
    # Edit these, then run: python divide_excel_file.py
    INPUT_PATH = r"ihmcl_mokha_apr_to_jun.xlsx"
    NUM_PARTS = 10
    OUTPUT_DIR = None  # None = same folder as INPUT_PATH; else set a folder path string

    paths = divide_excel_file(INPUT_PATH, NUM_PARTS, output_dir=OUTPUT_DIR)
    for p in paths:
        print(p)
