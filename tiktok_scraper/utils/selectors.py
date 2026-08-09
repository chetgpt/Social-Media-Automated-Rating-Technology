"""Helper functions for TikTok selectors and elements."""
import json
import os
import re

def get_selectors(logs_folder):
    """
    Get TikTok selectors from configuration.
    
    Args:
        logs_folder (str): Path to logs folder where recorded_elements.json might be stored
        
    Returns:
        dict: Dictionary of selectors for the TikTok site
    """
    # Default selectors
    selectors = {
        "comment_button": "button[data-e2e='comment-icon']",
        "video_cards": "[data-e2e='recommend-list-item-container']",
        "comment_text": "[data-e2e='comment-text']",
        "comment_list": "[data-e2e='comment-list']",
        "comment_items": "[data-e2e='comment-level-1']",
        "reply_items": "[data-e2e='comment-level-2']",
        "load_more": "button[data-e2e='view-more-1'] span",
        "username": "[data-e2e='comment-username-1']",
        "like_count": "[data-e2e='comment-like-count']",
        "post_time": "[data-e2e='comment-time-1']",
        "replies_button": "[data-e2e='view-more-1'] span",
        "close_button": "[data-e2e='comment-close']",
        "reply_username": "[data-e2e='comment-username-2']",
        "reply_text": "[data-e2e='comment-text']",
        "reply_like_count": "[data-e2e='comment-like-count']",
        "reply_post_time": "[data-e2e='comment-time-2']",
    }
    
    # Try to load recorded elements
    try:
        elements_file = os.path.join(logs_folder, "recorded_elements.json")
        if os.path.exists(elements_file):
            with open(elements_file, "r", encoding="utf-8") as f:
                recorded_elements = json.load(f)
                
            if "comment_button" in recorded_elements:
                button_data = recorded_elements["comment_button"]
                
                # Get the selector based on priority
                # 1. Try CSS selector first
                if "css_selector" in button_data and button_data["css_selector"]:
                    selectors["comment_button"] = button_data["css_selector"]
                    
                # 2. Try data-e2e attribute if available (TikTok specific)
                elif "attributes" in button_data and "data-e2e" in button_data["attributes"]:
                    selectors["comment_button"] = f"[data-e2e='{button_data['attributes']['data-e2e']}']"
                    
                # 3. Try XPath as fallback
                elif "xpath" in button_data and button_data["xpath"]:
                    selectors["comment_button_xpath"] = button_data["xpath"]
    except Exception as e:
        print(f"Error loading recorded elements: {e}")
    
    return selectors

def extract_comment_count_from_page(page, logs_folder=None):
    """
    Extract the total comment count from a TikTok video page.
    
    Args:
        page: The Playwright page object
        logs_folder: Path to logs folder where recorded elements might be stored
        
    Returns:
        int: The total comment count, or 0 if it couldn't be found
    """
    # Try to extract from recorded elements first
    try:
        if logs_folder:
            elements_file = os.path.join(logs_folder, "recorded_elements.json")
            if os.path.exists(elements_file):
                with open(elements_file, "r", encoding="utf-8") as f:
                    recorded_elements = json.load(f)
                
                if "comment_count" in recorded_elements:
                    count_data = recorded_elements["comment_count"]
                    
                    # Try to extract using CSS selector if available
                    if "css_selector" in count_data and count_data["css_selector"]:
                        selector = count_data["css_selector"]
                        try:
                            text_content = page.locator(selector).inner_text()
                            # Extract digits from the text
                            digits = re.findall(r'\d+', text_content)
                            if digits:
                                return int(digits[0])
                        except:
                            pass
                            
                    # If that fails, try using the XPath
                    if "xpath" in count_data and count_data["xpath"]:
                        try:
                            text_content = page.locator(f"xpath={count_data['xpath']}").inner_text()
                            digits = re.findall(r'\d+', text_content)
                            if digits:
                                return int(digits[0])
                        except:
                            pass
    except Exception as e:
        print(f"Error extracting comment count from recorded elements: {e}")
    
    # Fallback to common patterns
    try:
        # Try common selectors for comment counts
        selectors = [
            "[data-e2e='comment-count']",
            "strong[data-e2e='comment-count']",
            "button[data-e2e='comment-icon'] + span",
            "button[data-e2e='comment-icon'] ~ span",
            "button[data-e2e='comment-icon'] span",
            "text=Comments ("
        ]
        
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    text_content = page.locator(selector).first.inner_text()
                    # Extract digits from text like "Comments (123)"
                    digits = re.findall(r'\d+', text_content)
                    if digits:
                        return int(digits[0])
            except:
                continue
                
        # Try looking for text that contains "Comments" and numbers
        try:
            comment_elements = page.locator("text=Comments").all()
            for element in comment_elements:
                text = element.inner_text()
                digits = re.findall(r'\d+', text)
                if digits:
                    return int(digits[0])
        except:
            pass
            
    except Exception as e:
        print(f"Error extracting comment count using fallback methods: {e}")
    
    return 0  # Default to 0 if we couldn't find the count 