from tiktok_scraper.utils.selectors import get_selectors, extract_comment_count_from_page
import os
import asyncio

class TikTokScraper:
    def __init__(self, headless=False, logs_folder="logs"):
        """Initialize TikTok scraper."""
        self.headless = headless
        self.browser = None
        self.page = None
        self.context = None
        self.logs_folder = logs_folder
        os.makedirs(logs_folder, exist_ok=True)

    async def scrape_comments(self, url, max_comments=None, include_replies=False):
        """
        Scrape comments from a TikTok video.
        
        Args:
            url (str): URL of the TikTok video
            max_comments (int, optional): Maximum number of comments to scrape
            include_replies (bool, optional): Whether to include replies to comments
            
        Returns:
            tuple: (list of comment dictionaries, total comment count)
        """
        self.logger.info(f"Scraping comments from: {url}")
        
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
            await self.page.wait_for_load_state("networkidle", timeout=10000)
            
            # Get total comment count if available
            total_comment_count = extract_comment_count_from_page(self.page, self.logs_folder)
            self.logger.info(f"Total comment count: {total_comment_count}")
            
            # Get selectors
            selectors = get_selectors(self.logs_folder)
            
            # Click on comment button
            comment_button_selector = selectors.get("comment_button")
            comment_button_xpath = selectors.get("comment_button_xpath")
            
            try:
                if comment_button_xpath:
                    # Use XPath if available from recorded elements
                    await self.page.locator(f"xpath={comment_button_xpath}").click(timeout=5000)
                else:
                    # Otherwise use CSS selector
                    await self.page.locator(comment_button_selector).click(timeout=5000)
            except Exception as e:
                self.logger.error(f"Failed to click comment button: {e}")
                # Try an alternative approach
                try:
                    # Try using JavaScript to click the button
                    await self.page.evaluate(f"document.querySelector('{comment_button_selector}').click()")
                except Exception as e:
                    self.logger.error(f"Failed to click comment button using JavaScript: {e}")
                    return [], 0
            
            # Wait for comment list to load
            await self.page.wait_for_selector(selectors["comment_list"], timeout=10000)
            
            # Begin scraping comments
            comments = []
            loaded_comments = 0
            
            # Load more comments until we have enough or there are no more
            last_comment_count = 0
            same_count_iterations = 0
            
            while True:
                # Wait for comments to load
                await asyncio.sleep(2)
                
                # Get current comments
                comment_elements = await self.page.query_selector_all(selectors["comment_items"])
                
                # Check if we have loaded new comments
                current_count = len(comment_elements)
                if current_count == last_comment_count:
                    same_count_iterations += 1
                    if same_count_iterations >= 3:  # If we see the same count multiple times, assume no more comments
                        break
                else:
                    same_count_iterations = 0
                    last_comment_count = current_count
                
                # Load more comments if needed
                if max_comments and current_count >= max_comments:
                    break
                
                try:
                    # Try to click "Load more" button if it exists
                    load_more_elements = await self.page.query_selector_all(selectors["load_more"])
                    if load_more_elements:
                        await load_more_elements[0].scroll_into_view_if_needed()
                        await load_more_elements[0].click()
                    else:
                        # If no load more button, try scrolling to bottom
                        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception as e:
                    self.logger.warning(f"Could not load more comments: {e}")
                    break
            
            # Process all comment elements
            comment_elements = await self.page.query_selector_all(selectors["comment_items"])
            
            for comment_element in comment_elements[:max_comments if max_comments else None]:
                try:
                    # Extract comment data
                    username_element = await comment_element.query_selector(selectors["username"])
                    text_element = await comment_element.query_selector(selectors["comment_text"])
                    like_count_element = await comment_element.query_selector(selectors["like_count"])
                    post_time_element = await comment_element.query_selector(selectors["post_time"])
                    
                    username = await username_element.inner_text() if username_element else "Unknown"
                    text = await text_element.inner_text() if text_element else ""
                    likes = await like_count_element.inner_text() if like_count_element else "0"
                    post_time = await post_time_element.inner_text() if post_time_element else ""
                    
                    # Parse likes count (remove non-numeric characters)
                    try:
                        likes = int(''.join(filter(str.isdigit, likes))) if likes else 0
                    except ValueError:
                        likes = 0
                    
                    comment_data = {
                        "username": username,
                        "text": text,
                        "likes": likes,
                        "post_time": post_time,
                        "replies": []
                    }
                    
                    # Get replies if requested
                    if include_replies:
                        try:
                            replies_button = await comment_element.query_selector(selectors["replies_button"])
                            if replies_button:
                                button_text = await replies_button.inner_text()
                                if "View" in button_text and "replies" in button_text:
                                    await replies_button.click()
                                    await asyncio.sleep(1)  # Wait for replies to load
                                    
                                    # Scrape replies
                                    reply_elements = await self.page.query_selector_all(selectors["reply_items"])
                                    for reply_element in reply_elements:
                                        reply_username_element = await reply_element.query_selector(selectors["reply_username"])
                                        reply_text_element = await reply_element.query_selector(selectors["reply_text"])
                                        reply_like_count_element = await reply_element.query_selector(selectors["reply_like_count"])
                                        reply_post_time_element = await reply_element.query_selector(selectors["reply_post_time"])
                                        
                                        reply_username = await reply_username_element.inner_text() if reply_username_element else "Unknown"
                                        reply_text = await reply_text_element.inner_text() if reply_text_element else ""
                                        reply_likes = await reply_like_count_element.inner_text() if reply_like_count_element else "0"
                                        reply_post_time = await reply_post_time_element.inner_text() if reply_post_time_element else ""
                                        
                                        try:
                                            reply_likes = int(''.join(filter(str.isdigit, reply_likes))) if reply_likes else 0
                                        except ValueError:
                                            reply_likes = 0
                                        
                                        reply_data = {
                                            "username": reply_username,
                                            "text": reply_text,
                                            "likes": reply_likes,
                                            "post_time": reply_post_time
                                        }
                                        
                                        comment_data["replies"].append(reply_data)
                        except Exception as e:
                            self.logger.warning(f"Failed to scrape replies: {e}")
                    
                    comments.append(comment_data)
                    
                except Exception as e:
                    self.logger.warning(f"Failed to scrape comment: {e}")
            
            # Close comments dialog
            try:
                close_button = await self.page.query_selector(selectors["close_button"])
                if close_button:
                    await close_button.click()
            except Exception as e:
                self.logger.warning(f"Failed to close comments dialog: {e}")
            
            self.logger.info(f"Scraped {len(comments)} comments")
            return comments, total_comment_count
            
        except Exception as e:
            self.logger.error(f"Error scraping comments: {e}")
            return [], 0 