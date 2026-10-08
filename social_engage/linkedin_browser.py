"""LinkedIn website transport in verified existing Edge Profile 7.

Only normalized evidence leaves memory. Observed comment pagination requests
use the verified browser session, with native read controls as fallback.
This module never uses LinkedIn's official API, replays write actions, downloads media,
persists credentials, creates browser profiles, or closes the shared browser.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from .adapters import AdapterError, eligible_time, now_iso
from .linkedin_data import HOSTS, FlightData, linkedin_post_id, linkedin_profile, find_profile, collect_posts, collect_comments
from .linkedin_read_api import observed_context, comment_pagers, fetch_comments_page, PAGINATION_PATH


READ_PATHS = {
    '/flagship-web/rsc-action/actions/pagination',
    '/flagship-web/rsc-action/actions/server-stream-request',
    '/flagship-web/rsc-action/actions/server-request',
}


def response_path(url):
    """Filter before scheduling; unrelated assets must not consume capture slots."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname != 'www.linkedin.com'
                or parts.username or parts.password or parts.port not in (None, 443)):
            return None
        if parts.path in READ_PATHS or parts.path.startswith((
                '/flagship-web/in/', '/flagship-web/feed/', '/flagship-web/search/')):
            return parts.path
    except ValueError:
        pass
    return None


def html_flight(raw):
    marker=re.search(r'window\.__como_rehydration__\s*=\s*',raw)
    if not marker:return FlightData([])
    try:
        chunks,_=json.JSONDecoder().raw_decode(raw[marker.end():])
        return FlightData(chunks)
    except (ValueError,RecursionError):
        raise AdapterError('linkedin_bootstrap_unavailable') from None


class LinkedInBrowserAdapter:
    platform='linkedin'
    max_text=1250

    def __init__(self,runtime_dir=None):
        self.runtime_dir=Path(runtime_dir).resolve() if runtime_dir else None
        self.loop=asyncio.new_event_loop()
        self.pw=self.browser=self.context=self.page=None
        self.closed=False
        self.tasks=set()
        self.streams=[]
        self.actor=None
        self.generation=0
        self.prepared=None
        self.blocked=None
        self.comment_post=None
        self.comment_context=None
        self.comment_pagers=[]
        self.comment_seen=set()
        self.direct_read=False
        self.comment_pages=0
        self.comment_stop='not_attempted'
        self.capture_timeouts=0

    def _run(self,method,*args):
        if self.closed:raise AdapterError('browser_adapter_closed')
        if self.blocked:raise AdapterError(self.blocked)
        try:
            if self.page is None:
                import social_browser as sb
                runtime=self.runtime_dir or sb.DEFAULT_RUNTIME_DIR
                designation=sb.load_engage_profile7_designation(runtime)
                try:state=sb.load_verified_profile7_state(runtime,designation)
                except Exception:
                    with contextlib.redirect_stdout(io.StringIO()):
                        ready=sb.ensure_engage_profile7_browser(runtime)
                    designation,state=ready['designation'],ready['state']
                self.loop.run_until_complete(asyncio.wait_for(self._connect(designation,state),180))
            return self.loop.run_until_complete(asyncio.wait_for(method(*args),300))
        except AdapterError as exc:
            if any(part in str(exc) for part in ('verification','access_denied','login','account_mismatch','viewer','self_profile')):
                self.blocked=str(exc)
            raise
        except Exception:
            self.blocked='linkedin_browser_operation_failed'
            raise AdapterError(self.blocked) from None

    async def _connect(self,designation,state):
        import social_browser as sb
        from playwright.async_api import async_playwright
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(state['cdp_url'],timeout=120000)
        self.context,_=await sb.verified_profile_context(self.browser,designation)
        if not (await sb.platform_authentication(self.context,'linkedin'))['authenticated']:
            raise AdapterError('linkedin_login_required')
        self.page=await self.context.new_page()
        self.page.set_default_timeout(10000)
        self.page.on('response',self._schedule)

    def _schedule(self,response):
        path=response_path(response.url)
        if path is None or (self.direct_read and path == PAGINATION_PATH):return
        if len(self.tasks)>=30:
            self.streams.append({'error':'linkedin_capture_overflow'})
            return
        task=self.loop.create_task(self._response(response,self.generation))
        self.tasks.add(task);task.add_done_callback(self.tasks.discard)

    async def _response(self,response,generation):
        try:
            path=response_path(response.url)
            if path is None or generation!=self.generation:return
            if response.status in (401,403,429):
                self.streams.append({'error':'linkedin_access_denied_or_rate_limited'});return
            if response.status!=200:return
            raw=await response.text()
            if generation!=self.generation:return
            if len(self.streams)>=35:
                self.streams.append({'error':'linkedin_capture_overflow'});return
            flight=FlightData(raw)
            self.streams.append({'path':path,'flight':flight})
            if self.comment_post and path in READ_PATHS:
                request=response.request
                try:body=json.loads(request.post_data or '{}')
                except (ValueError,TypeError):body={}
                context=observed_context(response.url,request.method,request.headers,body,self.comment_post)
                if context:
                    self.comment_context=context
                    pagers=comment_pagers(flight,self.comment_post)
                    if pagers or path==PAGINATION_PATH:
                        self.comment_pagers=pagers
        except (AdapterError,ValueError):
            if generation==self.generation:self.streams.append({'error':'linkedin_stream_unavailable'})
        except Exception:
            if generation==self.generation:self.streams.append({'error':'linkedin_stream_unavailable'})

    async def _settle(self):
        await self.page.wait_for_timeout(1800)
        pending=list(self.tasks)
        if pending:
            _,unfinished=await asyncio.wait(pending,timeout=12)
            for task in unfinished:task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            self.capture_timeouts+=len(unfinished)
        for stream in self.streams:
            if stream.get('error'):raise AdapterError(stream['error'])

    async def _page_guard(self):
        parts=urlsplit(self.page.url)
        if parts.scheme!='https' or parts.hostname not in HOSTS:
            raise AdapterError('linkedin_origin_or_login_redirect')
        if any(x in parts.path.lower() for x in ('/login','/checkpoint','/challenge','/authwall')):
            raise AdapterError('linkedin_login_or_human_verification_required')
        if await self.page.locator('iframe[src*="captcha"],input[autocomplete="one-time-code"]').count():
            raise AdapterError('linkedin_human_verification_required')

    async def _navigate(self,url):
        self.generation+=1;self.streams=[]
        self.comment_post=None;self.comment_context=None;self.comment_pagers=[]
        self.comment_seen.clear();self.comment_pages=0;self.comment_stop='not_attempted'
        self.capture_timeouts=0
        response=await self.page.goto(url,wait_until='domcontentloaded',timeout=60000)
        await self._page_guard()
        if response is None or response.status!=200:raise AdapterError('linkedin_page_unavailable')
        raw=await response.text()
        if len(raw)>16000000:raise AdapterError('linkedin_page_too_large')
        flight=html_flight(raw)
        self.streams.insert(0,{'path':urlsplit(self.page.url).path,'flight':flight})
        await self._settle();await self._page_guard()
        return flight

    async def _viewer_slug(self):
        paths=await self.page.locator('a[href*="/in/"][href*="/edit/"]').evaluate_all('(els)=>els.map(e=>new URL(e.href).pathname)')
        slugs={match.group(1) for path in paths if (match:=re.fullmatch(r'/in/([^/]+)/edit/.*',path))}
        if len(slugs)!=1:raise AdapterError('linkedin_viewer_navigation_unverified')
        return linkedin_profile(next(iter(slugs)))

    async def _identity(self,expected=None):
        await self._navigate('https://www.linkedin.com/feed/')
        slug=await self._viewer_slug()
        if expected is not None and slug!=linkedin_profile(expected):raise AdapterError('account_mismatch')
        for stream in self.streams:
            if stream.get('path')==f'/flagship-web/in/{slug}/':
                try:actor=find_profile(stream['flight'],slug)
                except AdapterError:continue
                self.actor=actor;return actor
        flight=await self._navigate(f'https://www.linkedin.com/in/{slug}/')
        actor=find_profile(flight,slug)
        self.actor=actor;return actor

    def identity(self,expected=None):return self._run(self._identity,expected)

    def _posts(self):
        output={}
        for stream in self.streams:
            if stream.get('error'):raise AdapterError(stream['error'])
            # Ignore speculative prefetch of unrelated pages.
            path=stream['path']
            if path!=urlsplit(self.page.url).path and path not in {
                '/flagship-web/rsc-action/actions/pagination','/flagship-web/rsc-action/actions/server-stream-request',
                '/flagship-web/rsc-action/actions/server-request'}:continue
            for post in collect_posts(stream['flight']):output[post['id']]=post
        return list(output.values())

    async def _verify_viewer(self,actor):
        ids={obj['currentUserNonIterableProfileId'] for stream in self.streams if stream.get('flight')
             for obj in stream['flight'].walk() if isinstance(obj,dict) and isinstance(obj.get('currentUserNonIterableProfileId'),str)}
        if ids and ids!={actor['id'].removeprefix('urn:li:fsd_profile:')}:
            raise AdapterError('account_mismatch')
        if not ids and await self._viewer_slug()!=actor['username']:
            raise AdapterError('account_mismatch')

    async def _discover(self,scope):
        if scope['source']=='post':return [linkedin_post_id(scope['target'])]
        if scope['source']=='own':owner=scope['account']
        elif scope['source']=='creator':owner=scope['target']
        else:owner=None
        url=(f'https://www.linkedin.com/in/{linkedin_profile(owner)}/recent-activity/all/' if owner else
             'https://www.linkedin.com/search/results/content/?keywords='+quote(scope['target'],safe=''))
        await self._navigate(url)
        await self._verify_viewer(self.actor)
        selected={};stalls=0
        rounds = max(15, min(100, scope['candidate_limit'] // 2 + 5))
        for _ in range(rounds):
            before=len(selected)
            for post in self._posts():
                if (owner is None or post['author']==linkedin_profile(owner)) and eligible_time(post,scope):selected[post['id']]=post
            if len(selected)>=scope['candidate_limit']:break
            stalls=stalls+1 if len(selected)==before else 0
            if stalls>=3:break
            btn=self.page.locator('button:has-text("Load more")')
            if await btn.count()>0 and await btn.first.is_visible():
                with contextlib.suppress(Exception):
                    await btn.first.scroll_into_view_if_needed()
                    await btn.first.click()
                    await self.page.wait_for_timeout(2000)
            else:
                await self.page.evaluate('window.scrollBy(0,Math.max(700,window.innerHeight))')
            await self._settle();await self._page_guard()
        await self._verify_viewer(self.actor)
        return list(selected)[:scope['candidate_limit']]

    def discover(self,scope):return self._run(self._discover,scope)

    def _comments(self,post_id):
        result={}
        for stream in self.streams:
            if stream.get('error'):raise AdapterError(stream['error'])
            path=stream['path']
            if path!=urlsplit(self.page.url).path and path not in {
                '/flagship-web/rsc-action/actions/pagination','/flagship-web/rsc-action/actions/server-stream-request',
                '/flagship-web/rsc-action/actions/server-request'}:continue
            for comment in collect_comments(stream['flight'],post_id):result[comment['id']]=comment
        return list(result.values())

    def _card_locator(self,key):
        # JSON encoding supplies a quoted CSS attribute value, never selector code.
        return self.page.locator('[role="listitem"][componentkey='+json.dumps(key)+']')

    async def _read_comment_page(self,post_id,actor):
        if not self.comment_context:
            return 'unavailable'
        # The site also embeds its initial comment pager in the page bootstrap,
        # separately from the response that opens the visible comment section.
        if not self.comment_pagers and not self.comment_pages:
            for stream in reversed(self.streams):
                if stream.get('path') not in READ_PATHS | {urlsplit(self.page.url).path}:
                    continue
                if not stream.get('flight'):continue
                pagers=comment_pagers(stream['flight'],post_id)
                if pagers:
                    self.comment_pagers=pagers;break
        if not self.comment_pagers:
            return 'no_observed_next_page' if self.comment_pages else 'unavailable'
        if len(self.comment_pagers)!=1:
            return 'ambiguous_pagination'
        pager=self.comment_pagers[0]
        key=(pager['pagerId'],pager['requestedArguments']['payload']['pageToken'])
        if key in self.comment_seen:
            return 'repeated_cursor'
        await self._page_guard();await self._verify_viewer(actor)
        self.comment_seen.add(key)
        self.direct_read=True
        try:
            flight=await fetch_comments_page(self.page,self.comment_context,pager,post_id)
        finally:
            self.direct_read=False
        if len(self.streams)>=35:
            raise AdapterError('linkedin_capture_overflow')
        self.streams.append({'path':PAGINATION_PATH,'flight':flight})
        self.comment_pagers=comment_pagers(flight,post_id)
        self.comment_pages+=1
        await self._page_guard();await self._verify_viewer(actor)
        return 'page'

    async def _collect(self,post_id,scope,actor):
        post_id=linkedin_post_id(post_id)
        if scope['source']=='post' and post_id!=linkedin_post_id(scope['target']):
            raise AdapterError('exact_post_scope_mismatch')
        if actor.get('username')!=linkedin_profile(scope['account']):raise AdapterError('account_mismatch')
        url='https://www.linkedin.com/feed/update/'+post_id+'/'
        await self._navigate(url)
        await self._verify_viewer(actor)
        if linkedin_post_id(self.page.url)!=post_id:raise AdapterError('exact_post_page_identity_mismatch')
        roots=[post for post in self._posts() if post['id']==post_id]
        if len(roots)!=1:raise AdapterError('exact_linkedin_post_not_available')
        root=roots[0]
        owner=(scope['account'] if scope['source']=='own' else scope['target'] if scope['source']=='creator' else None)
        if owner and root['author']!=linkedin_profile(owner):raise AdapterError('creator_mismatch')
        if not eligible_time(root,scope):raise AdapterError('publication_time_outside_scope')
        self.comment_post=post_id
        card=self._card_locator(root['component_key'])
        if await card.count()!=1:raise AdapterError('exact_post_card_unavailable')
        comment_button=card.get_by_role('button',name='Comment',exact=True)
        if await comment_button.count()==1:
            label=(await comment_button.inner_text()).strip()
            if re.fullmatch(r'[0-9]+(?:,[0-9]{3})*',label):
                root['comment_count']=int(label.replace(',',''))
                root['metrics']['comments']=root['comment_count'];root['metrics_status']='website_declared'
            if root['comment_count'] != 0 and len(self._comments(post_id)) < scope['comments']:
                await comment_button.click()
                await self._settle();await self._page_guard();await self._verify_viewer(actor)
        # Only the observed, exact-post comment pager is replayable. Unknown
        # native schemas keep the existing bounded read-control fallback.
        stalls=0
        for _ in range(12):
            if len(self._comments(post_id))>=scope['comments']:
                self.comment_stop='requested_limit';break
            if root['comment_count']==0 and not self._comments(post_id):
                self.comment_stop='declared_zero';break
            before={c['id'] for c in self._comments(post_id)}
            outcome=await self._read_comment_page(post_id,actor)
            if outcome not in {'page','unavailable'}:
                self.comment_stop=outcome;break
            if outcome=='unavailable':
                more=self.page.get_by_role('button',name=re.compile(r'^(?:Load|Show|View) more comments$',re.I))
                if await more.count()==1 and await more.is_visible():await more.click()
                else:await self.page.evaluate('window.scrollBy(0,Math.max(700,window.innerHeight))')
                await self._settle();await self._page_guard()
            stalls=stalls+1 if {c['id'] for c in self._comments(post_id)}==before else 0
            if stalls>=2:
                self.comment_stop='pagination_stalled';break
        else:self.comment_stop='page_budget_reached'
        await self._verify_viewer(actor)
        comments=self._comments(post_id)
        # A numeric aggregate cannot establish nested-parent coverage.
        complete=root['comment_count']==0 and not comments and not self.capture_timeouts
        root.pop('component_key',None)
        return {**root,'authority':'linkedin_authenticated_browser_website','observed_at':now_iso(),
                'collection_method':('observed_website_streamed_json_and_authenticated_read_api' if self.comment_pages else
                                     'observed_website_streamed_json_and_native_read_controls'),
                'comments':comments[:scope['comments']],'comments_complete':complete,
                'comments_status':'available' if complete else 'bounded_or_parent_link_unavailable',
                'comment_pagination':{'direct_pages':self.comment_pages,'stop_reason':self.comment_stop,
                                      'capture_timeouts':self.capture_timeouts},
                **{key:{'status':'unsupported','reason':'not_exposed_by_this_browser_adapter'}
                   for key in ('music','transcript','subtitles','catalog')}}

    def collect(self,post_id,scope,actor):return self._run(self._collect,post_id,scope,actor)

    def preflight(self,evidence,scope,actor):
        self.prepared=None
        # Fail before the shared state reserves a one-time publication attempt.
        raise AdapterError('linkedin_native_publication_not_yet_verified')

    async def _publish(self,evidence,actor,text,guard):
        if not self.prepared or self.prepared[0]!=evidence or self.prepared[2]!=actor:
            raise AdapterError('browser_publication_preflight_required')
        # Publication stays fail-closed until native composer parent/actor and
        # direct-comment receipt schemas have been observed and validated.
        raise AdapterError('linkedin_native_publication_not_yet_verified')

    def publish(self,evidence,actor,text,guard):return self._run(self._publish,evidence,actor,text,guard)

    async def _close(self):
        try:
            if self.page is not None:
                with contextlib.suppress(Exception):await asyncio.wait_for(self.page.close(),10)
        finally:
            try:
                pending=list(self.tasks)
                for task in pending:task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
            finally:
                self.comment_context=None;self.comment_pagers=[];self.comment_seen.clear()
                if self.pw is not None:
                    with contextlib.suppress(Exception):await asyncio.wait_for(self.pw.stop(),10)

    def close(self):
        if not self.closed:
            try:self.loop.run_until_complete(self._close())
            finally:
                self.closed=True;self.loop.close();self.streams.clear();self.prepared=None
