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

# Add back ReportLab imports for PDF generation
try:
    from reportlab.lib.pagesizes import A4, letter, landscape
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

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
    DEFAULT_FONT = 'Helvetica'  # Fallback font name

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
    root = tk.Tk()
    root.title("Select Session Folder")
    root.geometry("650x400")  # Wider to show more details

    # Create label
    label = tk.Label(root, text="Select a session folder:")
    label.pack(pady=10)

    # Create frame for listbox and scrollbar
    list_frame = tk.Frame(root)
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
            comment_files = len([f for f in os.listdir(comment_path) if f.endswith("_comments.json")])

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
            root.destroy()
        else:
            messagebox.showerror("Error", "Please select a session folder")

    # Function to handle double-click
    def on_double_click(event):
        on_select()

    # Bind double-click event
    listbox.bind("<Double-Button-1>", on_double_click)

    # Function to handle cancel
    def on_cancel():
        root.destroy()

    # Create buttons
    button_frame = tk.Frame(root)
    button_frame.pack(pady=10)

    select_button = tk.Button(button_frame, text="Select", command=on_select)
    select_button.pack(side=tk.LEFT, padx=10)

    cancel_button = tk.Button(button_frame, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=10)

    # Run the dialog
    root.mainloop()

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

    # Process each file
    for file_path in sorted(json_files, key=lambda x: int(os.path.basename(x).split('_')[1]) if os.path.basename(x).split('_')[1].isdigit() else 0):
        file_name = os.path.basename(file_path)

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                video_data = json.load(file)

            # Validate required fields
            if not isinstance(video_data, dict):
                print(f"Warning: Skipping {file_name} - not a valid JSON object")
                continue

            # Add to combined data
            combined_data["total_videos"] += 1
            combined_data["total_comments"] += video_data.get("comment_count", 0)
            processed_files += 1

            # Make sure all required fields exist
            if "video_number" not in video_data:
                # Try to extract from filename
                try:
                    video_number = int(file_name.split('_')[1])
                    video_data["video_number"] = video_number
                except:
                    video_data["video_number"] = combined_data["total_videos"]

            # Ensure comments field exists
            if "comments" not in video_data:
                video_data["comments"] = []

            combined_data["videos"].append(video_data)

        except json.JSONDecodeError:
            print(f"Warning: Skipping {file_name} - invalid JSON format")
            continue
        except Exception as e:
            print(f"Error processing {file_name}: {str(e)}")
            continue  # Continue processing other files even if one fails

    print(f"Processed {processed_files} of {total_files} files")

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

        # Add minimized comments (without replies)
        for comment in video.get("comments", []):
            try:
                min_comment = {
                    "id": comment.get("id", ""),
                    "text": comment.get("text", ""),
                    "digg_count": comment.get("digg_count", 0),
                    "user": comment.get("user", {}).get("nickname", "Unknown")
                }
                min_video["comments"].append(min_comment)
            except Exception as e:
                print(f"Warning: Could not process comment: {str(e)}")
                continue

        minimized_data["videos"].append(min_video)

    return minimized_data

def clean_text_for_pdf(text, max_length=None):
    """
    Clean and prepare text for PDF output, handling emoji characters
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

def estimate_pdf_size(combined_data, videos_per_file=None):
    """
    Estimate the size of a PDF that would be generated from the combined data
    Returns an estimate in megabytes and suggested file splitting
    """
    # Base size for PDF overhead, headers, etc.
    BASE_SIZE_BYTES = 50000  # 50KB base overhead

    # Average size estimate per component (very rough approximations)
    BYTES_PER_VIDEO_METADATA = 500    # Video number, ID, caption summary
    BYTES_PER_COMMENT = 300           # Average comment with user info and likes

    # Count videos and comments
    total_videos = combined_data.get('total_videos', len(combined_data.get('videos', [])))
    total_comments = combined_data.get('total_comments', 0)
    videos = combined_data.get('videos', [])

    # If total_comments is not available, count from videos
    if total_comments == 0 and videos:
        for video in videos:
            total_comments += len(video.get('comments', []))

    # Calculate estimated size
    estimated_bytes = BASE_SIZE_BYTES
    estimated_bytes += total_videos * BYTES_PER_VIDEO_METADATA
    estimated_bytes += total_comments * BYTES_PER_COMMENT

    # Convert to megabytes
    estimated_mb = estimated_bytes / (1024 * 1024)

    # Determine if we need to split files for LLM compatibility
    # Most LLMs have context limits around 10-15MB for PDFs
    MAX_PDF_SIZE_MB = 10

    need_splitting = estimated_mb > MAX_PDF_SIZE_MB

    if need_splitting and not videos_per_file:
        # Calculate videos per file to stay under limit
        bytes_per_video = BYTES_PER_VIDEO_METADATA
        if total_videos > 0:
            # Average comments per video
            comments_per_video = total_comments / total_videos
            bytes_per_video += comments_per_video * BYTES_PER_COMMENT

        # Calculate videos per file based on max size
        max_bytes_per_file = MAX_PDF_SIZE_MB * 1024 * 1024
        videos_per_file = max(1, int((max_bytes_per_file - BASE_SIZE_BYTES) / bytes_per_video))

    # If not splitting or no valid videos_per_file, use all videos
    if not need_splitting or not videos_per_file:
        videos_per_file = total_videos

    # Calculate number of files needed
    if total_videos > 0:
        num_files = (total_videos + videos_per_file - 1) // videos_per_file  # ceiling division
    else:
        num_files = 1

    return {
        "estimated_size_mb": estimated_mb,
        "need_splitting": need_splitting,
        "videos_per_file": videos_per_file,
        "num_files": num_files,
        "total_videos": total_videos,
        "total_comments": total_comments
    }

def create_structured_pdf(combined_data, pdf_path, start_video_idx=0, end_video_idx=None):
    """
    Create a PDF that preserves the JSON structure of the data
    Supports partial rendering (start_video_idx to end_video_idx) for file splitting
    """
    if not REPORTLAB_AVAILABLE:
        return False, "ReportLab library not installed. Run 'pip install reportlab' to enable PDF export."

    try:
        # Get list of videos to include
        videos = combined_data.get('videos', [])

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

        # Create PDF document
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

        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontName=DEFAULT_FONT,
            fontSize=10,
            leading=14
        )

        # Comment style
        comment_style = ParagraphStyle(
            'CommentStyle',
            parent=normal_style,
            fontName=DEFAULT_FONT,
            fontSize=9,
            leading=12,
            spaceAfter=6
        )

        # Create content elements
        elements = []

        # Add title and company information
        elements.append(Paragraph(f"{company_signature}", title_style))
        elements.append(Paragraph(f"TikTok Comments Report - Part {start_video_idx//100 + 1}", heading2_style))
        elements.append(Spacer(1, 0.1*inch))
        elements.append(Paragraph(f"Session: {session_name}", normal_style))
        elements.append(Paragraph(f"Generated on: {created_on}", normal_style))

        # Add PDF information
        if len(videos) > len(videos_to_include):
            elements.append(Paragraph(
                f"Showing videos {start_video_idx+1} to {min(end_video_idx, len(videos))} of {len(videos)} total",
                normal_style
            ))

        elements.append(Spacer(1, 0.3*inch))

        # Process each video
        for video_idx, video in enumerate(videos_to_include):
            # Extract video data
            video_number = video.get('video_number', start_video_idx + video_idx + 1)
            video_id = video.get('video_id', 'Unknown')
            username = video.get('username', 'Unknown')
            caption = clean_text_for_pdf(video.get('caption', 'No caption'))
            comment_count = video.get('comment_count', 0)

            # Create video header
            video_header = f"Video #{video_number} - {video_id} (@{username})"
            elements.append(Paragraph(video_header, heading3_style))

            # Add video details
            elements.append(Paragraph(f"<b>Caption:</b> {caption}", normal_style))
            elements.append(Paragraph(f"<b>Comments:</b> {comment_count}", normal_style))
            elements.append(Spacer(1, 0.1*inch))

            # Add comments
            comments = video.get('comments', [])
            if comments:
                # Table header
                comment_data = [['Comment', 'User', 'Likes']]

                # Process comments
                for comment in comments:
                    comment_text = clean_text_for_pdf(comment.get('text', 'No text'))

                    # Get user - handling different formats
                    if isinstance(comment.get('user', {}), dict):
                        user = comment.get('user', {}).get('nickname', 'Unknown')
                    else:
                        user = comment.get('user', 'Unknown')

                    likes = comment.get('digg_count', 0)

                    # Add to table data
                    comment_data.append([comment_text, user, str(likes)])

                # Create table
                table = Table(comment_data, colWidths=[doc.width*0.6, doc.width*0.25, doc.width*0.15])
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ]))

                elements.append(table)
            else:
                elements.append(Paragraph("No comments found for this video", normal_style))

            # Add spacer between videos
            elements.append(Spacer(1, 0.3*inch))

            # Add a separator if not the last video
            if video_idx < len(videos_to_include) - 1:
                elements.append(Paragraph("<hr width='100%'/>", normal_style))
                elements.append(Spacer(1, 0.2*inch))

        # Add footer
        elements.append(Spacer(1, 0.4*inch))
        elements.append(Paragraph(
            f"Data by Kita Co. Lab TM - Generated on {datetime.now().strftime('%Y-%m-%d')}",
            ParagraphStyle('Footer', parent=normal_style, alignment=1, fontSize=8)
        ))

        # Build PDF
        doc.build(elements)

        return True, ""
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return False, f"Error generating PDF: {str(e)}\n{error_details}"

def save_output_files(combined_data, folder_path):
    """Save the output files in the source folder"""
    # Extract session folder name for use in filenames
    session_name = os.path.basename(folder_path)

    # Get current timestamp for signature
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    company_signature = "Data by Kita Co. Lab TM"

    # Add metadata to JSON files
    metadata = {
        "signature": company_signature,
        "created_on": timestamp,
        "session_name": session_name
    }
    combined_data["metadata"] = metadata

    # Save complete data
    complete_path = os.path.join(folder_path, f"{session_name}_complete_comments.json")
    with open(complete_path, 'w', encoding='utf-8') as file:
        json.dump(combined_data, file, indent=2, ensure_ascii=False)

    # Create and save minimized data
    minimized_data = create_minimized_data(combined_data)
    # Add metadata to minimized data too
    minimized_data["metadata"] = metadata
    minimized_path = os.path.join(folder_path, f"{session_name}_minimized_comments.json")
    with open(minimized_path, 'w', encoding='utf-8') as file:
        json.dump(minimized_data, file, indent=2, ensure_ascii=False)

    # Estimate PDF size and determine splitting
    pdf_size_info = estimate_pdf_size(combined_data)

    # Create PDFs
    pdf_results = []
    videos = combined_data.get('videos', [])
    videos_per_file = pdf_size_info.get('videos_per_file', len(videos))

    for i in range(0, len(videos), videos_per_file):
        # Calculate end index (exclusive)
        end_idx = min(i + videos_per_file, len(videos))

        # Create part number suffix if needed
        part_suffix = f"_part{i//videos_per_file + 1}" if pdf_size_info.get('need_splitting', False) else ""

        # Generate PDF filename
        pdf_path = os.path.join(folder_path, f"{session_name}_comments{part_suffix}.pdf")

        # Create PDF
        pdf_success, pdf_error = create_structured_pdf(combined_data, pdf_path, i, end_idx)

        pdf_results.append({
            "path": pdf_path,
            "success": pdf_success,
            "error": pdf_error if not pdf_success else "",
            "start_idx": i,
            "end_idx": end_idx,
            "video_count": end_idx - i
        })

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

def main():
    print("TikTok Comments Compiler")
    print("=" * 25)

    # Select session folder
    session_folder = select_session_folder()
    if not session_folder:
        print("No session folder selected. Exiting.")
        return

    # Get comments folder
    comments_folder = get_comments_folder(session_folder)
    if not comments_folder:
        print(f"Error: Could not find comments folder in {session_folder}")
        messagebox.showerror("Error", f"Could not find comments folder in {session_folder}")
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

    # Count JSON files before processing
    all_files = os.listdir(comments_folder)
    print(f"Total files in directory: {len(all_files)}")

    # Check for non-JSON files
    non_json_files = [f for f in all_files if not f.endswith('.json')]
    if non_json_files:
        print(f"Found {len(non_json_files)} non-JSON files in directory")

    # Get all comment JSON files
    json_files = [f for f in all_files if f.endswith('_comments.json')]
    print(f"Found {len(json_files)} comment JSON files in the folder")

    # List the first and last few files sorted by number to check for pattern issues
    sorted_files = sorted(json_files, key=lambda x: int(x.split('_')[1]) if x.split('_')[1].isdigit() else 0)
    print("\nFirst 5 files (sorted):")
    for f in sorted_files[:5]:
        print(f"  {f}")

    print("\nLast 5 files (sorted):")
    for f in sorted_files[-5:]:
        print(f"  {f}")

    # Check if os.walk finds more files
    walk_files = []
    for root, dirs, files in os.walk(comments_folder):
        for file in files:
            if file.endswith('_comments.json'):
                walk_files.append(file)

    if len(walk_files) > len(json_files):
        print(f"\nWarning: os.walk found {len(walk_files)} comment files vs {len(json_files)} with os.listdir")

    # Combine comments
    combined_data, error = combine_comments(comments_folder)
    if error:
        print(f"Error: {error}")
        messagebox.showerror("Error", error)
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
            successful_pdfs = [r for r in pdf_results if r['success']]
            failed_pdfs = [r for r in pdf_results if not r['success']]

            pdf_size_info = result_files.get('pdf_size_info', {})

            if successful_pdfs:
                if len(successful_pdfs) > 1:
                    print(f"- PDF files: {len(successful_pdfs)} files created")
                    print(f"  Estimated total size: {pdf_size_info.get('estimated_size_mb', 0):.2f} MB")
                    print(f"  Videos per file: ~{pdf_size_info.get('videos_per_file', 0)}")
                    for idx, pdf in enumerate(successful_pdfs[:3]):  # Show first 3
                        print(f"  - {os.path.basename(pdf['path'])} ({pdf['video_count']} videos)")
                    if len(successful_pdfs) > 3:
                        print(f"  - ... and {len(successful_pdfs) - 3} more")
                else:
                    print(f"- PDF file: {os.path.basename(successful_pdfs[0]['path'])}")
                    print(f"  Estimated size: {pdf_size_info.get('estimated_size_mb', 0):.2f} MB")

            if failed_pdfs:
                print(f"- Failed PDF files: {len(failed_pdfs)}")
                for pdf in failed_pdfs[:2]:  # Show first 2 errors
                    print(f"  Error: {pdf['error'][:100]}...")

            if not REPORTLAB_AVAILABLE:
                print("- PDF generation failed: ReportLab library not installed")
                print("  Install with: pip install reportlab")

        # Show message box to user
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

        messagebox.showinfo(
            "Compilation Complete",
            f"Successfully compiled {combined_data['total_videos']} videos with {combined_data['total_comments']} comments.{log_message}\n\n"
            f"Files saved to:\n{session_folder}{pdf_message}\n\n"
            f"Data by Kita Co. Lab TM - Generated on {datetime.now().strftime('%Y-%m-%d')}"
        )

    except Exception as e:
        error_msg = f"Error saving output files: {str(e)}"
        print(error_msg)
        messagebox.showerror("Error", error_msg)

# Add a test function that can be run directly
if __name__ == "__main__":
    # If the script is run directly, you can execute this test
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Running diagnostic test...")

        # Test folder path
        test_folder = "comments_data/session_20250502_111632/comments"
        if os.path.exists(test_folder):
            combine_comments(test_folder)
        else:
            print(f"Test folder not found: {test_folder}")
    else:
        # Run normal main function
        main()