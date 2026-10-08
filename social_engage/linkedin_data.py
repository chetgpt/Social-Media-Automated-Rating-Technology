"""Closed LinkedIn website identity and evidence projections; no API credentials."""
from __future__ import annotations

import re
import json
from urllib.parse import unquote, urlsplit

from .adapters import AdapterError

HOSTS = {"www.linkedin.com", "linkedin.com"}


class FlightData:
    """Read the site's observed JSON rows, without evaluating JS or action payloads."""
    def __init__(self, chunks):
        self.rows = {}
        if isinstance(chunks,str):
            chunks = [chunks]
        raw = ''.join(s for s in chunks if isinstance(s,str)) if isinstance(chunks,list) else ''
        if len(raw)>16000000:
            raise AdapterError('linkedin_stream_too_large')
        payload=raw.encode('utf-8');cursor=0
        while cursor<len(payload):
            match=re.match(rb'([0-9a-f]+):',payload[cursor:cursor+40])
            if not match:
                end=payload.find(b'\n',cursor)
                if end<0:break
                cursor=end+1;continue
            key=match.group(1).decode('ascii');cursor+=match.end()
            text_row=re.match(rb'T([0-9a-f]+),',payload[cursor:cursor+40])
            if text_row:
                size=int(text_row.group(1),16);cursor+=text_row.end()
                if size>16000000 or cursor+size>len(payload):raise AdapterError('linkedin_stream_truncated')
                try:self.rows[key]=payload[cursor:cursor+size].decode('utf-8')
                except UnicodeDecodeError:raise AdapterError('linkedin_stream_invalid_text') from None
                cursor+=size
                if payload[cursor:cursor+1]==b'\n':cursor+=1
                continue
            end=payload.find(b'\n',cursor)
            if end<0:end=len(payload)
            line=payload[cursor:end];cursor=end+1
            try:
                value = json.loads(line)
            except ValueError:
                continue
            self.rows[key] = value

    def ref(self,value):
        if isinstance(value,str) and re.fullmatch(r'\$[L@]?[0-9a-f]+',value):
            return self.rows.get(value.lstrip('$L@'),value)
        return value

    def walk(self,value=None,*,prune=None):
        stack = list(self.rows.values()) if value is None else [value]
        seen=set()
        visits=0
        while stack and visits<180000:
            item=self.ref(stack.pop());visits+=1
            if isinstance(item,(dict,list)):
                if id(item) in seen: continue
                seen.add(id(item))
                if prune is not None and prune(item):continue
                yield item
                stack.extend(reversed(list(item.values()) if isinstance(item,dict) else item))

    def visible_text(self,value):
        """Only rendered children; never traverse action configuration as text."""
        out=[];stack=[(value,False)];active=set();visits=0
        while stack:
            value,leaving=stack.pop()
            if leaving:
                active.discard(value);continue
            item=self.ref(value);visits+=1
            if visits>180000:raise AdapterError('linkedin_text_too_complex')
            if isinstance(item,str):
                if re.fullmatch(r'\$[L@]?[0-9a-f]+',item):raise AdapterError('linkedin_text_reference_unavailable')
                if item.startswith('$$'):out.append(item[1:])
                elif not item.startswith('$'):out.append(item)
            elif isinstance(item,(dict,list)):
                if id(item) in active:continue
                active.add(id(item));stack.append((id(item),True))
                if isinstance(item,dict):stack.append((item.get('children'),False))
                elif len(item)==4 and item[0]=='$':
                    if item[1]=='br':out.append('\n')
                    props=self.ref(item[3])
                    if isinstance(props,dict):stack.append((props.get('children'),False))
                else:stack.extend((child,False) for child in reversed(item))
        return ''.join(out)


def linkedin_profile(value):
    if not isinstance(value, str):
        raise AdapterError("invalid_linkedin_profile")
    if value.startswith("https://"):
        parts = safe_url(value)
        if parts.hostname not in HOSTS or parts.username or parts.password or parts.port not in (None, 443):
            raise AdapterError("invalid_linkedin_profile")
        match = re.fullmatch(r"/in/([^/]+)/?", parts.path)
        if not match:
            raise AdapterError("invalid_linkedin_profile")
        value = unquote(match.group(1))
    value = value.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", value):
        raise AdapterError("invalid_linkedin_profile")
    return value


def linkedin_post_id(value):
    if not isinstance(value, str):
        raise AdapterError("invalid_linkedin_activity")
    if value.startswith("https://"):
        parts = safe_url(value)
        if parts.hostname not in HOSTS or parts.username or parts.password or parts.port not in (None, 443):
            raise AdapterError("invalid_linkedin_activity")
        path = unquote(parts.path)
        match = re.fullmatch(r"/feed/update/urn:li:activity:([1-9][0-9]{0,24})/?", path)
        if not match:
            match = re.fullmatch(r"/posts/[A-Za-z0-9_%.-]+-activity-([1-9][0-9]{0,24})-[A-Za-z0-9_-]+/?", path)
        if not match:
            raise AdapterError("invalid_linkedin_activity")
        value = match.group(1)
    match = re.fullmatch(r"(?:urn:li:activity:)?([1-9][0-9]{0,24})", value)
    if not match:
        raise AdapterError("invalid_linkedin_activity")
    return "urn:li:activity:" + match.group(1)


def find_profile(flight,expected):
    expected=linkedin_profile(expected)
    identifiers=set()
    self_ids=set()
    for obj in flight.walk():
        if not isinstance(obj,dict):continue
        if obj.get('vanityName')==expected:
            ident=obj.get('nonIterableProfileId') or obj.get('profileId')
            if isinstance(ident,str) and re.fullmatch(r'ACo[A-Za-z0-9_-]{10,100}',ident):identifiers.add(ident)
        if obj.get('isSelfView') is True and obj.get('profileRefreshKey')==expected:
            self_ids.add(obj.get('vieweeProfileId'))
    ids=identifiers & self_ids
    if len(ids)!=1:raise AdapterError('linkedin_self_profile_unverified')
    return {'id':'urn:li:fsd_profile:'+next(iter(ids)),'username':expected}


def post_cards(flight):
    for obj in flight.walk():
        if isinstance(obj,dict) and obj.get('role')=='listitem' and str(obj.get('componentkey','')).startswith('update-card-focus'):
            yield obj


def project_card(flight,card):
    nodes=[obj for obj in flight.walk(card) if isinstance(obj,dict)]
    ids=set();owners=set();captions=[];reply_counts=set()
    for obj in nodes:
        activity=obj.get('activityUrn')
        if isinstance(activity,dict) and isinstance(activity.get('activityId'),str):
            ids.add(linkedin_post_id(activity['activityId']))
        if 'updateKeyContainer' in obj and isinstance(obj.get('vanityName'),str):
            owners.add(linkedin_profile(obj['vanityName']))
        if obj.get('isExpandableTextV2Enabled') is True or 'expansionKey' in obj or 'onShowMoreAction' in obj:
            rendered=flight.visible_text(obj.get('textProps'))
            if rendered.strip():captions.append(rendered.strip())
        if obj.get('aria-label')=='Comment':
            value=flight.visible_text(obj.get('children')).strip()
            if re.fullmatch(r'[0-9]+(?:,[0-9]{3})*',value):reply_counts.add(int(value.replace(',','')))
    if len(ids)!=1 or len(owners)!=1 or len(set(captions))!=1:
        raise AdapterError('linkedin_post_identity_or_text_unavailable')
    post_id=next(iter(ids));caption=captions[0]
    if len(caption)>20000:raise AdapterError('linkedin_post_text_too_large')
    count=next(iter(reply_counts)) if len(reply_counts)==1 else None
    return {'platform':'linkedin','id':post_id,'author':next(iter(owners)),
            'url':'https://www.linkedin.com/feed/update/'+post_id+'/',
            'text':caption,'published_at':'','publication_time_status':'not_exposed_as_exact_timestamp',
            'media_type':'website_post','metrics':{'comments':count} if count is not None else {},
            'metrics_status':'website_declared' if count is not None else 'not_provided',
            'comment_count':count,'component_key':card['componentkey']}


def collect_posts(flight):
    posts={}
    for card in post_cards(flight):
        try:
            post=project_card(flight,card)
        except AdapterError:continue
        posts[post['id']]=post
    return list(posts.values())


def safe_url(value):
    try:
        parts=urlsplit(value)
        parts.port
        return parts
    except ValueError:raise AdapterError('invalid_linkedin_url') from None


def collect_comments(flight,post_id):
    """Project observed comment identities/text; activity membership is not parentage."""
    post_id=linkedin_post_id(post_id)
    comments={}
    for card in flight.walk():
        if not isinstance(card,dict):continue
        match=re.fullmatch(r'CommentComponentReference_(urn:li:comment:\(activity:([1-9][0-9]*),([1-9][0-9]*)\))',str(card.get('componentkey','')))
        if not match or linkedin_post_id(match[2])!=post_id:continue
        owners=set();captions=set();bindings=set()
        # A nested comment is projected separately by the outer traversal. Its
        # author, text and identity must not be attributed to this comment.
        for obj in flight.walk(card,prune=lambda item: item is not card
                               and isinstance(item,dict)
                               and str(item.get('componentkey','')).startswith('CommentComponentReference_')):
            if not isinstance(obj,dict):continue
            if 'contentRef' in obj and isinstance(obj.get('vanityName'),str):
                owners.add(linkedin_profile(obj['vanityName']))
            if obj.get('isExpandableTextV2Enabled') is True or ('expansionKey' in obj and 'onShowMoreAction' in obj):
                captions.add(flight.visible_text(obj.get('textProps')).strip())
            urn=obj.get('commentUrn')
            if isinstance(urn,dict):bindings.add((urn.get('thread'),urn.get('commentId')))
        if len(owners)!=1 or len(captions)!=1 or not next(iter(captions),'') or (post_id,match[3]) not in bindings:continue
        comments[match[1]]={'id':match[1],'author':next(iter(owners)),'text':next(iter(captions)),
            'root_id':post_id,'parent_id':'','parent_binding':'not_exposed','published_at':'',
            'metrics':{},'metrics_status':'not_provided'}
    return list(comments.values())
