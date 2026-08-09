import os
import json
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
import platform
import re
import math

# ReportLab imports for PDF generation
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.utils import ImageReader

    # Font setup for better emoji support
    EMOJI_FONT_AVAILABLE = False
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

    # Register a standard font for regular text
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

# Constants for LLM compatibility
MAX_PDF_SIZE_MB = 10  # Maximum PDF size for LLM processing in MB
BYTES_PER_VIDEO_ESTIMATE = 500000  # Rough estimate of bytes per video in PDF (including comments)
MAX_COMMENTS_PER_PAGE = 100  # Maximum number of comments to show on a single page

def clean_text_for_pdf(text, max_length=None):
    """
    Clean and prepare text for PDF output, handling emoji characters
    """
    if not text:
        return ""

    # Replace common emoji with descriptions if emoji font not available
    if not EMOJI_FONT_AVAILABLE:
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

        for emoji, description in emoji_map.items():
            text = text.replace(emoji, description)

    # Remove or replace unsupported characters
    text = text.replace('\u200b', '')  # Zero-width space
    text = text.replace('\u200d', '')  # Zero-width joiner

    # Strip control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)

    # Truncate if max_length is specified
    if max_length and len(text) > max_length:
        text = text[:max_length] + "..."

    return text

def select_json_file():
    """Open file dialog to select the complete JSON file"""
    root = tk.Tk()
    root.withdraw()  # Hide the root window

    file_path = filedialog.askopenfilename(
        title="Select Complete JSON File",
        filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
        initialdir="comments_data"
    )

    if not file_path:
        return None

    # Check if "complete" is in the filename
    filename = os.path.basename(file_path)
    if "complete" not in filename.lower():
        if not messagebox.askyesno(
            "Confirm Selection",
            f"The selected file '{filename}' doesn't appear to be a complete JSON file. Are you sure you want to use this file?"
        ):
            return None

    return file_path

def load_json_data(file_path):
    """Load JSON data from the file"""
    try:
        print(f"Loading JSON data from: {file_path}")
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        print(f"Successfully loaded JSON data")

        # Check the structure of the JSON data
        if "videos" not in data:
            print("Warning: 'videos' key not found in the JSON data")
        else:
            total_videos = len(data["videos"])
            print(f"Found {total_videos} videos in the dataset")

            # Check the first video's structure
            if total_videos > 0:
                first_video = data["videos"][0]
                print(f"Video keys: {list(first_video.keys())}")

                if "comments" in first_video:
                    total_comments = first_video.get("comment_count", len(first_video["comments"]))
                    print(f"First video has {total_comments} comments")
                else:
                    print("Warning: 'comments' key not found in the first video")

        return data, ""
    except Exception as e:
        error_msg = f"Error loading JSON data: {str(e)}"
        print(error_msg)
        return None, error_msg

def estimate_pdf_size(json_data):
    """
    Estimate the PDF size based on the JSON data and calculate splitting
    Returns estimated size in MB and number of videos per file for splitting
    """
    # Get total number of videos and comments
    videos = json_data.get("videos", [])
    total_videos = len(videos)

    if total_videos == 0:
        return {
            "estimated_size_mb": 0,
            "needs_splitting": False,
            "videos_per_file": 0,
            "num_files": 0,
            "splits": []
        }

    # Count total comments and estimate sizes
    total_comments = 0
    total_text_length = 0
    largest_video_index = 0
    largest_video_comments = 0

    for i, video in enumerate(videos):
        comment_count = len(video.get("comments", []))
        total_comments += comment_count

        # Track the video with the most comments
        if comment_count > largest_video_comments:
            largest_video_index = i
            largest_video_comments = comment_count

        # Estimate text length from comments
        for comment in video.get("comments", []):
            total_text_length += len(comment.get("text", ""))

    # Base estimate on the number of videos, comments, and text length
    estimated_bytes = (
        total_videos * 50000 +                  # Base overhead per video
        total_comments * 1000 +                 # Base overhead per comment
        total_text_length * 2                   # Estimate for text content
    )

    # Convert to MB
    estimated_size_mb = estimated_bytes / (1024 * 1024)

    # Determine if splitting is needed
    needs_splitting = estimated_size_mb > MAX_PDF_SIZE_MB

    # Calculate videos per file to keep each file under MAX_PDF_SIZE_MB
    if needs_splitting:
        # If there's a video with too many comments that would exceed the limit by itself,
        # we'll need per-video splitting
        if largest_video_comments > 2000:  # Arbitrary threshold for "too many comments"
            print(f"Warning: Video #{largest_video_index} has {largest_video_comments} comments and may need special handling")

        # Calculate videos per file based on average size
        average_video_size_mb = estimated_size_mb / total_videos
        videos_per_file = max(1, int(MAX_PDF_SIZE_MB / average_video_size_mb))

        # Calculate number of files needed (ceiling division)
        num_files = (total_videos + videos_per_file - 1) // videos_per_file

        # Calculate video ranges for each file
        splits = []
        for i in range(0, total_videos, videos_per_file):
            end_idx = min(i + videos_per_file, total_videos)
            splits.append((i, end_idx))
    else:
        videos_per_file = total_videos
        num_files = 1
        splits = [(0, total_videos)]

    return {
        "estimated_size_mb": estimated_size_mb,
        "needs_splitting": needs_splitting,
        "videos_per_file": videos_per_file,
        "num_files": num_files,
        "splits": splits,
        "total_videos": total_videos,
        "total_comments": total_comments
    }

def create_complete_pdf(json_data, output_path, start_idx=0, end_idx=None, part_num=None):
    """Create a complete PDF with improved text handling for emojis and long text"""
    if not REPORTLAB_AVAILABLE:
        return False, "ReportLab library not installed. Run 'pip install reportlab' to enable PDF export."

    try:
        # Get videos to include
        videos = json_data.get('videos', [])
        total_videos = len(videos)

        if end_idx is None:
            end_idx = total_videos

        videos_to_include = videos[start_idx:end_idx]

        if not videos_to_include:
            return False, f"No videos found in range {start_idx} to {end_idx}"

        # Create PDF document
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=48,
            leftMargin=48,
            topMargin=48,
            bottomMargin=48,
            title="Complete Social Media Comments PDF"
        )

        # Define styles
        styles = getSampleStyleSheet()

        # Header text (centered)
        header_style = ParagraphStyle(
            'HeaderStyle',
            parent=styles['Heading1'],
            fontName='Helvetica-Bold',
            fontSize=16,
            alignment=1,  # Center
            spaceAfter=20
        )

        # Subheaders
        heading2_style = ParagraphStyle(
            'CustomHeading2',
            parent=styles['Heading2'],
            fontName='Helvetica-Bold',
            fontSize=14,
            spaceAfter=12
        )

        # Video headers with background color
        heading3_style = ParagraphStyle(
            'CustomHeading3',
            parent=styles['Heading3'],
            fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
            fontSize=12,
            spaceAfter=6
        )

        # Special style for text with potential emojis
        if EMOJI_FONT_AVAILABLE:
            emoji_font = EMOJI_FONT_AVAILABLE
        else:
            emoji_font = DEFAULT_FONT

        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontName=DEFAULT_FONT,
            fontSize=10,
            leading=14  # Extra space between lines
        )

        # Style specifically for comments
        comment_style = ParagraphStyle(
            'CommentStyle',
            parent=styles['Normal'],
            fontName=emoji_font,
            fontSize=9,
            leading=12,  # Space between lines
            spaceAfter=6
        )

        # Create content elements
        elements = []

        # Add title and metadata
        elements.append(Paragraph("Data by Kita Co. Lab TM", styles['Title']))

        # Add part number if this is a split file
        if part_num is not None:
            elements.append(Paragraph(f"Complete Social Media Comments Report - Part {part_num}", heading2_style))
        else:
            elements.append(Paragraph("Complete Social Media Comments Report", heading2_style))

        elements.append(Spacer(1, 0.1*inch))

        # Add metadata
        metadata = json_data.get('metadata', {})
        session_name = metadata.get('session_name', 'Unknown Session')
        created_on = metadata.get('created_on', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        elements.append(Paragraph(f"Session: {session_name}", normal_style))
        elements.append(Paragraph(f"Original data created on: {created_on}", normal_style))
        elements.append(Paragraph(f"PDF generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style))

        # File specific info
        if part_num is not None:
            elements.append(Paragraph(f"Total videos in complete dataset: {total_videos}", normal_style))
            elements.append(Paragraph(f"Videos in this file: {len(videos_to_include)} (#{start_idx+1} to #{end_idx})", normal_style))
        else:
            elements.append(Paragraph(f"Total videos: {total_videos}", normal_style))

        if 'total_comments' in json_data:
            elements.append(Paragraph(f"Total comments in full dataset: {json_data.get('total_comments', 0)}", normal_style))
        else:
            total_comments = sum(len(v.get('comments', [])) for v in videos)
            elements.append(Paragraph(f"Total comments in full dataset: {total_comments}", normal_style))

        elements.append(Spacer(1, 0.3*inch))

        # Process each video
        for i, video in enumerate(videos_to_include):
            # Get video details
            video_number = video.get('video_number', start_idx + i + 1)
            video_id = video.get('video_id', 'Unknown')
            username = video.get('username', 'Unknown')
            caption = clean_text_for_pdf(video.get('caption', 'No caption'))
            comment_count = video.get('comment_count', len(video.get('comments', [])))

            # Get additional video metadata if available
            digg_count = video.get('digg_count', 0)
            play_count = video.get('play_count', 0)
            share_count = video.get('share_count', 0)

            platform = "tiktok"
            if video.get("comments"):
                first_comment = video.get("comments")[0]
                platform = first_comment.get("platform", "tiktok")
                if video_id == 'Unknown':
                    video_id = first_comment.get("video_id", 'Unknown')
                if caption == 'No caption':
                    caption = clean_text_for_pdf(first_comment.get("caption", 'No caption'))

            video_url = video.get("url", "")
            if not video_url:
                if platform == "youtube" and video_id != 'Unknown':
                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                elif platform == "tiktok" and video_id != 'Unknown' and username != 'Unknown':
                    video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"

            video_header_text = f"Video #{video_number} - {video_id} (@{username})"
            if video_url:
                video_header_text = f'<a href="{video_url}" color="blue">{video_header_text}</a>'
                caption = f'<a href="{video_url}" color="blue">{caption}</a>'

            # Add video header with background color
            elements.append(Paragraph(video_header_text, heading3_style))

            # Add video details
            elements.append(Paragraph(f"<b>Caption:</b> {caption}", normal_style))
            elements.append(Paragraph(f"<b>Comments:</b> {comment_count} | <b>Likes:</b> {digg_count:,} | <b>Views:</b> {play_count:,} | <b>Shares:</b> {share_count:,}", normal_style))
            elements.append(Spacer(1, 0.1*inch))

            # Process comments
            comments = video.get('comments', [])

            if comments:
                # Check if we need pagination for comments (if too many comments)
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
                        # Add page header for paginated comments
                        elements.append(PageBreak())
                        elements.append(Paragraph(f"Video #{video_number} - Comments (Page {page_num+1}/{len(comment_pages)})", heading3_style))
                        elements.append(Spacer(1, 0.1*inch))

                    # Create comment data as paragraphs for proper wrapping
                    comment_rows = []

                    # Add headers
                    header_row = [
                        Paragraph("<b>Comment</b>", comment_style),
                        Paragraph("<b>User</b>", comment_style),
                        Paragraph("<b>Likes</b>", comment_style)
                    ]
                    comment_rows.append(header_row)

                    # Process each comment
                    for comment in comment_page:
                        comment_text = clean_text_for_pdf(comment.get('text', 'No text'))

                        # Get user - handle different formats
                        if isinstance(comment.get('user', {}), dict) and comment.get('user'):
                            user = comment.get('user', {}).get('nickname', 'Unknown')
                        elif comment.get('author'):
                            user = comment.get('author')
                        elif comment.get('username'):
                            user = comment.get('username')
                        elif isinstance(comment.get('user'), str):
                            user = comment.get('user')
                        else:
                            user = 'Unknown'

                        likes = comment.get('digg_count', comment.get('likes_text', comment.get('likes', 0)))

                        # Create URLs
                        cid = comment.get('comment_id', comment.get('id', ''))
                        user_url = ""
                        comment_url = ""

                        if platform == "youtube":
                            author_id = comment.get('author_id', '')
                            if author_id:
                                user_url = f"https://www.youtube.com/channel/{author_id}"
                            if cid and video_id != 'Unknown':
                                comment_url = f"https://www.youtube.com/watch?v={video_id}&lc={cid}"
                        else: # tiktok
                            unique_id = comment.get('user', {}).get('unique_id', '') if isinstance(comment.get('user'), dict) else ""
                            if not unique_id and user != 'Unknown':
                                unique_id = user
                            if unique_id:
                                user_url = f"https://www.tiktok.com/@{unique_id}"
                            if cid and video_id != 'Unknown' and username != 'Unknown':
                                comment_url = f"https://www.tiktok.com/@{username}/video/{video_id}?commentId={cid}"

                        if comment_url:
                            comment_text = f'<a href="{comment_url}" color="blue">{comment_text}</a>'
                        if user_url:
                            user = f'<a href="{user_url}" color="blue">{user}</a>'

                        # Create paragraph objects for the table cells - key to proper text wrapping
                        comment_para = Paragraph(comment_text, comment_style)
                        user_para = Paragraph(user, comment_style)
                        likes_para = Paragraph(str(likes), comment_style)

                        comment_rows.append([comment_para, user_para, likes_para])

                    # Create table with appropriate column widths - wider first column for comments
                    table = Table(comment_rows, colWidths=[doc.width*0.64, doc.width*0.25, doc.width*0.11])

                    # Style the table
                    table.setStyle(TableStyle([
                        # Header row styling
                        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                        ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, 0), 10),
                        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),

                        # Body styling
                        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                        ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('ALIGN', (2, 1), (2, -1), 'CENTER'),  # Center-align likes column
                        ('TOPPADDING', (0, 1), (-1, -1), 6),   # Add padding to improve readability
                        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
                    ]))

                    elements.append(table)
            else:
                elements.append(Paragraph("No comments found for this video", normal_style))

            # Add page break if not the last video
            if i < len(videos_to_include) - 1:
                elements.append(PageBreak())

        # Add footer
        elements.append(Spacer(1, 0.4*inch))

        footer_text = f"Data by Kita Co. Lab TM - Generated on {datetime.now().strftime('%Y-%m-%d')}"
        if part_num is not None:
            footer_text += f" - Part {part_num} of {json_data.get('_pdf_info', {}).get('num_files', '?')}"

        elements.append(Paragraph(
            footer_text,
            ParagraphStyle('Footer', parent=normal_style, alignment=1, fontSize=8)
        ))

        # Generate PDF
        print(f"Building PDF document with {len(elements)} elements (this may take a while)...")
        doc.build(elements)

        return True, ""
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return False, f"Error generating PDF: {str(e)}\n{error_details}"

def main():
    print("Complete Social Media PDF Generator")
    print("=" * 30)

    if not REPORTLAB_AVAILABLE:
        error_msg = "ReportLab library not installed. Run 'pip install reportlab' to enable PDF export."
        print(f"Error: {error_msg}")
        messagebox.showerror("Error", error_msg)
        return

    # Display font info
    print(f"Default font: {DEFAULT_FONT}")
    print(f"Emoji font available: {EMOJI_FONT_AVAILABLE or 'None'}")

    # Select JSON file
    json_path = select_json_file()
    if not json_path:
        print("No file selected. Exiting.")
        return

    # Load JSON data
    json_data, error = load_json_data(json_path)
    if error:
        messagebox.showerror("Error", error)
        return

    # Generate base output path
    filename = os.path.basename(json_path)
    name_without_ext = os.path.splitext(filename)[0]
    output_dir = os.path.dirname(json_path)
    base_output_path = os.path.join(output_dir, f"{name_without_ext}_complete")

    # Estimate PDF size and determine if splitting is needed
    print("Analyzing data for PDF size estimation...")
    size_info = estimate_pdf_size(json_data)

    # Add size info to json_data for reference in the PDF
    json_data['_pdf_info'] = size_info

    if size_info["needs_splitting"]:
        print(f"Data will be split into {size_info['num_files']} files (estimated size: {size_info['estimated_size_mb']:.2f} MB)")
        print(f"Each file will contain approximately {size_info['videos_per_file']} videos")

        # Confirm with user
        if not messagebox.askyesno(
            "Confirm File Splitting",
            f"The PDF will be split into {size_info['num_files']} files to stay under {MAX_PDF_SIZE_MB}MB per file for LLM compatibility.\n\n"
            f"Each file will contain approx. {size_info['videos_per_file']} videos.\n\n"
            f"Do you want to continue?"
        ):
            print("Operation cancelled by user.")
            return

        # Create multiple PDFs
        success_count = 0
        error_files = []

        for part_num, (start_idx, end_idx) in enumerate(size_info["splits"], 1):
            output_path = f"{base_output_path}_part{part_num}.pdf"
            print(f"\nGenerating Part {part_num}: videos {start_idx+1}-{end_idx} -> {output_path}")

            success, error = create_complete_pdf(
                json_data,
                output_path,
                start_idx=start_idx,
                end_idx=end_idx,
                part_num=part_num
            )

            if success:
                success_count += 1
                print(f"Successfully created PDF part {part_num}")
            else:
                error_files.append((part_num, error))
                print(f"Error creating PDF part {part_num}: {error}")

        # Show final results
        if success_count == size_info["num_files"]:
            print(f"\nSuccessfully created all {size_info['num_files']} PDF files!")
            messagebox.showinfo(
                "PDF Generation Complete",
                f"Successfully created {size_info['num_files']} PDF files with all comments.\n\n"
                f"Files: {base_output_path}_part1.pdf through {base_output_path}_part{size_info['num_files']}.pdf\n\n"
                f"Data by Kita Co. Lab TM"
            )
        else:
            error_message = "\n".join([f"Part {p}: {e}" for p, e in error_files])
            print(f"\nCreated {success_count} of {size_info['num_files']} PDF files with errors in {len(error_files)} files:")
            print(error_message)

            messagebox.showwarning(
                "PDF Generation Partially Complete",
                f"Successfully created {success_count} of {size_info['num_files']} PDF files.\n\n"
                f"Errors occurred in {len(error_files)} files:\n{error_message[:200]}...\n\n"
                f"Data by Kita Co. Lab TM"
            )
    else:
        # Create a single PDF file
        output_path = f"{base_output_path}.pdf"
        print(f"\nGenerating single PDF file (estimated size: {size_info['estimated_size_mb']:.2f} MB)")
        print(f"Output file: {output_path}")

        success, error = create_complete_pdf(json_data, output_path)

        if success:
            print(f"Successfully created complete PDF file: {output_path}")
            messagebox.showinfo(
                "PDF Generation Complete",
                f"Successfully created complete PDF file with all comments.\n\n"
                f"File: {output_path}\n\n"
                f"Data by Kita Co. Lab TM"
            )
        else:
            print(f"Error creating PDF: {error}")
            messagebox.showerror("Error", f"Error creating PDF:\n{error}")

if __name__ == "__main__":
    main()