"""Debug utilities for analyzing TikTok page structure."""
import os
import json
import time

async def save_page_structure(page, folder_path):
    """Saves information about the current page structure for debugging.
    
    Args:
        page: The Playwright page object
        folder_path: Path to save the debug files
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Create screenshots folder if it doesn't exist
        screenshots_dir = os.path.join(folder_path, "screenshots")
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)
        
        # Take a screenshot of the current page
        screenshot_path = os.path.join(screenshots_dir, f"page_structure_{int(time.time())}.png")
        await page.screenshot(path=screenshot_path, full_page=True)
        print(f"[DEBUG] Saved page screenshot to {screenshot_path}")
        
        # Extract key page information using JavaScript
        page_info = await page.evaluate("""() => {
            // Extract information about important elements
            const extractPageStructure = () => {
                const result = {
                    url: window.location.href,
                    title: document.title,
                    timestamp: new Date().toISOString(),
                    video_containers: [],
                    video_links: []
                };
                
                // Check for common TikTok video containers
                const containerSelectors = [
                    'div.css-1uqux2o-DivItemContainerV2',
                    'div[data-e2e="user-post-item"]',
                    'div[class*="DivItemContainer"]'
                ];
                
                containerSelectors.forEach(selector => {
                    const containers = document.querySelectorAll(selector);
                    result.video_containers.push({
                        selector: selector,
                        count: containers.length,
                        examples: Array.from(containers).slice(0, 3).map(el => ({
                            class: el.className,
                            html: el.outerHTML.substring(0, 500) + '...',
                            attributes: Array.from(el.attributes).map(attr => ({
                                name: attr.name,
                                value: attr.value
                            }))
                        }))
                    });
                });
                
                // Find all video links
                const videoLinks = document.querySelectorAll('a[href*="/video/"]');
                result.video_links = Array.from(videoLinks).slice(0, 10).map(link => ({
                    href: link.href,
                    text: link.textContent,
                    parent_class: link.parentElement ? link.parentElement.className : 'none',
                    parent_tag: link.parentElement ? link.parentElement.tagName : 'none'
                }));
                
                // Check for the profile structure
                const profileHeader = document.querySelector('h1[data-e2e="user-title"], h2[data-e2e="user-subtitle"]');
                result.profile_info = {
                    title_found: Boolean(profileHeader),
                    title_text: profileHeader ? profileHeader.textContent : 'Not found'
                };
                
                // Check for key page sections
                result.page_sections = {
                    tab_bar: Boolean(document.querySelector('[data-e2e="tabs-bar"], div[class*="DivProfileHeader"]')),
                    video_grid: Boolean(document.querySelector('div[class*="DivVideoFeedContainer"]')),
                    no_content_message: Boolean(document.querySelector('div[class*="DivNoContent"]'))
                };
                
                return result;
            };
            
            return extractPageStructure();
        }""")
        
        # Save the page information as JSON
        info_path = os.path.join(folder_path, f"page_structure_{int(time.time())}.json")
        with open(info_path, 'w', encoding='utf-8') as f:
            json.dump(page_info, f, indent=2, ensure_ascii=False)
        print(f"[DEBUG] Saved page structure analysis to {info_path}")
        
        return True
    
    except Exception as e:
        print(f"[ERROR] Failed to save page structure information: {e}")
        return False

async def analyze_tiktok_page(page, folder_path):
    """Analyzes a TikTok page and executes multiple approaches to identify videos.
    
    Args:
        page: The Playwright page object
        folder_path: Path to save the debug files
        
    Returns:
        dict: Dictionary with analysis results including potential video URLs
    """
    try:
        print("[DEBUG] Analyzing TikTok page structure...")
        
        # Save a snapshot of the current page
        await save_page_structure(page, folder_path)
        
        # Try multiple selectors to find videos
        analysis_results = await page.evaluate("""() => {
            const results = {
                url: window.location.href,
                selectors_tested: [],
                potential_videos: []
            };
            
            // Test multiple selectors that might contain videos
            const testSelectors = [
                'div.css-1uqux2o-DivItemContainerV2', 
                'div[data-e2e="user-post-item"]',
                'a[href*="/video/"]',
                'div[class*="DivItemContainer"]',
                'div.css-8dx572-DivContainer-StyledDivContainerV2',
                'div.tiktok-x6y88p-DivItemContainerV2'
            ];
            
            testSelectors.forEach(selector => {
                const elements = document.querySelectorAll(selector);
                
                results.selectors_tested.push({
                    selector: selector,
                    count: elements.length,
                    first_element: elements.length > 0 ? 
                        {
                            tag: elements[0].tagName,
                            classes: elements[0].className,
                            id: elements[0].id || 'none'
                        } : null
                });
                
                // If selector found elements, check for video links
                if (elements.length > 0) {
                    elements.forEach(el => {
                        // If the element is a link, check it directly
                        if (el.tagName === 'A' && el.href && el.href.includes('/video/')) {
                            results.potential_videos.push(el.href);
                        } else {
                            // Otherwise look for links inside
                            const links = el.querySelectorAll('a[href*="/video/"]');
                            links.forEach(link => {
                                if (link.href) {
                                    results.potential_videos.push(link.href);
                                }
                            });
                        }
                    });
                }
            });
            
            // Remove duplicates
            results.potential_videos = [...new Set(results.potential_videos)];
            return results;
        }""")
        
        # Save the analysis results
        analysis_path = os.path.join(folder_path, f"page_analysis_{int(time.time())}.json")
        with open(analysis_path, 'w', encoding='utf-8') as f:
            json.dump(analysis_results, f, indent=2, ensure_ascii=False)
            
        print(f"[DEBUG] Found {len(analysis_results.get('potential_videos', []))} potential videos")
        print(f"[DEBUG] Analysis saved to {analysis_path}")
        
        return analysis_results
    
    except Exception as e:
        print(f"[ERROR] Failed to analyze TikTok page: {e}")
        return {"error": str(e), "potential_videos": []} 