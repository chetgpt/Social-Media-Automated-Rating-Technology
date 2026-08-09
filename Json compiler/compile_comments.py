import os
import json
import argparse
from datetime import datetime
from pathlib import Path
import glob
import re


def get_all_json_files(comments_data_dir, session_name=None):
    """
    Find all JSON files in the comments_data directory and subdirectories.
    If session_name is provided, only search in that specific session folder.
    """
    json_files = []
    
    # Handle both absolute and relative paths
    comments_data_dir = os.path.abspath(comments_data_dir)
    
    # If a specific session is requested, only look in that session folder
    if session_name:
        session_path = os.path.join(comments_data_dir, session_name)
        if os.path.isdir(session_path):
            comments_dir = os.path.join(session_path, "comments")
            if os.path.isdir(comments_dir):
                for json_file in glob.glob(os.path.join(comments_dir, "*.json")):
                    json_files.append(json_file)
            else:
                print(f"Warning: No comments directory found in session {session_name}")
        else:
            print(f"Warning: Session folder {session_name} not found in {comments_data_dir}")
        return json_files
    
    # Otherwise, check all session directories
    for session_dir in os.listdir(comments_data_dir):
        session_path = os.path.join(comments_data_dir, session_dir)
        
        if not os.path.isdir(session_path):
            continue
            
        # Look for comments directory in each session
        comments_dir = os.path.join(session_path, "comments")
        if os.path.isdir(comments_dir):
            # Get all JSON files in the comments directory
            for json_file in glob.glob(os.path.join(comments_dir, "*.json")):
                json_files.append(json_file)
    
    # Also check for any JSON files directly in the comments_data/comments directory
    comments_dir = os.path.join(comments_data_dir, "comments")
    if os.path.isdir(comments_dir):
        for json_file in glob.glob(os.path.join(comments_dir, "*.json")):
            json_files.append(json_file)
    
    return json_files


def find_diagnostic_file(comments_file):
    """Find the corresponding diagnostic log file for a comments file."""
    # Extract the video number from the comments file name
    match = re.search(r'scraped_comments_video_(\d+)\.json', os.path.basename(comments_file))
    if not match:
        return None
    
    video_number = match.group(1)
    
    # Construct the path to the logs directory
    comments_dir = os.path.dirname(comments_file)
    session_dir = os.path.dirname(comments_dir)
    logs_dir = os.path.join(session_dir, "logs")
    
    # Check if logs directory exists
    if not os.path.isdir(logs_dir):
        return None
    
    # Look for the diagnostic file with the same video number
    diagnostic_file = os.path.join(logs_dir, f"diagnostic_{video_number}.json")
    if os.path.exists(diagnostic_file):
        return diagnostic_file
    
    return None


def get_session_folder(json_files):
    """Extract the session folder from a list of JSON files."""
    if not json_files:
        return None
    
    # Get the first file path and extract its session directory
    first_file = json_files[0]
    comments_dir = os.path.dirname(first_file)
    session_dir = os.path.dirname(comments_dir)
    
    # Check if it's a valid session directory
    if os.path.basename(session_dir).startswith("session_"):
        return session_dir
    
    return None


def list_available_sessions(comments_data_dir):
    """List all available session folders in the comments_data directory."""
    sessions = []
    
    comments_data_dir = os.path.abspath(comments_data_dir)
    
    for item in os.listdir(comments_data_dir):
        item_path = os.path.join(comments_data_dir, item)
        if os.path.isdir(item_path) and item.startswith("session_"):
            # Check if this session has a comments directory
            comments_dir = os.path.join(item_path, "comments")
            if os.path.isdir(comments_dir):
                # Count the JSON files in this session
                json_count = len(glob.glob(os.path.join(comments_dir, "*.json")))
                sessions.append((item, json_count))
    
    return sessions


def select_session_interactively(comments_data_dir):
    """Prompt the user to select a session folder interactively."""
    sessions = list_available_sessions(comments_data_dir)
    
    if not sessions:
        print(f"No session folders found in {comments_data_dir}")
        return None
    
    print("\nAvailable session folders:")
    print("---------------------------")
    for i, (session, count) in enumerate(sessions, 1):
        # Extract the date from the session name (e.g., session_20250416_185038 -> 2025-04-16)
        if len(session) > 8 and session.startswith("session_"):
            date_part = session[8:16]  # Extract the date portion
            if len(date_part) == 8:
                formatted_date = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                print(f"{i}. {session} - {count} files (Date: {formatted_date})")
            else:
                print(f"{i}. {session} - {count} files")
        else:
            print(f"{i}. {session} - {count} files")
    
    print(f"{len(sessions) + 1}. All sessions")
    print("0. Exit")
    
    while True:
        try:
            choice = int(input("\nEnter your choice (number): "))
            if choice == 0:
                return "exit"
            elif choice == len(sessions) + 1:
                return None  # Process all sessions
            elif 1 <= choice <= len(sessions):
                return sessions[choice - 1][0]  # Return the selected session name
            else:
                print(f"Please enter a number between 0 and {len(sessions) + 1}")
        except ValueError:
            print("Please enter a valid number")


def compile_comments(json_files, output_file, add_session_info=True, filter_by_username=None, include_diagnostic_data=True):
    """Compile comments from multiple JSON files into a single file."""
    all_comments = []
    total_comments = 0
    videos_processed = 0
    
    for json_file in json_files:
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Extract session info from the path if needed
            session_info = None
            if add_session_info:
                # Extract session name from path (e.g., session_20250416_185038)
                path_parts = Path(json_file).parts
                for part in path_parts:
                    if part.startswith("session_"):
                        session_info = part
                        break
            
            # Extract video ID from filename
            filename = os.path.basename(json_file)
            video_id = filename.replace("scraped_comments_video_", "").replace(".json", "")
            
            # Find and load corresponding diagnostic file if available
            diagnostic_data = {}
            if include_diagnostic_data:
                diagnostic_file = find_diagnostic_file(json_file)
                if diagnostic_file and os.path.exists(diagnostic_file):
                    try:
                        with open(diagnostic_file, 'r', encoding='utf-8') as f:
                            diagnostic_data = json.load(f)
                    except Exception as e:
                        print(f"Error reading diagnostic file {diagnostic_file}: {e}")
            
            # Process comments
            if "comments" in data and isinstance(data["comments"], list):
                comments_to_add = []
                
                for comment in data["comments"]:
                    # Add source info to each comment
                    comment_with_source = comment.copy()
                    
                    # Ensure all required fields exist (even if null)
                    if "username" not in comment_with_source:
                        comment_with_source["username"] = None
                    if "commentText" not in comment_with_source:
                        comment_with_source["commentText"] = None
                    if "timeStamp" not in comment_with_source:
                        comment_with_source["timeStamp"] = None
                    
                    # Add video ID
                    comment_with_source["video_id"] = video_id
                    
                    # Add session info if available
                    if session_info:
                        comment_with_source["session"] = session_info
                    
                    # Add diagnostic data to each comment
                    if diagnostic_data:
                        # Add key metadata from diagnostic file
                        if "url" in diagnostic_data:
                            comment_with_source["tiktok_url"] = diagnostic_data["url"]
                        if "timestamp" in diagnostic_data:
                            comment_with_source["scraping_timestamp"] = diagnostic_data["timestamp"]
                    
                    # Apply username filter if specified
                    if filter_by_username is None or (
                            "username" in comment and 
                            comment["username"] and
                            filter_by_username.lower() in comment["username"].lower()
                    ):
                        comments_to_add.append(comment_with_source)
                
                all_comments.extend(comments_to_add)
                total_comments += len(comments_to_add)
                videos_processed += 1
                
                # Print information about processed file
                comment_count_info = f"Added {len(comments_to_add)} comments"
                diagnostic_info = ""
                if diagnostic_data:
                    diagnostic_info = f" (URL: {diagnostic_data.get('url', 'N/A')})"
                print(f"Processed {json_file} - {comment_count_info}{diagnostic_info}")
            else:
                print(f"Warning: No comments found in {json_file}")
        
        except Exception as e:
            print(f"Error processing {json_file}: {e}")
    
    # Create the output data structure
    output_data = {
        "compiled_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_videos_processed": videos_processed,
        "total_comments": total_comments,
        "comments": all_comments
    }
    
    # Only create directories if the output path has a directory component
    dirname = os.path.dirname(output_file)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    
    # Write the compiled data to the output file
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    
    return output_data


def main():
    parser = argparse.ArgumentParser(description="Compile TikTok comments from multiple JSON files")
    # Default to the parent directory's comments_data folder
    default_comments_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "comments_data"))
    
    parser.add_argument("--comments_dir", default=default_comments_dir, 
                       help=f"Directory containing comments data (default: {default_comments_dir})")
    parser.add_argument("--output", default=None, help="Output JSON file (default: save in session folder)")
    parser.add_argument("--filter_username", help="Filter comments by username (case insensitive)")
    parser.add_argument("--no_session_info", action="store_true", help="Don't add session info to comments")
    parser.add_argument("--session", help="Process only a specific session folder (e.g., session_20250416_185038)")
    parser.add_argument("--list_sessions", action="store_true", help="List all available session folders and exit")
    parser.add_argument("--non_interactive", action="store_true", help="Run in non-interactive mode")
    parser.add_argument("--no_diagnostic_data", action="store_true", help="Don't include diagnostic data from log files")
    
    args = parser.parse_args()
    
    # List available sessions if requested
    if args.list_sessions:
        print(f"Available session folders in {args.comments_dir}:")
        sessions = list_available_sessions(args.comments_dir)
        if sessions:
            for i, (session, count) in enumerate(sessions, 1):
                print(f"{i}. {session} ({count} files)")
        else:
            print("No session folders found")
        return
    
    # If session is not specified and not in non-interactive mode, prompt for session
    selected_session = args.session
    if not args.non_interactive and not selected_session:
        selected_session = select_session_interactively(args.comments_dir)
        if selected_session == "exit":
            print("Exiting...")
            return
    
    # Prompt for username filter if in interactive mode and not already specified
    filter_username = args.filter_username
    if not args.non_interactive and not filter_username:
        filter_choice = input("\nDo you want to filter comments by username? (y/n): ").lower().strip()
        if filter_choice == 'y':
            filter_username = input("Enter username to filter by: ").strip()
    
    # Ask about including diagnostic data
    include_diagnostic_data = not args.no_diagnostic_data
    if not args.non_interactive:
        diagnostic_choice = input("\nInclude diagnostic data from log files? (y/n): ").lower().strip()
        include_diagnostic_data = diagnostic_choice == 'y'
    
    print(f"\nSearching for JSON files in {args.comments_dir}...")
    json_files = get_all_json_files(args.comments_dir, selected_session)
    print(f"Found {len(json_files)} JSON files")
    
    if not json_files:
        print("No JSON files found. Exiting.")
        return
    
    # Get the session folder to save the output file there
    session_folder = None
    if selected_session:
        session_folder = os.path.join(args.comments_dir, selected_session)
    else:
        session_folder = get_session_folder(json_files)
    
    # Construct output filename
    output_file = args.output
    if not output_file:
        if session_folder and os.path.isdir(session_folder):
            # Save in the session folder
            if selected_session:
                output_name = f"compiled_comments_{selected_session}.json"
            else:
                session_name = os.path.basename(session_folder)
                output_name = f"compiled_comments_{session_name}.json"
            
            output_file = os.path.join(session_folder, output_name)
        else:
            # Default filename if no session folder is found
            output_file = "compiled_comments.json"
    
    # Ask the user to confirm or change the output path in interactive mode
    if not args.non_interactive:
        print(f"\nDefault output file: {output_file}")
        custom_path = input("Press Enter to accept or type a different filename: ").strip()
        if custom_path:
            output_file = custom_path
    
    print(f"\nCompiling comments to {output_file}...")
    result = compile_comments(
        json_files, 
        output_file, 
        add_session_info=not args.no_session_info,
        filter_by_username=filter_username,
        include_diagnostic_data=include_diagnostic_data
    )
    
    print(f"\nCompilation complete! Processed {result['total_videos_processed']} videos with {result['total_comments']} comments.")
    print(f"Output saved to {output_file}")


if __name__ == "__main__":
    main() 