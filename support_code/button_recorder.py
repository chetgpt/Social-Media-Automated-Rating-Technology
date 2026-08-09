import asyncio
import subprocess
import os
import time
import socket
import json
from playwright.async_api import async_playwright

# -----------------------------
# Utility Functions
# -----------------------------

def close_edge_tasks():
    """Terminates all running Microsoft Edge processes."""
    try:
        cmd = "taskkill /F /IM msedge.exe /T"
        subprocess.run(cmd, shell=True, check=True)
        print("All Microsoft Edge tasks have been terminated.")
    except Exception as e:
        print("Warning: Could not kill Edge tasks. Error:", e)

def wait_for_port(host, port, timeout=90):
    """Waits for a network port to become available."""
    start_time = time.time()
    while True:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except Exception:
            if time.time() - start_time > timeout:
                return False
            time.sleep(1)

def save_element_info(element_info, filename="recorded_elements.json"):
    """Saves the captured element information to a JSON file."""
    try:
        # Initialize the data structure
        data = {"layouts": {}}
        
        # Load existing data if file exists
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "layouts" not in data:
                        data["layouts"] = {}
            except json.JSONDecodeError:
                # If file is corrupted, start fresh
                data = {"layouts": {}}
        
        # Add new layout with timestamp
        layout_name = time.strftime("%Y%m%d_%H%M%S")
        data["layouts"][layout_name] = element_info
        
        # Save updated data
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\nElement information saved to {filename} under layout '{layout_name}'")
        return True
    except Exception as e:
        print(f"Failed to save element information to {filename}. Error:", str(e))
        return False

# -----------------------------
# Element Recorder
# -----------------------------

async def record_button_click(page):
    """Waits for user to click buttons and records the element information."""
    print("\n" + "="*50)
    print("RECORDING MODE ACTIVE")
    print("="*50)
    print("\nPlease follow these steps:")
    print("You can record up to 5 button clicks.")
    print("For each button you want to record:")
    print("1. Click the button/element you want to record")
    print("2. Press Enter to confirm and move to the next one")
    print("3. Type 'done' and press Enter when finished")
    print("\nThe recorder will capture information about each element.")
    print("DO NOT close the browser window - the script will do that automatically.")
    print("\nWaiting for your clicks...")

    recorded_elements = {}

    # Setup click event listener using JavaScript
    await page.evaluate('''() => {
        window.addEventListener('click', function(e) {
            if (e.target.tagName) {
                e.target.setAttribute('data-recorded-click', 'true');
                e.target.setAttribute('data-recorded-timestamp', Date.now());
                
                if (e.target.tagName === 'BUTTON' || 
                    e.target.tagName === 'A' ||
                    e.target.closest('button') ||
                    e.target.closest('a') ||
                    e.target.tagName === 'SPAN' ||
                    e.target.tagName === 'DIV' ||
                    e.target.tagName === 'P') {
                    console.log('Recorded click on:', e.target.outerHTML);
                    window.__elementClicks = (window.__elementClicks || 0) + 1;
                }
            }
        }, true);
    }''')

    # Record up to 5 elements
    for i in range(5):
        print(f"\nRECORDING ELEMENT {i+1}/5")
        print("Click on the element you want to record, then press Enter.")
        print("Or type 'done' to finish recording.")
        
        user_input = input()
        if user_input.lower() == 'done':
            break

        # Find the most recently clicked element
        all_clicked = await page.locator('[data-recorded-click="true"]').all()
        if not all_clicked:
            print(f"❌ No element was clicked. Skipping element {i+1}.")
            continue

        # Get timestamps and find the most recent click
        timestamps = []
        for idx, el in enumerate(all_clicked):
            ts = await el.get_attribute("data-recorded-timestamp")
            timestamps.append((idx, int(ts) if ts else 0))

        # Sort by timestamp (newest first)
        sorted_clicks = sorted(timestamps, key=lambda x: x[1], reverse=True)
        newest_index = sorted_clicks[0][0]

        # Get information about the clicked element
        element_info = await get_element_info(all_clicked[newest_index])
        
        # Ask user to name this element
        print("\nWhat would you like to name this element? (e.g., comment_button, share_button)")
        element_name = input().strip()
        if not element_name:
            element_name = f"element_{i+1}"

        recorded_elements[element_name] = element_info
        print(f"\n✓ Element '{element_name}' recorded successfully!")
        print(f"CSS Selector: {element_info['css_selector']}")

    if not recorded_elements:
        print("\n❌ No elements were recorded.")
        return None

    print("\n" + "="*50)
    print(f"RECORDED {len(recorded_elements)} ELEMENTS SUCCESSFULLY!")
    print("="*50)
    
    return recorded_elements

async def get_element_info(clicked):
    """Extracts detailed information about a clicked element."""
    # Get information about the clicked element
    tag_name = await clicked.evaluate("el => el.tagName")
    outer_html = await clicked.evaluate("el => el.outerHTML")
    inner_text = await clicked.evaluate("el => el.innerText || ''")
    
    # Get various attributes that might be useful for finding this element again
    attrs = {}
    
    # Common useful attributes for buttons/interactive elements
    for attr in ['id', 'class', 'aria-label', 'data-e2e', 'role', 'name', 'type']:
        try:
            value = await clicked.get_attribute(attr)
            if value:
                attrs[attr] = value
        except:
            pass
    
    # Get CSS selector
    selector = await clicked.evaluate('''el => {
        // Try to build a unique CSS selector for this element
        let parts = [];
        let current = el;
        
        // Go up at most 3 levels
        for (let i = 0; i < 3; i++) {
            if (!current) break;
            
            let part = current.tagName.toLowerCase();
            
            // Add useful attributes
            if (current.id) {
                part += `#${current.id}`;
            } else {
                // Try data-e2e first (TikTok specific)
                if (current.getAttribute('data-e2e')) {
                    part += `[data-e2e="${current.getAttribute('data-e2e')}"]`;
                } 
                // Then try aria-label
                else if (current.getAttribute('aria-label')) {
                    part += `[aria-label^="${current.getAttribute('aria-label').split(' ')[0]}"]`;
                }
                // Then try class (but only if it seems stable)
                else if (current.className && typeof current.className === 'string' && !current.className.includes('--') && current.className.length < 50) {
                    part += `.${current.className.split(' ')[0]}`;
                }
                // Alternative approach using classList
                else if (current.classList && current.classList.length > 0) {
                    const firstClass = current.classList[0];
                    // Only use class if it seems stable
                    if (firstClass && !firstClass.includes('--') && firstClass.length < 50) {
                        part += `.${firstClass}`;
                    }
                }
            }
            
            parts.unshift(part);
            current = current.parentElement;
        }
        
        return parts.join(' > ');
    }''')
    
    # Get XPath as an alternative
    xpath = await clicked.evaluate('''el => {
        // Try to build a robust XPath
        let xpath = '';
        let current = el;
        let tag = current.tagName.toLowerCase();
        
        // Handle button specifically
        if (tag === 'button') {
            if (current.getAttribute('aria-label')) {
                // Match by partial text
                return `//button[contains(@aria-label, '${current.getAttribute('aria-label').split(' ')[0]}')]`;
            }
        }
        
        // Default case - just get the tag and position
        let sameTagSiblings = Array.from(current.parentElement.children)
            .filter(sibling => sibling.tagName === current.tagName);
        let position = sameTagSiblings.indexOf(current) + 1;
        
        return `//${tag}[${position}]`;
    }''')
    
    return {
        "tag_name": tag_name,
        "outer_html": outer_html,
        "inner_text": inner_text,
        "attributes": attrs,
        "css_selector": selector,
        "xpath": xpath,
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }

# -----------------------------
# Main Asynchronous Function
# -----------------------------

async def main():
    # Get the URL from user input
    print("\nEnter the TikTok URL to navigate to:")
    url = input().strip()
    if not url:
        print("Error: URL is required")
        return
    
    # Close any running Edge instances
    close_edge_tasks()
    time.sleep(1)

    # Define your Edge profile path (adjust this to your system)
    profile_path = r"C:\Users\DELL\AppData\Local\Microsoft\Edge\User Data\Profile 2"
    user_data_dir = os.path.dirname(profile_path)
    profile_directory = os.path.basename(profile_path)

    # Locate the Edge executable
    msedge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(msedge_path):
        msedge_path = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(msedge_path):
        print("[ERROR] Edge executable not found on system.")
        return

    # Command to launch Edge with remote debugging enabled
    cmd = [
        msedge_path,
        f"--profile-directory={profile_directory}",
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1"
    ]
    print("[INFO] Launching Edge with your existing profile...")
    proc = subprocess.Popen(cmd)
    print("Edge launched with PID:", proc.pid)

    # Wait for Edge to fully launch and the debugging port to open
    print("[INFO] Waiting for browser to initialize...")
    if not wait_for_port("127.0.0.1", 9222, timeout=30):
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

        # Navigate to the specified URL first
        print(f"[INFO] Navigating to {url}...")
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            print("[INFO] Successfully navigated to the URL")
        except Exception as e:
            print(f"[ERROR] Failed to navigate to URL: {e}")
            await browser.close()
            proc.terminate()
            return

        # Try to enable mobile emulation
        try:
            print("[INFO] Attempting to emulate a mobile device (iPhone 13)...")
            iphone = p.devices['iPhone 13']
            await page.emulate(**iphone)
            print("[INFO] Emulation set. Reloading page...")
            await page.reload()
            await page.wait_for_load_state("networkidle", timeout=10000)
            print(f"[INFO] Page reloaded in mobile emulation. Current URL = {page.url}")
        except Exception as emu_err:
            print(f"[WARN] Failed to apply mobile emulation or reload: {emu_err}. Continuing without emulation.")

        # Wait for user confirmation before starting recording
        print("\n" + "="*80)
        print("PAGE LOADED - READY TO RECORD".center(80))
        print("="*80)
        print("\nVerify that you're on the correct page.")
        print("Press Enter when the page layout is stable and you're ready to start recording...")
        input()
        
        print("\n" + "="*80)
        print("RECORDER ACTIVE: Please follow the instructions to record elements".center(80))
        print("="*80)
        
        # Wait for user to manually click the elements
        elements_info = await record_button_click(page)
        
        if elements_info:
            # Save the element information
            if save_element_info(elements_info):
                print("\n" + "="*80)
                print("SUCCESS: Recording completed!".center(80))
                print("="*80)
                print("\nThe element information has been saved to 'recorded_elements.json'")
                print(f"Number of elements recorded: {len(elements_info)}")
            else:
                print("\n" + "="*80)
                print("ERROR: Failed to save element information".center(80))
                print("="*80)
        else:
            print("\n" + "="*80)
            print("ERROR: No elements were recorded".center(80))
            print("="*80)
            print("\nPossible reasons:")
            print("- You didn't click any elements within the time limit")
            print("- The clicked elements weren't properly detected")
        
        # Close browser and exit
        print("\nRecording session complete. Closing browser...")
        time.sleep(2)
        await browser.close()
        proc.terminate()

# -----------------------------
# Main Execution
# -----------------------------

if __name__ == "__main__":
    print("\n" + "="*50)
    print("BUTTON RECORDER TOOL")
    print("="*50)
    print("\nThis script will help you record button locations")
    print("for a specific TikTok video layout.")
    print("You can record up to 5 different buttons/elements.")
    print("\nPress Ctrl+C now if you want to cancel.")
    print("Otherwise, continue in 3 seconds...")
    time.sleep(3)
    
    asyncio.run(main()) 