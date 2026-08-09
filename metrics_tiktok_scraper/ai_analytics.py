import os
import json
import logging
from typing import Dict, Any
import requests
import base64
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

try:
    import google.generativeai as genai
    from dotenv import load_dotenv
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('ai_analytics')

def setup_gemini():
    if not HAS_GENAI:
        logger.error("google-generativeai or python-dotenv is not installed. Please run: pip install google-generativeai python-dotenv")
        return False

    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not found in .env file.")
        return False

    genai.configure(api_key=api_key)
    return True

def load_comments_data(file_path: str) -> tuple:
    """Load and format comments data for the LLM. Returns (creator_name, formatted_string)."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        comments = []
        creator_name = "the brand/creator"

        if "videos" in data:
            for video in data["videos"]:
                if "creator_name" in video and creator_name == "the brand/creator":
                    creator_name = video["creator_name"]
                elif "username" in video and creator_name == "the brand/creator":
                    creator_name = video["username"]

                v_comments = video.get("comments", [])
                comments.extend(v_comments)
        else:
            comments = data.get("comments", [])
            creator_name = data.get("creator_name") or data.get("username") or "the brand/creator"

        if not comments:
            return creator_name, "No comments found in file."

        # Format comments compactly to save tokens
        formatted_comments = []
        for i, c in enumerate(comments):
            text = c.get("text") or c.get("commentText") or ""
            if text:
                formatted_comments.append(f"[{i+1}] {text}")

        # Combine video metadata and comments
        metadata = f"Video Creator: {creator_name}\n"
        # Only take up to 200 comments to avoid hitting token limits
        max_comments = 200
        comments_str = "\n".join(formatted_comments[:max_comments])
        if len(formatted_comments) > max_comments:
            comments_str += f"\n... (and {len(formatted_comments) - max_comments} more comments omitted for brevity)"

        return creator_name, metadata + "\n--- COMMENTS ---\n" + comments_str

    except Exception as e:
        logger.error(f"Failed to load comments file {file_path}: {e}")
        return "", ""

def run_analysis_pipeline(comments_file: str, output_dir: str = "analysis_reports"):
    if not setup_gemini():
        return

    logger.info(f"Loading comments from {comments_file}")
    creator_name, data_str = load_comments_data(comments_file)
    if not data_str:
        logger.error("Could not load comments data. Aborting analysis.")
        return

    # Ensure output directories exist
    os.makedirs(output_dir, exist_ok=True)

    # We use gemini-2.5-flash as it is widely supported in this environment
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
    except AttributeError:
        # Fallback
        model = genai.GenerativeModel('gemini-2.5-flash')

    logger.info("Initializing Gemini model...")

    def get_safe_text(res, step_name):
        try:
            return res.text
        except (ValueError, Exception):
            logger.warning(f"{step_name} returned no parts. Finish reason: {res.candidates[0].finish_reason if res.candidates else 'Unknown'}")
            return f"[{step_name} Analysis Unavailable: The model returned an empty response. This may be due to safety filters regarding medical or sensitive topics.]"

    # Step 1: Message Reception Analysis
    logger.info("Running Step 1: Message Reception Analysis...")
    prompt_1 = (
        f"Analyze the following social media comments to understand how the message conveyed by {creator_name} "
        "(through its caption and replies) is received by the customers. Identify the main themes, sentiments, "
        "and direct feedback from the audience.\n\n"
        f"{data_str}"
    )
    response_1 = model.generate_content(prompt_1)
    step1_result = get_safe_text(response_1, "Step 1")

    # Step 2: Audio Branding Plan
    logger.info("Running Step 2: Audio Branding Plan...")
    prompt_2 = (
        f"Based on the following analysis of customer reception for {creator_name}, create an actionable "
        "audio branding plan. Outline the specific moods, physical scenes, colors, eras, and textures that "
        "should represent their audio identity.\n\n"
        f"--- Reception Analysis ---\n{step1_result}"
    )
    response_2 = model.generate_content(prompt_2)
    step2_result = get_safe_text(response_2, "Step 2")

    # Step 3: Intended Message vs Customer Feelings
    logger.info("Running Step 3: Intended Message vs Customer Feelings...")
    prompt_3 = (
        f"Analyze what the intended message by {creator_name} likely was, and contrast that with how customers "
        "actually feel based on their comments. Highlight any gaps or alignments.\n\n"
        f"{data_str}"
    )
    response_3 = model.generate_content(prompt_3)
    step3_result = get_safe_text(response_3, "Step 3")

    # Step 4: Spotify Search Query Translator
    logger.info("Running Step 4: Spotify Advanced Search Query Translator...")
    prompt_4 = (
        "You are a master Spotify Advanced Search Query translator. Your job is to convert any abstract input "
        "(moods, physical scenes, colors, shapes, eras, weather, or textures) into a single, highly refined "
        "Spotify search string using advanced operators (OR, NOT, \"\").\n\n"
        "RULES:\n"
        "- Deconstruct the user's prompt into its core aesthetic, cultural, or physical elements.\n"
        "- Cross-map those elements to exactly 8 to 10 specific artists that embody this vibe (do not list more than 10).\n"
        "- Group these artist names together using the capitalized 'OR' operator so they are all searched at once.\n"
        "- Do NOT use the \"artist:\" tag prefix anywhere in the query. Simply use the artists' raw names.\n"
        "- Wrap multi-word artist names or concepts in double quotes (e.g., \"Pink Floyd\" OR \"Tame Impala\").\n"
        "- Output ONLY the raw search string. No explanations, no markdown blocks, no introductory text.\n\n"
        "4. The output must be formatted like this example:\n"
        "   artist:\"Artist Name\" OR artist:\"Artist Name\" OR genre:\"genre name\"\n\n"
        f"--- Input (Audio Branding Plan) ---\n{step2_result}"
    )
    response_4 = model.generate_content(prompt_4)
    step4_result = get_safe_text(response_4, "Step 4").strip().replace("```", "").strip() # Clean up any accidental markdown blocks

    # Step 5: Spotify Search and PDF Generation
    logger.info("Running Step 5: Spotify Search and PDF Generation...")

    spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID")
    spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    pdf_path = os.path.join(output_dir, "comprehensive_ai_analysis.pdf")
    comments_pdf_path = os.path.join(output_dir, "comments_data.pdf")

    tracks = []
    if not spotify_client_id or not spotify_client_secret:
        logger.warning("Spotify credentials missing. Will skip Spotify search, but still generate the analysis PDF.")
    else:
        try:
            # 1. Authenticate with Spotify
            auth_string = f"{spotify_client_id}:{spotify_client_secret}"
            auth_bytes = auth_string.encode("utf-8")
            auth_base64 = str(base64.b64encode(auth_bytes), "utf-8")

            token_url = "https://accounts.spotify.com/api/token"
            headers = {
                "Authorization": f"Basic {auth_base64}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            data = {"grant_type": "client_credentials"}
            token_res = requests.post(token_url, headers=headers, data=data)
            token_res.raise_for_status()
            access_token = token_res.json().get("access_token")

            # 2. Search Spotify using the query
            search_url = "https://api.spotify.com/v1/search"
            search_headers = {"Authorization": f"Bearer {access_token}"}
            search_params = {
                "q": step4_result,
                "type": "track"
            }
            search_res = requests.get(search_url, headers=search_headers, params=search_params)
            search_res.raise_for_status()
            tracks = search_res.json().get("tracks", {}).get("items", [])[:15]
        except Exception as e:
            logger.error(f"Error searching Spotify: {e}")

    # Build the single PDF
    try:
        from reportlab.lib.styles import getSampleStyleSheet

        doc = SimpleDocTemplate(pdf_path, pagesize=letter)
        styles = getSampleStyleSheet()
        title_style = styles['Title']
        h1_style = styles['Heading1']
        h2_style = styles['Heading2']
        normal_style = styles['Normal']
        # Create a style for the analysis text to preserve some spacing
        body_style = ParagraphStyle(
            name='BodyText',
            parent=styles['Normal'],
            spaceBefore=6,
            spaceAfter=6,
            leading=14
        )

        elements = []
        elements.append(Paragraph(f"Comprehensive AI Analysis: {creator_name}", title_style))
        elements.append(Spacer(1, 20))

        # Helper to safely parse markdown-like text (very basic: replace newlines)
        def add_text_section(title, text):
            elements.append(Paragraph(title, h1_style))
            elements.append(Spacer(1, 10))
            # Split by double newline to form paragraphs
            paragraphs = text.split("\n\n")
            for para in paragraphs:
                para = para.replace("\n", " ").strip()
                # Remove problematic tags like * or # which reportlab XML parser might choke on if misused
                para = para.replace('<', '&lt;').replace('>', '&gt;')
                # Basic bold replacement (not perfect, but handles standard **text**)
                import re
                para = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', para)
                para = re.sub(r'\*(.*?)\*', r'<i>\1</i>', para)
                if para:
                    elements.append(Paragraph(para, body_style))
            elements.append(Spacer(1, 20))

        add_text_section("1. Message Reception Analysis", step1_result)
        add_text_section("2. Audio Branding Plan", step2_result)
        add_text_section("3. Intended Message vs Customer Feelings", step3_result)

        elements.append(Paragraph("4. Spotify Search Query Translation", h1_style))
        elements.append(Spacer(1, 10))
        elements.append(Paragraph(f"<b>Query Used:</b> {step4_result.replace('<', '&lt;').replace('>', '&gt;')}", body_style))
        elements.append(Spacer(1, 20))

        elements.append(Paragraph("5. Spotify Track Recommendations", h1_style))
        elements.append(Spacer(1, 10))

        if not tracks:
            elements.append(Paragraph("No tracks found for this query or Spotify integration failed.", normal_style))
        else:
            table_data = [["Track Name", "Artist", "Listen on Spotify"]]
            for t in tracks:
                track_name = t.get("name", "Unknown").replace('<', '&lt;').replace('>', '&gt;')
                artist_name = ", ".join([a.get("name") for a in t.get("artists", [])]).replace('<', '&lt;').replace('>', '&gt;')
                url = t.get("external_urls", {}).get("spotify", "")

                if url:
                    link_text = f"<link href='{url}' color='blue'>Open in Spotify</link>"
                else:
                    link_text = "No link"

                table_data.append([
                    Paragraph(track_name, normal_style),
                    Paragraph(artist_name, normal_style),
                    Paragraph(link_text, normal_style)
                ])

            t = Table(table_data, colWidths=[200, 150, 150])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.grey),
                ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0,0), (-1,0), 12),
                ('BACKGROUND', (0,1), (-1,-1), colors.beige),
                ('GRID', (0,0), (-1,-1), 1, colors.black),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ]))
            elements.append(t)

        doc.build(elements)
        logger.info("Comprehensive AI Analysis PDF generated successfully.")

    except Exception as e:
        logger.error(f"Error generating Comprehensive PDF: {e}")

    # Compile the comments to a PDF
    try:
        import complete_pdf_generator
        logger.info("Generating Comments Data PDF...")
        with open(comments_file, 'r', encoding='utf-8') as f:
            json_data = json.load(f)

        # Check if the JSON structure is the one complete_pdf_generator expects (which expects a dict with 'videos' list)
        # If it's a single video JSON (which might be the case), wrap it.
        if "comments" in json_data and "videos" not in json_data:
            json_data = {"videos": [json_data]}

        complete_pdf_generator.create_complete_pdf(json_data, comments_pdf_path)
        logger.info(f"Comments Data PDF generated at {comments_pdf_path}")
    except Exception as e:
        logger.error(f"Failed to generate Comments Data PDF: {e}")

    logger.info(f"Analysis complete! Unified reports saved to {os.path.abspath(output_dir)}")
