import os
import json
import tkinter as tk
from tkinter import filedialog, messagebox
import glob
from datetime import datetime
import re
import platform
import sys

# ReportLab imports for PDF generation
try:
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Setup fonts
    try:
        os_name = platform.system()
        if os_name == 'Windows':
            font_path = os.path.join(os.environ['WINDIR'], 'Fonts', 'seguiemj.ttf')
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont('SegoeEmoji', font_path))
                EMOJI_FONT_AVAILABLE = 'SegoeEmoji'
            else:
                EMOJI_FONT_AVAILABLE = False
        elif os_name == 'Darwin':  # macOS
            font_paths = ['/System/Library/Fonts/Apple Color Emoji.ttc', '/System/Library/Fonts/Apple Color Emoji.ttf']
            for font_path in font_paths:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('AppleEmoji', font_path))
                    EMOJI_FONT_AVAILABLE = 'AppleEmoji'
                    break
            else:
                EMOJI_FONT_AVAILABLE = False
        else:  # Linux and others
            font_paths = ['/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf', '/usr/share/fonts/google-noto/NotoColorEmoji.ttf']
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
            DEFAULT_FONT = 'Helvetica'
    except:
        DEFAULT_FONT = 'Helvetica'

    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    EMOJI_FONT_AVAILABLE = False
    DEFAULT_FONT = 'Helvetica'

def list_session_folders():
    """Find all session folders in the comments_data directory"""
    base_dir = "comments_data"
    if not os.path.exists(base_dir):
        return []

    session_folders = []
    for d in os.listdir(base_dir):
        folder_path = os.path.join(base_dir, d)
        if not os.path.isdir(folder_path):
            continue
        has_comments = os.path.isdir(os.path.join(folder_path, "comments"))
        has_logs = os.path.isdir(os.path.join(folder_path, "logs"))
        if has_comments or has_logs:
            session_folders.append(d)

    return session_folders

def select_session_folder():
    """Allow user to select a session folder"""
    session_folders = list_session_folders()

    if not session_folders:
        messagebox.showerror("Error", "No session folders found in comments_data directory.")
        return None

    root = tk.Tk()
    root.title("Select Session Folder")
    root.geometry("650x400")

    label = tk.Label(root, text="Select a session folder:")
    label.pack(pady=10)

    list_frame = tk.Frame(root)
    list_frame.pack(pady=10, padx=20, fill=tk.BOTH, expand=True)

    scrollbar = tk.Scrollbar(list_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    listbox = tk.Listbox(list_frame, width=70, height=15, font=("Courier New", 10))
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    listbox.config(yscrollcommand=scrollbar.set)
    scrollbar.config(command=listbox.yview)

    folder_info = []
    for folder in sorted(session_folders, reverse=True):
        base_path = os.path.join("comments_data", folder)
        has_comments = os.path.isdir(os.path.join(base_path, "comments"))
        has_logs = os.path.isdir(os.path.join(base_path, "logs"))

        comment_files = 0
        if has_comments:
            comment_path = os.path.join(base_path, "comments")
            comment_files = len([f for f in os.listdir(comment_path) if f.endswith("_comments.json")])

        display_text = f"{folder} [{comment_files} comments]"
        display_text += " [+logs]" if has_logs else ""

        listbox.insert(tk.END, display_text)
        folder_info.append(folder)

    if session_folders:
        listbox.selection_set(0)

    selected_folder = [None]

    def on_select():
        if listbox.curselection():
            index = listbox.curselection()[0]
            folder_name = folder_info[index]
            selected_folder[0] = os.path.join("comments_data", folder_name)
            root.destroy()
        else:
            messagebox.showerror("Error", "Please select a session folder")

    def on_double_click(event):
        on_select()

    listbox.bind("<Double-Button-1>", on_double_click)

    def on_cancel():
        root.destroy()

    button_frame = tk.Frame(root)
    button_frame.pack(pady=10)

    select_button = tk.Button(button_frame, text="Select", command=on_select)
    select_button.pack(side=tk.LEFT, padx=10)

    cancel_button = tk.Button(button_frame, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=10)

    root.mainloop()

    return selected_folder[0]

def extract_additional_data(session_folder):
    """Extract additional TikTok data not found in comment files"""
    if not session_folder:
        return None, "No session folder selected"

    result_data = {
        "session_info": {},
        "video_stats": [],
        "hashtag_analysis": {},
        "user_analysis": {},
        "engagement_metrics": {},
        "log_analysis": {}
    }

    # 1. Extract session information
    session_name = os.path.basename(session_folder)
    result_data["session_info"]["name"] = session_name

    # 2. Check for logs folder and extract data
    logs_folder = os.path.join(session_folder, "logs")
    if os.path.exists(logs_folder) and os.path.isdir(logs_folder):
        result_data["session_info"]["has_logs"] = True

        # Extract data from logs
        diagnostics_folder = os.path.join(logs_folder, "diagnostics")
        if os.path.exists(diagnostics_folder):
            diagnostic_files = [f for f in os.listdir(diagnostics_folder)
                               if f.endswith('_diagnostics.txt')]

            result_data["log_analysis"]["diagnostic_count"] = len(diagnostic_files)

            # Sample diagnostics from the first few files
            keywords = ["success", "error", "duration", "response", "status"]
            diagnostics_stats = {k: 0 for k in keywords}

            sample_files = diagnostic_files[:min(10, len(diagnostic_files))]
            for file_name in sample_files:
                file_path = os.path.join(diagnostics_folder, file_name)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read().lower()
                        for keyword in keywords:
                            if keyword in content:
                                diagnostics_stats[keyword] += 1
                except:
                    pass

            result_data["log_analysis"]["diagnostics_stats"] = diagnostics_stats
    else:
        result_data["session_info"]["has_logs"] = False

    # 3. Parse video metadata from comments folder
    comments_folder = os.path.join(session_folder, "comments")
    if os.path.exists(comments_folder) and os.path.isdir(comments_folder):
        result_data["session_info"]["has_comments"] = True

        json_files = [f for f in os.listdir(comments_folder)
                     if f.endswith('_comments.json')]

        # Process all JSON files to extract video metadata
        hashtags = {}
        usernames = {}
        engagement_totals = {"likes": 0, "comments": 0, "shares": 0, "views": 0}

        for file_name in json_files:
            file_path = os.path.join(comments_folder, file_name)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Extract video stats
                video_stats = {
                    "video_number": data.get("video_number", 0),
                    "video_id": data.get("video_id", "Unknown"),
                    "username": data.get("username", "Unknown"),
                    "comment_count": data.get("comment_count", 0),
                    "digg_count": data.get("digg_count", 0),
                    "play_count": data.get("play_count", 0),
                    "share_count": data.get("share_count", 0)
                }

                # Add to totals
                engagement_totals["likes"] += video_stats.get("digg_count", 0)
                engagement_totals["comments"] += video_stats.get("comment_count", 0)
                engagement_totals["shares"] += video_stats.get("share_count", 0)
                engagement_totals["views"] += video_stats.get("play_count", 0)

                result_data["video_stats"].append(video_stats)

                # Extract hashtags from caption
                caption = data.get("caption", "")
                if caption:
                    tags = re.findall(r'#(\w+)', caption)
                    for tag in tags:
                        hashtags[tag] = hashtags.get(tag, 0) + 1

                # Update username count
                username = data.get("username", "Unknown")
                usernames[username] = usernames.get(username, 0) + 1

            except Exception as e:
                print(f"Error processing {file_name}: {str(e)}")

        # Sort hashtags and users by frequency
        result_data["hashtag_analysis"] = {
            "total_unique": len(hashtags),
            "top_hashtags": sorted(hashtags.items(), key=lambda x: x[1], reverse=True)[:20]
        }

        result_data["user_analysis"] = {
            "total_unique": len(usernames),
            "top_users": sorted(usernames.items(), key=lambda x: x[1], reverse=True)[:20]
        }

        result_data["engagement_metrics"] = engagement_totals
    else:
        result_data["session_info"]["has_comments"] = False

    return result_data, ""

def create_analytics_pdf(analytics_data, output_path):
    """Create a PDF with TikTok analytics information"""
    if not REPORTLAB_AVAILABLE:
        return False, "ReportLab library not installed. Run 'pip install reportlab' to enable PDF export."

    try:
        # Create PDF document
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=48,
            leftMargin=48,
            topMargin=48,
            bottomMargin=48,
            title=f"TikTok Analytics Data"
        )

        # Define styles
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontName=f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold',
            fontSize=18,
            alignment=1,
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

        # Create content elements
        elements = []

        # Add title and company information
        elements.append(Paragraph("Data by Kita Co. Lab TM", title_style))
        elements.append(Paragraph("TikTok Analytics Report", heading2_style))
        elements.append(Spacer(1, 0.1*inch))

        # Session information
        session_info = analytics_data.get("session_info", {})
        elements.append(Paragraph("Session Information", heading2_style))
        elements.append(Paragraph(f"Session: {session_info.get('name', 'Unknown')}", normal_style))
        elements.append(Paragraph(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style))
        elements.append(Paragraph(f"Has logs data: {'Yes' if session_info.get('has_logs', False) else 'No'}", normal_style))
        elements.append(Paragraph(f"Has comments data: {'Yes' if session_info.get('has_comments', False) else 'No'}", normal_style))
        elements.append(Spacer(1, 0.2*inch))

        # Engagement Summary
        engagement = analytics_data.get("engagement_metrics", {})
        elements.append(Paragraph("Engagement Summary", heading2_style))

        # Create engagement table
        engagement_data = [['Metric', 'Count']]
        engagement_data.append(['Views', f"{engagement.get('views', 0):,}"])
        engagement_data.append(['Likes', f"{engagement.get('likes', 0):,}"])
        engagement_data.append(['Comments', f"{engagement.get('comments', 0):,}"])
        engagement_data.append(['Shares', f"{engagement.get('shares', 0):,}"])

        engagement_table = Table(engagement_data, colWidths=[doc.width*0.6, doc.width*0.4])
        engagement_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
        ]))

        elements.append(engagement_table)
        elements.append(Spacer(1, 0.2*inch))

        # Hashtag Analysis
        hashtag_analysis = analytics_data.get("hashtag_analysis", {})
        if hashtag_analysis and hashtag_analysis.get("top_hashtags"):
            elements.append(Paragraph("Hashtag Analysis", heading2_style))
            elements.append(Paragraph(f"Total unique hashtags: {hashtag_analysis.get('total_unique', 0)}", normal_style))
            elements.append(Spacer(1, 0.1*inch))
            elements.append(Paragraph("Top Hashtags:", heading3_style))

            # Create hashtag table
            hashtag_data = [['Hashtag', 'Occurrences']]
            for hashtag, count in hashtag_analysis.get("top_hashtags", [])[:10]:
                hashtag_data.append([f"#{hashtag}", str(count)])

            hashtag_table = Table(hashtag_data, colWidths=[doc.width*0.6, doc.width*0.4])
            hashtag_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                ('ALIGN', (1, 1), (1, -1), 'CENTER'),
            ]))

            elements.append(hashtag_table)
            elements.append(Spacer(1, 0.2*inch))

        # User Analysis
        user_analysis = analytics_data.get("user_analysis", {})
        if user_analysis and user_analysis.get("top_users"):
            elements.append(Paragraph("User Analysis", heading2_style))
            elements.append(Paragraph(f"Total unique users: {user_analysis.get('total_unique', 0)}", normal_style))
            elements.append(Spacer(1, 0.1*inch))
            elements.append(Paragraph("Top Users:", heading3_style))

            # Create user table
            user_data = [['Username', 'Videos']]
            for username, count in user_analysis.get("top_users", [])[:10]:
                user_data.append([f"@{username}", str(count)])

            user_table = Table(user_data, colWidths=[doc.width*0.6, doc.width*0.4])
            user_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                ('ALIGN', (1, 1), (1, -1), 'CENTER'),
            ]))

            elements.append(user_table)
            elements.append(Spacer(1, 0.2*inch))

        # Video Statistics Table
        video_stats = analytics_data.get("video_stats", [])
        if video_stats:
            elements.append(Paragraph("Video Statistics", heading2_style))
            elements.append(Paragraph(f"Total videos: {len(video_stats)}", normal_style))
            elements.append(Spacer(1, 0.1*inch))

            # Calculate averages
            if video_stats:
                avg_comments = sum(v.get("comment_count", 0) for v in video_stats) / len(video_stats)
                avg_likes = sum(v.get("digg_count", 0) for v in video_stats) / len(video_stats)
                avg_views = sum(v.get("play_count", 0) for v in video_stats) / len(video_stats)
                avg_shares = sum(v.get("share_count", 0) for v in video_stats) / len(video_stats)

                elements.append(Paragraph("Average Engagement Per Video:", heading3_style))
                avg_data = [['Metric', 'Average']]
                avg_data.append(['Views', f"{avg_views:,.1f}"])
                avg_data.append(['Likes', f"{avg_likes:,.1f}"])
                avg_data.append(['Comments', f"{avg_comments:,.1f}"])
                avg_data.append(['Shares', f"{avg_shares:,.1f}"])

                avg_table = Table(avg_data, colWidths=[doc.width*0.6, doc.width*0.4])
                avg_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                    ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
                ]))

                elements.append(avg_table)
                elements.append(Spacer(1, 0.2*inch))

            # Top 10 videos by views
            elements.append(Paragraph("Top 10 Videos by Views", heading3_style))

            # Sort videos by view count
            sorted_videos = sorted(video_stats, key=lambda x: x.get("play_count", 0), reverse=True)[:10]

            # Create top videos table
            top_video_data = [['#', 'Video ID', 'Username', 'Views', 'Likes', 'Comments']]
            for i, video in enumerate(sorted_videos, 1):
                top_video_data.append([
                    str(i),
                    video.get("video_id", "Unknown"),
                    f"@{video.get('username', 'Unknown')}",
                    f"{video.get('play_count', 0):,}",
                    f"{video.get('digg_count', 0):,}",
                    f"{video.get('comment_count', 0):,}"
                ])

            top_video_table = Table(top_video_data, colWidths=[
                doc.width*0.05, doc.width*0.25, doc.width*0.25,
                doc.width*0.15, doc.width*0.15, doc.width*0.15
            ])

            top_video_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                ('ALIGN', (0, 1), (0, -1), 'CENTER'),
                ('ALIGN', (3, 1), (5, -1), 'RIGHT'),
            ]))

            elements.append(top_video_table)
            elements.append(Spacer(1, 0.2*inch))

        # Log Analysis
        log_analysis = analytics_data.get("log_analysis", {})
        if log_analysis:
            elements.append(Paragraph("Log Analysis", heading2_style))
            elements.append(Paragraph(f"Diagnostic files found: {log_analysis.get('diagnostic_count', 0)}", normal_style))

            diagnostics_stats = log_analysis.get("diagnostics_stats", {})
            if diagnostics_stats:
                elements.append(Spacer(1, 0.1*inch))
                elements.append(Paragraph("Diagnostics Keywords Frequency:", heading3_style))

                stats_data = [['Keyword', 'Occurrences']]
                for keyword, count in diagnostics_stats.items():
                    stats_data.append([keyword.capitalize(), str(count)])

                stats_table = Table(stats_data, colWidths=[doc.width*0.6, doc.width*0.4])
                stats_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.darkblue),
                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold" if DEFAULT_FONT != 'Helvetica' else 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                    ('ALIGN', (1, 1), (1, -1), 'CENTER'),
                ]))

                elements.append(stats_table)

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

def main():
    print("TikTok Analytics PDF Generator")
    print("=" * 30)

    # Select session folder
    session_folder = select_session_folder()
    if not session_folder:
        print("No session folder selected. Exiting.")
        return

    print(f"Processing data from: {session_folder}")

    # Extract additional data
    analytics_data, error = extract_additional_data(session_folder)

    if error:
        print(f"Error: {error}")
        messagebox.showerror("Error", error)
        return

    if not analytics_data:
        print("No analytics data found. Exiting.")
        messagebox.showerror("Error", "No analytics data found")
        return

    # Generate PDF filename from session folder
    session_name = os.path.basename(session_folder)
    pdf_path = os.path.join(session_folder, f"{session_name}_analytics.pdf")

    # Create PDF with analytics data
    success, error = create_analytics_pdf(analytics_data, pdf_path)

    if not success:
        print(f"Error generating PDF: {error}")
        messagebox.showerror("Error", f"Error generating PDF: {error}")
        return

    print("\nAnalytics PDF generated successfully!")
    print(f"Output file: {pdf_path}")

    # Display statistics summary
    video_count = len(analytics_data.get("video_stats", []))
    engagement = analytics_data.get("engagement_metrics", {})

    print(f"\nStatistics Summary:")
    print(f"- Total videos: {video_count}")
    print(f"- Total views: {engagement.get('views', 0):,}")
    print(f"- Total likes: {engagement.get('likes', 0):,}")
    print(f"- Total comments: {engagement.get('comments', 0):,}")

    hashtags = analytics_data.get("hashtag_analysis", {})
    print(f"- Unique hashtags: {hashtags.get('total_unique', 0)}")

    users = analytics_data.get("user_analysis", {})
    print(f"- Unique users: {users.get('total_unique', 0)}")

    # Show message box to user
    messagebox.showinfo(
        "Analytics PDF Generated",
        f"Successfully generated analytics PDF with data from {video_count} videos.\n\n"
        f"File saved to:\n{pdf_path}\n\n"
        f"Data by Kita Co. Lab TM - Generated on {datetime.now().strftime('%Y-%m-%d')}"
    )

if __name__ == "__main__":
    main()