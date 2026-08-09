import os
import json
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import time
import glob
from datetime import datetime
import textwrap
import platform
import re
import math
import traceback
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed


# Add back ReportLab imports for PDF generation
try:
    from reportlab.lib.pagesizes import A4, letter, landscape
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Constants for LLM compatibility
    MAX_PDF_SIZE_MB = 10  # Maximum PDF size for LLM processing in MB
    MAX_COMMENTS_PER_PAGE = 100  # Maximum number of comments to show on a single page
    BYTES_PER_VIDEO_ESTIMATE = 500000  # Rough estimate of bytes per video in PDF

    # Try to register emoji-compatible fonts based on OS
    try:
        os_name = platform.system()
        if os_name == 'Windows':
            # Try to register Segoe UI Emoji font for Windows
            font_path = os.path.join(os.environ['WINDIR'], 'Fonts', 'seguiemj.ttf')
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont('SegoeEmoji', font_path))
                EMOJI_FONT_AVAILABLE = 'SegoeEmoji'
            else:
                EMOJI_FONT_AVAILABLE = False
        elif os_name == 'Darwin':  # macOS
            # Try common emoji font locations on macOS
            font_paths = [
                '/System/Library/Fonts/Apple Color Emoji.ttc',
                '/System/Library/Fonts/Apple Color Emoji.ttf'
            ]
            for font_path in font_paths:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('AppleEmoji', font_path))
                    EMOJI_FONT_AVAILABLE = 'AppleEmoji'
                    break
            else:
                EMOJI_FONT_AVAILABLE = False
        else:  # Linux and others
            # Try Noto Color Emoji for Linux
            font_paths = [
                '/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf',
                '/usr/share/fonts/google-noto/NotoColorEmoji.ttf'
            ]
            for font_path in font_paths:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('NotoEmoji', font_path))
                    EMOJI_FONT_AVAILABLE = 'NotoEmoji'
                    break
            else:
                EMOJI_FONT_AVAILABLE = False
    except Exception as e:
        print(f"Note: Emoji font registration failed: {str(e)}")
        EMOJI_FONT_AVAILABLE = False

    # Register a fallback standard font
    try:
        # Register Arial or a similar common font
        common_fonts = [
            ('Arial', 'arial.ttf', 'arialbd.ttf'),
            ('Helvetica', 'Helvetica.ttf', 'Helvetica-Bold.ttf'),
            ('DejaVuSans', 'DejaVuSans.ttf', 'DejaVuSans-Bold.ttf')
        ]

        for font_name, regular_file, bold_file in common_fonts:
            try:
                pdfmetrics.registerFont(TTFont(font_name, regular_file))
                pdfmetrics.registerFont(TTFont(f"{font_name}-Bold", bold_file))
                DEFAULT_FONT = font_name
                break
            except:
                continue
        else:
            DEFAULT_FONT = 'Helvetica'  # Default ReportLab font
    except:
        DEFAULT_FONT = 'Helvetica'  # Default ReportLab font

    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    EMOJI_FONT_AVAILABLE = False
    DEFAULT_FONT = None

def list_session_folders():
    """Find all session folders in the comments_data directory"""
    base_dir = "comments_data"
    if not os.path.exists(base_dir):
        return []

    # Find all potential session folders
    session_folders = []

    for d in os.listdir(base_dir):
        folder_path = os.path.join(base_dir, d)

        # Skip if not a directory
        if not os.path.isdir(folder_path):
            continue

        # Check if it has the required structure (comments folder and logs folder)
        has_comments = os.path.isdir(os.path.join(folder_path, "comments"))
        has_logs = os.path.isdir(os.path.join(folder_path, "logs"))

        # If it has either comments or logs folder, consider it a valid session folder
        if has_comments or has_logs:
            session_folders.append(d)

    return session_folders

def select_session_folder():
    """Allow user to select a session folder"""
    # Get list of available session folders
    session_folders = list_session_folders()

    if not session_folders:
        messagebox.showerror("Error", "No session folders found in comments_data directory.")
        return None

    # Create root window
    select_root = tk.Toplevel()
    select_root.title("Select Session Folder")
    select_root.geometry("650x400")  # Wider to show more details
    select_root.focus_force()  # Make this window focused
    select_root.grab_set()  # Make the window modal

    # Create label
    label = tk.Label(select_root, text="Select a session folder:")
    label.pack(pady=10)

    # Create frame for listbox and scrollbar
    list_frame = tk.Frame(select_root)
    list_frame.pack(pady=10, padx=20, fill=tk.BOTH, expand=True)

    # Add scrollbar
    scrollbar = tk.Scrollbar(list_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    # Create listbox with scrollbar
    listbox = tk.Listbox(list_frame, width=70, height=15, font=("Courier New", 10))
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Connect scrollbar to listbox
    listbox.config(yscrollcommand=scrollbar.set)
    scrollbar.config(command=listbox.yview)

    # Add session folders to listbox with additional info
    folder_info = []
    for folder in sorted(session_folders, reverse=True):  # Newest first (assuming timestamp in name)
        base_path = os.path.join("comments_data", folder)

        # Check for comments and logs folders
        has_comments = os.path.isdir(os.path.join(base_path, "comments"))
        has_logs = os.path.isdir(os.path.join(base_path, "logs"))

        # Count files in comments folder if it exists
        comment_files = 0
        if has_comments:
            comment_path = os.path.join(base_path, "comments")
            try:
                comment_files = len([f for f in os.listdir(comment_path) if f.endswith("_comments.json")])
            except Exception as e:
                print(f"Error counting files in {comment_path}: {str(e)}")
                comment_files = -1  # Indicate error

        # Format display string
        display_text = f"{folder} [{comment_files} comments]"
        display_text += " [+logs]" if has_logs else ""

        listbox.insert(tk.END, display_text)
        folder_info.append(folder)

    # Select first item by default
    if session_folders:
        listbox.selection_set(0)

    # Variable to store selected folder
    selected_folder = [None]

    # Function to handle selection
    def on_select():
        if listbox.curselection():
            index = listbox.curselection()[0]
            folder_name = folder_info[index]
            selected_folder[0] = os.path.join("comments_data", folder_name)
            print(f"Selected folder in dialog: {selected_folder[0]}")
            select_root.grab_release()
            select_root.destroy()
        else:
            print("No selection made in listbox")
            messagebox.showerror("Error", "Please select a session folder")
            # Don't close the dialog, let the user try again

    # Function to handle double-click
    def on_double_click(event):
        on_select()

    # Bind double-click event
    listbox.bind("<Double-Button-1>", on_double_click)

    # Function to handle cancel
    def on_cancel():
        selected_folder[0] = None
        select_root.grab_release()
        select_root.destroy()

    # Create buttons
    button_frame = tk.Frame(select_root)
    button_frame.pack(pady=10)

    select_button = tk.Button(button_frame, text="Select", command=on_select)
    select_button.pack(side=tk.LEFT, padx=10)

    cancel_button = tk.Button(button_frame, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=10)

    # Handle window close event
    select_root.protocol("WM_DELETE_WINDOW", on_cancel)

    # Wait until this window is closed
    select_root.wait_window()

    return selected_folder[0]

def get_comments_folder(session_folder):
    """Get the comments folder path from the session folder"""
    comments_path = os.path.join(session_folder, "comments")
    if os.path.exists(comments_path) and os.path.isdir(comments_path):
        # Check if there are comment files directly in this folder
        json_files = [f for f in os.listdir(comments_path) if f.endswith('_comments.json')]
        if json_files:
            return comments_path

    # If not found, try to find any folder containing comment files
    print("Searching for comment files in all subdirectories...")
    all_potential_folders = []

    for root, dirs, files in os.walk(session_folder):
        json_files = [f for f in files if f.endswith('_comments.json')]
        if json_files:
            all_potential_folders.append((root, len(json_files)))

    if all_potential_folders:
        # Sort by number of comment files (descending)
        all_potential_folders.sort(key=lambda x: x[1], reverse=True)
        folder, count = all_potential_folders[0]
        print(f"Found main comments folder: {folder} with {count} files")

        # If there are multiple folders with comments, report them
        if len(all_potential_folders) > 1:
            print("Note: Found multiple folders with comment files:")
            for folder, count in all_potential_folders:
                print(f"  - {folder}: {count} files")

        return folder

    return None

def analyze_logs_folder(session_folder):
    """Analyze the logs folder to determine expected video count and compare with comments folder"""
    logs_folder = os.path.join(session_folder, "logs")
    comments_folder = os.path.join(session_folder, "comments")

    if not os.path.exists(logs_folder):
        return {
            "success": False,
            "message": f"Logs folder not found in {session_folder}"
        }

    if not os.path.exists(comments_folder):
        return {
            "success": False,
            "message": f"Comments folder not found in {session_folder}"
        }

    # Find all diagnostics files
    diagnostics_folder = os.path.join(logs_folder, "diagnostics")
    if not os.path.exists(diagnostics_folder):
        return {
            "success": False,
            "message": f"Diagnostics folder not found in {logs_folder}"
        }

    try:
        # Get expected videos from diagnostics files
        expected_videos = []
        pattern = re.compile(r'video_(\d+)_diagnostics\.txt')

        for entry in os.listdir(diagnostics_folder):
            if os.path.isfile(os.path.join(diagnostics_folder, entry)):
                match = pattern.match(entry)
                if match:
                    video_number = int(match.group(1))
                    expected_videos.append(video_number)

        if not expected_videos:
            return {
                "success": False,
                "message": f"No diagnostic files found in {diagnostics_folder}"
            }

        # Get actual videos from comments files
        actual_videos = []
        comment_pattern = re.compile(r'video_(\d+)_comments\.json')

        for entry in os.listdir(comments_folder):
            if os.path.isfile(os.path.join(comments_folder, entry)):
                match = comment_pattern.match(entry)
                if match:
                    video_number = int(match.group(1))
                    actual_videos.append(video_number)

        if not actual_videos:
            return {
                "success": False,
                "message": f"No comment files found in {comments_folder}"
            }

        # Sort both lists for easier comparison
        expected_videos.sort()
        actual_videos.sort()

        # Find missing videos (in diagnostics but not in comments)
        missing_videos = list(set(expected_videos) - set(actual_videos))
        missing_videos.sort()

        # Find extra videos (in comments but not in diagnostics)
        extra_videos = list(set(actual_videos) - set(expected_videos))
        extra_videos.sort()

        # Generate summary
        result = {
            "success": True,
            "expected_count": len(expected_videos),
            "actual_count": len(actual_videos),
            "missing_count": len(missing_videos),
            "extra_count": len(extra_videos),
            "missing_videos": missing_videos,
            "extra_videos": extra_videos,
            "expected_range": [min(expected_videos), max(expected_videos)] if expected_videos else [0, 0],
            "actual_range": [min(actual_videos), max(actual_videos)] if actual_videos else [0, 0]
        }

        # Additional analysis: check if there are videos with no comments
        zero_comment_videos = []

        for video_num in actual_videos:
            file_path = os.path.join(comments_folder, f"video_{video_num}_comments.json")
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get("comment_count", 0) == 0 or len(data.get("comments", [])) == 0:
                        zero_comment_videos.append(video_num)
            except Exception as e:
                # If file can't be read, consider it as potentially problematic
                print(f"Warning: Could not analyze file for video {video_num}: {str(e)}")
                zero_comment_videos.append(video_num)

        result["zero_comment_videos"] = zero_comment_videos
        result["zero_comment_count"] = len(zero_comment_videos)

        return result

    except Exception as e:
        return {
            "success": False,
            "message": f"Error analyzing logs folder: {str(e)}"
        }

def walk_comment_tree(comments):
    """Yield every comment and nested reply once, with its tree depth."""
    if not isinstance(comments, list):
        return
    stack = [(comment, 0) for comment in reversed(comments)]
    seen_objects = set()
    while stack:
        comment, depth = stack.pop()
        if not isinstance(comment, dict) or id(comment) in seen_objects:
            continue
        seen_objects.add(id(comment))
        yield comment, depth
        replies = comment.get("replies")
        if isinstance(replies, list):
            stack.extend((reply, depth + 1) for reply in reversed(replies))


def get_comment_total(video_data):
    """Return the complete scraped comment count, including nested replies."""
    comments = video_data.get("comments", [])
    if isinstance(comments, list):
        return sum(1 for _comment, _depth in walk_comment_tree(comments))
    try:
        return int(video_data.get("comment_count", 0) or 0)
    except (TypeError, ValueError):
        return 0

PLATFORM_AGGREGATE_FILES = {
    "youtube_comments.json": "youtube",
    "instagram_comments.json": "instagram",
    "facebook_comments.json": "facebook",
    "tiktok_comments.json": "tiktok",
    "x_comments.json": "x",
}


def infer_video_platform(video_data, file_name=""):
    """Infer platform from explicit metadata, URL, or a platform-prefixed file."""
    explicit = str(video_data.get("platform", "") or "").strip().lower()
    if explicit in {"youtube", "tiktok", "instagram", "facebook", "x"}:
        return explicit

    url = str(video_data.get("url") or video_data.get("video_url") or "").lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    if "instagram.com" in url:
        return "instagram"
    if "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    if "x.com" in url or "twitter.com" in url:
        return "x"

    lower_name = os.path.basename(file_name).lower()
    if lower_name in PLATFORM_AGGREGATE_FILES:
        return PLATFORM_AGGREGATE_FILES[lower_name]
    for platform_name in ("youtube", "tiktok", "instagram", "facebook", "x"):
        if lower_name.startswith(f"{platform_name}_video_"):
            return platform_name
    return ""


def is_youtube_video_record(video_data, file_name=""):
    """Return true only when metadata identifies a record as YouTube."""
    return infer_video_platform(video_data, file_name) == "youtube"


def video_record_key(video_data, file_name=""):
    platform_name = infer_video_platform(video_data, file_name) or "unknown"
    identity = str(
        video_data.get("video_id")
        or video_data.get("media_id")
        or video_data.get("url")
        or video_data.get("video_url")
        or ""
    ).strip()
    return (platform_name, identity) if identity else None

def comment_file_sort_key(file_path):
    """Prefer richer aggregate exports, then process per-video files."""
    file_name = os.path.basename(file_path)
    if file_name in PLATFORM_AGGREGATE_FILES:
        return (0, 0, file_name)
    match = re.search(r'video_(\d+)_comments\.json$', file_name)
    if match:
        return (1, int(match.group(1)), file_name)
    return (2, 0, file_name)

def append_video_record(combined_data, video_data, file_name, fallback_number=None, seen_keys=None):
    """Normalize and append one video record to combined data."""
    if not isinstance(video_data, dict):
        return False

    platform_name = infer_video_platform(video_data, file_name)
    if platform_name:
        video_data.setdefault("platform", platform_name)
    record_key = video_record_key(video_data, file_name)
    if seen_keys is not None and record_key and record_key in seen_keys:
        return False

    if "video_number" not in video_data:
        if fallback_number is not None:
            video_data["video_number"] = fallback_number
        else:
            try:
                video_data["video_number"] = int(file_name.split('_')[1])
            except Exception:
                video_data["video_number"] = combined_data["total_videos"] + 1

    if "comments" not in video_data or not isinstance(video_data.get("comments"), list):
        video_data["comments"] = []

    video_data["comment_count"] = get_comment_total(video_data)
    combined_data["total_videos"] += 1
    combined_data["total_comments"] += video_data["comment_count"]
    combined_data["videos"].append(video_data)
    if seen_keys is not None and record_key:
        seen_keys.add(record_key)
    return True

def combine_comments(folder_path):
    """Combine all comment JSON files in the folder"""
    # Get all JSON files in the folder using glob (might find more files)
    import glob

    # Diagnostic output
    print("\n=== Diagnostic File Information ===")

    # Try multiple methods to find files
    # 1. Using os.listdir()
    list_files = [f for f in os.listdir(folder_path) if f.endswith('_comments.json')]
    print(f"Files found with os.listdir(): {len(list_files)}")

    # 2. Using glob
    glob_pattern = os.path.join(folder_path, "*_comments.json")
    glob_files = glob.glob(glob_pattern)
    print(f"Files found with glob.glob(): {len(glob_files)}")

    # 3. Using os.walk
    walk_files = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.endswith('_comments.json'):
                walk_files.append(os.path.join(root, file))
    print(f"Files found with os.walk(): {len(walk_files)}")

    # Determine which method found the most files
    files_count = [len(list_files), len(glob_files), len(walk_files)]
    max_index = files_count.index(max(files_count))
    method_names = ["os.listdir()", "glob.glob()", "os.walk()"]

    print(f"Using method {method_names[max_index]} which found the most files: {max(files_count)}")

    # Use the method that found the most files
    if max_index == 0:
        json_files = [os.path.join(folder_path, f) for f in list_files]
    elif max_index == 1:
        json_files = glob_files
    else:
        json_files = walk_files

    # Extract just the filenames for reporting
    file_names = [os.path.basename(f) for f in json_files]

    if not json_files:
        return None, "No comment files found in the selected folder."

    # Get video number range
    try:
        sorted_file_names = sorted(file_names, key=lambda x: int(x.split('_')[1]) if x.split('_')[1].isdigit() else 0)
        first_num = int(sorted_file_names[0].split('_')[1])
        last_num = int(sorted_file_names[-1].split('_')[1])
        print(f"Video number range: {first_num} to {last_num}")
        print(f"Expected sequential files: {last_num - first_num + 1}")
        print(f"Actual files found: {len(json_files)}")
        print(f"Missing files: {(last_num - first_num + 1) - len(json_files)}")

        # Print first and last 5 files
        print("\nFirst 5 files:")
        for f in sorted_file_names[:5]:
            print(f"  {f}")

        print("\nLast 5 files:")
        for f in sorted_file_names[-5:]:
            print(f"  {f}")

    except Exception as e:
        print(f"Error analyzing file numbers: {str(e)}")

    print("==============================\n")

    # Initialize combined data
    combined_data = {
        "total_videos": 0,
        "total_comments": 0,
        "videos": []
    }

    # Track total processed files for logging
    total_files = len(json_files)
    processed_files = 0

    seen_content_keys = set()
    expanded_aggregate_videos = 0
    skipped_duplicate_records = 0

    # Process aggregate exports first so their complete nested video lists are preserved.
    for file_path in sorted(json_files, key=comment_file_sort_key):
        file_name = os.path.basename(file_path)

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                video_data = json.load(file)

            # Validate required fields
            if not isinstance(video_data, dict):
                print(f"Warning: Skipping {file_name} - not a valid JSON object")
                continue

            nested_videos = video_data.get("videos")
            if isinstance(nested_videos, list):
                added = 0
                for nested_video in nested_videos:
                    was_added = append_video_record(
                        combined_data,
                        nested_video,
                        file_name,
                        fallback_number=combined_data["total_videos"] + 1,
                        seen_keys=seen_content_keys,
                    )
                    added += 1 if was_added else 0
                    skipped_duplicate_records += 0 if was_added else 1
                processed_files += 1
                expanded_aggregate_videos += added
                print(f"Expanded {added} nested videos from {file_name}")
                continue

            if append_video_record(
                combined_data,
                video_data,
                file_name,
                seen_keys=seen_content_keys,
            ):
                processed_files += 1
            else:
                skipped_duplicate_records += 1

        except json.JSONDecodeError:
            print(f"Warning: Skipping {file_name} - invalid JSON format")
            continue
        except Exception as e:
            print(f"Error processing {file_name}: {str(e)}")
            continue  # Continue processing other files even if one fails

    print(f"Processed {processed_files} of {total_files} files")
    if expanded_aggregate_videos:
        print(f"Expanded aggregate videos: {expanded_aggregate_videos}")
    if skipped_duplicate_records:
        print(f"Skipped duplicate content records: {skipped_duplicate_records}")

    if processed_files == 0:
        return None, "No valid comment files could be processed."

    return combined_data, ""

def create_minimized_data(combined_data):
    """Create minimized version of the combined data"""
    minimized_data = {
        "total_videos": combined_data["total_videos"],
        "total_comments": combined_data["total_comments"],
        "videos": []
    }

    for video in combined_data["videos"]:
        # Create minimized video entry
        min_video = {
            "video_number": video.get("video_number", 0),
            "video_id": video.get("video_id", ""),
            "caption": video.get("caption", ""),
            "comment_count": video.get("comment_count", 0),
            "comments": []
        }

        # Flatten replies in the minimized export while preserving parent/depth metadata.
        for comment, reply_depth in walk_comment_tree(video.get("comments", [])):
            try:
                user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
                min_comment = {
                    "id": comment.get("id") or comment.get("comment_id", ""),
                    "text": comment.get("text", ""),
                    "digg_count": comment.get("digg_count", comment.get("likes", 0)),
                    "user": user.get("nickname") or comment.get("author") or "Unknown",
                    "parent_comment_id": comment.get("parent_comment_id", ""),
                    "reply_depth": reply_depth,
                }
                min_video["comments"].append(min_comment)
            except Exception as e:
                print(f"Warning: Could not process comment: {str(e)}")
                continue

        minimized_data["videos"].append(min_video)

    return minimized_data

def clean_text_for_csv(text, max_length=None):
    """
    Clean and prepare text for CSV output, handling emoji characters
    """
    if not text:
        return ""

    # Truncate text if max_length is specified
    if max_length and len(text) > max_length:
        text = text[:max_length] + "..."

    # Replace common emoji with descriptions
    emoji_map = {
        '😀': '[smile]', '😃': '[grin]', '😄': '[smile]', '😁': '[grin]',
        '😆': '[laugh]', '😅': '[sweat_smile]', '🤣': '[rofl]', '😂': '[joy]',
        '🙂': '[slight_smile]', '🙃': '[upside_down]', '😉': '[wink]', '😊': '[smile]',
        '😇': '[innocent]', '😎': '[sunglasses]', '🤩': '[star_struck]', '😘': '[kiss]',
        '😗': '[kissing]', '☺️': '[relaxed]', '😚': '[kissing]', '😙': '[kissing]',
        '😋': '[yum]', '😛': '[stuck_out_tongue]', '😜': '[stuck_out_tongue_winking_eye]',
        '🤪': '[zany]', '😝': '[stuck_out_tongue_closed_eyes]', '🤑': '[money_mouth]',
        '🤗': '[hug]', '🤭': '[hand_over_mouth]', '🤫': '[shushing]', '🤔': '[thinking]',
        '👍': '[thumbsup]', '👎': '[thumbsdown]', '❤️': '[heart]', '💕': '[hearts]',
        '🔥': '[fire]', '👏': '[clap]', '🙏': '[pray]', '✅': '[check]'
    }

    # Replace emoji with text descriptions
    for emoji, description in emoji_map.items():
        text = text.replace(emoji, description)

    # Remove or replace unsupported characters
    text = text.replace('\u200b', '')  # Zero-width space
    text = text.replace('\u200d', '')  # Zero-width joiner

    # Strip control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)

    return text

def clean_text_for_pdf(text, max_length=None):
    """
    Clean and prepare text for PDF output, completely removing all emoji characters and problematic Unicode
    """
    if not text:
        return ""

    # Handle non-string inputs
    if not isinstance(text, str):
        try:
            text = str(text)
        except:
            return "[Non-text content]"

    # Remove all non-ASCII characters including emojis
    # This is a simple but effective way to remove all emoji and other special characters
    text = re.sub(r'[^\x00-\x7F]+', '', text)

    # Remove or replace problematic characters
    problematic_chars = {
        '\u200b': '',  # Zero-width space
        '\u200c': '',  # Zero-width non-joiner
        '\u200d': '',  # Zero-width joiner
        '\u2028': ' ', # Line separator
        '\u2029': ' ', # Paragraph separator
        '\ufeff': '',  # Byte order mark
        '\u202d': '',  # Left-to-right override
        '\u202e': '',  # Right-to-left override
        '\u061c': '',  # Arabic letter mark
        '\u00A0': ' ', # Non-breaking space
        '\t': '    ',  # Tab
        '\r': '',      # Carriage return
        '\u2060': '',  # Word joiner
        '\u2066': '',  # Left-to-right isolate
        '\u2067': '',  # Right-to-left isolate
        '\u2068': '',  # First strong isolate
        '\u2069': '',  # Pop directional isolate
    }

    for char, replacement in problematic_chars.items():
        text = text.replace(char, replacement)

    # Strip all control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)

    # Replace problematic HTML characters that might break ReportLab
    html_escape = {
        '<': '&lt;',
        '>': '&gt;',
        '&': '&amp;',
        '"': '&quot;',
        "'": '&#39;'
    }

    for char, replacement in html_escape.items():
        text = text.replace(char, replacement)

    # Truncate if max_length is specified
    if max_length and len(text) > max_length:
        text = text[:max_length] + "..."

    # Check for very long words that might cause rendering issues
    max_word_length = 30  # Reduced from 40 to prevent overshooting table width
    words = text.split()
    for i, word in enumerate(words):
        if len(word) > max_word_length:
            # Insert spaces in very long words to allow line breaks
            words[i] = " ".join([word[j:j+max_word_length] for j in range(0, len(word), max_word_length)])

    # Reassemble text
    text = " ".join(words)

    # Filter out any remaining non-printable characters
    text = ''.join(c for c in text if c.isprintable() or c in [' ', '\n', '\t'])

    return text

def estimate_pdf_size(combined_data, videos_per_file=None):
    """
    Estimate the size of a PDF that would be generated from the combined data
    Returns an estimate in megabytes and suggested file splitting
    """
    # Get total number of videos and comments
    videos = combined_data.get("videos", [])
    total_videos = len(videos)

    if total_videos == 0:
        return {
            "estimated_size_mb": 0,
            "need_splitting": False,
            "videos_per_file": 0,
            "num_files": 0,
            "splits": []
        }

    # Count total comments and estimate sizes
    total_comments = combined_data.get("total_comments", 0)
    total_text_length = 0
    largest_video_index = 0
    largest_video_comments = 0

    # If total_comments not provided, count from videos
    if total_comments == 0:
        for i, video in enumerate(videos):
            comment_count = len(video.get("comments", []))
            total_comments += comment_count

            # Track the video with the most comments
            if comment_count > largest_video_comments:
                largest_video_index = i
                largest_video_comments = comment_count

            # Estimate text length from comments
            for comment in video.get("comments", []):
                total_text_length += len(str(comment.get("text", "")))

    # Base estimate on the number of videos, comments, and text length
    BASE_SIZE_BYTES = 50000  # Base PDF overhead

    estimated_bytes = (
        BASE_SIZE_BYTES +
        total_videos * 50000 +                  # Base overhead per video
        total_comments * 1000 +                 # Base overhead per comment
        total_text_length * 2                   # Estimate for text content
    )

    # Convert to megabytes
    estimated_size_mb = estimated_bytes / (1024 * 1024)

    # Determine if splitting is needed
    need_splitting = estimated_size_mb > MAX_PDF_SIZE_MB

    # Calculate videos per file to keep each file under MAX_PDF_SIZE_MB
    if need_splitting and not videos_per_file:
        # If there's a video with too many comments that would exceed the limit
        if largest_video_comments > 2000:  # Arbitrary threshold for "too many comments"
            print(f"Warning: Video #{largest_video_index} has {largest_video_comments} comments and may need special handling")

        # Calculate videos per file based on average size
        average_video_size_mb = estimated_size_mb / total_videos
        videos_per_file = max(1, int(MAX_PDF_SIZE_MB / average_video_size_mb))

    # If not splitting or no valid videos_per_file, use all videos
    if not need_splitting or not videos_per_file:
        videos_per_file = total_videos

    # Calculate number of files needed (ceiling division)
    if total_videos > 0:
        num_files = (total_videos + videos_per_file - 1) // videos_per_file
    else:
        num_files = 1

    # Calculate video ranges for each split
    splits = []
    for i in range(0, total_videos, videos_per_file):
        end_idx = min(i + videos_per_file, total_videos)
        splits.append((i, end_idx))

    return {
        "estimated_size_mb": estimated_size_mb,
        "need_splitting": need_splitting,
        "videos_per_file": videos_per_file,
        "num_files": num_files,
        "splits": splits,
        "total_videos": total_videos,
        "total_comments": total_comments
    }

def create_structured_pdf(combined_data, pdf_path, start_video_idx=0, end_video_idx=None, part_num=None, max_retries=3):
    """
    Create a PDF that preserves the JSON structure of the data with improved emoji support
    Supports partial rendering (start_video_idx to end_video_idx) for file splitting
    Added retry mechanism and improved error handling
    Now includes LLM-friendly structure with tables and markers
    """
    if not REPORTLAB_AVAILABLE:
        return False, "ReportLab library not installed. Run 'pip install reportlab' to enable PDF export."

    retry_count = 0
    last_error = None

    while retry_count <= max_retries:
        try:
            # Get list of videos to include
            videos = combined_data.get('videos', [])
            total_videos = len(videos)

            if end_video_idx is None:
                end_video_idx = len(videos)

            videos_to_include = videos[start_video_idx:end_video_idx]

            if not videos_to_include:
                return False, "No videos found in the specified range"

            # Get metadata
            metadata = combined_data.get('metadata', {})
            company_signature = metadata.get('signature', 'Data by Kita Co. Lab TM')
            created_on = metadata.get('created_on', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            session_name = metadata.get('session_name', 'Unknown Session')

            # Get font for emojis
            if EMOJI_FONT_AVAILABLE:
                emoji_font = EMOJI_FONT_AVAILABLE
            else:
                emoji_font = DEFAULT_FONT

            # Create PDF document with memory optimization
            doc = SimpleDocTemplate(
                pdf_path,
                pagesize=letter,
                rightMargin=48,
                leftMargin=48,
                topMargin=48,
                bottomMargin=48,
                title=f"TikTok Comment Data - {session_name}"
            )

            # Define styles
            styles = getSampleStyleSheet()

            # Create custom styles
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
                fontSize=18,
                alignment=1,  # Center
                spaceAfter=12
            )

            heading2_style = ParagraphStyle(
                'CustomHeading2',
                parent=styles['Heading2'],
                fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
                fontSize=14,
                spaceAfter=8
            )

            heading3_style = ParagraphStyle(
                'CustomHeading3',
                parent=styles['Heading3'],
                fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
                fontSize=12,
                spaceAfter=6
            )

            # Normal text style
            normal_style = ParagraphStyle(
                'CustomNormal',
                parent=styles['Normal'],
                fontName=DEFAULT_FONT,
                fontSize=10,
                leading=14,
                spaceAfter=4
            )

            # Field style (for key-value pairs)
            field_style = ParagraphStyle(
                'FieldStyle',
                parent=normal_style,
                fontName=DEFAULT_FONT,
                fontSize=9,
                leading=12,
                leftIndent=12,
                spaceAfter=2
            )

            # Add comment style for comment text
            comment_style = ParagraphStyle(
                'CommentStyle',
                parent=normal_style,
                fontName=DEFAULT_FONT,
                fontSize=9,
                leading=12,
                leftIndent=5,
                rightIndent=5,
                spaceAfter=3,
                wordWrap='CJK',  # Better word wrapping for all characters
                firstLineIndent=0
            )

            # Reply style (more indented)
            reply_style = ParagraphStyle(
                'ReplyStyle',
                parent=comment_style,
                leftIndent=36,
                spaceAfter=2
            )

            # Create content elements
            elements = []

            # Add title and company information
            elements.append(Paragraph(f"{company_signature}", title_style))

            # Add part number if provided
            if part_num is not None:
                elements.append(Paragraph(f"TikTok Comments Report - Part {part_num}", heading2_style))
            else:
                elements.append(Paragraph(f"TikTok Comments Report", heading2_style))

            elements.append(Spacer(1, 0.1*inch))
            elements.append(Paragraph(f"Session: {session_name}", normal_style))
            elements.append(Paragraph(f"Generated on: {created_on}", normal_style))

            # Add LLM navigation guide
            elements.append(Spacer(1, 0.2*inch))
            elements.append(Paragraph("<b>LLM ANALYSIS GUIDE</b>", heading2_style))
            elements.append(Paragraph("This document contains TikTok comments organized by video. Each video section is clearly marked with VIDEO_START and VIDEO_END tags for easier LLM analysis.", normal_style))
            elements.append(Paragraph(f"Total videos in document: {len(videos_to_include)}", normal_style))
            elements.append(Paragraph("Comments are presented in table format with Username and Comment columns. Replies are indicated with a '↳' prefix.", normal_style))

            # Add PDF information
            if len(videos) > len(videos_to_include):
                elements.append(Paragraph(
                    f"Showing videos {start_video_idx+1} to {min(end_video_idx, len(videos))} of {len(videos)} total",
                    normal_style
                ))

            elements.append(Spacer(1, 0.3*inch))

            # Process each video with improved error handling
            for video_idx, video in enumerate(videos_to_include):
                try:
                    # Extract video data with safety checks
                    video_number = video.get('video_number', start_video_idx + video_idx + 1)
                    video_id = video.get('video_id', 'Unknown')
                    username = video.get('username', 'Unknown')

                    # Clean caption without length limitation
                    caption = clean_text_for_pdf(video.get('caption', 'No caption'))
                    comment_count = video.get('comment_count', 0)

                    # Create video section header with marker for LLM
                    video_header = f"VIDEO_START #{video_number}"
                    elements.append(Paragraph(video_header, heading2_style))
                    elements.append(Paragraph(f"Video #{video_number} - {video_id} (@{username})", heading3_style))

                    # Create a table for video metadata
                    video_data = [
                        ["Video ID", video_id],
                        ["Username", f"@{username}"],
                        ["Caption", caption],
                        ["Comment Count", str(comment_count)]
                    ]

                    # Add additional metrics if available
                    if video.get('digg_count', 0):
                        video_data.append(["Likes", f"{video.get('digg_count', 0):,}"])
                    if video.get('play_count', 0):
                        video_data.append(["Views", f"{video.get('play_count', 0):,}"])
                    if video.get('share_count', 0):
                        video_data.append(["Shares", f"{video.get('share_count', 0):,}"])

                    # Create metadata table
                    video_table = Table(video_data, colWidths=[1.2*inch, 5.0*inch])
                    video_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
                        ('TEXTCOLOR', (0, 0), (0, -1), colors.black),
                        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (0, -1), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 9),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ]))
                    elements.append(video_table)

                    # Add comments section header
                    elements.append(Spacer(1, 0.2*inch))
                    elements.append(Paragraph("<b>COMMENTS</b>", heading3_style))

                    # Get comments with error protection
                    comments = []
                    try:
                        comments = video.get('comments', [])
                    except Exception as comment_error:
                        print(f"Error getting comments for video {video_number}: {str(comment_error)}")
                        elements.append(Paragraph(f"Error loading comments: {str(comment_error)}", comment_style))
                        comments = []

                    if comments:
                        # Use reduced page size for safer processing
                        MAX_COMMENTS_PER_PAGE = 50
                        comment_pages = []

                        if len(comments) > MAX_COMMENTS_PER_PAGE:
                            # Split comments into pages
                            for j in range(0, len(comments), MAX_COMMENTS_PER_PAGE):
                                end_j = min(j + MAX_COMMENTS_PER_PAGE, len(comments))
                                comment_pages.append(comments[j:end_j])

                            elements.append(Paragraph(f"Comments split into {len(comment_pages)} pages", normal_style))
                        else:
                            comment_pages = [comments]  # Single page

                        # Process each page of comments
                        for page_num, comment_page in enumerate(comment_pages):
                            if page_num > 0:
                                # Add page break and header for paginated comments
                                elements.append(PageBreak())
                                elements.append(Paragraph(f"VIDEO_START #{video_number} (continued)", heading2_style))
                                elements.append(Paragraph(f"Comments Page {page_num+1}/{len(comment_pages)}", heading3_style))
                                elements.append(Spacer(1, 0.1*inch))

                            # Create a table for comments
                            comment_table_data = [["Username", "Comment"]]
                            filtered_comment_count = 0

                            # Display each comment in table format
                            for comment_idx, comment in enumerate(comment_page):
                                try:
                                    # Validate comment is a dict to avoid errors
                                    if not isinstance(comment, dict):
                                        continue

                                    # Get comment text and clean it
                                    comment_text = clean_text_for_pdf(comment.get('text', 'No text'), max_length=700)

                                    # Clean text for length check by stripping punctuation
                                    stripped_text = comment_text.strip(' \t\n\r[]{}()-_.,?!;:\'"')
                                    # Skip comments that are too short or only contain emojis
                                    if len(stripped_text) <= 1 or all(c in '😀😃😄😁😆😅🤣😂🙂🙃😉😊😇😎🤩😘😗☺️😚😙😋😛😜🤪😝🤑🤗🤭🤫🤔👍👎❤️💕🔥👏🙏✅' for c in stripped_text):
                                        continue

                                    # Get username ID
                                    username_id = ""
                                    if isinstance(comment.get('user', {}), dict):
                                        username_id = comment.get('user', {}).get('unique_id', '')


                                    # Format comment text as a wrapped Paragraph
                                    safe_text = comment_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
                                    comment_paragraph = Paragraph(safe_text, ParagraphStyle('CommentStyle', fontName=DEFAULT_FONT, fontSize=8, leading=10))

                                    # Format username and add BOT warning if needed
                                    bot_prob = comment.get('pattern_bot_probability', 'HUMAN')
                                    username_safe = username_id.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                                    username_display = f"@{username_safe}"
                                    if bot_prob == "SUSPICIOUS":
                                        username_display += "<br/><font color='red'><b>[BOT?]</b></font>"
                                    username_paragraph = Paragraph(username_display, ParagraphStyle('UsernameStyle', fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold', fontSize=8, leading=10))

                                    # Add to table
                                    comment_table_data.append([username_paragraph, comment_paragraph])
                                    filtered_comment_count += 1

                                    # Process replies
                                    try:
                                        replies = comment.get('replies', [])
                                        for reply_idx, reply in enumerate(replies):
                                            # Skip if not a dict
                                            if not isinstance(reply, dict):
                                                continue

                                            # Get reply text and clean it
                                            reply_text = clean_text_for_pdf(reply.get('text', 'No text'), max_length=600)

                                            # Clean text for length check by stripping punctuation
                                            stripped_reply = reply_text.strip(' \t\n\r[]{}()-_.,?!;:\'"')
                                            # Skip replies that are too short or only contain emojis
                                            if len(stripped_reply) <= 1 or all(c in '😀😃😄😁😆😅🤣😂🙂🙃😉😊😇😎🤩😘😗☺️😚😙😋😛😜🤪😝🤑🤗🤭🤫🤔👍👎❤️💕🔥👏🙏✅' for c in stripped_reply):
                                                continue

                                            # Get reply username ID
                                            reply_username_id = ""
                                            if isinstance(reply.get('user', {}), dict):
                                                reply_username_id = reply.get('user', {}).get('unique_id', '')



                                            # Format reply text as a wrapped Paragraph
                                            safe_reply = reply_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
                                            reply_paragraph = Paragraph(f"REPLY: {safe_reply}", ParagraphStyle('CommentStyle', fontName=DEFAULT_FONT, fontSize=8, leading=10))

                                            # Format reply username and add BOT warning if needed
                                            r_bot_prob = reply.get('pattern_bot_probability', 'HUMAN')
                                            reply_username_safe = reply_username_id.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                                            r_username_display = f"↳ @{reply_username_safe}"
                                            if r_bot_prob == "SUSPICIOUS":
                                                r_username_display += "<br/><font color='red'><b>[BOT?]</b></font>"
                                            r_username_paragraph = Paragraph(r_username_display, ParagraphStyle('UsernameStyle', fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold', fontSize=8, leading=10))

                                            # Add reply to table with indent marker
                                            comment_table_data.append([r_username_paragraph, reply_paragraph])
                                            filtered_comment_count += 1
                                    except Exception as reply_error:
                                        # Skip reply errors
                                        pass
                                except Exception as e:
                                    # Handle errors for individual comments
                                    print(f"Error processing comment {comment_idx+1} for video {video_number}: {str(e)}")

                            # If we have comments to display
                            if len(comment_table_data) > 1:
                                # Create the comment table
                                comment_table = Table(comment_table_data, colWidths=[1.5*inch, 4.7*inch])

                                # Process colors for status column based on value
                                table_styles = [
                                    ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
                                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                                    ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                                    ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
                                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                                    ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                    ('FONTSIZE', (0, 1), (1, -1), 8),  # Apply font size to all cols
                                    ('LEFTPADDING', (0, 0), (-1, -1), 4),  # Add padding
                                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                                    ('TOPPADDING', (0, 0), (-1, -1), 3),
                                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3)
                                ]

                                comment_table.setStyle(TableStyle(table_styles))
                                elements.append(comment_table)
                                elements.append(Paragraph(f"Displayed {filtered_comment_count} filtered comments and replies", field_style))
                            else:
                                elements.append(Paragraph("No meaningful comments found for this video after filtering.", field_style))
                    else:
                        elements.append(Paragraph("No comments found for this video", field_style))

                    # Add end marker for LLM
                    elements.append(Spacer(1, 0.2*inch))
                    elements.append(Paragraph(f"VIDEO_END #{video_number}", heading3_style))

                    # Add a page break if not the last video
                    if video_idx < len(videos_to_include) - 1:
                        elements.append(PageBreak())

                except Exception as video_error:
                    # Handle errors for individual videos
                    print(f"Error processing video at index {video_idx}: {str(video_error)}")
                    elements.append(Paragraph(f"Error processing video: {str(video_error)}", normal_style))
                    elements.append(PageBreak())

            # Add footer to the last page
            elements.append(Spacer(1, 0.4*inch))

            footer_text = f"Data by Kita Co. Lab TM - Generated on {datetime.now().strftime('%Y-%m-%d')}"
            if part_num:
                footer_text += f" - Part {part_num}"

            elements.append(Paragraph(
                footer_text,
                ParagraphStyle('Footer', parent=normal_style, alignment=1, fontSize=8)
            ))

            # Build PDF with memory management
            print(f"Building PDF document with {len(elements)} elements...")

            # Use a try/except specifically for the build step
            try:
                doc.build(elements)
                return True, ""
            except Exception as build_error:
                print(f"Error during PDF build: {str(build_error)}")
                # Re-raise for the outer retry mechanism
                raise build_error

        except Exception as e:
            retry_count += 1
            last_error = str(e)
            error_details = traceback.format_exc()

            print(f"Error generating PDF (attempt {retry_count}/{max_retries}): {last_error}")

            if retry_count <= max_retries:
                print(f"Retrying in {retry_count * 2} seconds...")
                time.sleep(retry_count * 2)  # Wait longer between each retry
            else:
                return False, f"Failed after {max_retries} attempts. Last error: {last_error}\n{error_details}"

    # This should never be reached if the while loop works correctly
    return False, f"Unknown error in PDF generation: {last_error}"

def save_output_files(combined_data, folder_path):
    """Save the output files in the source folder with improved PDF generation reliability"""
    session_name = os.path.basename(folder_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    company_signature = "Data by Kita Co. Lab TM"
    metadata = {
        "signature": company_signature,
        "created_on": timestamp,
        "session_name": session_name
    }
    combined_data["metadata"] = metadata

    # Save complete JSON
    complete_path = os.path.join(folder_path, f"{session_name}_complete_comments.json")
    with open(complete_path, 'w', encoding='utf-8') as file:
        json.dump(combined_data, file, indent=2, ensure_ascii=False)

    # Save minimized JSON
    minimized_data = create_minimized_data(combined_data)
    minimized_data["metadata"] = metadata
    minimized_path = os.path.join(folder_path, f"{session_name}_minimized_comments.json")
    with open(minimized_path, 'w', encoding='utf-8') as file:
        json.dump(minimized_data, file, indent=2, ensure_ascii=False)

    # Create regular complete PDF
    pdf_path = os.path.join(folder_path, f"{session_name}_comments.pdf")
    pdf_success, pdf_error = create_structured_pdf(combined_data, pdf_path)

    # Create high-engagement PDF with popular comments
    high_engagement_path = os.path.join(folder_path, f"{session_name}_high_engagement_comments.pdf")
    # Set a like threshold (can be adjusted based on data)
    like_threshold = 50  # Comments with 50+ likes
    max_comments_per_video = 10  # Show top 10 comments per video
    high_engagement_success, high_engagement_error = create_high_engagement_pdf(
        combined_data,
        high_engagement_path,
        like_threshold=like_threshold,
        max_comments_per_video=max_comments_per_video
    )

    # Track results
    pdf_results = [
        {
            "path": pdf_path,
            "success": pdf_success,
            "error": pdf_error if not pdf_success else "",
            "start_idx": 0,
            "end_idx": len(combined_data.get('videos', [])),
            "video_count": len(combined_data.get('videos', [])),
            "type": "full"
        },
        {
            "path": high_engagement_path,
            "success": high_engagement_success,
            "error": high_engagement_error if not high_engagement_success else "",
            "type": "high_engagement",
            "like_threshold": like_threshold,
            "max_comments_per_video": max_comments_per_video
        }
    ]

    pdf_size_info = estimate_pdf_size(combined_data, videos_per_file=len(combined_data.get('videos', [])))

    result_files = {
        "complete_json": complete_path,
        "minimized_json": minimized_path,
        "pdf_results": pdf_results,
        "pdf_size_info": pdf_size_info
    }

    return result_files

def check_files_in_detail(folder_path):
    """Basic file checking function to count and list comment files"""
    # Get all files in directory that match the pattern
    all_files = []
    for entry in os.listdir(folder_path):
        if entry.endswith('_comments.json'):
            all_files.append(entry)

    print(f"Total comment files found: {len(all_files)}")

    # Extract and display a sample of file names
    if all_files:
        # Sort files to show them in order
        sorted_files = sorted(all_files)

        if len(sorted_files) > 6:
            # Show first 3 and last 3 files
            print("Sample files:")
            for f in sorted_files[:3]:
                print(f"  {f}")
            print("  ...")
            for f in sorted_files[-3:]:
                print(f"  {f}")
        else:
            # Show all files if there are only a few
            print("Files found:")
            for f in sorted_files:
                print(f"  {f}")

    return all_files

def main_with_folder(session_folder):
    """Run the main function with a specific folder without UI selection"""
    # Get comments folder
    comments_folder = get_comments_folder(session_folder)
    if not comments_folder:
        print(f"Error: Could not find comments folder in {session_folder}")
        return

    print(f"Processing files from: {comments_folder}")

    # Analyze logs folder to compare with comments folder
    print("\n=== Log Analysis ===")
    log_analysis = analyze_logs_folder(session_folder)

    if not log_analysis["success"]:
        print(f"Warning: {log_analysis['message']}")
        print("Continuing with processing without log comparison...")
    else:
        print(f"Expected videos (from logs): {log_analysis['expected_count']}")
        print(f"Actual videos (in comments folder): {log_analysis['actual_count']}")
        print(f"Expected video range: {log_analysis['expected_range'][0]} to {log_analysis['expected_range'][1]}")
        print(f"Actual video range: {log_analysis['actual_range'][0]} to {log_analysis['actual_range'][1]}")

        if log_analysis["missing_count"] > 0:
            print(f"\nMissing videos (in logs but not in comments): {log_analysis['missing_count']}")
            if log_analysis["missing_count"] <= 20:
                print(f"Missing video numbers: {log_analysis['missing_videos']}")
            else:
                print(f"First 20 missing videos: {log_analysis['missing_videos'][:20]}...")

        if log_analysis["extra_count"] > 0:
            print(f"\nExtra videos (in comments but not in logs): {log_analysis['extra_count']}")
            if log_analysis["extra_count"] <= 20:
                print(f"Extra video numbers: {log_analysis['extra_videos']}")
            else:
                print(f"First 20 extra videos: {log_analysis['extra_videos'][:20]}...")

        if log_analysis["zero_comment_count"] > 0:
            print(f"\nVideos with zero comments: {log_analysis['zero_comment_count']}")
            if log_analysis["zero_comment_count"] <= 20:
                print(f"Zero comment video numbers: {log_analysis['zero_comment_videos']}")
            else:
                print(f"First 20 zero comment videos: {log_analysis['zero_comment_videos'][:20]}...")

    print("====================\n")

    # Detailed file check
    print("\n=== Detailed File Check ===")
    all_files = check_files_in_detail(comments_folder)
    print("=========================\n")

    # Combine comments
    combined_data, error = combine_comments(comments_folder)
    if error:
        print(f"Error: {error}")
        return

    # Add log analysis information to combined data
    if log_analysis["success"]:
        combined_data["log_analysis"] = {
            "expected_videos": log_analysis["expected_count"],
            "missing_videos": log_analysis["missing_count"],
            "videos_with_zero_comments": log_analysis["zero_comment_count"]
        }

    # Save output files to session folder (not comments folder)
    try:
        result_files = save_output_files(combined_data, session_folder)

        print("\nCompilation completed successfully!")
        print(f"Total videos processed: {combined_data['total_videos']}")
        print(f"Total comments: {combined_data['total_comments']}")

        if log_analysis["success"]:
            print(f"Expected videos from logs: {log_analysis['expected_count']}")
            print(f"Missing videos: {log_analysis['missing_count']}")

        print(f"\nOutput files saved to: {session_folder}")
        print(f"- Complete file: {os.path.basename(result_files['complete_json'])}")
        print(f"- Minimized file: {os.path.basename(result_files['minimized_json'])}")

        # PDF status
        if 'pdf_results' in result_files:
            pdf_results = result_files['pdf_results']

            # Regular PDF results
            full_pdfs = [r for r in pdf_results if r.get('type') == 'full' and r['success']]
            he_pdfs = [r for r in pdf_results if r.get('type') == 'high_engagement' and r['success']]
            failed_pdfs = [r for r in pdf_results if not r['success']]

            pdf_size_info = result_files.get('pdf_size_info', {})

            # Report on regular PDF
            if full_pdfs:
                print(f"- Complete PDF file: {os.path.basename(full_pdfs[0]['path'])}")
                print(f"  Estimated size: {pdf_size_info.get('estimated_size_mb', 0):.2f} MB")

            # Report on high-engagement PDF
            if he_pdfs:
                he_pdf = he_pdfs[0]
                print(f"- High-engagement PDF: {os.path.basename(he_pdf['path'])}")
                print(f"  Contains comments with {he_pdf.get('like_threshold', 0)}+ likes")
                print(f"  Limited to {he_pdf.get('max_comments_per_video', 10)} comments per video")

            if failed_pdfs:
                print(f"- Failed PDF files: {len(failed_pdfs)}")
                for pdf in failed_pdfs[:2]:  # Show first 2 errors
                    print(f"  Error: {pdf['error'][:100]}...")

            if not REPORTLAB_AVAILABLE:
                print("- PDF generation failed: ReportLab library not installed")
                print("  Install with: pip install reportlab")

    except Exception as e:
        error_msg = f"Error saving output files: {str(e)}"
        print(error_msg)

def main():
    """Main function with improved UI and menu system"""
    print("Starting TikTok Comments Compiler...")

    # Make sure comments_data directory exists
    if not os.path.exists("comments_data"):
        os.makedirs("comments_data", exist_ok=True)
        print("Created comments_data directory")
        messagebox.showinfo("Setup", "Created comments_data directory.\nPlace your session folders here.")

    # Create root window
    root = tk.Tk()
    root.title("TikTok Comments Compiler")
    root.geometry("650x500")
    print("Main window created")

    # Add app title and description
    title_label = tk.Label(root, text="TikTok Comments Compiler", font=("Arial", 16, "bold"))
    title_label.pack(pady=10)

    description = (
        "This tool compiles TikTok comments from JSON files into consolidated formats.\n"
        "It creates JSON files and PDFs for easy analysis."
    )
    desc_label = tk.Label(root, text=description, font=("Arial", 10))
    desc_label.pack(pady=5)

    # Create a frame for the buttons
    button_frame = tk.Frame(root)
    button_frame.pack(pady=20, fill=tk.X)

    # Add company branding
    company_label = tk.Label(root, text="Data by Kita Co. Lab TM", font=("Arial", 9, "italic"))
    company_label.pack(side=tk.BOTTOM, pady=10)

    # Create session frame
    session_frame = tk.LabelFrame(button_frame, text="Select Processing Mode", font=("Arial", 10, "bold"), padx=10, pady=10)
    session_frame.pack(padx=20, pady=10, fill=tk.X)

    # Define button styles
    button_width = 25
    button_height = 2
    button_font = ("Arial", 10)

    # Function to close main window
    def close_app():
        print("Closing application")
        root.destroy()

    # Function to process a single session
    def single_session():
        print("Single session mode selected")
        root.withdraw()  # Hide main window

        # Select session folder
        print("Opening session folder selection dialog...")
        session_folder = select_session_folder()

        print(f"Selected folder: {session_folder}")

        if session_folder:
            # Process the selected folder
            print(f"Looking for comments folder in: {session_folder}")
            comments_folder = get_comments_folder(session_folder)
            if not comments_folder:
                messagebox.showerror("Error", f"Could not find comments folder in {session_folder}")
                root.deiconify()  # Show main window again
                return

            print(f"Found comments folder: {comments_folder}")

            # Analyze logs folder to compare with comments folder
            print("Analyzing logs folder...")
            log_analysis = analyze_logs_folder(session_folder)

            # Combine comments
            print("Combining comments from JSON files...")
            combined_data, error = combine_comments(comments_folder)
            if error:
                messagebox.showerror("Error", error)
                root.deiconify()  # Show main window again
                return

            print(f"Combined {combined_data['total_videos']} videos with {combined_data['total_comments']} comments")

            # Add log analysis information to combined data
            if log_analysis["success"]:
                combined_data["log_analysis"] = {
                    "expected_videos": log_analysis["expected_count"],
                    "missing_videos": log_analysis["missing_count"],
                    "videos_with_zero_comments": log_analysis["zero_comment_count"]
                }

            # Save output files to session folder
            try:
                print("Saving output files...")
                result_files = save_output_files(combined_data, session_folder)

                # Show success message
                pdf_message = ""
                if 'pdf_results' in result_files:
                    pdf_results = result_files['pdf_results']
                    successful_pdfs = [r for r in pdf_results if r['success']]

                    if successful_pdfs:
                        if len(successful_pdfs) > 1:
                            pdf_message = f"\nPDF: {len(successful_pdfs)} files created"
                            pdf_message += f"\n  First file: {os.path.basename(successful_pdfs[0]['path'])}"
                        else:
                            pdf_message = f"\nPDF file: {os.path.basename(successful_pdfs[0]['path'])}"
                    else:
                        pdf_message = "\nPDF: Generation failed"

                # Add log analysis to message box if available
                log_message = ""
                if log_analysis["success"]:
                    missing_percent = (log_analysis["missing_count"] / log_analysis["expected_count"]) * 100 if log_analysis["expected_count"] > 0 else 0
                    log_message = (f"\n\nDiscrepancy Analysis:\n"
                                  f"Expected videos (from logs): {log_analysis['expected_count']}\n"
                                  f"Missing videos: {log_analysis['missing_count']} ({missing_percent:.1f}%)")

                print("Showing success message")
                messagebox.showinfo(
                    "Compilation Complete",
                    f"Successfully compiled {combined_data['total_videos']} videos with {combined_data['total_comments']} comments.{log_message}\n\n"
                    f"Files saved to:\n{session_folder}{pdf_message}"
                )

            except Exception as e:
                error_msg = f"Error saving output files: {str(e)}"
                print(f"Error: {error_msg}")
                print(traceback.format_exc())
                messagebox.showerror("Error", error_msg)
        else:
            print("No folder selected or selection canceled")

        print("Returning to main window")
        root.deiconify()  # Show main window again

    # Function to start batch processing
    def batch_sessions():
        print("Batch session mode selected")
        root.withdraw()  # Hide main window
        batch_process_sessions()
        print("Returning to main window")
        root.deiconify()  # Show main window again

    # Add processing mode buttons
    single_button = tk.Button(session_frame, text="Process Single Session", width=button_width,
                              height=button_height, font=button_font, command=single_session)
    single_button.pack(pady=5)

    batch_button = tk.Button(session_frame, text="Batch Process Multiple Sessions", width=button_width,
                             height=button_height, font=button_font, command=batch_sessions)
    batch_button.pack(pady=5)

    # Create options frame
    options_frame = tk.LabelFrame(button_frame, text="Additional Options", font=("Arial", 10, "bold"), padx=10, pady=10)
    options_frame.pack(padx=20, pady=10, fill=tk.X)

    # Function to select custom folder
    def select_custom_folder():
        root.withdraw()  # Hide main window

        # Use direct file dialog
        folder_path = filedialog.askdirectory(title="Select Folder with TikTok Comment Files")

        if folder_path:
            # Process the selected folder
            try:
                # Create a session folder path from the selected directory
                session_name = os.path.basename(folder_path)
                if not session_name:  # In case the folder ends with a slash
                    session_name = os.path.basename(os.path.dirname(folder_path))

                # Treat selected folder as a comments folder
                combined_data, error = combine_comments(folder_path)

                if error:
                    messagebox.showerror("Error", error)
                    root.deiconify()  # Show main window again
                    return

                # Save output files to the selected folder
                result_files = save_output_files(combined_data, folder_path)

                # Show success message
                messagebox.showinfo(
                    "Compilation Complete",
                    f"Successfully compiled {combined_data['total_videos']} videos with {combined_data['total_comments']} comments.\n\n"
                    f"Files saved to:\n{folder_path}"
                )

            except Exception as e:
                error_msg = f"Error processing custom folder: {str(e)}"
                messagebox.showerror("Error", error_msg)

        root.deiconify()  # Show main window again

    # Add additional option buttons
    custom_button = tk.Button(options_frame, text="Select Custom Folder", width=button_width,
                              height=button_height, font=button_font, command=select_custom_folder)
    custom_button.pack(pady=5)

    # Create help/about frame
    help_frame = tk.LabelFrame(button_frame, text="Help & About", font=("Arial", 10, "bold"), padx=10, pady=10)
    help_frame.pack(padx=20, pady=10, fill=tk.X)

    # Function to show help information
    def show_help():
        help_win = tk.Toplevel(root)
        help_win.title("Help & Documentation")
        help_win.geometry("600x500")

        # Add scrollable text widget
        help_frame = tk.Frame(help_win)
        help_frame.pack(padx=20, pady=20, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(help_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        help_text = tk.Text(help_frame, wrap=tk.WORD, width=80, height=25, yscrollcommand=scrollbar.set)
        help_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=help_text.yview)

        # Help content
        help_content = """
TikTok Comments Compiler - Help Documentation

This tool compiles TikTok comment JSON files into consolidated formats for easier analysis.

== MAIN FEATURES ==

1. Process Single Session
   - Select a session folder from the TikTok Scraper's output
   - Creates compiled JSON files and PDFs in the session folder

2. Batch Process Multiple Sessions
   - Process multiple session folders in parallel
   - Saves time when dealing with many sessions
   - Shows a summary of all processed sessions

3. Select Custom Folder
   - Choose any folder containing TikTok comment JSON files
   - Useful for custom or non-standard folder structures

== OUTPUT FILES ==

For each processed session, the tool generates:

1. Complete JSON (session_name_complete_comments.json)
   - Contains all original data from comment files

2. Minimized JSON (session_name_minimized_comments.json)
   - Contains only essential information for smaller file size

3. PDF Files
   - Full Comments PDF: All comments in a structured format
   - High-Engagement PDF: Only popular comments with many likes
     (better for analysis with large datasets)

== TIPS ==

- For very large comment collections, the high-engagement PDF is
  much more manageable for analysis

- When using batch processing, 3-4 parallel workers is usually
  optimal on most computers

- If PDFs fail to generate, ensure ReportLab is installed:
  pip install reportlab

== ABOUT ==

TikTok Comments Compiler
Version: 2.1.0
Data by Kita Co. Lab TM
"""

        help_text.insert(tk.END, help_content)
        help_text.config(state=tk.DISABLED)  # Make read-only

        # Close button
        close_button = tk.Button(help_win, text="Close", command=help_win.destroy)
        close_button.pack(pady=10)

    # Function to show about information
    def show_about():
        messagebox.showinfo(
            "About TikTok Comments Compiler",
            "TikTok Comments Compiler\nVersion 2.1.0\n\n"
            "This tool compiles TikTok comments from JSON files into consolidated formats.\n\n"
            "Features:\n"
            "- Combines multiple JSON files into single outputs\n"
            "- Creates PDF reports with comments in easily readable formats\n"
            "- Generates high-engagement PDFs with sentiment analysis\n"
            "- Supports batch processing of multiple sessions\n\n"
            "Data by Kita Co. Lab TM"
        )

    # Add help/about buttons
    help_button = tk.Button(help_frame, text="Help & Documentation", width=button_width,
                           height=button_height, font=button_font, command=show_help)
    help_button.pack(pady=5)

    about_button = tk.Button(help_frame, text="About", width=button_width,
                            height=1, font=button_font, command=show_about)
    about_button.pack(pady=5)

    # Exit button
    exit_button = tk.Button(button_frame, text="Exit", width=button_width,
                           height=1, font=button_font, command=close_app)
    exit_button.pack(pady=15)

    # Check if ReportLab is available
    if not REPORTLAB_AVAILABLE:
        warning_text = "Warning: ReportLab not installed. PDF generation will be disabled.\nInstall with: pip install reportlab"
        warning_label = tk.Label(root, text=warning_text, fg="red", font=("Arial", 10))
        warning_label.pack(pady=10)

    # Run the application
    root.mainloop()

def create_high_engagement_pdf(combined_data, pdf_path, like_threshold=10, max_comments_per_video=15, max_retries=3):
    """
    Create a minimized PDF that only includes high-engagement comments (comments with lots of likes)
    This creates a much smaller, more focused document for analysis with sentiment and hashtag detection
    """
    if not REPORTLAB_AVAILABLE:
        return False, "ReportLab library not installed. Run 'pip install reportlab' to enable PDF export."

    retry_count = 0
    last_error = None

    while retry_count <= max_retries:
        try:
            # Get list of videos to include
            videos = combined_data.get('videos', [])
            total_videos = len(videos)

            if not videos:
                return False, "No videos found in the data"

            # Get metadata
            metadata = combined_data.get('metadata', {})
            company_signature = metadata.get('signature', 'Data by Kita Co. Lab TM')
            created_on = metadata.get('created_on', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            session_name = metadata.get('session_name', 'Unknown Session')

            # Create PDF document
            doc = SimpleDocTemplate(
                pdf_path,
                pagesize=letter,
                rightMargin=48,
                leftMargin=48,
                topMargin=48,
                bottomMargin=48,
                title=f"TikTok High-Engagement Comments - {session_name}"
            )

            # Define styles
            styles = getSampleStyleSheet()

            # Create custom styles
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
                fontSize=18,
                alignment=1,  # Center
                spaceAfter=12
            )

            heading2_style = ParagraphStyle(
                'CustomHeading2',
                parent=styles['Heading2'],
                fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
                fontSize=14,
                spaceAfter=8
            )

            heading3_style = ParagraphStyle(
                'CustomHeading3',
                parent=styles['Heading3'],
                fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
                fontSize=12,
                spaceAfter=6
            )

            normal_style = ParagraphStyle(
                'CustomNormal',
                parent=styles['Normal'],
                fontName=DEFAULT_FONT,
                fontSize=10,
                leading=14,
                spaceAfter=4
            )

            # Field style (for key-value pairs)
            field_style = ParagraphStyle(
                'FieldStyle',
                parent=normal_style,
                fontName=DEFAULT_FONT,
                fontSize=9,
                leading=12,
                leftIndent=12,
                spaceAfter=2
            )

            # Add comment style for comment text
            comment_style = ParagraphStyle(
                'CommentStyle',
                parent=normal_style,
                fontName=DEFAULT_FONT,
                fontSize=9,
                leading=12,
                leftIndent=5,
                rightIndent=5,
                spaceAfter=3,
                wordWrap='CJK',  # Better word wrapping for all characters
                firstLineIndent=0
            )

            # Reply style (more indented)
            reply_style = ParagraphStyle(
                'ReplyStyle',
                parent=comment_style,
                leftIndent=36,
                spaceAfter=2
            )

            # Create content elements
            elements = []

            # Add title and company information
            elements.append(Paragraph(f"{company_signature}", title_style))
            elements.append(Paragraph(f"TikTok High-Engagement Comments Report", heading2_style))
            elements.append(Spacer(1, 0.1*inch))
            elements.append(Paragraph(f"Session: {session_name}", normal_style))
            elements.append(Paragraph(f"Generated on: {created_on}", normal_style))

            # Add document explanation
            elements.append(Spacer(1, 0.2*inch))
            elements.append(Paragraph("<b>HIGH-ENGAGEMENT FILTER</b>", heading2_style))
            elements.append(Paragraph(f"This document contains only comments with {like_threshold}+ likes, maximum {max_comments_per_video} comments per video.", normal_style))
            elements.append(Paragraph(f"Total videos analyzed: {len(videos)}", normal_style))
            elements.append(Paragraph("Includes sentiment analysis and hashtag detection to improve LLM processing capabilities.", normal_style))

            # Track total comments included
            total_comments_included = 0
            videos_with_popular_comments = 0

            # Process each video
            for video_idx, video in enumerate(videos):
                try:
                    # Extract video data
                    video_number = video.get('video_number', video_idx + 1)
                    video_id = video.get('video_id', 'Unknown')
                    username = video.get('username', 'Unknown')
                    caption = clean_text_for_pdf(video.get('caption', 'No caption'))

                    # Get comments with error protection
                    comments = []
                    try:
                        comments = video.get('comments', [])
                    except Exception as comment_error:
                        print(f"Error getting comments for video {video_number}: {str(comment_error)}")
                        continue

                    # Filter for high-engagement comments
                    popular_comments = []

                    for comment in comments:
                        if not isinstance(comment, dict):
                            continue

                        # Get like count
                        digg_count = comment.get('digg_count', 0)

                        # Only include comments with sufficient likes
                        if digg_count >= like_threshold:
                            # Clean and validate comment text
                            comment_text = clean_text_for_pdf(comment.get('text', 'No text'))

                            # Skip comments that are too short or only contain emojis
                            if len(comment_text.strip()) <= 1 or all(c in '😀😃😄😁😆😅🤣😂🙂🙃😉😊😇😎🤩😘😗☺️😚😙😋😛😜🤪😝🤑🤗🤭🤫🤔👍👎❤️💕🔥👏🙏✅' for c in comment_text.strip()):
                                continue

                            # Get username ID
                            username_id = ""
                            if isinstance(comment.get('user', {}), dict):
                                username_id = comment.get('user', {}).get('unique_id', '')

                            # Add to popular comments list with its like count
                            popular_comments.append({
                                'username': username_id,
                                'text': comment_text,
                                'likes': digg_count
                            })

                    # Sort by likes (highest first) and limit to max_comments_per_video
                    popular_comments.sort(key=lambda x: x['likes'], reverse=True)
                    popular_comments = popular_comments[:max_comments_per_video]

                    # Only include videos that have popular comments
                    if popular_comments:
                        videos_with_popular_comments += 1
                        total_comments_included += len(popular_comments)

                        # Create video section header
                        elements.append(Spacer(1, 0.3*inch))
                        elements.append(Paragraph(f"VIDEO #{video_number}", heading2_style))

                        # Create a table for video info
                        video_data = [
                            ["Video ID", video_id],
                            ["Username", f"@{username}"],
                            ["Caption", caption]
                        ]
                        video_table = Table(video_data, colWidths=[1.2*inch, 5.0*inch])
                        video_table.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
                            ('TEXTCOLOR', (0, 0), (0, -1), colors.black),
                            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                            ('FONTNAME', (0, 0), (0, -1), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                            ('FONTSIZE', (0, 0), (-1, -1), 9),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                        ]))
                        elements.append(video_table)

                        # Add popular comments table
                        elements.append(Spacer(1, 0.2*inch))
                        elements.append(Paragraph("<b>TOP COMMENTS</b>", heading3_style))

                        # Create comment table
                        comment_table_data = [["Likes", "Username", "Comment"]]

                        for comment in popular_comments:
                            comment_table_data.append([
                                str(comment['likes']),
                                f"@{comment['username']}",
                                Paragraph(comment['text'], comment_style)
                            ])

                        # Create the table
                        comment_table = Table(comment_table_data, colWidths=[0.7*inch, 1.5*inch, 4.0*inch])
                        comment_table.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
                            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                            ('ALIGN', (0, 1), (0, -1), 'CENTER'),
                            ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                            ('FONTSIZE', (0, 0), (-1, 0), 9),
                            ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
                            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                            ('FONTSIZE', (0, 1), (1, -1), 8),  # Only first two columns
                            ('LEFTPADDING', (0, 0), (-1, -1), 4),  # Add padding
                            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                            ('TOPPADDING', (0, 0), (-1, -1), 3),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                        ]))
                        elements.append(comment_table)

                except Exception as video_error:
                    print(f"Error processing video {video_number}: {str(video_error)}")
                    continue

            # Add summary at the end
            elements.append(Spacer(1, 0.4*inch))
            elements.append(Paragraph("<b>SUMMARY</b>", heading3_style))
            elements.append(Paragraph(f"Videos with high-engagement comments: {videos_with_popular_comments} of {total_videos}", normal_style))
            elements.append(Paragraph(f"Total high-engagement comments: {total_comments_included}", normal_style))
            elements.append(Paragraph(f"Engagement threshold: {like_threshold}+ likes", normal_style))

            # Add footer
            elements.append(Spacer(1, 0.4*inch))
            elements.append(Paragraph(
                f"Data by Kita Co. Lab TM - Generated on {datetime.now().strftime('%Y-%m-%d')}",
                ParagraphStyle('Footer', parent=normal_style, alignment=1, fontSize=8)
            ))

            # Build PDF
            print(f"Building high-engagement PDF with {len(elements)} elements...")
            doc.build(elements)
            return True, ""

        except Exception as e:
            retry_count += 1
            last_error = str(e)
            error_details = traceback.format_exc()

            print(f"Error generating high-engagement PDF (attempt {retry_count}/{max_retries}): {last_error}")

            if retry_count <= max_retries:
                print(f"Retrying in {retry_count * 2} seconds...")
                time.sleep(retry_count * 2)
            else:
                return False, f"Failed after {max_retries} attempts. Last error: {last_error}\n{error_details}"

    return False, f"Unknown error in PDF generation: {last_error}"

def batch_process_sessions(max_workers=3):
    """Process multiple session folders in parallel for faster compilation"""
    # Get all available session folders
    session_folders = list_session_folders()

    if not session_folders:
        print("No session folders found in comments_data directory.")
        messagebox.showerror("Error", "No session folders found in comments_data directory.")
        return

    # Create a simple GUI for selection
    batch_root = tk.Toplevel()
    batch_root.title("Select Sessions for Batch Processing")
    batch_root.geometry("700x500")
    batch_root.focus_force()  # Make this window focused
    batch_root.grab_set()  # Make the window modal

    # Main instruction
    label = tk.Label(batch_root, text="Select session folders to process in batch mode:", font=("Arial", 11))
    label.pack(pady=10)

    # Add explanation
    explanation = tk.Label(batch_root, text="Multiple sessions will be processed in parallel to save time.", font=("Arial", 10))
    explanation.pack(pady=5)

    # Create frame for listbox and scrollbar
    list_frame = tk.Frame(batch_root)
    list_frame.pack(pady=10, padx=20, fill=tk.BOTH, expand=True)

    # Add scrollbar
    scrollbar = tk.Scrollbar(list_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    # Create listbox with multiple selection enabled
    listbox = tk.Listbox(list_frame, width=70, height=15, font=("Courier New", 10), selectmode=tk.MULTIPLE)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Connect scrollbar to listbox
    listbox.config(yscrollcommand=scrollbar.set)
    scrollbar.config(command=listbox.yview)

    # Store folder paths mapped to their display names
    folder_info = {}

    # Add session folders to listbox with additional info
    for folder in sorted(session_folders, reverse=True):  # Newest first
        base_path = os.path.join("comments_data", folder)

        # Check for comments and logs folders
        has_comments = os.path.isdir(os.path.join(base_path, "comments"))
        has_logs = os.path.isdir(os.path.join(base_path, "logs"))

        # Count files in comments folder if it exists
        comment_files = 0
        if has_comments:
            comment_path = os.path.join(base_path, "comments")
            try:
                comment_files = len([f for f in os.listdir(comment_path) if f.endswith("_comments.json")])
            except Exception as e:
                print(f"Error counting files in {comment_path}: {str(e)}")
                comment_files = -1  # Indicate error

        # Format display string
        display_text = f"{folder} [{comment_files} comments]"
        display_text += " [+logs]" if has_logs else ""

        listbox.insert(tk.END, display_text)
        folder_info[display_text] = os.path.join("comments_data", folder)

    # Variables to store selected folders
    selected_folders = []
    processing_completed = [False]  # Use list to track if processing was started

    # Frame for batch options
    options_frame = tk.Frame(batch_root)
    options_frame.pack(pady=10, fill=tk.X)

    # Add parallel processing option
    workers_label = tk.Label(options_frame, text="Number of parallel processes:")
    workers_label.pack(side=tk.LEFT, padx=10)

    workers_var = tk.StringVar(value=str(max_workers))
    workers_entry = tk.Entry(options_frame, textvariable=workers_var, width=5)
    workers_entry.pack(side=tk.LEFT, padx=5)

    # Function to handle selection
    def on_select():
        selections = listbox.curselection()
        if not selections:
            messagebox.showerror("Error", "Please select at least one session folder")
            return

        # Get the actual folder paths for selected items
        selected_folders.clear()
        for i in selections:
            display_name = listbox.get(i)
            folder_path = folder_info[display_name]
            selected_folders.append(folder_path)

        # Get number of workers
        try:
            num_workers = int(workers_var.get())
            if num_workers < 1:
                num_workers = 1
            elif num_workers > 10:  # reasonable upper limit
                num_workers = 10
        except ValueError:
            num_workers = max_workers

        processing_completed[0] = True
        batch_root.grab_release()
        batch_root.destroy()

        # Process the selected folders
        process_multiple_sessions(selected_folders, num_workers)

    # Function to select all
    def select_all():
        listbox.select_set(0, tk.END)

    # Function to deselect all
    def deselect_all():
        listbox.selection_clear(0, tk.END)

    # Function to handle cancel
    def on_cancel():
        selected_folders.clear()
        batch_root.grab_release()
        batch_root.destroy()

    # Create buttons
    button_frame = tk.Frame(batch_root)
    button_frame.pack(pady=10)

    select_all_button = tk.Button(button_frame, text="Select All", command=select_all)
    select_all_button.pack(side=tk.LEFT, padx=10)

    deselect_all_button = tk.Button(button_frame, text="Deselect All", command=deselect_all)
    deselect_all_button.pack(side=tk.LEFT, padx=10)

    select_button = tk.Button(button_frame, text="Process Selected", command=on_select)
    select_button.pack(side=tk.LEFT, padx=10)

    cancel_button = tk.Button(button_frame, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=10)

    # Handle window close event
    batch_root.protocol("WM_DELETE_WINDOW", on_cancel)

    # Wait until this window is closed
    batch_root.wait_window()

    return selected_folders if processing_completed[0] else []

def process_session_threaded(session_folder, result_queue):
    """Process a single session folder in a thread and put result in queue"""
    try:
        print(f"\n=== Processing {os.path.basename(session_folder)} ===")

        # Get comments folder
        comments_folder = get_comments_folder(session_folder)
        if not comments_folder:
            result = {
                "session": os.path.basename(session_folder),
                "success": False,
                "error": f"Could not find comments folder in {session_folder}",
                "file_count": 0,
                "comment_count": 0
            }
            result_queue.put(result)
            return

        # Combine comments
        combined_data, error = combine_comments(comments_folder)
        if error:
            result = {
                "session": os.path.basename(session_folder),
                "success": False,
                "error": error,
                "file_count": 0,
                "comment_count": 0
            }
            result_queue.put(result)
            return

        # Analyze logs folder
        log_analysis = analyze_logs_folder(session_folder)
        if log_analysis["success"]:
            combined_data["log_analysis"] = {
                "expected_videos": log_analysis["expected_count"],
                "missing_videos": log_analysis["missing_count"],
                "videos_with_zero_comments": log_analysis["zero_comment_count"]
            }

        # Save output files
        result_files = save_output_files(combined_data, session_folder)

        # Create result info
        result = {
            "session": os.path.basename(session_folder),
            "success": True,
            "video_count": combined_data.get('total_videos', 0),
            "comment_count": combined_data.get('total_comments', 0),
            "files": result_files,
            "log_analysis": log_analysis if log_analysis.get("success", False) else None
        }

        result_queue.put(result)

    except Exception as e:
        error_info = traceback.format_exc()
        print(f"Error processing {os.path.basename(session_folder)}: {str(e)}")
        print(error_info)

        result = {
            "session": os.path.basename(session_folder),
            "success": False,
            "error": str(e),
            "traceback": error_info,
            "file_count": 0,
            "comment_count": 0
        }
        result_queue.put(result)

def process_multiple_sessions(session_folders, num_workers=3):
    """Process multiple session folders using a thread pool"""
    if not session_folders:
        return

    # Create a results queue
    results_queue = queue.Queue()

    # Start progress window
    progress_root = tk.Tk()
    progress_root.title("Batch Processing Progress")
    progress_root.geometry("600x400")

    # Main title
    title_label = tk.Label(progress_root, text="TikTok Comments Batch Compilation", font=("Arial", 14, "bold"))
    title_label.pack(pady=10)

    # Status message
    status_var = tk.StringVar(value="Initializing batch processing...")
    status_label = tk.Label(progress_root, textvariable=status_var, font=("Arial", 10))
    status_label.pack(pady=5)

    # Progress counts
    count_var = tk.StringVar(value=f"Processing 0/{len(session_folders)} sessions")
    count_label = tk.Label(progress_root, textvariable=count_var, font=("Arial", 11))
    count_label.pack(pady=5)

    # Create text widget for log output
    log_frame = tk.Frame(progress_root)
    log_frame.pack(pady=10, padx=20, fill=tk.BOTH, expand=True)

    log_scrollbar = tk.Scrollbar(log_frame)
    log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    log_text = tk.Text(log_frame, height=15, width=80, yscrollcommand=log_scrollbar.set)
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    log_scrollbar.config(command=log_text.yview)

    # Function to update log
    def update_log(message):
        log_text.insert(tk.END, message + "\n")
        log_text.see(tk.END)  # Scroll to end

    # Results tracking
    all_results = []
    completed_count = 0

    # Function to check queue and update UI
    def check_queue():
        nonlocal completed_count

        # Check if there are results in the queue
        try:
            while True:  # Process all available results
                result = results_queue.get_nowait()
                all_results.append(result)
                completed_count += 1

                # Update progress
                count_var.set(f"Completed {completed_count}/{len(session_folders)} sessions")

                # Add to log
                session_name = result.get("session", "Unknown")
                if result.get("success", False):
                    video_count = result.get("video_count", 0)
                    comment_count = result.get("comment_count", 0)
                    update_log(f"✓ {session_name}: {video_count} videos, {comment_count} comments")
                else:
                    error = result.get("error", "Unknown error")
                    update_log(f"✗ {session_name}: FAILED - {error}")

                results_queue.task_done()
        except queue.Empty:
            pass

        # Check if all done
        if completed_count >= len(session_folders):
            status_var.set("All sessions processed!")
            show_summary_button.config(state=tk.NORMAL)
        else:
            # Schedule next check
            progress_root.after(1000, check_queue)

    # Function to show summary
    def show_summary():
        summary_win = tk.Toplevel(progress_root)
        summary_win.title("Batch Processing Summary")
        summary_win.geometry("700x500")

        # Successful sessions
        successful = [r for r in all_results if r.get("success", False)]
        failed = [r for r in all_results if not r.get("success", False)]

        # Calculate totals
        total_videos = sum(r.get("video_count", 0) for r in successful)
        total_comments = sum(r.get("comment_count", 0) for r in successful)

        # Add summary
        summary_label = tk.Label(
            summary_win,
            text=f"Batch Processing Complete\n\n"
                f"Sessions processed: {len(session_folders)}\n"
                f"Successful: {len(successful)}\n"
                f"Failed: {len(failed)}\n\n"
                f"Total videos processed: {total_videos}\n"
                f"Total comments processed: {total_comments}",
            font=("Arial", 11),
            justify=tk.LEFT
        )
        summary_label.pack(pady=20, padx=20, anchor=tk.W)

        # Create result details
        details_frame = tk.Frame(summary_win)
        details_frame.pack(pady=10, padx=20, fill=tk.BOTH, expand=True)

        details_scrollbar = tk.Scrollbar(details_frame)
        details_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        details_text = tk.Text(details_frame, height=15, width=80, yscrollcommand=details_scrollbar.set)
        details_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        details_scrollbar.config(command=details_text.yview)

        # Add details
        details_text.insert(tk.END, "=== SUCCESS ===\n\n")
        for i, result in enumerate(successful, 1):
            session_name = result.get("session", "Unknown")
            video_count = result.get("video_count", 0)
            comment_count = result.get("comment_count", 0)

            # Get PDF paths
            pdf_paths = []
            if "files" in result and "pdf_results" in result["files"]:
                for pdf_result in result["files"]["pdf_results"]:
                    if pdf_result.get("success", False):
                        pdf_paths.append(os.path.basename(pdf_result["path"]))

            details_text.insert(tk.END, f"{i}. {session_name}:\n")
            details_text.insert(tk.END, f"   - Videos: {video_count}\n")
            details_text.insert(tk.END, f"   - Comments: {comment_count}\n")

            if pdf_paths:
                details_text.insert(tk.END, f"   - PDFs: {', '.join(pdf_paths)}\n")

            details_text.insert(tk.END, "\n")

        if failed:
            details_text.insert(tk.END, "=== FAILED ===\n\n")
            for i, result in enumerate(failed, 1):
                session_name = result.get("session", "Unknown")
                error = result.get("error", "Unknown error")
                details_text.insert(tk.END, f"{i}. {session_name}:\n")
                details_text.insert(tk.END, f"   - Error: {error}\n\n")

        # Close button
        close_button = tk.Button(summary_win, text="Close", command=summary_win.destroy)
        close_button.pack(pady=10)

    # Add summary button (disabled initially)
    show_summary_button = tk.Button(progress_root, text="Show Summary", command=show_summary, state=tk.DISABLED)
    show_summary_button.pack(pady=10)

    # Close button
    close_button = tk.Button(progress_root, text="Close", command=progress_root.destroy)
    close_button.pack(pady=10)

    # Start thread pool for processing
    status_var.set(f"Starting batch processing with {num_workers} workers...")
    update_log(f"Initializing batch processing of {len(session_folders)} sessions...")

    def run_batch_processing():
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            # Submit all tasks
            futures = {
                executor.submit(process_session_threaded, folder, results_queue): folder
                for folder in session_folders
            }

            # Wait for completion
            for future in as_completed(futures):
                folder = futures[future]
                try:
                    # Just ensure the future is done - results are in the queue
                    future.result()
                except Exception as e:
                    # Handle any exceptions not caught in the thread
                    session_name = os.path.basename(folder)
                    error_msg = f"Critical error processing {session_name}: {str(e)}"
                    print(error_msg)
                    update_log(error_msg)

    # Start processing thread
    threading.Thread(target=run_batch_processing, daemon=True).start()

    # Start queue checking
    progress_root.after(1000, check_queue)

    # Run the progress window
    progress_root.mainloop()

# Add a test function that can be run directly
if __name__ == "__main__":
    # If the script is run directly, you can execute this test
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            print("Running diagnostic test...")

            # Test folder path
            test_folder = "comments_data/session_20250502_111632/comments"
            if os.path.exists(test_folder):
                combine_comments(test_folder)
            else:
                print(f"Test folder not found: {test_folder}")
        elif sys.argv[1] == "batch":
            # Run in batch mode
            print("Starting batch processing mode...")
            batch_process_sessions()
        else:
            # Use the provided folder path directly
            folder_path = sys.argv[1]
            if os.path.exists(folder_path):
                print(f"Using specified folder: {folder_path}")
                session_folder = folder_path
                main_with_folder(session_folder)
            else:
                print(f"Specified folder not found: {folder_path}")
    else:
        # Run normal main function
        main()
