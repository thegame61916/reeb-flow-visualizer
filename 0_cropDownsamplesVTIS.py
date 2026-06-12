import os
from paraview.simple import *

# ============================================================
# Global parameters
# ============================================================

INPUT_DIR = "/media/mohit/8tbh/vgl/8tbSamsung/2TB backup/FinalResultsForPaper/caseStudy1/grids/s2"
OUTPUT_DIR = "/media/mohit/4TB_kingston_tufA2/hpc/datasets/MVK_s2/downsampledGrids"

#stilbene
# [xmin, xmax, ymin, ymax, zmin, zmax]
#VOI = [10, 65, 10, 55, 10, 90]


#MVK_s1/s2
# [xmin, xmax, ymin, ymax, zmin, zmax]
VOI = [20, 75, 20, 80, 20, 48]

# [x_rate, y_rate, z_rate]
SAMPLE_RATE = [1, 1, 1]




# Set to None to load all arrays
POINT_ARRAYS = ['orb00', 'orb01', 'seg']

# ============================================================
# Script
# ============================================================

os.makedirs(OUTPUT_DIR, exist_ok=True)

param_file = os.path.join(OUTPUT_DIR, "subset_parameters.txt")
with open(param_file, "w") as f:
    f.write("Batch VTI subset + tetrahedralization parameters\n")
    f.write("===============================================\n")
    f.write(f"Input directory: {INPUT_DIR}\n")
    f.write(f"Output directory: {OUTPUT_DIR}\n")
    f.write(f"VOI: {VOI}\n")
    f.write(f"Sample rate: {SAMPLE_RATE}\n")
    f.write(f"Point arrays: {POINT_ARRAYS}\n")

vti_files = sorted(
    f for f in os.listdir(INPUT_DIR)
    if f.lower().endswith(".vti")
)

if not vti_files:
    raise RuntimeError(f"No .vti files found in: {INPUT_DIR}")

for filename in vti_files:
    input_path = os.path.join(INPUT_DIR, filename)

    base_name = os.path.splitext(filename)[0]
    output_path = os.path.join(OUTPUT_DIR, base_name + ".vtu")

    print(f"Processing: {input_path}")

    reader = XMLImageDataReader(
        registrationName=base_name,
        FileName=[input_path]
    )

    if POINT_ARRAYS is not None:
        reader.Set(
            PointArrayStatus=POINT_ARRAYS,
            TimeArray='None'
        )

    subset = ExtractSubset(
    registrationName=base_name + "_subset",
    Input=reader
    )

    subset.VOI = VOI

    # ParaView 6.0 expects one scalar value per direction
    subset.SampleRateI = SAMPLE_RATE[0]
    subset.SampleRateJ = SAMPLE_RATE[1]
    subset.SampleRateK = SAMPLE_RATE[2]

    try:
        subset.IncludeBoundary = 1
    except Exception:
        pass

    tetra = Tetrahedralize(
        registrationName=base_name + "_tetra",
        Input=subset
    )

    SaveData(output_path, proxy=tetra)

    Delete(tetra)
    Delete(subset)
    Delete(reader)

    print(f"Saved: {output_path}")

print("Done.")
