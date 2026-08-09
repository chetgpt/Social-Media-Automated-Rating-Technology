import asyncio
import subprocess
import os
import time
import socket
import json
import sys  # Added for command-line argument support
from playwright.async_api import async_playwright
import datetime  # For timestamped folder names

# -----------------------------
# Utility Functions
# -----------------------------

def create_folder_structure():
    """Creates the folder structure for saving comments and logs."""
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

def store_comments_to_json(comments, filename="scraped_comments.json", total_comment_count=0, folder_path=None):
    """Saves the scraped comments to a JSON file."""
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
            with open(filename, "w", encoding="utf-8") as f:
                json.dump({"warning": "No comments were found", "comments": []}, f, ensure_ascii=False, indent=2)
            print(f"Created empty comments file {filename} with warning.")
            return
        
        # Print some sample comments for debugging
        print(f"Preparing to save {len(comments)} comments. Sample:")
        for i, comment in enumerate(comments[:3]):  # Show up to 3 sample comments
            print(f"  Comment {i+1}: {comment.get('username', 'No username')} - {comment.get('commentText', 'No text')[:50]}...")
        
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
    """Saves the captured element information to a JSON file."""
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
    """Loads previously captured element information from a JSON file."""
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

# -----------------------------
# Element Recorder
# -----------------------------

async def record_button_click(page):
    """Waits for a user to click a button and records the element information."""
    print("\n" + "="*50)
    print("RECORDING MODE ACTIVE")
    print("="*50)
    print("\nPlease navigate to a TikTok video and click the comment button.")
    print("The recorder will capture the button's information.")
    print("DO NOT close the browser window - the script will do that automatically.")
    print("\n1. Find a video with comments")
    print("2. Click on the comment button (to open comments)")
    print("3. Wait for confirmation in this console")
    print("\nWaiting for your click...")

    clicked_element = {}
    click_recorded = False

    def handle_click(element):
        nonlocal clicked_element, click_recorded
        click_recorded = True
        return element

    # Setup click event listener using JavaScript
    await page.evaluate('''() => {
        window.addEventListener('click', function(e) {
            // Mark the clicked element with a special attribute so we can find it
            if (e.target.tagName) {
                e.target.setAttribute('data-recorded-click', 'true');
                
                // If this is a common clickable element, send info to Python
                if (e.target.tagName === 'BUTTON' || 
                    e.target.tagName === 'A' ||
                    e.target.closest('button') ||
                    e.target.closest('a')) {
                    console.log('Recorded click on:', e.target.outerHTML);
                    window.__elementClicked = true;
                }
            }
        }, true);
    }''')

    # Wait for the click to happen
    max_wait_time = 120  # seconds
    start_time = time.time()
    
    while not click_recorded and (time.time() - start_time) < max_wait_time:
        # Check if click was registered in browser
        has_clicked = await page.evaluate("() => window.__elementClicked === true")
        
        if has_clicked:
            # Find the element that was marked with our attribute
            clicked = await page.locator('[data-recorded-click="true"]').first
            
            if await clicked.count() > 0:
                # Get information about the clicked element
                tag_name = await clicked.evaluate("el => el.tagName")
                outer_html = await clicked.evaluate("el => el.outerHTML")
                
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
                            else if (current.className && !current.className.includes('--') && current.className.length < 50) {
                                part += `.${current.className.split(' ')[0]}`;
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
                
                clicked_element = {
                    "tag_name": tag_name,
                    "outer_html": outer_html,
                    "attributes": attrs,
                    "css_selector": selector,
                    "xpath": xpath,
                    "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                print("\n" + "="*50)
                print("CLICK RECORDED SUCCESSFULLY!")
                print("="*50)
                print(f"\nElement: {tag_name}")
                if 'aria-label' in attrs:
                    print(f"Aria-Label: {attrs['aria-label']}")
                print(f"\nCSS Selector: {selector}")
                print(f"XPath: {xpath}")
                print("\nSaving element information for future use...")
                
                click_recorded = True
                break
        
        await asyncio.sleep(0.5)  # Check every half second
    
    if not click_recorded:
        print("\nNo click was recorded within the time limit.")
        return None
    
    return clicked_element

# -----------------------------
# Helper Function to Click the Next Video Button
# -----------------------------

async def click_next_video(page, selector):
    """
    Finds all buttons matching the selector, selects the one positioned at the bottom 
    (i.e. with the highest y coordinate), and clicks it. This should correspond to the 
    "next video" button in a vertical arrangement.
    """
    buttons = await page.query_selector_all(selector)
    if not buttons:
        print("[WARN] No navigation buttons found using the selector.")
        
        # Try alternative selectors for navigation
        alt_selectors = [
            "button.css-1egy55o:not([disabled])",  # More generic version
            "[data-e2e='arrow-right']",  # Common TikTok navigation element
            "button[aria-label*='Next']",  # Buttons with "Next" in aria-label
            "svg[aria-label*='Next']",     # SVG icons with "Next" in aria-label
            ".arrow-right"                 # Common class for right navigation arrows
        ]
        
        for alt_selector in alt_selectors:
            print(f"[INFO] Trying alternative selector: {alt_selector}")
            alt_buttons = await page.query_selector_all(alt_selector)
            if alt_buttons:
                print(f"[INFO] Found {len(alt_buttons)} alternative navigation buttons")
                buttons = alt_buttons
                break
        
        # If we still don't have buttons, try scrolling down as last resort
        if not buttons:
            print("[INFO] No navigation buttons found. Trying to scroll down to next video...")
            try:
                # Scroll down to potentially load next video
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(3000)  # Wait for potential load
                print("[INFO] Scrolled down to try to reveal next video")
                return True  # Assume scrolling may have worked
            except Exception as e:
                print(f"[ERROR] Failed to scroll: {e}")
                return False

    next_button = None
    max_y = -1
    for btn in buttons:
        box = await btn.bounding_box()
        if box and box["y"] > max_y:
            max_y = box["y"]
            next_button = btn

    if next_button:
        try:
            # Before clicking, get some info about the button
            tag_name = await next_button.evaluate("el => el.tagName")
            aria_label = await next_button.evaluate("el => el.getAttribute('aria-label') || ''")
            print(f"[INFO] Found next button: {tag_name} with aria-label '{aria_label}'")
            
            await next_button.click()
            print("[INFO] Clicked next video button.")
            await page.wait_for_timeout(3000)  # Wait for navigation
            return True
        except Exception as e:
            print(f"[ERROR] Failed to click next button: {e}")
            return False
    else:
        print("[ERROR] Could not determine the next video button.")
        return False

# -----------------------------
# JavaScript for Scraping Comments & Revealing Hidden Comments
# -----------------------------

scrape_js = r"""
(async () => {
    const delay = (ms) => new Promise(res => setTimeout(res, ms));

    // DOM Inspector - Log the structure of elements to help find patterns
    function inspectDom() {
        console.log("==================== DOM INSPECTION ====================");
        console.log("Looking for comment-related elements in the entire document...");
        
        // Function to filter relevant comment-related attributes/classes
        function isRelevantAttr(attr) {
            return attr.toLowerCase().includes('comment') || 
                   attr.toLowerCase().includes('reply') ||
                   attr.toLowerCase().includes('user') ||
                   attr.toLowerCase().includes('text');
        }
        
        // Look for elements with comment-related attributes or classes
        const allElements = document.querySelectorAll('*');
        let relevantElements = [];
        
        for (const el of allElements) {
            // Check for relevant classes
            if (el.className && typeof el.className === 'string') {
                if (el.className.toLowerCase().includes('comment') || 
                    el.className.toLowerCase().includes('reply')) {
                    
                    relevantElements.push({
                        element: el,
                        tag: el.tagName,
                        class: el.className,
                        id: el.id,
                        children: el.children.length,
                        text: el.innerText?.substring(0, 50) + '...'
                    });
                    continue;
                }
            }
            
            // Check for data-e2e attributes (TikTok specific)
            if (el.hasAttribute('data-e2e') && 
                isRelevantAttr(el.getAttribute('data-e2e'))) {
                
                relevantElements.push({
                    element: el,
                    tag: el.tagName,
                    attr: 'data-e2e=' + el.getAttribute('data-e2e'),
                    children: el.children.length,
                    text: el.innerText?.substring(0, 50) + '...'
                });
            }
        }
        
        // Log the most promising elements
        console.log(`Found ${relevantElements.length} potential comment-related elements`);
        relevantElements.slice(0, 20).forEach((info, i) => {
            console.log(`Element ${i+1}:`, 
                `Tag: ${info.tag}`, 
                info.class ? `Class: ${info.class}` : '', 
                info.attr ? `Attr: ${info.attr}` : '',
                `Children: ${info.children}`,
                `Text: ${info.text}`);
        });
        
        // Find potential comment containers (scrollable divs with many children)
        const allDivs = document.querySelectorAll('div');
        const potentialContainers = Array.from(allDivs).filter(div => {
            return div.scrollHeight > div.clientHeight && // Has scrolling
                  div.children.length > 5 &&             // Has multiple children
                  div.clientHeight > 100;                // Reasonable size
        });
        
        console.log(`Found ${potentialContainers.length} potential scrollable containers`);
        potentialContainers.forEach((container, i) => {
            console.log(`Container ${i+1}:`, 
                `Children: ${container.children.length}`,
                `Height: ${container.clientHeight}px`,
                `Scroll Height: ${container.scrollHeight}px`,
                `Class: ${container.className}`);
            
            // Inspect the first few children of each potential container
            Array.from(container.children).slice(0, 3).forEach((child, j) => {
                console.log(`  - Child ${j+1}: ${child.tagName}`, 
                    `Class: ${child.className}`,
                    `Text: ${child.innerText?.substring(0, 30)}...`);
                
                // Find any user links or usernames in this child
                const userLinks = child.querySelectorAll('a[href^="/@"]');
                if (userLinks.length > 0) {
                    console.log(`    * Contains ${userLinks.length} user links!`);
                    console.log(`    * First user: ${userLinks[0].innerText}`);
                }
            });
        });
        
        console.log("==================== END DOM INSPECTION ====================");
        return { relevantElements, potentialContainers };
    }

    // Get the comment container element
    function getCommentContainer() {
        const selectors = [
            ".css-7whb78-DivCommentListContainer",
            ".css-1qp5gj2-DivCommentListContainer",
            ".css-13wx63w-DivCommentObjectWrapper",
            ".tiktok-16r0vzi-DivCommentListContainer",  // Added newer class name
            "[class*='CommentListContainer']",  // Generic class-based selector
            "[class*='CommentContainer']",      // Alternative generic selector
            "[data-e2e='comment-list']",        // Data attribute selector
            ".comment-list",                    // Simple class approach
        ];
        
        console.log("Searching for comment container with selectors:");
        
        for (const sel of selectors) {
            console.log(`Trying selector: ${sel}`);
            const el = document.querySelector(sel);
            if (el) {
                console.log(`Found comment container with selector: ${sel}`);
                
                // Log some properties to help with debugging
                console.log(`Container height: ${el.offsetHeight}px`);
                console.log(`Container has scroll: ${el.scrollHeight > el.clientHeight}`);
                console.log(`Number of children: ${el.children.length}`);
                
                return el;
            }
        }
        
        // If we can't find the container with standard selectors, try this fallback method:
        // Look for any scrollable div that might contain comments
        console.log("Standard selectors failed, trying fallback method...");
        
        const potentialContainers = Array.from(document.querySelectorAll('div'))
            .filter(div => {
                // Must be reasonably sized
                const height = div.offsetHeight;
                // Must have scrolling capability
                const hasScroll = div.scrollHeight > div.clientHeight;
                // Must contain user links as children (typical of comments)
                const hasUserLinks = div.querySelectorAll('a[href^="/@"]').length > 0;
                
                return height > 100 && hasScroll && hasUserLinks;
            });
        
        if (potentialContainers.length > 0) {
            console.log(`Found ${potentialContainers.length} potential containers using fallback method`);
            // Return the tallest one, which is likely the main comment section
            const bestContainer = potentialContainers.sort((a, b) => b.offsetHeight - a.offsetHeight)[0];
            console.log(`Selected container with height: ${bestContainer.offsetHeight}px`);
            return bestContainer;
        }
        
        console.log("Failed to find any comment container");
        return null;
    }

    // Scroll the comment container to its bottom
    async function scrollCommentContainer(container) {
        if (!container) return;
        container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
        await delay(3000 + Math.random() * 2000);
    }

    // Click the bottom-most "view more" button that reveals additional stored comments.
    // This is not about nested replies but about forcing the site to load more comments.
    async function clickViewMoreAtBottom(container) {
        if (!container) return;
        // Look for elements that suggest more comments are available
        const candidates = Array.from(container.querySelectorAll('p, span, button, div'))
          .filter(el => {
              const text = (el.textContent || "").trim();
              return /(View|See|Load)\s+(more|all|\d+)\s+(replies|comments)/i.test(text);
          });
        if (candidates.length > 0) {
            // Choose the candidate that is positioned lowest on the screen
            let candidate = null;
            let maxY = -1;
            for (let el of candidates) {
                const rect = el.getBoundingClientRect();
                if (rect && rect.y > maxY) {
                    maxY = rect.y;
                    candidate = el;
                }
            }
            if (candidate) {
                candidate.click();
                await delay(2000 + Math.random() * 1000);
            }
        }
    }

    // Extract all visible comments from the comment container
    async function extractItems() {
        const container = getCommentContainer();
        if (!container) {
            console.log("Comment container not found, cannot extract comments");
            return [];
        }
        
        console.log("Found comment container, looking for comments...");
        
        // SPECIAL APPROACH: TikTok might be using a specific structure with a comment wrapper div
        // First, try a direct search for TikTok's current (as of 2023) comment structure
        console.log("Trying TikTok-specific extraction approach first...");
        const tiktokComments = [];
        
        // This is a function to extract comments using a specific pattern match approach
        function scanForCommentPatterns(rootNode) {
            // Get all divs that might be comment wrappers
            const divs = rootNode.querySelectorAll('div');
            console.log(`Scanning ${divs.length} divs for comment patterns...`);
            
            let commentCount = 0;
            // For each div, check if it has a typical comment structure
            for (const div of divs) {
                try {
                    // Get user link (if present)
                    const userLink = div.querySelector('a[href^="/@"]');
                    if (!userLink) continue; // Not a comment if no user link
                    
                    const username = userLink.textContent.trim();
                    if (!username) continue;
                    
                    // Check if this div or a close parent might be a comment wrapper
                    // by looking for typical comment characteristics
                    
                    // 1. It should have enough content to be meaningful
                    const fullText = div.textContent.trim();
                    if (fullText.length < username.length + 10) continue; // Too short for a real comment
                    
                    // 2. It should have some specific elements that comments typically have
                    const hasTimestamp = Array.from(div.querySelectorAll('span')).some(span => 
                        /\d+[smhdwy] ago/.test(span.textContent) || span.textContent.includes('ago')
                    );
                    
                    // 3. Calculate a comment likelihood score
                    let score = 0;
                    if (hasTimestamp) score += 3;
                    if (div.querySelector('button')) score += 1; // Likely has like/reply buttons
                    if (div.querySelectorAll('div').length > 2) score += 1; // Has some structure
                    if (div.querySelectorAll('svg').length > 0) score += 1; // Has icons
                    
                    if (score >= 3) { // Good candidate for being a comment
                        // Extract the comment text: full text minus username and UI elements
                        let commentText = fullText
                            .replace(username, '')
                            .replace(/Reply/g, '')
                            .replace(/Report/g, '')
                            .replace(/Like/g, '')
                            .replace(/\d+[smhdwy] ago/g, '')
                            .replace(/View replies/g, '')
                            .replace(/\s{2,}/g, ' ')
                            .trim();
                        
                        // Find timestamp if present
                        let timeStamp = null;
                        const timeSpans = Array.from(div.querySelectorAll('span')).filter(span => 
                            /\d+[smhdwy] ago/.test(span.textContent) || span.textContent.includes('ago')
                        );
                        if (timeSpans.length > 0) {
                            timeStamp = timeSpans[0].textContent.trim();
                        }
                        
                        if (commentText && commentText.length > 0) {
                            tiktokComments.push({ username, commentText, timeStamp });
                            commentCount++;
                            // To avoid processing children again
                            div.setAttribute('data-processed', 'true');
                        }
                    }
                } catch (err) {
                    console.log("Error in pattern scan:", err);
                }
            }
            console.log(`Pattern scan found ${commentCount} comments`);
            return commentCount;
        }
        
        // Scan the container for comment patterns
        scanForCommentPatterns(container);
        
        // If we found comments with the pattern approach, use those
        if (tiktokComments.length > 0) {
            console.log(`Found ${tiktokComments.length} comments using TikTok-specific pattern approach`);
            return tiktokComments;
        }
        
        // If the direct approach failed, continue with the selector-based approach
        // Try different selectors for comment wrappers - TikTok often changes these
        const selectorOptions = [
            ".css-1gstnae-DivCommentItemWrapper",  // Original selector
            ".tiktok-16r0vzi-DivCommentItemContainer",  // Another potential selector
            "[class*='CommentItem']",   // More generic approach
            "[data-e2e='comment-item']", // Data attribute approach
            ".comment-item"  // Simple class approach
        ];
        
        let wrappers = [];
        let usedSelector = '';
        
        // Try each selector until we find comments
        for (const selector of selectorOptions) {
            wrappers = container.querySelectorAll(selector);
            if (wrappers && wrappers.length > 0) {
                console.log(`Found ${wrappers.length} comments using selector: ${selector}`);
                usedSelector = selector;
                break;
            }
        }
        
        if (wrappers.length === 0) {
            // If no comments found with predefined selectors, try a more generic approach
            console.log("No comments found with standard selectors, trying generic approach");
            // Look for any elements that might contain both username and comment text
            const possibleComments = Array.from(container.querySelectorAll('div'))
                .filter(div => {
                    // Look for divs that might contain a username and comment text
                    if (div.hasAttribute('data-processed') && div.getAttribute('data-processed') === 'true') {
                        return false; // Skip already processed divs
                    }
                    const hasUserLink = div.querySelector('a[href^="/@"]');
                    const hasText = div.textContent.length > 10; // Arbitrary minimum length
                    return hasUserLink && hasText;
                });
                
            if (possibleComments.length > 0) {
                console.log(`Found ${possibleComments.length} potential comments using generic approach`);
                wrappers = possibleComments;
            }
        }
        
        let items = [];
        for (let w of wrappers) {
            try {
                // Skip if this element was already processed
                if (w.hasAttribute('data-processed') && w.getAttribute('data-processed') === 'true') {
                    continue;
                }
                
                // Try multiple selector approaches to find username and comment text
                // First approach: using data-e2e attributes (more stable)
                let username = null;
                let commentText = null;
                let timeStamp = null;
                
                // Try to find username with different selectors
                const userSelectors = [
                    ".css-13x3qpp-DivUsernameContentWrapper a[href^='/@']",  // Original
                    "a[href^='/@']",  // Simplified
                    "[data-e2e='comment-username']"  // Data attribute
                ];
                
                for (const selector of userSelectors) {
                    const userEl = w.querySelector(selector);
                    if (userEl) {
                        username = userEl.textContent.trim();
                        break;
                    }
                }
                
                if (!username) {
                    console.log("No username found in comment, skipping");
                    continue;
                }
                
                console.log(`Found username: ${username}`);
                
                // Get the full text and remove the username and UI elements
                const fullText = w.textContent.trim();
                commentText = fullText
                    .replace(username, '')
                    .replace(/Reply/g, '')
                    .replace(/Report/g, '')
                    .replace(/Like/g, '')
                    .replace(/View replies/g, '')
                    .replace(/\d+[smhdwy] ago/g, '')
                    .replace(/\s{2,}/g, ' ')
                    .trim();
                
                // Try to find timestamp
                const timeSelectors = [
                    "[data-e2e^='comment-time-']",
                    ".comment-time",
                    "span[class*='Time']",
                    "span.time"
                ];
                
                for (const selector of timeSelectors) {
                    const timeEl = w.querySelector(selector);
                    if (timeEl) {
                        timeStamp = timeEl.textContent.trim();
                        break;
                    }
                }
                
                if (!timeStamp) {
                    // Try to find it using a pattern match
                    const timeSpans = Array.from(w.querySelectorAll('span')).filter(span => 
                        /\d+[smhdwy] ago/.test(span.textContent) || span.textContent.includes('ago')
                    );
                    if (timeSpans.length > 0) {
                        timeStamp = timeSpans[0].textContent.trim();
                    }
                }
                
                if (username && commentText && commentText.length > 0) {
                    items.push({ username, commentText, timeStamp });
                    console.log(`Added comment: ${username} - ${commentText.substring(0, 30)}...`);
                    // Mark as processed to avoid duplicates
                    w.setAttribute('data-processed', 'true');
                }
            } catch (err) {
                console.log("Error extracting a comment:", err);
            }
        }
        
        console.log(`Extracted ${items.length} comments from container using standard method`);
        
        // Combine results from both approaches (pattern scan and standard method)
        const combinedItems = [...tiktokComments, ...items];
        
        // Remove duplicates
        const uniqueComments = [];
        const seen = new Set();
        
        for (const comment of combinedItems) {
            const key = `${comment.username}|${comment.commentText}`;
            if (!seen.has(key)) {
                seen.add(key);
                uniqueComments.push(comment);
            }
        }
        
        console.log(`Final count: ${uniqueComments.length} unique comments`);
        return uniqueComments;
    }

    // Function to get the total number of comments for the video
    function getTotalCommentCount() {
        // Try different selectors for comment count
        const selectors = [
            'strong[data-e2e="comment-count"]',
            'strong[data-e2e="browse-comment-count"]',
            '[class*="comment-count"]',
            'div[class*="TabMenu"] div:contains("Comments")'
        ];
        
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (el) {
                const text = el.textContent.trim();
                // Extract numbers from the text
                const match = text.match(/(\d+[,.]?\d*[KMB]?)/i);
                if (match) {
                    let countText = match[1];
                    // Convert K, M, B suffixes to actual numbers
                    if (countText.includes('K')) {
                        return parseInt(parseFloat(countText.replace('K', '')) * 1000);
                    } else if (countText.includes('M')) {
                        return parseInt(parseFloat(countText.replace('M', '')) * 1000000);
                    } else if (countText.includes('B')) {
                        return parseInt(parseFloat(countText.replace('B', '')) * 1000000000);
                    } else {
                        return parseInt(countText.replace(/[,. ]/g, ''));
                    }
                }
            }
        }
        
        // Check for the "Comments (123)" format
        const tabElements = document.querySelectorAll('div, span, p');
        for (const el of tabElements) {
            const text = el.textContent.trim();
            const match = text.match(/Comments\s*\((\d+[,.]?\d*[KMB]?)\)/i);
            if (match) {
                let countText = match[1];
                // Convert K, M, B suffixes to actual numbers
                if (countText.includes('K')) {
                    return parseInt(parseFloat(countText.replace('K', '')) * 1000);
                } else if (countText.includes('M')) {
                    return parseInt(parseFloat(countText.replace('M', '')) * 1000000);
                } else if (countText.includes('B')) {
                    return parseInt(parseFloat(countText.replace('B', '')) * 1000000000);
                } else {
                    return parseInt(countText.replace(/[,. ]/g, ''));
                }
            }
        }
        
        // If no count found, return 0
        return 0;
    }

    // Main loop: repeatedly scroll, click the "view more" button, and then re-check for new comments.
    // First, run the DOM inspector to analyze the page structure
    console.log("Running DOM inspection to analyze page structure...");
    const domAnalysis = inspectDom();
    
    // Get total comment count and calculate 10% target
    const totalCommentCount = getTotalCommentCount();
    console.log(`Total comment count detected: ${totalCommentCount}`);
    const targetCommentCount = Math.ceil(totalCommentCount * 0.1); // 10% of total
    console.log(`Target comment count (10%): ${targetCommentCount}`);
    
    // Extract Comments using a dynamically generated selector based on DOM inspection
    async function extractCommentsNewMethod() {
        console.log("Attempting to extract comments using new dynamic method");
        
        let bestContainer = null;
        let commentItems = [];
        
        // Find the most likely comment container from our analysis
        if (domAnalysis.potentialContainers.length > 0) {
            // Sort by most likely to be a comment container (most user links)
            const sortedContainers = domAnalysis.potentialContainers.sort((a, b) => {
                const aUserLinks = a.querySelectorAll('a[href^="/@"]').length;
                const bUserLinks = b.querySelectorAll('a[href^="/@"]').length;
                return bUserLinks - aUserLinks;
            });
            
            bestContainer = sortedContainers[0];
            console.log(`Using most promising container with ${bestContainer.querySelectorAll('a[href^="/@"]').length} user links`);
        } else {
            console.log("No potential containers found from DOM analysis");
            bestContainer = getCommentContainer(); // Fall back to original method
        }
        
        if (!bestContainer) {
            console.log("Failed to find any container for comments");
            return [];
        }
        
        // Try different approaches to extract comments
        
        // Approach 1: Look for child elements that contain user links directly
        const childrenWithUserLinks = Array.from(bestContainer.children).filter(child => 
            child.querySelector('a[href^="/@"]')
        );
        
        console.log(`Found ${childrenWithUserLinks.length} children with user links`);
        
        if (childrenWithUserLinks.length > 0) {
            // These are likely comment items
            commentItems = childrenWithUserLinks;
            console.log("Using children with user links as comment items");
        } else {
            // Approach 2: Look deeper for comment-like structures
            const possibleCommentItems = Array.from(bestContainer.querySelectorAll('div')).filter(div => {
                // A comment item typically has:
                // 1. A username (link starting with /@)
                // 2. Some text content (comment text)
                // 3. Usually has reasonable dimensions
                const hasUserLink = div.querySelector('a[href^="/@"]');
                const hasReasonableHeight = div.offsetHeight > 30; // Comments usually aren't tiny
                const notTooDeep = div.querySelectorAll('div').length < 15; // Not too deeply nested
                
                return hasUserLink && hasReasonableHeight && notTooDeep;
            });
            
            console.log(`Found ${possibleCommentItems.length} possible comment items using deep search`);
            
            if (possibleCommentItems.length > 0) {
                // Group by parent to find the comment list pattern
                const parentCounts = {};
                possibleCommentItems.forEach(item => {
                    const parent = item.parentElement;
                    parentCounts[parent] = (parentCounts[parent] || 0) + 1;
                });
                
                // Find the parent with the most similar children (likely the comment list)
                const bestParent = Object.entries(parentCounts)
                    .sort((a, b) => b[1] - a[1])[0][0];
                
                // Get all direct children of this parent
                commentItems = Array.from(bestParent.children);
                console.log(`Using ${commentItems.length} children of best parent as comment items`);
            }
        }
        
        // Extract data from the identified comment items
        const extractedComments = [];
        for (const item of commentItems) {
            try {
                // Debug info about this comment item
                console.log(`Processing comment item: ${item.className || 'no-class'}, children: ${item.children.length}`);
                
                // Find the username (the link text of the first /@... href)
                const userLink = item.querySelector('a[href^="/@"]');
                const username = userLink ? userLink.textContent.trim() : null;
                
                if (!username) {
                    console.log('No username found in this item, skipping');
                    continue; // Skip if no username found
                }
                
                console.log(`Found username: ${username}`);
                
                // SIMPLIFIED: Just take all text content and remove the username
                const fullText = item.textContent.trim();
                
                // Remove the username and common TikTok UI text
                let commentText = fullText
                    .replace(username, '') // Remove username
                    .replace(/Reply/g, '') // Remove "Reply" text
                    .replace(/Report/g, '') // Remove "Report" text
                    .replace(/Like/g, '') // Remove "Like" text
                    .replace(/View replies/g, '') // Remove "View replies" text
                    .replace(/\d+[smhdwy] ago/g, '') // Remove timestamp pattern
                    .replace(/\s{2,}/g, ' ') // Replace multiple spaces with single space
                    .trim();
                
                // Detect if text is still reasonable length
                if (commentText.length < 1) {
                    console.log('Extracted comment text is empty, trying alternative method');
                    
                    // Try again but look for paragraph elements specifically
                    const paragraphs = Array.from(item.querySelectorAll('p, span'))
                        .filter(el => el.textContent.trim().length > 0 && 
                                     !el.textContent.includes(username) &&
                                     el.textContent.length > 4);
                        
                    if (paragraphs.length > 0) {
                        // Sort by length and take the longest one
                        paragraphs.sort((a, b) => b.textContent.length - a.textContent.length);
                        commentText = paragraphs[0].textContent.trim();
                        console.log(`Found text using paragraph method: ${commentText.substring(0, 30)}...`);
                    } else {
                        console.log('Failed to find comment text with paragraph method too');
                    }
                } else {
                    console.log(`Extracted comment text: ${commentText.substring(0, 30)}...`);
                }
                
                // Try to find a timestamp
                let timeStamp = null;
                // Timestamps often contain "ago", "hours", "minutes", etc.
                const timePatterns = ['ago', 'hour', 'minute', 'day', 'week', 'month', 'year'];
                const timeNodes = Array.from(item.querySelectorAll('*'))
                    .filter(el => {
                        const text = el.textContent.trim();
                        return timePatterns.some(pattern => text.includes(pattern)) &&
                               text.length < 20; // Timestamps are usually short
                    });
                
                if (timeNodes.length > 0) {
                    timeStamp = timeNodes[0].textContent.trim();
                    console.log(`Found timestamp: ${timeStamp}`);
                }
                
                // Check if we have a non-empty comment text
                if (username && commentText && commentText.length > 0) {
                    extractedComments.push({ username, commentText, timeStamp });
                    console.log('Successfully added comment to results');
                } else {
                    console.log('Skipping comment due to missing text');
                }
            } catch (err) {
                console.log("Error extracting a comment:", err);
            }
        }
        
        console.log(`Extracted ${extractedComments.length} comments with new method`);
        return extractedComments;
    }
    
    const container = getCommentContainer();
    if (container) {
        let previousCount = 0;
        let stableCount = 0;
        // Loop up to 20 iterations (or break if no new comments are loaded for several iterations)
        for (let attempt = 0; attempt < 20; attempt++) {
            console.log(`Scroll attempt ${attempt+1}/20`);
            
            // Scroll multiple times in one attempt to ensure full coverage
            for (let scrolls = 0; scrolls < 3; scrolls++) {
                await scrollCommentContainer(container);
                await delay(1000);  // Short delay between scrolls
            }
            
            // Click any "view more" or "load more" buttons
            await clickViewMoreAtBottom(container);
            
            // Wait a bit longer to ensure loading completes
            await delay(3000 + Math.random() * 2000);
            
            const currentItems = await extractItems();
            const currentCount = currentItems.length;
            
            console.log(`Found ${currentCount} comments after attempt ${attempt+1} (previously: ${previousCount})`);
            
            // Check if we've reached our 10% target
            if (targetCommentCount > 0 && currentCount >= targetCommentCount) {
                console.log(`Reached target of ${targetCommentCount} comments (10% of ${totalCommentCount}). Stopping.`);
                break;
            }
            
            if (currentCount > previousCount) {
                console.log(`Progress: +${currentCount - previousCount} new comments loaded`);
                previousCount = currentCount;
                stableCount = 0;
            } else {
                stableCount++;
                console.log(`No new comments found for ${stableCount} consecutive attempts`);
                if (stableCount >= 3) {
                    console.log("Comment count stable for 3 attempts, stopping scrolling");
                    break;
                }
            }
            
            // Sometimes TikTok only loads comments after significant pause
            // This longer delay helps with that
            await delay(5000 + Math.random() * 3000);
        }
    } else {
        console.log("No comment container found on this page");
    }
    
    // Try both extraction methods and combine results
    console.log("Performing final comment extraction...");
    const standardExtractedItems = await extractItems();
    console.log(`Standard method found ${standardExtractedItems.length} comments`);
    
    const newMethodExtractedItems = await extractCommentsNewMethod();
    console.log(`New method found ${newMethodExtractedItems.length} comments`);
    
    // Combine results and remove duplicates
    const combinedComments = [...standardExtractedItems];
    
    // Only add comments from the new method that aren't already in the standard results
    for (const newComment of newMethodExtractedItems) {
        const isDuplicate = standardExtractedItems.some(existing => 
            existing.username === newComment.username && 
            existing.commentText === newComment.commentText
        );
        
        if (!isDuplicate) {
            combinedComments.push(newComment);
        }
    }
    
    console.log(`Final combined results: ${combinedComments.length} comments`);
    return combinedComments;
})();
"""

# -----------------------------
# Main Asynchronous Function
# -----------------------------

async def main(record_mode=False):
    # Create folder structure for comments and logs
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
    
    # Close any running Edge instances
    close_edge_tasks()
    time.sleep(2)

    # Define your Edge profile path (adjust this to your system)
    profile_path = r"C:\Users\DELL\AppData\Local\Microsoft\Edge\User Data\Profile 2"
    user_data_dir = os.path.dirname(profile_path)
    profile_directory = os.path.basename(profile_path)

    # Locate the Edge executable (update paths if necessary)
    msedge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(msedge_path):
        msedge_path = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(msedge_path):
        print("[ERROR] Edge executable not found on system.")
        return

    # Use the TikTok homepage as the initial page
    tiktok_url = "https://www.tiktok.com"

    # Command to launch Edge with remote debugging enabled
    cmd = [
        msedge_path,
        f"--profile-directory={profile_directory}",
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        tiktok_url
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

        # --- BEGIN MOBILE EMULATION --- 
        # Mobile emulation removed as requested
        # --- END MOBILE EMULATION ---

        await page.wait_for_load_state("networkidle", timeout=15000)
        print("[INFO] Connected to Edge via CDP. Current URL =", page.url)

        # Handle record mode if selected
        if record_mode:
            print("\n" + "="*80)
            print("RECORDER ACTIVE: Please manually click the comment button when you're ready".center(80))
            print("="*80)
            print("\nSTEP 1: Navigate to a TikTok video with comments")
            print("STEP 2: When you see the video, click the COMMENT BUTTON once")
            print("STEP 3: Wait for confirmation here before doing anything else")
            print("\nThe script is now waiting for your click... (you have 2 minutes)")
            print("WAITING FOR BUTTON PRESS...".center(80, "-"))
            
            # Wait for user to manually click the comment button
            element_info = await record_button_click(page)
            
            if element_info:
                # Save the element information for future use
                if save_element_info(element_info, folder_path=logs_folder):
                    print("\n" + "="*80)
                    print("SUCCESS: Recording completed!".center(80))
                    print("="*80)
                    saved_path = os.path.join(logs_folder, "recorded_element.json")
                    print(f"\nThe element information has been saved to '{saved_path}'")
                    print("Run the script without --record next time to use this information.")
                    print("\nExiting recording mode...")
                else:
                    print("\n" + "="*80)
                    print("ERROR: Failed to save element information".center(80))
                    print("="*80)
            else:
                print("\n" + "="*80)
                print("ERROR: No button click was recorded".center(80))
                print("="*80)
                print("\nPossible reasons:")
                print("- You didn't click any button within the time limit")
                print("- The clicked element wasn't properly detected")
                print("\nPlease try again with the --record flag.")
            
            # Make it clear we're exiting
            print("\nRecording session complete. Closing browser...")
            time.sleep(3)  # Give the user time to read the message
            
            # Close browser and exit
            await browser.close()
            proc.terminate()
            return  # This explicit return ensures the script doesn't continue

        # Load recorded element information if available
        element_info = load_element_info(folder_path=logs_folder)
        comment_button_selector = None
        
        if element_info:
            # Use the recorded CSS selector as primary, with XPath as fallback
            comment_button_selector = element_info.get("css_selector")
            print(f"[INFO] Using recorded comment button selector: {comment_button_selector}")
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

        num_videos = 5  # Number of videos to process
        for i in range(num_videos):
            print(f"\n[INFO] Processing video {i+1} of {num_videos}")
            await page.wait_for_load_state("networkidle", timeout=15000)

            # --- BEGIN REVISED ADDITION ---
            print("[INFO] Checking if comment section is open...")
            # Selectors for the comment container
            comment_container_selector = ".css-7whb78-DivCommentListContainer, .css-1qp5gj2-DivCommentListContainer, .css-13wx63w-DivCommentObjectWrapper, div[class*=\"-DivCommentListContainer\"]" # Added general class pattern

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
                                except Exception as click_wait_err:
                                    print(f"[ERROR] Failed to click comment button or section did not appear after click: {click_wait_err}")
                            else:
                                # Check if it's disabled because the container *did* load while we were checking
                                try:
                                    await page.locator(comment_container_selector).first.wait_for(state="visible", timeout=1000)
                                    print("[INFO] Comment button was disabled, but comment section loaded anyway.")
                                except Exception:
                                    print("[WARN] Comment button found but is disabled, and comment section didn't load. Comments might be disabled for this video.")
                        except Exception as find_err:
                            print(f"[ERROR] Could not find or interact with the comment button: {find_err}. Maybe comments are disabled or the selector needs update.")

            except Exception as e:
                # Catch potential errors during the check/click process
                print(f"[WARN] Unexpected error during comment section check/open: {e}. Proceeding to scrape anyway...")
            # --- END REVISED ADDITION ---

            # Execute the scraping script to extract (and reveal more) comments
            try:
                print("\n[INFO] Executing comment scraping script...")
                
                # Add a console log handler to capture browser console logs
                logs = []
                page.on("console", lambda msg: logs.append(f"[BROWSER] {msg.type}: {msg.text}"))
                
                # Execute the scraping script with a longer timeout
                comments = await page.evaluate(scrape_js)  # Remove the timeout parameter
                
                # Print the captured logs to help diagnose issues
                if logs:
                    print("\n--- BROWSER CONSOLE LOGS ---")
                    # Only print the most relevant logs (limit to 50 to avoid overwhelming output)
                    for log in logs[-50:]:
                        print(log)
                    print("--- END BROWSER CONSOLE LOGS ---\n")
                
                # Extract total comment count from logs
                total_comment_count = 0
                for log in logs:
                    if "Total comment count detected:" in log:
                        try:
                            total_comment_count = int(log.split("Total comment count detected:")[1].strip())
                            print(f"[INFO] Total comment count detected: {total_comment_count}")
                            break
                        except:
                            pass
                
                # Verify we got a valid result
                if not comments:
                    print("[WARN] No comments returned from scraping script")
                elif not isinstance(comments, list):
                    print(f"[WARN] Expected a list of comments but got: {type(comments).__name__}")
                else:
                    print(f"[INFO] Scraped {len(comments)} comments from video {i+1}")
                
                # Save the scraped comments to a JSON file in the comments folder
                json_filename = f"scraped_comments_video_{i+1}.json"
                store_comments_to_json(comments, json_filename, total_comment_count, comments_folder)
                
                # Add a small diagnostic info file with more technical details in the logs folder
                try:
                    diagnostic_info = {
                        "url": page.url,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "video_number": i+1,
                        "comment_count": len(comments) if isinstance(comments, list) else 0,
                        "total_comment_count": total_comment_count,
                        "percentage_scraped": round((len(comments) / total_comment_count) * 100, 2) if total_comment_count > 0 else 0,
                        "console_logs": logs[-20:] if logs else [],  # Last 20 logs
                    }
                    
                    diagnostic_filename = os.path.join(logs_folder, f"diagnostic_{i+1}.json")
                    with open(diagnostic_filename, "w", encoding="utf-8") as f:
                        json.dump(diagnostic_info, f, ensure_ascii=False, indent=2)
                    print(f"[INFO] Saved diagnostic information to {diagnostic_filename}")
                except Exception as diag_err:
                    print(f"[WARN] Failed to save diagnostic info: {diag_err}")
                
            except Exception as e:
                print(f"[ERROR] Failed to execute comment scraping script: {e}")
                # Try to save any console logs that might help diagnose the issue
                if logs:
                    try:
                        error_log_filename = os.path.join(logs_folder, f"error_logs_video_{i+1}.txt")
                        with open(error_log_filename, "w", encoding="utf-8") as f:
                            f.write("\n".join(logs))
                        print(f"[INFO] Saved error logs to {error_log_filename}")
                    except Exception as log_err:
                        print(f"[ERROR] Failed to save error logs: {log_err}")
                        pass

            # Use the helper function to click the next video button (based on vertical position)
            success = await click_next_video(page, button_selector)
            if not success:
                print(f"[ERROR] Could not navigate to next video from video {i+1}.")
                break

            await asyncio.sleep(5)

        await browser.close()
        print("[INFO] Browser closed; process complete.")
    proc.terminate()

# -----------------------------
# Main Execution
# -----------------------------

if __name__ == "__main__":
    # Check if the script was run with the --record flag
    record_mode = "--record" in sys.argv
    
    if record_mode:
        print("\n" + "="*50)
        print("RUNNING IN RECORDING MODE")
        print("="*50)
        print("\nIn this mode, you will manually click the comment button,")
        print("and the script will record information about that element")
        print("for future automated use.")
        print("\nPress Ctrl+C now if you want to cancel.")
        print("Otherwise, the browser will launch in 5 seconds...")
        time.sleep(5)
    
    asyncio.run(main(record_mode=record_mode))
