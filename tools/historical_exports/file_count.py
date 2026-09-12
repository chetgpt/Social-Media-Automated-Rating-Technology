import os
import glob

# Directory path
directory = r"C:\Users\DELL\Downloads\TIKTOK\TEST\TEST Tiktok Scraper Modules\comments_data\session_20250502_111632\comments"

# Get all files with os.listdir
files_listdir = [f for f in os.listdir(directory) if f.endswith('_comments.json')]
print(f"Files found with os.listdir: {len(files_listdir)}")

# Get all files with glob.glob
glob_pattern = os.path.join(directory, "*_comments.json")
files_glob = glob.glob(glob_pattern)
print(f"Files found with glob.glob: {len(files_glob)}")

# Get first and last 5 files when sorted
sorted_files = sorted(files_listdir, key=lambda x: int(x.split('_')[1]) if x.split('_')[1].isdigit() else 0)
print("\nFirst 5 files:")
for f in sorted_files[:5]:
    print(f"  {f}")

print("\nLast 5 files:")
for f in sorted_files[-5:]:
    print(f"  {f}")

# Count expected files vs actual
if sorted_files:
    first_num = int(sorted_files[0].split('_')[1])
    last_num = int(sorted_files[-1].split('_')[1])
    expected_count = last_num - first_num + 1
    print(f"\nExpected files if sequential: {expected_count}")
    print(f"Actual files: {len(sorted_files)}")
    print(f"Missing files: {expected_count - len(sorted_files)}")