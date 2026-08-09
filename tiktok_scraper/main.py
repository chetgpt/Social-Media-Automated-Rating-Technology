"""Main entry point for the TikTok scraper."""
import asyncio
import subprocess
import os
import sys
import time
import json
from playwright.async_api import async_playwright

from .utils.browser_utils import close_edge_tasks, wait_for_port, get_edge_executable_path
from .utils.file_utils import create_session_folders, load_element_info, save_element_info, load_recorded_elements
from .utils.element_recorder import record_button_click, record_elements
from .utils.navigation import click_next_video, navigate_to_url
from .utils.comment_count import extract_total_comment_count
from .utils.debug_utils import analyze_tiktok_page, save_page_structure
from .scrapers.comment_scraper import open_comment_section, scrape_comments, save_diagnostic_info, save_error_logs

# Add this section after finding video elements and before processing them
async def wait_and_scroll_for_videos(page, max_attempts=5):
    """Waits and scrolls to try to load videos on a profile page.
    
    Args:
        page: The Playwright page object
        max_attempts: Maximum number of scroll attempts
        
    Returns:
        bool: True if scrolling was performed
    """
    print(f"[INFO] Scrolling page to load videos (max {max_attempts} attempts)...")
    
    for i in range(max_attempts):
        # Scroll down
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        print(f"[INFO] Scroll attempt {i+1}/{max_attempts}")
        
        # Wait for content to load
        await page.wait_for_timeout(3000)
        
        # Check if we've found any videos after scrolling
        has_videos = await page.evaluate("""() => {
            const videoLinks = document.querySelectorAll('a[href*="/video/"]');
            return videoLinks.length > 0;
        }""")
        
        if has_videos:
            print("[INFO] Videos found after scrolling")
            return True
    
    # One final check with longer wait time
    await page.wait_for_timeout(5000)
    return True

async def find_profile_videos(page, logs_folder):
    """Finds all video elements on a profile page.
    
    Args:
        page: The Playwright page object
        logs_folder: Folder to save debug information
        
    Returns:
        list: Locator objects of video elements found on the profile
    """
    print("[INFO] Scanning for videos on profile page...")
    
    try:
        # Wait for videos to load
        await page.wait_for_load_state("networkidle", timeout=15000)
        
        # Take a screenshot and save page structure for analysis
        await save_page_structure(page, logs_folder)
        
        # Try scrolling to reveal videos
        await wait_and_scroll_for_videos(page)
        
        # Identify video elements for direct clicking rather than URL navigation
        print("[INFO] Looking for clickable video elements...")
        
        # Try the exact selector from the example
        video_elements = await page.locator('div.css-1uqux2o-DivItemContainerV2, div.css-8dx572-DivContainer-StyledDivContainerV2, div[data-e2e="user-post-item"]').all()
        
        if video_elements:
            print(f"[INFO] Found {len(video_elements)} video elements using primary selectors")
            return video_elements
        
        # If not found, try alternative selectors
        print("[INFO] Primary selectors didn't find videos. Trying alternatives...")
        alt_video_elements = await page.locator('div[class*="DivItemContainer"], div[class*="StyledDivContainer"]').all()
        
        if alt_video_elements:
            print(f"[INFO] Found {len(alt_video_elements)} video elements using alternative selectors")
            
            # Filter out elements that are likely not videos (too small, contain "playlist", etc.)
            filtered_elements = []
            for el in alt_video_elements:
                # Check element dimensions to avoid small elements
                box = await el.bounding_box()
                if not box or box["width"] < 100 or box["height"] < 100:
                    continue
                
                # Check content to filter out playlists and lives
                el_text = await el.text_content()
                if el_text and "playlist" in el_text.lower():
                    continue
                
                # Check if it contains a video link
                has_video_link = await el.locator('a[href*="/video/"]').count() > 0
                if has_video_link:
                    filtered_elements.append(el)
            
            if filtered_elements:
                print(f"[INFO] Filtered to {len(filtered_elements)} valid video elements")
                return filtered_elements
        
        # Final fallback
        print("[INFO] Trying fallback direct parent element selectors...")
        link_elements = await page.locator('a[href*="/video/"]').all()
        
        if link_elements:
            # Get the parent elements of these links as they're more likely to be clickable
            parent_elements = []
            for link in link_elements:
                parent = await link.evaluate('el => el.parentElement')
                if parent:
                    # Convert parent element to a locator
                    parent_locator = page.locator(f'xpath={await link.evaluate("el => getXPath(el.parentElement)")}'.replace("'" ,"\\'"))
                    parent_elements.append(parent_locator)
            
            if parent_elements:
                print(f"[INFO] Found {len(parent_elements)} parent elements of video links")
                return parent_elements
        
        print("[WARN] No video elements found. Check the page structure.")
        return []
    
    except Exception as e:
        print(f"[ERROR] Error finding profile videos: {e}")
        return []

async def process_videos_by_clicking(page, video_elements, comment_button_selector, folders, recorded_elements, max_videos):
    """Process videos by clicking on them directly instead of navigating to URLs.
    
    Args:
        page: The Playwright page object
        video_elements: List of video element locators
        comment_button_selector: CSS selector for the comment button
        folders: Dictionary containing folder paths
        recorded_elements: Recorded element information
        max_videos: Maximum number of videos to scrape, overrides the detected video count
        
    Returns:
        None
    """
    video_count = 0
    processed_video_urls = set()  # Track processed video URLs to avoid duplicates
    
    # Selector for the video navigation buttons
    button_selector = "button.TUXButton.TUXButton--capsule.TUXButton--medium.TUXButton--secondary.action-item.css-1egy55o:not([disabled])"
    
    # We'll only need to click the first video element, then use the next button navigation
    # This helps avoid problems with overlays blocking video element clicks
    initial_videos_to_click = min(1, len(video_elements))
    
    # First phase: Click on the first video to enter video view mode
    for i in range(initial_videos_to_click):
        video_element = video_elements[i]
        video_count += 1
        print(f"\n[INFO] Processing video {video_count} of {len(video_elements)}")
        
        try:
            # Take screenshot before clicking (for debugging if needed)
            screenshot_path = os.path.join(folders["logs"], "screenshots", f"before_click_video_{video_count}.png")
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await page.screenshot(path=screenshot_path)
            
            # Remember the profile URL before clicking
            profile_url = page.url
            
            # Click the video element
            print(f"[INFO] Clicking on video element {i+1}")
            await video_element.click(timeout=5000)
            
            # Wait for the video to load
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)  # Additional wait for dynamic content
            
            # Check if we've successfully navigated to a video
            is_video_page = await page.evaluate("""() => {
                return window.location.href.includes('/video/');
            }""")
            
            if not is_video_page:
                print("[WARN] Not on a video page after clicking. Taking diagnostic screenshot...")
                await save_page_structure(page, folders["logs"])
                print("[INFO] Initial navigation to video failed. Returning to profile and trying again...")
                
                # Return to the profile page if needed
                if page.url != profile_url:
                    await navigate_to_url(page, profile_url)
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                continue
            
            # Successfully entered video viewing mode, now process this video
            current_video_url = page.url
            processed_video_urls.add(current_video_url)
            
            # Continue to second phase (video processing)
            break
            
        except Exception as e:
            print(f"[ERROR] Error clicking initial video: {e}")
            # Try to return to the profile page
            await navigate_to_url(page, profile_url)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            return  # Exit if we can't even click the first video
    
    # Second phase: Process videos sequentially using the "Next" button
    max_videos_to_process = max_videos  # Use the parameter value
    video_limit = max_videos  # Always use max_videos instead of limiting to detected elements
    
    # If we're still on the profile page, we failed to enter video mode
    if not await page.evaluate("""() => {
        return window.location.href.includes('/video/');
    }"""):
        print("[ERROR] Failed to enter video viewing mode. Exiting.")
        return
    
    print(f"[INFO] Successfully entered video viewing mode. Processing up to {video_limit} videos.")
    
    # Now process videos using the next button for navigation
    videos_processed = 1  # Already processed the first video that we clicked
    while videos_processed < video_limit and len(processed_video_urls) < max_videos_to_process:
        try:
            # Process current video (we're already on a video page)
            current_video_url = page.url
            
            # Skip if already processed
            if current_video_url in processed_video_urls:
                print(f"[INFO] Already processed video {current_video_url}. Moving to next...")
                
                # Try to navigate to next video
                next_success = await click_next_video(page, button_selector)
                if not next_success:
                    print("[INFO] No more videos available or navigation failed. Finishing.")
                    break
                
                continue
            
            # Track this video
            processed_video_urls.add(current_video_url)
            
            print(f"\n[INFO] Processing video {videos_processed+1} (URL: {current_video_url})")
            
            # Try to open the comment section
            comment_section_open = await open_comment_section(page, comment_button_selector)
            
            if not comment_section_open:
                print("[WARN] Failed to open comment section. Trying alternative approaches...")
                
                # Try clicking on any comment button we can find
                alt_selectors = [
                    'span[data-e2e="comment-icon"]', 
                    'button[aria-label*="comment"]',
                    'button[data-e2e*="comment"]'
                ]
                
                for alt_selector in alt_selectors:
                    print(f"[INFO] Trying alternative selector: {alt_selector}")
                    try:
                        # Use first to avoid strict mode violation if selector matches multiple elements
                        button = page.locator(alt_selector).first
                        await button.click(timeout=3000, force=True)
                        await page.wait_for_timeout(2000)
                        
                        # Check if comments are now visible with a generic selector
                        comment_container = await page.locator(".css-7whb78-DivCommentListContainer, .css-1qp5gj2-DivCommentListContainer, div[class*=\"-DivCommentListContainer\"]").count()
                        if comment_container > 0:
                            print(f"[INFO] Comment section opened with alternative selector: {alt_selector}")
                            comment_section_open = True
                            break
                    except Exception as alt_err:
                        print(f"[WARN] Failed to click alternative selector {alt_selector}: {alt_err}")
                
                if not comment_section_open:
                    print("[ERROR] Could not open comment section after trying all methods. Skipping this video.")
                    
                    # Move to next video
                    next_success = await click_next_video(page, button_selector)
                    if not next_success:
                        print("[INFO] No more videos available or navigation failed. Finishing.")
                        break
                    
                    videos_processed += 1
                    continue
            
            # Get video data for enhanced diagnostics
            video_data = None
            try:
                video_data = {
                    "url": page.url,
                    "comment_count": await extract_total_comment_count(page, recorded_elements.get("comment_count", {}).get("css_selector") if recorded_elements else None)
                }
                print(f"[INFO] Video data: {video_data}")
            except Exception as vd_err:
                print(f"[WARN] Failed to extract video data: {vd_err}")
            
            # Scrape comments - use 100% for target_url mode
            comments, total_comment_count, logs = await scrape_comments(page, videos_processed+1, folders["comments"], target_percentage=100, video_data=video_data)
            
            # Save diagnostic information
            await save_diagnostic_info(page, videos_processed+1, comments, total_comment_count, logs, folders["logs"])
            
            if not comments:
                await save_error_logs(logs, videos_processed+1, folders["logs"])
            else:
                percentage = 0
                if total_comment_count > 0:
                    percentage = round((len(comments) / total_comment_count) * 100, 2)
                
                print(f"[SUCCESS] Scraped {len(comments)} comments from video {videos_processed+1}")
                if total_comment_count > 0:
                    print(f"[INFO] That's approximately {percentage}% of the total {total_comment_count} comments")
            
            # Wait a bit before trying to navigate to next video
            await asyncio.sleep(3)
            
            # Try to navigate to next video directly within TikTok
            print(f"[INFO] Navigating to next video...")
            next_success = await click_next_video(page, button_selector)
            
            if not next_success:
                print("[INFO] No more videos available or navigation failed. Finishing.")
                break
                
            # Increment counter for processed videos
            videos_processed += 1
            
        except Exception as e:
            print(f"[ERROR] Error processing video: {e}")
            
            # Try to navigate to next video and continue
            try:
                next_success = await click_next_video(page, button_selector)
                if not next_success:
                    print("[INFO] Navigation failed after error. Finishing.")
                    break
                videos_processed += 1
            except Exception as nav_err:
                print(f"[ERROR] Failed to navigate after error: {nav_err}")
                break
        
        # Wait between videos
        await asyncio.sleep(3)
    
    print(f"[INFO] Finished processing {videos_processed} videos out of {video_limit} target videos.")
    print(f"[INFO] Processed {len(processed_video_urls)} unique video URLs.")

async def main(record_mode=False, num_videos=3, default_percentage=50, target_url=None, max_videos=500):
    """Main function to run the TikTok scraper.
    
    Args:
        record_mode: Whether to run in recording mode to capture the comment button
        num_videos: Number of videos to scrape (default reduced to 3 to prioritize quality)
        default_percentage: Default percentage of comments to scrape (if not specified by user)
        target_url: Specific TikTok URL to scrape (e.g., a profile page)
        max_videos: Maximum number of videos to scrape, overrides the detected video count
    """
    # Create folder structure for comments and logs
    folders = create_session_folders()
    
    # Close any running Edge instances
    close_edge_tasks()
    time.sleep(2)

    # Define your Edge profile path (adjust this to your system)
    profile_path = r"C:\Users\DELL\AppData\Local\Microsoft\Edge\User Data\Profile 2"
    user_data_dir = os.path.dirname(profile_path)
    profile_directory = os.path.basename(profile_path)

    # Locate the Edge executable
    msedge_path = get_edge_executable_path()
    if not msedge_path:
        print("[ERROR] Edge executable not found on system.")
        return

    # Use the provided URL or default to TikTok homepage
    initial_url = target_url if target_url else "https://www.tiktok.com"

    # Command to launch Edge with remote debugging enabled
    cmd = [
        msedge_path,
        f"--profile-directory={profile_directory}",
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        initial_url
    ]
    print("[INFO] Launching Edge with your existing profile...")
    proc = subprocess.Popen(cmd)
    print("Edge launched with PID:", proc.pid)

    # Wait for Edge to fully launch and the debugging port to open
    time.sleep(20)
    if not wait_for_port("127.0.0.1", 9222, timeout=90):
        print("[ERROR] Remote debugging port did not open in time.")
        proc.terminate()
        return
    else:
        print("[INFO] Remote debugging port is open.")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        except Exception as e:
            print("[ERROR] Error connecting over CDP:", e)
            return

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        pages = context.pages
        page = pages[0] if pages else await context.new_page()

        await page.wait_for_load_state("networkidle", timeout=15000)
        print("[INFO] Connected to Edge via CDP. Current URL =", page.url)

        # Handle record mode if selected
        if record_mode:
            print("\n" + "="*80)
            print("RECORDER ACTIVE: Please manually navigate to a TikTok video".center(80))
            print("="*80)
            print("\nSTEP 1: Navigate to a TikTok video with comments")
            print("STEP 2: When you're ready, you'll be asked to click on 2 elements:")
            print("  * The comment button (to open comments)")
            print("  * The comment count display (showing number of comments)")
            print("\nRecording these elements helps the scraper accurately identify")
            print("comment sections and count totals for better scraping results.")
            
            # Wait for user to get to a video
            print("\nPress Enter when you're on a TikTok video with comments...")
            input()
            
            # Record both elements
            success = await record_elements(page, folders)
            
            if success:
                print("\n" + "="*80)
                print("SUCCESS: Elements recorded successfully!".center(80))
                print("="*80)
                print("\nRun the script without --record next time to use this information.")
                print("\nExiting recording mode...")
            else:
                print("\n" + "="*80)
                print("ERROR: Failed to record elements properly".center(80))
                print("="*80)
                print("\nPossible reasons:")
                print("- No elements were clicked within the time limit")
                print("- The clicked elements weren't properly detected")
                print("- There was an error saving the element information")
                print("\nPlease try again with the --record flag.")
            
            # Make it clear we're exiting
            print("\nRecording session complete. Closing browser...")
            time.sleep(3)  # Give the user time to read the message
            
            # Close browser and exit
            await browser.close()
            proc.terminate()
            return  # This explicit return ensures the script doesn't continue

        # Load recorded element information for comment counting and button clicking
        recorded_elements = load_recorded_elements(folder_path=folders["logs"])
        if recorded_elements:
            print("[INFO] Successfully loaded recorded elements for comment counting")
            
            # If we have recorded elements, use the comment_button from there
            if "comment_button" in recorded_elements:
                comment_button_selector = recorded_elements["comment_button"]["css_selector"]
                print(f"[INFO] Using comment button selector from recorded elements: {comment_button_selector}")
            else:
                # Fall back to loading the old-style single element
                element_info = load_element_info(folder_path=folders["logs"])
                if element_info:
                    comment_button_selector = element_info.get("css_selector")
                    print(f"[INFO] Using legacy recorded comment button selector: {comment_button_selector}")
                else:
                    # Use default selector
                    comment_button_selector = 'span[data-e2e="comment-icon"] > svg > use'
                    print(f"[INFO] No recorded button found. Using default selector: {comment_button_selector}")
        else:
            # Load recorded element information if available (legacy approach)
            element_info = load_element_info(folder_path=folders["logs"])
            if element_info:
                # Use the recorded CSS selector as primary, with XPath as fallback
                comment_button_selector = element_info.get("css_selector")
                print(f"[INFO] Using legacy recorded comment button selector: {comment_button_selector}")
            else:
                # Fallback to the previous approach, but first try our known good selector
                print("[INFO] No recorded element information found. Using pre-recorded selector.")
                # Use the selector we captured with button_recorder.py
                comment_button_selector = 'span[data-e2e="comment-icon"] > svg > use'
                
                # If that doesn't work, fall back to the original selector
                if not await page.locator(comment_button_selector).count():
                    print("[INFO] Pre-recorded selector not found. Using default selector.")
                    comment_button_selector = 'button[aria-label^="Read or add comments"]'
        
        # Selector for the video navigation buttons (which may include both next and previous)
        button_selector = "button.TUXButton.TUXButton--capsule.TUXButton--medium.TUXButton--secondary.action-item.css-1egy55o:not([disabled])"
        
        # If we have a target URL, look for videos on the profile page
        if target_url:
            # Make sure we're on the target URL
            if page.url != target_url:
                print(f"[INFO] Navigating to target URL: {target_url}")
                await navigate_to_url(page, target_url)
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                print(f"[INFO] Successfully navigated to: {page.url}")
                
            # Find video elements on the profile page - pass logs folder for debugging
            video_elements = await find_profile_videos(page, folders["logs"])
            
            if not video_elements or len(video_elements) == 0:
                print("[ERROR] No video elements found on the profile page. Exiting.")
                
                # Before giving up, take screenshots for debugging
                await save_page_structure(page, folders["logs"])
                
                # Attempt to salvage the session by opening the Videos tab explicitly
                try:
                    print("[INFO] Attempting to click on the Videos tab...")
                    # Try clicking on the Videos tab (common on profile pages)
                    await page.click('text="Videos"', timeout=5000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    
                    # Try finding videos again
                    video_elements = await find_profile_videos(page, folders["logs"])
                    if not video_elements or len(video_elements) == 0:
                        print("[ERROR] Still no video elements found after clicking Videos tab. Exiting.")
                        await browser.close()
                        proc.terminate()
                        return
                except Exception as tab_err:
                    print(f"[ERROR] Failed to click Videos tab: {tab_err}")
                    await browser.close()
                    proc.terminate()
                    return
                
            print(f"[INFO] Found {len(video_elements)} video elements to scrape")
            
            # Process videos by clicking on them directly
            await process_videos_by_clicking(page, video_elements, comment_button_selector, folders, recorded_elements, max_videos)
                
        else:
            # Original flow: Process a fixed number of videos in sequence using the "next" button
            for i in range(num_videos):
                print(f"\n[INFO] Processing video {i+1} of {num_videos}")
                await page.wait_for_load_state("networkidle", timeout=15000)

                # Try to open the comment section
                await open_comment_section(page, comment_button_selector)
                
                # Get the total comment count using the recorded elements
                total_comment_count = await extract_total_comment_count(page, recorded_elements)
                print(f"\n[INFO] Total comment count for video {i+1}: {total_comment_count}")
                
                # Ask the user what percentage of comments to scrape
                target_percentage = default_percentage
                try:
                    user_input = input(f"What percentage of comments do you want to scrape for this video? (1-100, default: {default_percentage}%): ")
                    if user_input.strip():
                        user_percentage = int(user_input.strip())
                        if 1 <= user_percentage <= 100:
                            target_percentage = user_percentage
                        else:
                            print(f"[INFO] Invalid percentage. Using default: {default_percentage}%")
                    else:
                        print(f"[INFO] Using default percentage: {default_percentage}%")
                except Exception as e:
                    print(f"[INFO] Error reading percentage: {e}. Using default: {default_percentage}%")
                
                # Execute the scraping script to extract comments
                logs = []
                page.on("console", lambda msg: logs.append(f"[BROWSER] {msg.type}: {msg.text}"))
                
                print(f"[INFO] Scraping comments for video {i+1}. This may take some time as we're aiming for {target_percentage}% of comments...")
                comments, total_comment_count_scraped, logs = await scrape_comments(page, i+1, folders["comments"], target_percentage)
                
                # Update total_comment_count if the scraped version is more accurate
                if total_comment_count_scraped > total_comment_count:
                    total_comment_count = total_comment_count_scraped
                    print(f"[INFO] Updated total comment count to {total_comment_count} based on scraper results")
                
                # Save diagnostic information
                await save_diagnostic_info(page, i+1, comments, total_comment_count, logs, folders["logs"])
                
                if not comments:
                    await save_error_logs(logs, i+1, folders["logs"])
                else:
                    percentage = 0
                    if total_comment_count > 0:
                        percentage = round((len(comments) / total_comment_count) * 100, 2)
                    
                    print(f"[SUCCESS] Scraped {len(comments)} comments from video {i+1}")
                    if total_comment_count > 0:
                        print(f"[INFO] That's approximately {percentage}% of the total {total_comment_count} comments")

                # Wait a bit longer before moving to the next video to ensure everything is complete
                await asyncio.sleep(3)
                
                # Define selector for next video button
                button_selector = "button.TUXButton.TUXButton--capsule.TUXButton--medium.TUXButton--secondary.action-item.css-1egy55o:not([disabled])"
                
                # Use the helper function to click the next video button (based on vertical position)
                success = await click_next_video(page, button_selector)
                if not success:
                    print(f"[ERROR] Could not navigate to next video from video {i+1}.")
                    break

                await asyncio.sleep(5)

        await browser.close()
        print("[INFO] Browser closed; process complete.")
    proc.terminate()

if __name__ == "__main__":
    # Check if the script was run with the --record flag
    record_mode = "--record" in sys.argv
    
    # Parse number of videos to scrape
    num_videos = 3  # Default reduced to 3 to prioritize quality over quantity
    default_percentage = 50  # Default percentage to scrape
    
    for i, arg in enumerate(sys.argv):
        if arg == "--videos" and i + 1 < len(sys.argv):
            try:
                num_videos = int(sys.argv[i + 1])
            except ValueError:
                pass
        elif arg == "--percentage" and i + 1 < len(sys.argv):
            try:
                default_percentage = int(sys.argv[i + 1])
                if default_percentage < 1 or default_percentage > 100:
                    print(f"Invalid percentage: {default_percentage}. Using default (50%).")
                    default_percentage = 50
            except ValueError:
                print(f"Invalid percentage format. Using default (50%).")
    
    if record_mode:
        print("\n" + "="*50)
        print("RUNNING IN RECORDING MODE")
        print("="*50)
        print("\nIn this mode, you will record key TikTok UI elements")
        print("to help the scraper navigate and collect data more effectively.")
        print("\nPress Ctrl+C now if you want to cancel.")
        print("Otherwise, the browser will launch in 5 seconds...")
        time.sleep(5)
    
    asyncio.run(main(record_mode=record_mode, num_videos=num_videos, default_percentage=default_percentage)) 