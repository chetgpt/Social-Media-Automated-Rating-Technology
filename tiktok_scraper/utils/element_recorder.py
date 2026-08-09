"""Utilities for recording UI elements in TikTok."""
import asyncio
import json
import os
import time
from datetime import datetime

async def record_button_click(page, element_type="comment_button"):
    """
    Records information about an element that is clicked by the user.
    
    Args:
        page: The Playwright page object
        element_type: Type of element being recorded (e.g., "comment_button" or "comment_count")
        
    Returns:
        dict: Information about the clicked element or None if no click was detected
    """
    # Create a promise that resolves when an element is clicked
    element_info = {}
    
    # Set up an event listener for clicks and extract element information
    click_detected = asyncio.Future()
    
    async def handle_click(e):
        try:
            element_info.update({
                "tag_name": await e.evaluate("el => el.tagName.toLowerCase()"),
                "element_type": element_type,
                "id": await e.evaluate("el => el.id"),
                "class_name": await e.evaluate("el => el.className"),
                "text_content": await e.evaluate("el => el.textContent?.trim()"),
                "inner_text": await e.evaluate("el => el.innerText?.trim()"),
                "css_selector": await e.evaluate("""el => {
                    let path = [];
                    while (el && el.nodeType === Node.ELEMENT_NODE) {
                        let selector = el.nodeName.toLowerCase();
                        if (el.id) {
                            selector += '#' + el.id;
                            path.unshift(selector);
                            break;
                        } else {
                            let sibling = el, nth = 1;
                            while (sibling.previousElementSibling) {
                                sibling = sibling.previousElementSibling;
                                if (sibling.nodeName.toLowerCase() === selector) nth++;
                            }
                            if (nth > 1) selector += `:nth-of-type(${nth})`;
                        }
                        path.unshift(selector);
                        el = el.parentNode;
                    }
                    return path.join(' > ');
                }"""),
                "xpath": await e.evaluate("""el => {
                    const getXPath = function(element) {
                        if (element.id !== '') 
                            return `//*[@id="${element.id}"]`;
                        
                        if (element === document.body) 
                            return '/html/body';
                            
                        let ix = 0;
                        const siblings = element.parentNode.childNodes;
                        
                        for (let i = 0; i < siblings.length; i++) {
                            const sibling = siblings[i];
                            if (sibling === element)
                                return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                            if (sibling.nodeType === 1 && sibling.tagName === element.tagName)
                                ix++;
                        }
                    };
                    return getXPath(el);
                }"""),
                "attributes": await e.evaluate("""el => {
                    const attrObj = {};
                    for (let i = 0; i < el.attributes.length; i++) {
                        const attr = el.attributes[i];
                        attrObj[attr.name] = attr.value;
                    }
                    return attrObj;
                }"""),
                "timestamp": datetime.now().isoformat()
            })
            
            # Mark as a comment button or count based on content and attributes
            tag_name = element_info.get("tag_name", "").lower()
            inner_text = element_info.get("inner_text", "").lower()
            attributes = element_info.get("attributes", {})
            
            # Override element_type based on element characteristics
            if "comment" in inner_text and any(char.isdigit() for char in inner_text):
                element_info["element_type"] = "comment_count"
            elif tag_name == "span" and "data-e2e" in attributes and "comment" in attributes.get("data-e2e", ""):
                element_info["element_type"] = "comment_button"
            
            click_detected.set_result(True)
            
        except Exception as e:
            print(f"Error capturing element info: {e}")
            click_detected.set_result(False)
    
    # Add event listener for clicks
    await page.evaluate("""() => {
        window.__clickedElement = null;
        document.addEventListener('click', function(e) {
            window.__clickedElement = e.target;
        }, true);
    }""")
    
    try:
        # Wait for a click event (timeout after 120 seconds)
        print(f"Waiting for user to click the {element_type.replace('_', ' ')}...")
        await asyncio.wait_for(asyncio.sleep(0.5), timeout=120)
        
        # Check if an element was clicked
        clicked = await page.evaluate("""() => {
            const el = window.__clickedElement;
            if (el) {
                window.__clickedElement = null;
                return true;
            }
            return false;
        }""")
        
        if clicked:
            el = await page.evaluate_handle("document.activeElement")
            await handle_click(el)
            await asyncio.wait_for(click_detected, timeout=5)
            print(f"Element clicked! Recorded information for {element_info.get('element_type', element_type)}")
            return element_info
        else:
            print("No click detected within the timeout period.")
            return None
            
    except asyncio.TimeoutError:
        print("Timeout waiting for button click.")
        return None
    except Exception as e:
        print(f"Error in record_button_click: {e}")
        return None

async def record_elements(page, folders):
    """
    Record both the comment button and comment count elements.
    
    Args:
        page: The Playwright page object
        folders: Dictionary containing folder paths
        
    Returns:
        bool: True if successful, False otherwise
    """
    recorded_elements = {}
    
    # First, record the comment button
    print("\n" + "-"*80)
    print("STEP 1: Please click on the COMMENT BUTTON".center(80))
    print("(The button that opens the comment section)".center(80))
    print("-"*80)
    
    button_info = await record_button_click(page, "comment_button")
    if not button_info:
        print("Failed to record comment button.")
        return False
    
    recorded_elements["comment_button"] = button_info
    
    # Ask the user if they want to record the comment count element
    print("\n" + "-"*80)
    print("STEP 2: Now click on the COMMENT COUNT display".center(80))
    print("(The text showing the number of comments)".center(80))
    print("-"*80)
    
    count_info = await record_button_click(page, "comment_count")
    if not count_info:
        print("Warning: Failed to record comment count element.")
        print("The scraper will still work, but may not be able to accurately report total comment counts.")
    else:
        recorded_elements["comment_count"] = count_info
    
    # Save the recorded elements to a file
    try:
        logs_folder = folders["logs"]
        output_path = os.path.join(logs_folder, "recorded_elements.json")
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(recorded_elements, f, indent=2)
        
        print(f"\nRecorded elements saved to {output_path}")
        return True
    except Exception as e:
        print(f"Error saving recorded elements: {e}")
        return False 