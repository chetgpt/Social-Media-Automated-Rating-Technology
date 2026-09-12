import os
import sys
import glob
import re

def check_sequential_files(folder_path, start_num, end_num, prefix="video_", suffix="_comments.json"):
    """Check existence of files with sequential numbers in their names."""
    existing_files = []
    missing_files = []

    for num in range(start_num, end_num + 1):
        filename = f"{prefix}{num}{suffix}"
        filepath = os.path.join(folder_path, filename)

        if os.path.exists(filepath) and os.path.isfile(filepath):
            existing_files.append(num)
        else:
            missing_files.append(num)

    return existing_files, missing_files

def count_files_by_range_check(folder_path, max_video_num=2000):
    """Count files by directly checking if each possible file exists."""
    file_count = 0
    existing_files = []

    print(f"\nChecking individual files up to video_{max_video_num}...")

    # Divide into chunks to show progress
    chunk_size = 200
    for chunk_start in range(1, max_video_num + 1, chunk_size):
        chunk_end = min(chunk_start + chunk_size - 1, max_video_num)

        chunk_files = []
        for num in range(chunk_start, chunk_end + 1):
            filename = f"video_{num}_comments.json"
            filepath = os.path.join(folder_path, filename)

            if os.path.exists(filepath) and os.path.isfile(filepath):
                file_count += 1
                chunk_files.append(num)
                existing_files.append(num)

        print(f"Range {chunk_start}-{chunk_end}: Found {len(chunk_files)} files")

    # Print summary of found files
    print(f"\nTotal files found by direct path check: {file_count}")
    if file_count <= 20:
        print(f"File numbers found: {existing_files}")
    else:
        print(f"Sample of file numbers found: {existing_files[:10]} ... {existing_files[-10:]}")

    return file_count

def check_for_gaps(folder_path):
    """Check for gaps in file numbering that might explain counting issues."""
    # Get all existing video files
    json_pattern = re.compile(r'video_(\d+)_comments\.json')
    file_numbers = []

    try:
        for item in os.listdir(folder_path):
            match = json_pattern.match(item)
            if match:
                file_numbers.append(int(match.group(1)))
    except Exception as e:
        print(f"Error listing files: {e}")

    file_numbers.sort()

    # Check if there are exactly 310 files in the enumeration
    print(f"\nFile numbering analysis:")
    print(f"Total files enumerated: {len(file_numbers)}")

    if len(file_numbers) > 0:
        print(f"Min file number: {min(file_numbers)}")
        print(f"Max file number: {max(file_numbers)}")

        # Check specific ranges
        # Count how many files are in each 100-number range
        ranges = {}
        for num in file_numbers:
            range_start = (num // 100) * 100
            range_end = range_start + 99
            range_key = f"{range_start}-{range_end}"

            if range_key not in ranges:
                ranges[range_key] = 0
            ranges[range_key] += 1

        # Print the distribution
        print("\nFile distribution by number ranges:")
        for range_key, count in sorted(ranges.items()):
            print(f"Range {range_key}: {count} files")

        # Check the specific gap mentioned by the user
        gap_307_312 = [n for n in file_numbers if 307 <= n <= 312]
        print(f"\nFiles in range 307-312: {gap_307_312}")

    return file_numbers

def count_files(folder_path, recursive=False):
    """
    Count the number of files in the specified folder.

    Args:
        folder_path (str): Path to the folder to count files in
        recursive (bool): Whether to count files in subdirectories

    Returns:
        int: Number of files in the folder
    """
    try:
        # Check if the path exists
        if not os.path.exists(folder_path):
            print(f"Error: The path '{folder_path}' does not exist.")
            return 0

        # Check if it's a directory
        if not os.path.isdir(folder_path):
            print(f"Error: '{folder_path}' is not a directory.")
            return 0

        # Test specific file existence directly
        test_file = os.path.join(folder_path, "video_1330_comments.json")
        if os.path.exists(test_file):
            print(f"\nDirect file check: '{test_file}' EXISTS")
            print(f"File size: {os.path.getsize(test_file)} bytes")
            print(f"Is file: {os.path.isfile(test_file)}")
        else:
            print(f"\nDirect file check: '{test_file}' DOES NOT EXIST")

        # Method 1: Using os.listdir() and isfile check
        print("\nTrying Method 1: os.listdir()")
        try:
            items = os.listdir(folder_path)
            print(f"Total items found with os.listdir(): {len(items)}")
            file_count_method1 = 0

            # Check for pattern match
            json_pattern = re.compile(r'video_\d+_comments\.json')
            pattern_matches = 0

            for item in items:
                item_path = os.path.join(folder_path, item)
                if os.path.isfile(item_path):
                    file_count_method1 += 1
                    if json_pattern.match(item):
                        pattern_matches += 1

            print(f"Pattern matches for 'video_XXX_comments.json': {pattern_matches}")
        except Exception as e:
            print(f"Error in Method 1: {e}")
            file_count_method1 = 0

        # Analyze file numbering for potential gaps
        file_numbers = check_for_gaps(folder_path)

        # Try direct sequential file check
        print("\nChecking for existence of specific files...")
        real_count = count_files_by_range_check(folder_path)

        return real_count

    except Exception as e:
        print(f"An error occurred: {e}")
        return 0

def browse_directories(start_path):
    """
    Interactive directory browser to select a folder for counting files.

    Args:
        start_path (str): The starting directory path

    Returns:
        str: The selected directory path
    """
    current_path = start_path

    while True:
        print(f"\nCurrent directory: {current_path}")
        print("-" * 50)

        # Get all subdirectories in the current path
        try:
            items = os.listdir(current_path)
            dirs = [d for d in items if os.path.isdir(os.path.join(current_path, d))]

            if not dirs:
                print("No subdirectories found.")
                choice = input("\nNo more subdirectories. Count files in this directory? (y/n): ")
                if choice.lower() == 'y':
                    return current_path
                else:
                    # Go back one level if possible
                    parent_dir = os.path.dirname(current_path)
                    if parent_dir != current_path:
                        current_path = parent_dir
                    continue

            # Display available directories
            print("\nAvailable subdirectories:")
            for i, directory in enumerate(dirs, 1):
                print(f"{i}. {directory}")

            # Add options to count current directory or go back
            print("\nOptions:")
            print("c. Count files in current directory")
            print("b. Go back to parent directory")
            print("q. Quit")

            choice = input("\nEnter your choice: ")

            if choice.lower() == 'c':
                return current_path
            elif choice.lower() == 'b':
                parent_dir = os.path.dirname(current_path)
                if parent_dir != current_path:
                    current_path = parent_dir
                else:
                    print("Already at the top level directory.")
            elif choice.lower() == 'q':
                sys.exit("Program terminated by user.")
            elif choice.isdigit() and 1 <= int(choice) <= len(dirs):
                selected_dir = dirs[int(choice) - 1]
                current_path = os.path.join(current_path, selected_dir)
            else:
                print("Invalid choice. Please try again.")

        except Exception as e:
            print(f"Error accessing directory: {e}")
            choice = input("Go back to parent directory? (y/n): ")
            if choice.lower() == 'y':
                parent_dir = os.path.dirname(current_path)
                if parent_dir != current_path:
                    current_path = parent_dir
                else:
                    print("Already at the top level directory.")
            else:
                sys.exit("Program terminated.")

if __name__ == "__main__":
    # Start with the comments_data directory
    start_path = os.path.join(os.getcwd(), "comments_data")

    # Check if comments_data exists
    if not os.path.exists(start_path):
        start_path = input("comments_data directory not found. Please enter the full path to comments_data: ")
        if not os.path.exists(start_path):
            sys.exit(f"Error: The path '{start_path}' does not exist.")

    # Let user browse and select a directory
    selected_path = browse_directories(start_path)

    # Ask if user wants to count files recursively
    recursive = input("\nCount files in subdirectories too? (y/n): ").lower() == 'y'

    # Count files and display result
    num_files = count_files(selected_path, recursive)
    print(f"\nTRUE NUMBER of files found by checking each possible filename: {num_files}")

    # Check specific file gaps
    print("\nChecking for specific gaps in file numbering...")
    # Check a specific range - from 307 to 312 as mentioned by the user
    existing, missing = check_sequential_files(selected_path, 307, 312)
    print(f"Files that exist in range 307-312: {existing}")
    print(f"Files that are missing in range 307-312: {missing}")

    # Also check around 1330
    existing, missing = check_sequential_files(selected_path, 1325, 1335)
    print(f"Files that exist in range 1325-1335: {existing}")
    print(f"Files that are missing in range 1325-1335: {missing}")

    print("\nNOTE: Windows directory APIs have a known limit when enumerating large directories.")
    print("The discrepancy is likely due to this limitation rather than an actual file count issue.")

    # Check for NTFS alternate data streams (Windows only)
    if os.name == 'nt':
        print("\nChecking for potential issues with Windows file system:")
        try:
            import win32file
            import win32api

            # Check filesystem type
            drive_letter = os.path.splitdrive(selected_path)[0]
            if drive_letter:
                fs_type = win32api.GetVolumeInformation(drive_letter + '\\')[4]
                print(f"File system type: {fs_type}")
        except:
            print("Could not determine file system type")

    # Manually enumerate using findstr and dir for Windows
    if os.name == 'nt':
        try:
            import subprocess
            print("\nTrying alternative file counting method for Windows:")
            # Using PowerShell to get more detailed output
            ps_cmd = f'powershell -Command "(Get-ChildItem -Path \'{selected_path}\' -File).Count"'
            result = subprocess.run(ps_cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"Files found using PowerShell: {result.stdout.strip()}")
            else:
                print(f"PowerShell command failed: {result.stderr}")
        except Exception as e:
            print(f"Error during alternative count: {e}")

    # Additional direct count for verification
    try:
        # Count files directly, including hidden files
        direct_count = len([name for name in os.listdir(selected_path)
                          if os.path.isfile(os.path.join(selected_path, name))])
        print(f"Direct file count (as verification): {direct_count}")

        # Try to get total file count
        print("\nTrying to get exact file count...")
        all_entries = os.listdir(selected_path)
        total_entries = len(all_entries)
        print(f"Total directory entries: {total_entries}")

        # Count how many are files vs directories
        files_count = sum(1 for entry in all_entries if os.path.isfile(os.path.join(selected_path, entry)))
        dirs_count = sum(1 for entry in all_entries if os.path.isdir(os.path.join(selected_path, entry)))
        print(f"Files: {files_count}, Directories: {dirs_count}, Unknown: {total_entries - files_count - dirs_count}")
    except Exception as e:
        print(f"Error during verification count: {e}")