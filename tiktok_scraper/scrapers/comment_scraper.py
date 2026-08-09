"""TikTok comment scraper module."""
import asyncio
from ..utils.file_utils import store_comments_to_json

# The JavaScript code for scraping comments is imported from a separate file
from .scraper_scripts import SCRAPE_COMMENTS_JS

async def open_comment_section(page, comment_button_selector):
    """Attempts to open the comment section on a TikTok video.
    
    Args:
        page: The Playwright page object
        comment_button_selector: CSS selector for the comment button
        
    Returns:
        bool: True if comment section was opened, False otherwise
    """
    print("[INFO] Checking if comment section is open...")
    # Selectors for the comment container
    comment_container_selector = ".css-7whb78-DivCommentListContainer, .css-1qp5gj2-DivCommentListContainer, .css-13wx63w-DivCommentObjectWrapper, div[class*=\"-DivCommentListContainer\"]"

    try:
        is_container_visible = False
        try:
            # Check if comment container is visible with a short timeout
            await page.locator(comment_container_selector).first.wait_for(state="visible", timeout=3000)
            is_container_visible = True
            print("[INFO] Comment section appears to be already open.")
        except Exception:
            print("[INFO] Comment section not initially visible.")
            is_container_visible = False

        if not is_container_visible:
            print(f"[INFO] Attempting to locate and click comment button with selector: {comment_button_selector}")
            
            # Try clicking the parent of the SVG element first (more likely to be clickable)
            if 'svg > use' in comment_button_selector:
                parent_selector = 'span[data-e2e="comment-icon"]'
                print(f"[INFO] First trying to click the parent element: {parent_selector}")
                try:
                    # Get count of matching elements
                    parent_button = page.locator(parent_selector)
                    count = await parent_button.count()
                    print(f"[INFO] Found {count} matching comment buttons")
                    
                    if count > 0:
                        # Target just the first element to avoid strict mode violations
                        first_button = parent_button.first
                        
                        # Print some info about the button we're clicking
                        try:
                            aria_label = await first_button.get_attribute("aria-label") or ""
                            print(f"[INFO] Clicking comment button with aria-label: {aria_label}")
                        except:
                            pass
                        
                        # Force click with strict=False
                        await first_button.click(timeout=5000, force=True)
                        print("[INFO] Clicked first comment button. Waiting for comment section...")
                        await page.wait_for_timeout(2000)  # Short wait to see if comments appear
                        
                        # Check if comment section appeared
                        if await page.locator(comment_container_selector).first.count() > 0:
                            print("[INFO] Comment section is now open after clicking parent.")
                            is_container_visible = True
                except Exception as parent_err:
                    print(f"[WARN] Could not click parent element: {parent_err}")
            
            # If comment section still not visible, try the original approach
            if not is_container_visible:
                try:
                    # Get the comment button but target only the first one
                    comment_button = page.locator(comment_button_selector).first
                    print("[INFO] Trying direct selector approach with .first modifier")
                     
                    # Wait for the button to potentially appear
                    await comment_button.wait_for(state="visible", timeout=5000)

                    if await comment_button.is_enabled(timeout=1000):
                        print("[INFO] Comment button found and enabled. Clicking...")
                        try:
                            await comment_button.click(timeout=5000, force=True)
                            print("[INFO] Clicked comment button (forced). Waiting for comment section to appear...")
                            # Wait longer for the container to appear after click
                            await page.locator(comment_container_selector).first.wait_for(state="visible", timeout=15000)
                            print("[INFO] Comment section is now open.")
                            is_container_visible = True
                        except Exception as click_wait_err:
                            print(f"[ERROR] Failed to click comment button or section did not appear after click: {click_wait_err}")
                    else:
                        # Check if it's disabled because the container *did* load while we were checking
                        try:
                            await page.locator(comment_container_selector).first.wait_for(state="visible", timeout=1000)
                            print("[INFO] Comment button was disabled, but comment section loaded anyway.")
                            is_container_visible = True
                        except Exception:
                            print("[WARN] Comment button found but is disabled, and comment section didn't load. Comments might be disabled for this video.")
                except Exception as find_err:
                    print(f"[ERROR] Could not find or interact with the comment button: {find_err}. Maybe comments are disabled or the selector needs update.")

        return is_container_visible

    except Exception as e:
        # Catch potential errors during the check/click process
        print(f"[WARN] Unexpected error during comment section check/open: {e}. Proceeding anyway...")
        return False

async def scrape_comments(page, video_number, comments_folder, target_percentage=50, video_data=None):
    """Scrapes comments from the current TikTok video.
    
    Args:
        page: The Playwright page object
        video_number: Current video number for logging
        comments_folder: Folder to save scraped comments
        target_percentage: Percentage of comments to scrape (default: 50, use 100 for all)
        video_data: Dictionary containing video metadata (creator_name, creator_id, content_id)
        
    Returns:
        tuple: (comments list, total comment count, logs)
    """
    try:
        print("\n[INFO] Executing comment scraping script...")
        
        # Add a console log handler to capture browser console logs
        logs = []
        page.on("console", lambda msg: logs.append(f"[BROWSER] {msg.type}: {msg.text}"))
        
        # Create configuration object to pass to the script
        config = {
            "targetPercentage": target_percentage,
            "completeExtraction": True if target_percentage >= 100 else False,
            "maxScrollAttempts": 200 if target_percentage >= 100 else 50,  # More scrolling for complete extraction
            "scrollDelay": 1000  # 1 second between scrolls to avoid rate limiting
        }
        
        print(f"[INFO] Configured for {'complete' if target_percentage >= 100 else 'partial'} comment extraction")
        if target_percentage >= 100:
            print("[INFO] This will attempt to extract ALL comments and may take a long time")
        
        # Execute the scraping script with a longer timeout and pass the config
        # Do NOT use timeout parameter in page.evaluate() as it's not supported correctly
        try:
            # First, set up a global variable to store comments in case of timeout
            await page.evaluate("""() => {
                window.__ScrapedComments = [];
            }""")
            
            # Execute the script with a standard page.evaluate (no timeout parameter)
            comments = await page.evaluate(SCRAPE_COMMENTS_JS, config)
        except Exception as script_err:
            print(f"[WARN] Script execution error: {script_err}")
            # Try to retrieve any comments that were collected before error
            try:
                print("[INFO] Attempting to retrieve partial results...")
                comments = await page.evaluate("""() => {
                    if (window.__ScrapedComments && Array.isArray(window.__ScrapedComments)) {
                        console.log(`Retrieved ${window.__ScrapedComments.length} comments after error`);
                        return window.__ScrapedComments;
                    }
                    return [];
                }""")
            except Exception as recovery_err:
                print(f"[ERROR] Failed to recover partial results: {recovery_err}")
                comments = []
        
        # Print the captured logs to help diagnose issues
        if logs:
            print("\n--- BROWSER CONSOLE LOGS ---")
            # Only print the most relevant logs (limit to 50 to avoid overwhelming output)
            for log in logs[-50:]:
                print(log)
            print("--- END BROWSER CONSOLE LOGS ---\n")
        
        # Use the total comment count from video_data if available
        total_comment_count = 0
        if video_data and "comment_count" in video_data and video_data["comment_count"] > 0:
            total_comment_count = video_data["comment_count"]
            print(f"[INFO] Using total comment count from video_data: {total_comment_count}")
        else:
            # Extract total comment count from logs as fallback
            for log in logs:
                if "Total comment count detected:" in log:
                    try:
                        extracted_count = int(log.split("Total comment count detected:")[1].strip())
                        # Only use the extracted count if it's greater than the current count and seems reasonable
                        if extracted_count > total_comment_count and extracted_count <= 100000:
                            total_comment_count = extracted_count
                            print(f"[INFO] Total comment count detected from logs: {total_comment_count}")
                    except:
                        pass
            
            # If we still don't have a valid count but have comments, use the comment count as minimum
            if total_comment_count < len(comments) and len(comments) > 0:
                total_comment_count = len(comments)
                print(f"[INFO] Using comment count as minimum total: {total_comment_count}")
        
        # Verify we got a valid result
        if not comments:
            print("[WARN] No comments returned from scraping script")
        elif not isinstance(comments, list):
            print(f"[WARN] Expected a list of comments but got: {type(comments).__name__}")
        else:
            print(f"[INFO] Scraped {len(comments)} comments from video {video_number}")
            
            # Check if we need to retry for complete extraction
            if target_percentage >= 100 and total_comment_count > 0:
                completion_percent = (len(comments) / total_comment_count) * 100
                if completion_percent < 90 and len(comments) < 5000:  # Don't retry for very large comment sets
                    print(f"[INFO] Only scraped {completion_percent:.2f}% of comments. Attempting one more pass...")
                    
                    # Reload the comment section to get a fresh start
                    try:
                        # Close and reopen comments
                        await page.evaluate("""() => {
                            // Try to click the close button if it exists
                            const closeBtn = document.querySelector('button[aria-label="Close"]');
                            if (closeBtn) closeBtn.click();
                        }""")
                        await page.wait_for_timeout(2000)
                        
                        # Try to reopen comments
                        from .comment_scraper import open_comment_section
                        await open_comment_section(page, 'span[data-e2e="comment-icon"]')
                        await page.wait_for_timeout(3000)
                        
                        # Second attempt with more aggressive settings
                        config["maxScrollAttempts"] = 300
                        config["scrollDelay"] = 800
                        
                        print("[INFO] Running second pass with more aggressive settings...")
                        try:
                            # Use standard evaluation without timeout parameter
                            retry_comments = await page.evaluate(SCRAPE_COMMENTS_JS, config)
                            
                            if isinstance(retry_comments, list) and len(retry_comments) > len(comments):
                                print(f"[INFO] Second pass improved results: {len(comments)} → {len(retry_comments)} comments")
                                comments = retry_comments
                            else:
                                print("[INFO] Second pass did not improve results, keeping original comments")
                        except Exception as retry_eval_err:
                            print(f"[WARN] Error during retry evaluation: {retry_eval_err}")
                    except Exception as retry_err:
                        print(f"[WARN] Error during retry: {retry_err}")
        
        # Save the scraped comments to a JSON file
        json_filename = f"scraped_comments_video_{video_number}.json"
        store_comments_to_json(comments, json_filename, total_comment_count, comments_folder, video_data)
        
        comments_list = comments if isinstance(comments, list) else []
        return comments_list, total_comment_count, logs
        
    except Exception as e:
        print(f"[ERROR] Failed to execute comment scraping script: {e}")
        return [], 0, []

async def save_enhanced_diagnostic_info(page, video_number, comments, total_comment_count, logs, logs_folder, video_data=None):
    """Saves enhanced diagnostic information about the comment scraping process, including video metadata.
    
    Args:
        page: The Playwright page object
        video_number: Current video number
        comments: Scraped comments
        total_comment_count: Total comment count
        logs: Browser console logs
        logs_folder: Folder to save diagnostic info
        video_data: Dictionary containing video metadata (creator_name, creator_id, content_id)
    """
    import json
    import time
    import os
    
    try:
        # Safely get the current URL
        try:
            # First try the standard method
            if callable(page.url):
                current_url = await page.url()
            else:
                # Fallback to JavaScript if page.url is not callable
                current_url = await page.evaluate("() => window.location.href")
        except Exception:
            # If all else fails, use a placeholder
            current_url = "URL not available"
        
        diagnostic_info = {
            "url": current_url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "video_number": video_number,
            "comment_count": len(comments) if isinstance(comments, list) else 0,
            "total_comment_count": total_comment_count,
            "percentage_scraped": round((len(comments) / total_comment_count) * 100, 2) if total_comment_count > 0 else 0,
            "console_logs": logs[-20:] if logs else [],  # Last 20 logs
        }
        
        # Add video metadata if available
        if video_data:
            diagnostic_info["creator_name"] = video_data.get("creator_name")
            diagnostic_info["creator_id"] = video_data.get("creator_id")
            diagnostic_info["content_id"] = video_data.get("content_id")
        
        # Save video information as separate JSON file for easier access
        if video_data and (video_data.get("creator_name") or video_data.get("content_id")):
            video_info = {
                "video_number": video_number,
                "creator_name": video_data.get("creator_name"),
                "creator_id": video_data.get("creator_id"),
                "content_id": video_data.get("content_id"),
                "total_comments": total_comment_count,
                "comments_scraped": len(comments) if isinstance(comments, list) else 0,
                "url": current_url,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            video_info_filename = os.path.join(logs_folder, f"video_info_{video_number}.json")
            with open(video_info_filename, "w", encoding="utf-8") as f:
                json.dump(video_info, f, ensure_ascii=False, indent=2)
            print(f"[INFO] Saved video information to {video_info_filename}")
        
        diagnostic_filename = os.path.join(logs_folder, f"diagnostic_{video_number}.json")
        with open(diagnostic_filename, "w", encoding="utf-8") as f:
            json.dump(diagnostic_info, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Saved enhanced diagnostic information to {diagnostic_filename}")
    except Exception as diag_err:
        print(f"[WARN] Failed to save enhanced diagnostic info: {diag_err}")

async def save_diagnostic_info(page, video_number, comments, total_comment_count, logs, logs_folder):
    """Saves diagnostic information about the comment scraping process.
    
    Args:
        page: The Playwright page object
        video_number: Current video number
        comments: Scraped comments
        total_comment_count: Total comment count
        logs: Browser console logs
        logs_folder: Folder to save diagnostic info
    """
    import json
    import time
    import os
    
    try:
        # Safely get the current URL
        try:
            # First try the standard method
            if callable(page.url):
                current_url = await page.url()
            else:
                # Fallback to JavaScript if page.url is not callable
                current_url = await page.evaluate("() => window.location.href")
        except Exception:
            # If all else fails, use a placeholder
            current_url = "URL not available"
            
        diagnostic_info = {
            "url": current_url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "video_number": video_number,
            "comment_count": len(comments) if isinstance(comments, list) else 0,
            "total_comment_count": total_comment_count,
            "percentage_scraped": round((len(comments) / total_comment_count) * 100, 2) if total_comment_count > 0 else 0,
            "console_logs": logs[-20:] if logs else [],  # Last 20 logs
        }
        
        diagnostic_filename = os.path.join(logs_folder, f"diagnostic_{video_number}.json")
        with open(diagnostic_filename, "w", encoding="utf-8") as f:
            json.dump(diagnostic_info, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Saved diagnostic information to {diagnostic_filename}")
    except Exception as diag_err:
        print(f"[WARN] Failed to save diagnostic info: {diag_err}")

async def save_error_logs(logs, video_number, logs_folder):
    """Saves error logs if comment scraping failed.
    
    Args:
        logs: Browser console logs
        video_number: Current video number
        logs_folder: Folder to save logs
    """
    import os
    
    if logs:
        try:
            error_log_filename = os.path.join(logs_folder, f"error_logs_video_{video_number}.txt")
            with open(error_log_filename, "w", encoding="utf-8") as f:
                f.write("\n".join(logs))
            print(f"[INFO] Saved error logs to {error_log_filename}")
        except Exception as log_err:
            print(f"[ERROR] Failed to save error logs: {log_err}") 