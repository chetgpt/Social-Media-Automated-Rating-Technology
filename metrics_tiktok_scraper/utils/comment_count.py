"""Module for extracting total comment counts from TikTok videos."""
import re
import asyncio
from .file_utils import load_recorded_elements

async def extract_total_comment_count(page, recorded_elements=None):
    """Extracts the total comment count from the page using multiple approaches.

    Args:
        page: The Playwright page object
        recorded_elements: Dictionary of recorded element information

    Returns:
        int: Estimated total comment count, or 0 if unknown
    """
    try:
        # Check if we have recorded element information for the comment count
        if recorded_elements and "comment_count" in recorded_elements:
            print("[INFO] Using recorded element information for comment count")

            comment_count_selector = recorded_elements["comment_count"]["css_selector"]
            comment_count_element = page.locator(comment_count_selector)

            # Check if the element exists
            if await comment_count_element.count() > 0:
                # Get the element's text content
                count_text = await comment_count_element.text_content()

                # Try to extract a number from the text
                if count_text:
                    # Remove any non-numeric characters except K, M, and decimal points
                    cleaned_text = re.sub(r'[^0-9KkMm.]', '', count_text)

                    if cleaned_text:
                        # Convert K (thousands) and M (millions) to their numeric values
                        if 'K' in cleaned_text or 'k' in cleaned_text:
                            cleaned_text = cleaned_text.lower().replace('k', '')
                            try:
                                return int(float(cleaned_text) * 1000)
                            except ValueError:
                                pass
                        elif 'M' in cleaned_text or 'm' in cleaned_text:
                            cleaned_text = cleaned_text.lower().replace('m', '')
                            try:
                                return int(float(cleaned_text) * 1000000)
                            except ValueError:
                                pass
                        else:
                            try:
                                return int(float(cleaned_text))
                            except ValueError:
                                pass

        # If we can't use the recorded element, try using JavaScript
        print("[INFO] No recorded elements available, using JavaScript fallback")

        # Add additional debugging
        print("[DEBUG] Debug complete")

        # Check if we're dealing with an iframe
        iframe_check = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            return iframes.length;
        }""")
        print(f"[DEBUG] Iframe check complete")

        # Try multiple approaches to extract the comment count using JavaScript
        count = await page.evaluate("""() => {
            try {
                // Try direct approach - look for the comment count element
                const selectors = [
                    // Direct data-e2e attributes
                    'strong[data-e2e="comment-count"]',
                    'span[data-e2e="comment-count"]',
                    'p[data-e2e="comment-count"]',

                    // Comment labels that include count in text
                    'button[aria-label*="comment"]',
                    'span[aria-label*="comment"]',

                    // Class-based selectors
                    'div[class*="CommentIconWrapper"] + strong',
                    'span[data-e2e="comment-icon"] + strong',

                    // Generic number pattern next to comment icon
                    '.comment-count',
                    '[class*="comment"] strong',
                    '[class*="Comment"] strong'
                ];

                // Try each selector
                for (const selector of selectors) {
                    const elements = document.querySelectorAll(selector);
                    if (elements.length > 0) {
                        // Get text content of the first matching element
                        const text = elements[0].textContent.trim();

                        // Check if it has numbers
                        if (/\\d/.test(text)) {
                            console.log(`Found comment count element with text: ${text}`);

                            // Extract number
                            const numericText = text.replace(/[^0-9KkMm.]/g, '');

                            // Convert to number, handling K and M suffixes
                            if (/[Kk]/.test(numericText)) {
                                return Math.round(parseFloat(numericText.replace(/[Kk]/, '')) * 1000);
                            } else if (/[Mm]/.test(numericText)) {
                                return Math.round(parseFloat(numericText.replace(/[Mm]/, '')) * 1000000);
                            } else {
                                return parseInt(numericText);
                            }
                        }
                    }
                }

                // If all selectors fail, try looking for any ActionItem element that might contain the count
                const actionItems = document.querySelectorAll('div[class*="ActionItem"], button[class*="action-item"]');
                for (let i = 0; i < actionItems.length; i++) {
                    const item = actionItems[i];
                    const text = item.textContent.trim();

                    // If it might be a comment icon/button (contains numbers and no obvious "like" related text)
                    if (/\\d/.test(text) &&
                        !text.toLowerCase().includes('like') &&
                        item.querySelector('svg')) {

                        // Get the numeric part
                        const numericText = text.replace(/[^0-9KkMm.]/g, '');
                        console.log(`Found possible comment count in action item: ${numericText}`);

                        // Convert to number with K/M handling
                        if (/[Kk]/.test(numericText)) {
                            return Math.round(parseFloat(numericText.replace(/[Kk]/, '')) * 1000);
                        } else if (/[Mm]/.test(numericText)) {
                            return Math.round(parseFloat(numericText.replace(/[Mm]/, '')) * 1000000);
                        } else {
                            return parseInt(numericText);
                        }
                    }
                }

                // Fallback: check for the comments container and count the actual elements
                const commentContainers = document.querySelectorAll('div[class*="CommentList"], div[class*="comment-list"]');
                if (commentContainers.length > 0) {
                    const commentElements = commentContainers[0].querySelectorAll('div[class*="CommentItem"], div[class*="comment-item"]');
                    return commentElements.length;
                }

                // If all else fails, return 0
                return 0;
            } catch (error) {
                console.error("Error extracting comment count:", error);
                return 0;
            }
        }""")

        if count > 0:
            print(f"[INFO] Got total comment count from JavaScript: {count}")
            return count

        # If all approaches fail, return 0
        print("[WARN] Could not determine total comment count, using 0")
        return 0

    except Exception as e:
        print(f"[ERROR] Failed to extract comment count: {e}")
        return 0

def convert_count_text(count_text):
    """
    Converts a count text (which might include K, M, B suffixes) to an integer.

    Args:
        count_text: String representation of a count, possibly with suffix

    Returns:
        int: The converted count
    """
    # Handle K, M, B suffixes
    if any(suffix in count_text for suffix in ['K', 'k']):
        return int(float(re.sub('[Kk]', '', count_text)) * 1000)
    elif any(suffix in count_text for suffix in ['M', 'm']):
        return int(float(re.sub('[Mm]', '', count_text)) * 1000000)
    elif any(suffix in count_text for suffix in ['B', 'b']):
        return int(float(re.sub('[Bb]', '', count_text)) * 1000000000)
    else:
        # Remove any commas or periods in the number
        return int(re.sub(r'[,.]', '', count_text))

async def extract_count_with_js(page):
    """
    Extracts the total comment count using JavaScript directly in the page.

    Args:
        page: The Playwright page object

    Returns:
        int: The total comment count, or 0 if not found
    """
    # First, let's add a debug script to print ALL strong elements on the page
    debug_script = """
    () => {
        console.log("==== DEBUGGING STRONG ELEMENTS ====");
        const allStrongs = document.querySelectorAll('strong');
        console.log(`Found ${allStrongs.length} <strong> elements on the page`);

        for (let i = 0; i < allStrongs.length; i++) {
            const el = allStrongs[i];
            console.log(`Strong #${i+1}:`);
            console.log(`  Text content: "${el.textContent}"`);
            console.log(`  Class: "${el.className}"`);
            console.log(`  data-e2e: "${el.getAttribute('data-e2e')}"`);
            console.log(`  Parent tag: ${el.parentElement ? el.parentElement.tagName : 'none'}`);
            console.log(`  Parent class: "${el.parentElement ? el.parentElement.className : 'none'}"`);
            console.log(`  HTML: ${el.outerHTML}`);
        }

        // Also check for any elements containing "Comments"
        const commentElements = Array.from(document.querySelectorAll('*')).filter(
            el => el.textContent.includes('Comment') || el.textContent.includes('comment')
        );
        console.log(`Found ${commentElements.length} elements containing "Comment" text`);

        // Print the first 5
        commentElements.slice(0, 5).forEach((el, i) => {
            console.log(`Comment-containing element #${i+1}:`);
            console.log(`  Text: "${el.textContent}"`);
            console.log(`  Tag: ${el.tagName}`);
            console.log(`  Class: "${el.className}"`);
            console.log(`  data-e2e: "${el.getAttribute('data-e2e')}"`);
        });

        return "Debug complete";
    }
    """

    # Run the debug script and print results
    try:
        debug_result = await page.evaluate(debug_script)
        print(f"[DEBUG] {debug_result}")
    except Exception as e:
        print(f"[DEBUG] Error running debug script: {e}")

    # Now check for iframes that might contain our element
    iframe_debug = """
    () => {
        const iframes = document.querySelectorAll('iframe');
        console.log(`Found ${iframes.length} iframes on the page`);

        // Check if any iframes are immediately accessible
        for (let i = 0; i < iframes.length; i++) {
            try {
                console.log(`Iframe #${i+1} src: ${iframes[i].src}`);
                // Try to access the iframe content - this may fail due to same-origin policy
                try {
                    const doc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                    console.log(`Can access iframe #${i+1} content`);

                    // Check for strong elements inside the iframe
                    const iframeStrongs = doc.querySelectorAll('strong');
                    console.log(`Found ${iframeStrongs.length} strong elements in iframe #${i+1}`);

                    // Look for browse-comment-count specifically
                    const commentCount = doc.querySelector('strong[data-e2e="browse-comment-count"]');
                    if (commentCount) {
                        console.log(`Found browse-comment-count in iframe: ${commentCount.textContent}`);
                    }
                } catch (e) {
                    console.log(`Cannot access iframe #${i+1} content due to same-origin policy`);
                }
            } catch (e) {
                console.log(`Error inspecting iframe #${i+1}: ${e.message}`);
            }
        }
        return "Iframe check complete";
    }
    """

    try:
        iframe_result = await page.evaluate(iframe_debug)
        print(f"[DEBUG] {iframe_result}")
    except Exception as e:
        print(f"[DEBUG] Error checking iframes: {e}")

    # JavaScript code to find the comment count using various selectors
    script = """
    () => {
        function detectTotalCommentCount() {
            // First check specifically for the exact element the user mentioned
            const exactElement = document.querySelector('strong[data-e2e="browse-comment-count"]');
            if (exactElement) {
                const text = exactElement.textContent.trim();
                console.log(`Found exact browse-comment-count element with text: ${text}`);

                // This element likely contains just the raw number
                if (/^\\d+$/.test(text)) {
                    return parseInt(text);
                }
            } else {
                console.log("Could not find the exact strong[data-e2e='browse-comment-count'] element");

                // Check for similar elements
                const similarElements = document.querySelectorAll('strong[data-e2e*="comment"]');
                console.log(`Found ${similarElements.length} strong elements with data-e2e containing "comment"`);

                // Log details of any found elements
                for (let i = 0; i < similarElements.length; i++) {
                    console.log(`Similar element #${i+1}:`);
                    console.log(`  Text: "${similarElements[i].textContent}"`);
                    console.log(`  data-e2e: "${similarElements[i].getAttribute('data-e2e')}"`);
                }
            }

            // Try different selectors for comment count
            const selectors = [
                'strong[data-e2e="comment-count"]',
                'strong[data-e2e="browse-comment-count"]',
                'strong[class*="StrongText"]',
                '[class*="comment-count"]',
                'div[class*="CommentCount"]',
                'div[class*="comment-count"]',
                '[data-e2e*="comment-count"]',
                // Adding the specific selector mentioned by the user
                'div[class*="DivTabItem"]',
                'div[class="css-1a6kzpf-DivTabItem e1aa9wve2"]',
                // Additional selectors to check
                'span[class*="CommentCount"]',
                'span[class*="count"]',
                'p[class*="CommentCount"]'
            ];

            for (const selector of selectors) {
                try {
                    const el = document.querySelector(selector);
                    if (el) {
                        const text = el.textContent.trim();
                        console.log(`Found potential comment count element with selector "${selector}" and text: ${text}`);

                        // First check if it's just a raw number
                        if (/^\\d+$/.test(text)) {
                            console.log(`Found raw number: ${text}`);
                            return parseInt(text);
                        }

                        // Then check specifically for the "Comments (123)" format
                        const commentParenMatch = text.match(/Comments\\s*\\((\\d+[,.]?\\d*[KMB]?)\\)/i);
                        if (commentParenMatch) {
                            let countText = commentParenMatch[1];
                            console.log(`Found count in "Comments (n)" format: ${countText}`);

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

                        // Then try general number extraction for other formats
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
                    } else {
                        console.log(`No element found with selector: ${selector}`);
                    }
                } catch (e) {
                    console.log(`Error with selector ${selector}: ${e}`);
                }
            }

            // More aggressive search - look at all divs
            console.log("Standard selectors failed, trying more aggressive search for 'Comments (n)' format");

            // Check all div elements for the "Comments (123)" format
            const allElements = document.querySelectorAll('div');
            for (const el of allElements) {
                const text = el.textContent.trim();
                if (text.includes('Comments (')) {
                    console.log(`Found potential element with text: ${text}`);
                    const match = text.match(/Comments\\s*\\((\\d+[,.]?\\d*[KMB]?)\\)/i);
                    if (match) {
                        let countText = match[1];
                        console.log(`Extracted count from aggressive search: ${countText}`);

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

            // Last resort: try to find any numbers on the page that might be the comment count
            console.log("Trying to find any numbers that might be the comment count");
            const allNumberElements = Array.from(document.querySelectorAll('*')).filter(
                el => /^\\d+$/.test(el.textContent.trim()) &&
                      parseInt(el.textContent.trim()) > 10 &&  // Avoid small numbers that are likely not comment counts
                      el.clientWidth > 0 &&  // Element must be visible
                      el.clientHeight > 0
            );

            console.log(`Found ${allNumberElements.length} potential number elements`);

            if (allNumberElements.length > 0) {
                // Sort by value (larger numbers first)
                allNumberElements.sort((a, b) =>
                    parseInt(b.textContent.trim()) - parseInt(a.textContent.trim())
                );

                // Take the largest number that seems reasonable
                const largestNumber = parseInt(allNumberElements[0].textContent.trim());
                console.log(`Largest number found: ${largestNumber}`);

                if (largestNumber > 0 && largestNumber < 1000000) {  // Sanity check
                    return largestNumber;
                }
            }

            return 0;  // Default if no number found
        }

        // Call the function and return the result
        return detectTotalCommentCount();
    }
    """

    try:
        count = await page.evaluate(script)
        print(f"[INFO] Found comment count using JavaScript: {count}")
        return count
    except Exception as e:
        print(f"[ERROR] Failed to extract comment count with JavaScript: {e}")
        return 0
