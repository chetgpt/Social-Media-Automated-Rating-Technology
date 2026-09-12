import asyncio
import json
import os
import subprocess
import time
from playwright.async_api import async_playwright
import sys

# --- START: Add imports from tiktok_scraper ---
# Assuming network_recorder.py is in the same parent directory as tiktok_scraper folder
try:
    from tiktok_scraper.utils.browser_utils import close_edge_tasks, wait_for_port, get_edge_executable_path
except ImportError:
    # Simple fallback if running from a different structure - might need adjustment
    print("Warning: Could not import from tiktok_scraper.utils. Attempting direct import...")
    # This part is less robust and assumes utils is accessible
    try:
        # Adjust path if necessary based on your project structure
        sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
        from tiktok_scraper.utils.browser_utils import close_edge_tasks, wait_for_port, get_edge_executable_path
    except ImportError as e:
        print(f"Fatal: Could not import browser_utils. Ensure network_recorder.py is placed correctly relative to the tiktok_scraper module. Error: {e}")
        sys.exit(1)
# --- END: Add imports from tiktok_scraper ---


async def handle_response(response):
    """Logs details of JSON responses."""
    try:
        # Check if the response looks like JSON
        content_type = response.headers.get('content-type', '')
        if 'application/json' in content_type:
            print(f"--- JSON Response Intercepted ---")
            print(f"URL: {response.url}")
            print(f"Method: {response.request.method}")
            print(f"Status: {response.status}")

            try:
                # Attempt to parse JSON and show top-level keys or a small snippet
                json_body = await response.json()
                if isinstance(json_body, dict):
                    print(f"JSON Keys: {list(json_body.keys())}")
                elif isinstance(json_body, list) and len(json_body) > 0:
                    # Show keys of the first item if it's a list of objects
                    if isinstance(json_body[0], dict):
                         print(f"JSON List Item Keys: {list(json_body[0].keys())}")
                    else:
                         print(f"JSON Snippet (List Start): {str(json_body)[:100]}...")
                else:
                    print(f"JSON Snippet: {str(json_body)[:100]}...")

            except Exception as json_err:
                # Sometimes content-type lies, or JSON is malformed
                print(f"JSON Parsing Error: {json_err}")
                try:
                    # Try to get raw text snippet as fallback
                    text_body = await response.text()
                    print(f"Raw Text Snippet: {text_body[:100]}...")
                except Exception as text_err:
                     print(f"Could not get response body: {text_err}")

            print(f"---------------------------------")

    except Exception as e:
        print(f"[ERROR] Error handling response {response.url}: {e}")


async def main(video_url):
    # --- START: Replicate run_scraper.py browser launch ---

    # Close any running Edge instances first
    close_edge_tasks()
    time.sleep(2)

    # Define your Edge profile path (MUST MATCH the one in main.py)
    profile_path = r"C:\Users\DELL\AppData\Local\Microsoft\Edge\User Data\Profile 2"
    if not os.path.exists(os.path.dirname(profile_path)):
        print(f"[ERROR] Edge user data directory not found at: {os.path.dirname(profile_path)}")
        print("Please ensure the path is correct and the profile exists.")
        return
    user_data_dir = os.path.dirname(profile_path)
    profile_directory = os.path.basename(profile_path)

    # Locate the Edge executable
    msedge_path = get_edge_executable_path()
    if not msedge_path:
        print("[ERROR] Edge executable not found on system.")
        return

    # Command to launch Edge with remote debugging enabled
    # Using the target video URL directly
    cmd = [
        msedge_path,
        f"--profile-directory={profile_directory}",
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-port=9222", # Use same port as main scraper
        "--remote-debugging-address=127.0.0.1",
        video_url # Launch directly into the video
    ]
    print("[INFO] Launching Edge with your existing profile (for network recording)...")
    proc = None
    try:
        proc = subprocess.Popen(cmd)
        print("Edge launched for recording with PID:", proc.pid)
    except Exception as popen_err:
        print(f"[ERROR] Failed to launch Edge process: {popen_err}")
        return

    # Wait for Edge to fully launch and the debugging port to open
    print("Waiting for browser and debugging port (up to 90s)...")
    time.sleep(15) # Initial wait before polling
    if not wait_for_port("127.0.0.1", 9222, timeout=75): # Reduced timeout slightly
        print("[ERROR] Remote debugging port 9222 did not open in time.")
        if proc: proc.terminate()
        return
    else:
        print("[INFO] Remote debugging port is open.")

    async with async_playwright() as p:
        browser = None
        page = None
        try:
            # Connect via CDP
            print("Connecting to Edge via CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            contexts = browser.contexts
            if not contexts:
                print("[WARN] No existing browser contexts found. Cannot get page.")
                # Attempt to create a new context - might not work as expected with connect_over_cdp
                context = await browser.new_context()
            else:
                context = contexts[0]

            pages = context.pages
            if not pages:
                 print("[WARN] No pages found in the context. Attempting to open a new page.")
                 # Fallback: Try to open the URL in a new page if none exists
                 page = await context.new_page()
                 await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
            else:
                 # Try to find the page with the video URL, otherwise use the first one
                 target_page = None
                 for p_instance in pages:
                     if video_url in p_instance.url:
                         target_page = p_instance
                         break
                 page = target_page if target_page else pages[0]
                 # If the current page isn't the target, navigate
                 if video_url not in page.url:
                     print(f"Navigating page to {video_url}...")
                     await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)

            print("[INFO] Connected via CDP. Current URL =", page.url)
            print("Page loaded. Browser window is ready for interaction.")

            # Attach the listener
            page.on("response", handle_response)
            print("Network listener attached.")

            print("!!! ACTION REQUIRED: Scroll down the comments section in the browser window. !!!")
            print("Watch the console output here for potential API calls.")
            print("Press Ctrl+C here in the console ONLY when you are finished recording.")

            # Keep the script running while the browser is open
            await page.wait_for_timeout(3600 * 1000) # Keep open for up to 1 hour

        except Exception as e:
            print(f"[ERROR] An error occurred during CDP connection or recording: {e}")
        finally:
            print("Closing browser connection...")
            if browser: await browser.close() # Disconnect Playwright
            if proc:
                print("Terminating Edge process...")
                proc.terminate() # Terminate the Edge process we started
    # --- END: Replicate run_scraper.py browser launch ---


if __name__ == "__main__":
    # --- START: Modify URL handling ---
    target_url = None
    if len(sys.argv) > 1:
        target_url = sys.argv[1]
    else:
        # Prompt the user if no URL is provided via arguments
        target_url = input("Enter the TikTok video URL to analyze: ")
        if not target_url:
            print("Error: No URL provided.")
            sys.exit(1)

    # Validate the URL roughly (optional, but good practice)
    if "tiktok.com" not in target_url or "/video/" not in target_url:
        print(f"Warning: Provided input '{target_url}' might not be a valid TikTok video URL.")
        # Decide if you want to exit or continue
        # sys.exit(1)

    print(f"Starting network recorder for URL: {target_url}")
    asyncio.run(main(target_url))
    # --- END: Modify URL handling ---

    # -- REMOVED Old Argument Handling --
    # if len(sys.argv) > 1:
    #     target_url = sys.argv[1]
    #     print(f"Starting network recorder for URL: {target_url}")
    #     asyncio.run(main(target_url))
    # else:
    #     print("Usage: python network_recorder.py <tiktok_video_url>")
    #     sys.exit(1)