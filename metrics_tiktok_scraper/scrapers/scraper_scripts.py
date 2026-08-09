"""JavaScript scripts for TikTok scraping."""

# JavaScript for scraping comments and revealing hidden comments
SCRAPE_COMMENTS_JS = r"""
(async (config = {}) => {
    const delay = (ms) => new Promise(res => setTimeout(res, ms));

    // Get target percentage from config or use default 50%
    const targetPercentage = config.targetPercentage || 50;
    console.log(`Aiming to scrape ${targetPercentage}% of total comments`);

    // Initialize the global variable for comment recovery in case of timeout/error
    window.__ScrapedComments = [];

    // Save comments to global variable for recovery in case of timeout
    const saveCommentsToGlobal = (comments) => {
        if (Array.isArray(comments) && comments.length > 0) {
            console.log(`Saving ${comments.length} comments to global variable for recovery`);
            window.__ScrapedComments = comments;
        }
    };

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
                    console.log("No username found in comment using standard selectors");

                    // Try alternative approaches to find username
                    // Look for avatar elements first - usernames are often close to avatars
                    const avatarElements = w.querySelectorAll('img[class*="avatar"], div[class*="avatar"], span[class*="avatar"]');

                    if (avatarElements.length > 0) {
                        // Try to find text near the avatar
                        const avatar = avatarElements[0];
                        const parentElement = avatar.parentElement;

                        if (parentElement && parentElement.parentElement) {
                            // Look for nearby spans/divs with short text
                            const nearbyTexts = Array.from(parentElement.parentElement.querySelectorAll('span, p, div'))
                                .filter(el => {
                                    const text = el.textContent.trim();
                                    // Usernames are usually short text nodes that don't contain certain phrases
                                    return text.length > 0 && text.length < 30 &&
                                           !text.includes('ago') && !text.includes('Reply') &&
                                           !text.includes('Like');
                                });

                            if (nearbyTexts.length > 0) {
                                // Use the shortest text as likely username
                                nearbyTexts.sort((a, b) => a.textContent.length - b.textContent.length);
                                username = nearbyTexts[0].textContent.trim();
                                console.log(`Found username near avatar: ${username}`);
                            }
                        }
                    }

                    // If that doesn't work, look for elements with class names suggesting usernames
                    if (!username) {
                        const usernameClasses = w.querySelectorAll('[class*="username"], [class*="author"], [class*="UserName"]');
                        if (usernameClasses.length > 0) {
                            username = usernameClasses[0].textContent.trim();
                            console.log(`Found username by class: ${username}`);
                        }
                    }

                    // As a last resort, use a generic name so we can still capture the comment
                    if (!username) {
                        username = "TikTok User";
                        console.log("Using generic username as last resort");
                    }
                }

                console.log(`Found username: ${username}`);

                // SIMPLIFIED: Just take all text content and remove the username
                const fullText = w.textContent.trim();
                commentText = fullText
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
                    const paragraphs = Array.from(w.querySelectorAll('p, span'))
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
                // Timestamps often contain "ago", "hours", "minutes", etc.
                const timePatterns = ['ago', 'hour', 'minute', 'day', 'week', 'month', 'year'];
                const timeNodes = Array.from(w.querySelectorAll('*'))
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
                    items.push({ username, commentText, timeStamp });
                    console.log('Successfully added comment to results');
                } else {
                    console.log('Skipping comment due to missing text');
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
        saveCommentsToGlobal(uniqueComments);
        return uniqueComments;
    }

    // Function to get the total number of comments for the video
    function getTotalCommentCount() {
        console.log("Attempting to detect total comment count...");

        // Try to find the metrics sequence pattern (likes, comments, favorites, shares)
        // This is a common pattern in the TikTok UI where metrics are displayed sequentially
        // Usually in this order: likes (49.2K), comments (729), favorites (5703), shares (1879)
        try {
            // First, look for the metrics in the action bar (bottom section with numbers)
            const actionBar = document.querySelector('section[class*="ActionBar"], div[class*="action-bar"], div[class*="ActionItem"]');
            if (actionBar) {
                console.log("Found action bar with metrics");

                // Find all strong elements with numbers in them
                const metricElements = actionBar.querySelectorAll('strong');
                const metricNumbers = [];

                for (const el of metricElements) {
                    const text = el.textContent.trim();
                    if (/^\d+[.,]?\d*[KkMmBb]?$/.test(text)) {
                        metricNumbers.push(text);
                        console.log(`Found metric: ${text}`);
                    }
                }

                // If we find at least 2 metrics, the second one is typically the comment count
                if (metricNumbers.length >= 2) {
                    const commentCountText = metricNumbers[1];
                    console.log(`Using second metric as comment count: ${commentCountText}`);

                    // Convert to number (handling K, M, B suffixes)
                    if (commentCountText.match(/[Kk]$/)) {
                        return parseInt(parseFloat(commentCountText.replace(/[Kk]/, '')) * 1000);
                    } else if (commentCountText.match(/[Mm]$/)) {
                        return parseInt(parseFloat(commentCountText.replace(/[Mm]/, '')) * 1000000);
                    } else if (commentCountText.match(/[Bb]$/)) {
                        return parseInt(parseFloat(commentCountText.replace(/[Bb]/, '')) * 1000000000);
                    } else {
                        return parseInt(commentCountText.replace(/[,. ]/g, ''));
                    }
                }
            }

            // If that fails, try looking for a sequence of numbers anywhere on the page
            // First, find all numbers in the document
            const allTextNodes = [];
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                null,
                false
            );

            let node;
            while (node = walker.nextNode()) {
                const text = node.textContent.trim();
                if (text && /\d/.test(text)) {
                    allTextNodes.push(node);
                }
            }

            // Look for a sequence of nodes that contain just numbers
            const pureNumberNodes = allTextNodes.filter(node =>
                /^\d+[.,]?\d*[KkMmBb]?$/.test(node.textContent.trim())
            );

            if (pureNumberNodes.length >= 2) {
                const commentCountText = pureNumberNodes[1].textContent.trim();
                console.log(`Found pure number sequence, using second number as comment count: ${commentCountText}`);

                // Convert to number
                if (commentCountText.match(/[Kk]$/)) {
                    return parseInt(parseFloat(commentCountText.replace(/[Kk]/, '')) * 1000);
                } else if (commentCountText.match(/[Mm]$/)) {
                    return parseInt(parseFloat(commentCountText.replace(/[Mm]/, '')) * 1000000);
                } else if (commentCountText.match(/[Bb]$/)) {
                    return parseInt(parseFloat(commentCountText.replace(/[Bb]/, '')) * 1000000000);
                } else {
                    return parseInt(commentCountText.replace(/[,. ]/g, ''));
                }
            }
        } catch (e) {
            console.log(`Error trying to find metrics sequence: ${e}`);
        }

        // Direct approach - look for the exact strong element with the comment count
        const commentCountStrong = document.querySelector('strong[data-e2e="comment-count"]');
        if (commentCountStrong) {
            const text = commentCountStrong.textContent.trim();
            console.log(`Found strong[data-e2e="comment-count"] element with text: "${text}"`);

            // Most likely this is the exact count value
            if (/^\d+$/.test(text) || /^\d+[.,]\d+[KkMmBb]?$/.test(text)) {
                console.log(`Found direct comment count: ${text}`);

                // Convert K, M, B suffixes to actual numbers
                if (text.match(/[Kk]$/)) {
                    return parseInt(parseFloat(text.replace(/[Kk]/, '')) * 1000);
                } else if (text.match(/[Mm]$/)) {
                    return parseInt(parseFloat(text.replace(/[Mm]/, '')) * 1000000);
                } else if (text.match(/[Bb]$/)) {
                    return parseInt(parseFloat(text.replace(/[Bb]/, '')) * 1000000000);
                } else {
                    return parseInt(text.replace(/[,. ]/g, ''));
                }
            }
        } else {
            console.log("Could not find strong[data-e2e='comment-count'] element");
        }

        // First, check specifically for the exact element the user mentioned
        try {
            const exactElement = document.querySelector('strong[data-e2e="browse-comment-count"]');
            if (exactElement) {
                const text = exactElement.textContent.trim();
                console.log(`Found exact browse-comment-count element with text: "${text}"`);

                // This element likely contains just the raw number
                if (/^\d+$/.test(text)) {
                    console.log(`Found direct number in strong tag: ${text}`);
                    return parseInt(text);
                }
            }
        } catch (e) {
            console.log(`Error checking for exact element: ${e}`);
        }

        // First, try specific selectors known to contain the comment count
        const specificSelectors = [
            'strong[data-e2e="comment-count"]',
            'strong[data-e2e="browse-comment-count"]',
            'strong[class*="StrongText"]',
            'div[class*="DivTabItem"]',  // The specific class mentioned by the user
            'div[class="css-1a6kzpf-DivTabItem e1aa9wve2"]',  // Exact class
            'div:contains("Comments ("'  // Any div containing "Comments ("
        ];

        // Try the specific selectors first (highest priority)
        for (const selector of specificSelectors) {
            try {
                const elements = document.querySelectorAll(selector);
                for (const el of elements) {
                    const text = el.textContent.trim();
                    console.log(`Checking text: "${text}" from selector: ${selector}`);

                    // First check if it's just a raw number
                    if (/^\d+$/.test(text)) {
                        console.log(`Found raw number: ${text}`);
                        return parseInt(text);
                    }

                    if (text.includes('Comments (')) {
                        const match = text.match(/Comments\s*\((\d+[,.]?\d*[KMB]?)\)/i);
                        if (match) {
                            let countText = match[1];
                            console.log(`Found comment count in "Comments (n)" format: ${countText}`);

                            // Convert K, M, B suffixes to actual numbers
                            if (countText.match(/[Kk]$/)) {
                                return parseInt(parseFloat(countText.replace(/[Kk]/, '')) * 1000);
                            } else if (countText.match(/[Mm]$/)) {
                                return parseInt(parseFloat(countText.replace(/[Mm]/, '')) * 1000000);
                            } else if (countText.match(/[Bb]$/)) {
                                return parseInt(parseFloat(countText.replace(/[Bb]/, '')) * 1000000000);
                            } else {
                                return parseInt(countText.replace(/[,. ]/g, ''));
                            }
                        }
                    }
                }
            } catch (e) {
                console.log(`Error with selector ${selector}: ${e}`);
            }
        }

        // Then, try the generic selectors (lower priority)
        const genericSelectors = [
            '[class*="comment-count"]',
            'div[class*="CommentCount"]',
            '[data-e2e*="comment-count"]'
        ];

        for (const selector of genericSelectors) {
            const el = document.querySelector(selector);
            if (el) {
                const text = el.textContent.trim();
                console.log(`Found text with generic selector: "${text}"`);

                // Check if it's just a raw number
                if (/^\d+$/.test(text)) {
                    console.log(`Found raw number: ${text}`);
                    return parseInt(text);
                }

                // Extract numbers from the text
                const match = text.match(/(\d+[,.]?\d*[KMB]?)/i);
                if (match) {
                    let countText = match[1];
                    console.log(`Extracted count: ${countText}`);

                    // Convert K, M, B suffixes to actual numbers
                    if (countText.match(/[Kk]$/)) {
                        return parseInt(parseFloat(countText.replace(/[Kk]/, '')) * 1000);
                    } else if (countText.match(/[Mm]$/)) {
                        return parseInt(parseFloat(countText.replace(/[Mm]/, '')) * 1000000);
                    } else if (countText.match(/[Bb]$/)) {
                        return parseInt(parseFloat(countText.replace(/[Bb]/, '')) * 1000000000);
                    } else {
                        return parseInt(countText.replace(/[,. ]/g, ''));
                    }
                }
            }
        }

        // As a last resort, look at ALL elements that might contain "Comments ("
        console.log("Trying full page scan for comment count...");
        const allElements = document.querySelectorAll('*');
        for (const el of allElements) {
            try {
                const text = el.textContent.trim();
                if (text.includes('Comments (')) {
                    const match = text.match(/Comments\s*\((\d+[,.]?\d*[KMB]?)\)/i);
                    if (match) {
                        let countText = match[1];
                        console.log(`Found comment count in full page scan: ${countText}`);

                        // Convert K, M, B suffixes to actual numbers
                        if (countText.match(/[Kk]$/)) {
                            return parseInt(parseFloat(countText.replace(/[Kk]/, '')) * 1000);
                        } else if (countText.match(/[Mm]$/)) {
                            return parseInt(parseFloat(countText.replace(/[Mm]/, '')) * 1000000);
                        } else if (countText.match(/[Bb]$/)) {
                            return parseInt(parseFloat(countText.replace(/[Bb]/, '')) * 1000000000);
                        } else {
                            return parseInt(countText.replace(/[,. ]/g, ''));
                        }
                    }
                }
            } catch (e) {
                // Ignore errors for individual elements
            }
        }

        // If no count found, return 0
        console.log("No comment count detected with any method");
        return 0;
    }

    // Main loop: repeatedly scroll, click the "view more" button, and then re-check for new comments.
    // First, run the DOM inspector to analyze the page structure
    console.log("Running DOM inspection to analyze page structure...");
    const domAnalysis = inspectDom();

    // Get total comment count and calculate 50% target (increased from 10%)
    const totalCommentCount = getTotalCommentCount();
    console.log(`Total comment count detected: ${totalCommentCount}`);
    const targetCommentCount = Math.ceil(totalCommentCount * 0.5); // 50% of total (increased from 10%)
    console.log(`Target comment count (50%): ${targetCommentCount}`);

    // Extract Comments using a dynamically generated selector based on DOM inspection
    async function extractCommentsNewMethod() {
        console.log("Attempting to extract comments using new dynamic method");

        let bestContainer = null;
        let commentItems = [];

        // Variables for comment extraction
        let commentText = null;
        let timeStamp = null;

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

                    // NEW: Try an alternative approach for finding username when direct link fails
                    // Look for any text node near an avatar image
                    const avatarElements = item.querySelectorAll('img[class*="avatar"], div[class*="avatar"], span[class*="avatar"]');
                    let alternativeUsername = null;

                    if (avatarElements.length > 0) {
                        // Try to find a username near the avatar
                        const avatar = avatarElements[0];
                        const parentElement = avatar.parentElement;

                        // Look for siblings of the avatar that might contain the username
                        if (parentElement && parentElement.parentElement) {
                            const potentialUsernames = Array.from(parentElement.parentElement.querySelectorAll('span, p, div'))
                                .filter(el => {
                                    const text = el.textContent.trim();
                                    // Usernames typically don't contain spaces and aren't too long
                                    return text.length > 0 && text.length < 30 &&
                                           !text.includes('ago') && !text.includes('Reply') &&
                                           !text.includes('Like');
                                });

                            if (potentialUsernames.length > 0) {
                                // Take the shortest text as it's most likely to be the username
                                potentialUsernames.sort((a, b) => a.textContent.length - b.textContent.length);
                                alternativeUsername = potentialUsernames[0].textContent.trim();
                                console.log(`Found alternative username: ${alternativeUsername}`);
                            }
                        }
                    }

                    // If we still don't have a username, try another approach - look for inline styles or class patterns
                    if (!alternativeUsername) {
                        const smallTexts = Array.from(item.querySelectorAll('span, p'))
                            .filter(el => {
                                const text = el.textContent.trim();
                                const hasAvatarRelatedClass = (el.className || '').toLowerCase().includes('user') ||
                                                             (el.className || '').toLowerCase().includes('name') ||
                                                             (el.className || '').toLowerCase().includes('author');
                                return text.length > 0 && text.length < 30 && hasAvatarRelatedClass;
                            });

                        if (smallTexts.length > 0) {
                            alternativeUsername = smallTexts[0].textContent.trim();
                            console.log(`Found username via class pattern: ${alternativeUsername}`);
                        }
                    }

                    // If we've found an alternative username, use it
                    if (alternativeUsername) {
                        console.log(`Using alternative username: ${alternativeUsername}`);
                        // Continue with this username
                        extractedComments.push({
                            username: alternativeUsername,
                            commentText: item.textContent
                                .replace(/Reply/g, '')
                                .replace(/Report/g, '')
                                .replace(/Like/g, '')
                                .replace(/View replies/g, '')
                                .replace(/\d+[smhdwy] ago/g, '')
                                .replace(/\s{2,}/g, ' ')
                                .trim(),
                            timeStamp: null
                        });
                        console.log('Successfully added comment with alternative username');
                    } else {
                        // If we still can't find a username, use a generic one as last resort
                        console.log('Using generic username as last resort');
                        extractedComments.push({
                            username: 'TikTok User',
                            commentText: item.textContent
                                .replace(/Reply/g, '')
                                .replace(/Report/g, '')
                                .replace(/Like/g, '')
                                .replace(/View replies/g, '')
                                .replace(/\d+[smhdwy] ago/g, '')
                                .replace(/\s{2,}/g, ' ')
                                .trim(),
                            timeStamp: null
                        });
                        console.log('Added comment with generic username');
                    }
                    continue; // Skip the rest of the original logic
                }

                console.log(`Found username: ${username}`);

                // SIMPLIFIED: Just take all text content and remove the username
                const fullText = item.textContent.trim();

                // Remove the username and common TikTok UI text
                commentText = fullText
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
        // Try to detect total comment count from the page
        let totalCommentCount = 0;
        try {
            // Look for elements that might show the total comment count
            const commentCountEl = document.querySelector(
                'strong[data-e2e="comment-count"], ' +
                'strong[data-e2e="browse-comment-count"], ' +
                '[class*="CommentCount"], ' +
                '[data-e2e*="comment-count"]'
            );

            if (commentCountEl) {
                const countText = commentCountEl.textContent.trim();
                const match = countText.match(/(\d+[,.]\d+[KkMm]?|\d+[KkMm]?|\d+)/);
                if (match) {
                    // Handle K/M suffixes
                    let count = match[1].replace(/[,.]/g, '');
                    if (count.match(/[Kk]$/)) {
                        count = parseFloat(count) * 1000;
                    } else if (count.match(/[Mm]$/)) {
                        count = parseFloat(count) * 1000000;
                    }
                    totalCommentCount = parseInt(count);
                    console.log(`Total comment count detected: ${totalCommentCount}`);
                }
            }
        } catch (e) {
            console.log(`Error detecting total comment count: ${e}`);
        }

        // Calculate target number of comments based on percentage
        const targetCommentCount = totalCommentCount > 0
            ? Math.ceil(totalCommentCount * (targetPercentage / 100))
            : 0;

        if (targetCommentCount > 0) {
            console.log(`Aiming to scrape ${targetCommentCount} comments (${targetPercentage}% of ${totalCommentCount})`);
        } else {
            console.log(`No total count detected, will continue until comments stop loading`);
        }

        let previousCount = 0;
        let stableCount = 0;
        // Loop up to 40 iterations (increased from 20) or break if no new comments are loaded
        for (let attempt = 0; attempt < 40; attempt++) {
            console.log(`Scroll attempt ${attempt+1}/40`); // Updated max attempts

            // Scroll multiple times in one attempt to ensure full coverage
            for (let scrolls = 0; scrolls < 5; scrolls++) { // Increased from 3 to 5 scrolls per attempt
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

            // Check if we've reached our target percentage
            if (targetCommentCount > 0 && currentCount >= targetCommentCount) {
                console.log(`Reached target of ${targetCommentCount} comments (${targetPercentage}% of ${totalCommentCount}). Stopping.`);
                break;
            }

            if (currentCount > previousCount) {
                console.log(`Progress: +${currentCount - previousCount} new comments loaded`);
                previousCount = currentCount;
                stableCount = 0;
            } else {
                stableCount++;
                console.log(`No new comments found for ${stableCount} consecutive attempts`);
                if (stableCount >= 5) { // Increased from 3 to 5 stable attempts before giving up
                    console.log("Comment count stable for 5 attempts, stopping scrolling");
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