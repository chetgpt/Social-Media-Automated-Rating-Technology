"""Navigation-related functions for TikTok scraping."""
import asyncio

async def navigate_to_url(page, url):
    """Navigates to a specific URL and waits for it to load.
    
    Args:
        page: The Playwright page object
        url: The URL to navigate to
        
    Returns:
        bool: True if navigation was successful, False otherwise
    """
    try:
        print(f"[INFO] Navigating to: {url}")
        
        # Navigate to the URL and wait for the page to load
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Wait for additional content to load
        await page.wait_for_timeout(5000)
        
        # Check if we ended up on the right page
        current_url = page.url
        
        if current_url != url and not url.startswith(current_url) and not current_url.startswith(url):
            print(f"[WARN] Navigation may have been redirected: {current_url}")
            
            # If we've been redirected to a login page or similar, try once more
            if "login" in current_url.lower() or "signup" in current_url.lower():
                print("[INFO] Detected potential login page. Trying navigation again...")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
        
        print(f"[INFO] Successfully navigated to: {page.url}")
        return True
        
    except Exception as e:
        print(f"[ERROR] Failed to navigate to {url}: {e}")
        return False

async def click_next_video(page, selector):
    """Finds and clicks the next video button.
    
    Args:
        page: The Playwright page object
        selector: CSS selector for the navigation buttons
        
    Returns:
        bool: True if navigation was successful, False otherwise
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