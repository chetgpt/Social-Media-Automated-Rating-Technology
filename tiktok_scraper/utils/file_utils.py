"""File-related utility functions for the TikTok scraper."""
import os
import json
import datetime

def create_folder_structure():
    """Creates the folder structure for saving comments and logs.
    
    Returns:
        dict: Dictionary containing paths to the created folders
    """
    # Create main data folder
    main_folder = "comments_data"
    if not os.path.exists(main_folder):
        os.makedirs(main_folder)
        print(f"Created main folder: {main_folder}")
    
    # Create subfolders for comments and logs
    comments_folder = os.path.join(main_folder, "comments")
    logs_folder = os.path.join(main_folder, "logs")
    
    if not os.path.exists(comments_folder):
        os.makedirs(comments_folder)
        print(f"Created comments folder: {comments_folder}")
    
    if not os.path.exists(logs_folder):
        os.makedirs(logs_folder)
        print(f"Created logs folder: {logs_folder}")
    
    # Return folder paths for later use
    return {
        "main": main_folder,
        "comments": comments_folder,
        "logs": logs_folder
    }

def create_session_folders():
    """Creates timestamp-based session folders.
    
    Returns:
        dict: Dictionary containing paths to the session folders
    """
    # Create base folder structure first
    folders = create_folder_structure()
    
    # Add timestamp to create a unique session folder
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_folder = os.path.join(folders["main"], f"session_{timestamp}")
    os.makedirs(session_folder)
    
    # Create session-specific subfolders
    comments_folder = os.path.join(session_folder, "comments")
    logs_folder = os.path.join(session_folder, "logs")
    os.makedirs(comments_folder)
    os.makedirs(logs_folder)
    
    print(f"[INFO] Created session folder: {session_folder}")
    
    return {
        "session": session_folder,
        "comments": comments_folder,
        "logs": logs_folder
    }

def store_comments_to_json(comments, filename="scraped_comments.json", total_comment_count=0, folder_path=None, video_metadata=None):
    """Saves the scraped comments to a JSON file.
    
    Args:
        comments: List of comment dictionaries to save
        filename: Name of the output JSON file
        total_comment_count: Total number of comments for the video
        folder_path: Path to save the file (optional)
        video_metadata: Dictionary containing video metadata (creator_name, creator_id, content_id)
    """
    # Use provided folder path or current directory
    if folder_path:
        filename = os.path.join(folder_path, filename)
        
    try:
        # Check if comments is a valid list
        if not isinstance(comments, list):
            print(f"Warning: Expected comments to be a list, but got {type(comments).__name__}")
            if isinstance(comments, dict):
                comments = [comments]  # Convert single comment dict to list
            else:
                comments = []  # Empty list as fallback
        
        # Check if we have any comments to save
        if not comments:
            print(f"Warning: No comments found to save to {filename}")
            # Save an empty array with a note
            output_data = {
                "warning": "No comments were found",
                "comments": []
            }
            
            # Add metadata if available
            if video_metadata:
                output_data["creator_name"] = video_metadata.get("creator_name")
                output_data["creator_id"] = video_metadata.get("creator_id")
                output_data["content_id"] = video_metadata.get("content_id")
            
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)
            print(f"Created empty comments file {filename} with warning.")
            return
        
        # Print some sample comments for debugging
        print(f"Preparing to save {len(comments)} comments. Sample:")
        for i, comment in enumerate(comments[:3]):  # Show up to 3 sample comments
            print(f"  Comment {i+1}: {comment.get('username', 'No username')} - {comment.get('commentText', 'No text')[:50]}...")
        
        # Make sure total_comment_count is valid and at least as large as our actual comment count
        if total_comment_count < len(comments):
            print(f"Warning: Reported total comment count ({total_comment_count}) is less than the number of comments scraped ({len(comments)})")
            print(f"Adjusting total_comment_count to match the number of scraped comments")
            total_comment_count = len(comments)
        
        # Calculate percentage if total count is provided
        percentage = 0
        if total_comment_count > 0:
            percentage = round((len(comments) / total_comment_count) * 100, 2)
            print(f"Scraped {percentage}% of total comments ({len(comments)} of {total_comment_count})")
        
        # Actually save the comments
        output_data = {
            "count": len(comments),
            "total_comment_count": total_comment_count if total_comment_count > 0 else "unknown",
            "percentage_scraped": percentage if total_comment_count > 0 else "unknown",
            "comments": comments
        }
        
        # Add metadata if available
        if video_metadata:
            output_data["creator_name"] = video_metadata.get("creator_name")
            output_data["creator_id"] = video_metadata.get("creator_id")
            output_data["content_id"] = video_metadata.get("content_id")
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"Successfully saved {len(comments)} comments to {filename}.")
    except Exception as e:
        print(f"Failed to save comments to {filename}. Error:", e)
        # Try to save with a simplified approach
        try:
            print("Attempting to save with simplified approach...")
            backup_filename = f"backup_{os.path.basename(filename)}"
            if folder_path:
                backup_filename = os.path.join(folder_path, backup_filename)
            with open(backup_filename, "w", encoding="utf-8") as f:
                f.write(str(comments))
            print(f"Backup saved to {backup_filename}")
        except Exception as backup_error:
            print(f"Backup save also failed: {backup_error}")

def save_element_info(element_info, filename="recorded_element.json", folder_path=None):
    """Saves the captured element information to a JSON file.
    
    Args:
        element_info: Dictionary containing element information
        filename: Name of the output JSON file
        folder_path: Path to save the file (optional)
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Use provided folder path if available
        if folder_path:
            filename = os.path.join(folder_path, filename)
            
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(element_info, f, ensure_ascii=False, indent=2)
        print(f"Element information saved to {filename}.")
        return True
    except Exception as e:
        print(f"Failed to save element information to {filename}. Error:", e)
        return False

def load_element_info(filename="recorded_element.json", folder_path=None):
    """Loads previously captured element information from a JSON file.
    
    Args:
        filename: Name of the JSON file to load
        folder_path: Path to look for the file (optional)
        
    Returns:
        dict: Loaded element information, or None if loading failed
    """
    try:
        # Check in provided folder path first
        if folder_path and os.path.exists(os.path.join(folder_path, filename)):
            filename = os.path.join(folder_path, filename)
        
        with open(filename, "r", encoding="utf-8") as f:
            element_info = json.load(f)
        print(f"Element information loaded from {filename}.")
        return element_info
    except FileNotFoundError:
        print(f"Element information file {filename} not found.")
        return None
    except Exception as e:
        print(f"Failed to load element information from {filename}. Error:", e)
        return None

def load_recorded_elements(folder_path=None):
    """Loads the recorded elements from a JSON file, specifically for comment count extraction.
    
    This function looks for files containing recorded comment button and count elements.
    It tries multiple possible filenames and formats.
    
    Args:
        folder_path: Path to look for the file (optional)
        
    Returns:
        dict: Dictionary containing recorded elements information, or None if loading failed
    """
    try:
        # Try several potential filenames in order of priority
        filenames = [
            "recorded_elements.json",  # Preferred format with multiple elements
            "recorded_element.json",   # Legacy format with single element
            "comment_elements.json"    # Alternative name
        ]
        
        loaded_file = None
        
        # Try each filename in the provided folder or current directory
        for filename in filenames:
            path = os.path.join(folder_path, filename) if folder_path else filename
            if os.path.exists(path):
                loaded_file = path
                break
                
        if not loaded_file:
            print(f"[INFO] No recorded elements file found for comment counting")
            return None
            
        # Load the file we found
        with open(loaded_file, "r", encoding="utf-8") as f:
            elements = json.load(f)
        
        # Check if we have a single element format (legacy) or multiple elements
        if "tag_name" in elements and "css_selector" in elements:
            # Single element format - convert to multi-element format
            converted = {"comment_button": elements}
            print(f"[INFO] Loaded legacy single element from {loaded_file}")
            return converted
        else:
            # Already in multi-element format
            print(f"[INFO] Loaded recorded elements from {loaded_file}")
            return elements
            
    except Exception as e:
        print(f"[WARN] Failed to load recorded elements: {e}")
        return None 