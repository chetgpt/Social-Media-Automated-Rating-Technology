"""In-memory LinkedIn website comment pagination; no official API or writes.

The endpoint and PaginationRequest structure were observed from the native
comment Load more control. Never replay generic server-request actions.
"""
from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit

from .adapters import AdapterError
from .linkedin_data import FlightData, linkedin_post_id


PAGINATION_PATH = '/flagship-web/rsc-action/actions/pagination'
PAGINATION_TYPE = 'proto.sdui.actions.requests.PaginationRequest'
HEADERS = {'accept', 'content-type', 'csrf-token', 'x-li-anchor-page-key',
           'x-li-application-instance', 'x-li-application-version', 'x-li-page-instance',
           'x-li-page-instance-tracking-id', 'x-li-pageforestid', 'x-li-rsc-stream',
           'x-li-track'}
PAYLOAD_KEYS = {'pageToken', 'threadUrn', 'pageSize', 'subsequentPageSize',
                'mobilePreloadDistance', 'webPreloadLength', 'updateKey',
                'fetchCommentsOnPageLoad', 'numCommentsDisplayed', 'sortOrder'}


def thread_matches(payload, post_id):
    try:
        thread = payload['threadUrn']
        if set(thread) != {'threadUrnActivityThreadUrn'}:
            return False
        container = thread['threadUrnActivityThreadUrn']
        if set(container) != {'activityUrn'} or set(container['activityUrn']) != {'activityId'}:
            return False
        activity = container['activityUrn']['activityId']
        return linkedin_post_id(activity) == linkedin_post_id(post_id)
    except (KeyError, TypeError, AdapterError):
        return False


def observed_context(url, method, headers, body, post_id):
    """Retain only session headers and client context from a bound native read."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.netloc != 'www.linkedin.com'
                or set(k for k, _ in parse_qsl(parts.query, keep_blank_values=True)) - {'sduiid', 'parentSpanId'}
                or parts.fragment or method != 'POST' or parts.path not in {
                    PAGINATION_PATH, '/flagship-web/rsc-action/actions/server-request'}):
            return None
        args = body.get('clientArguments') if parts.path == PAGINATION_PATH else body.get('requestedArguments')
        if not isinstance(args, dict) or not thread_matches(args.get('payload'), post_id):
            return None
        safe_headers = {k.lower(): v for k, v in headers.items() if k.lower() in HEADERS}
        if not safe_headers.get('csrf-token') or not isinstance(args.get('screenId'), str):
            return None
        return {'post_id': linkedin_post_id(post_id), 'headers': safe_headers,
                'path': PAGINATION_PATH + ('?' + parts.query if parts.query else ''),
                'client': deepcopy({k: args[k] for k in ('states', 'screenId', 'knownTemplateIds') if k in args})}
    except (AttributeError, TypeError, ValueError):
        return None


def comment_pagers(flight, post_id):
    """Accept only the observed comment pager schema and exact activity binding."""
    found = {}
    for obj in flight.walk():
        if not isinstance(obj, dict) or obj.get('$type') != PAGINATION_TYPE:
            continue
        if set(obj) - {'$type', 'pagerId', 'trigger', 'retryCount', 'requestedArguments', 'onClientError'}:
            continue
        args = obj.get('requestedArguments')
        if not isinstance(args, dict) or set(args) - {'$type', 'requestedStateKeys', 'payload', 'requestMetadata'}:
            continue
        payload = args.get('payload') if isinstance(args, dict) else None
        if not isinstance(payload, dict) or set(payload) - PAYLOAD_KEYS or not thread_matches(payload, post_id):
            continue
        token = payload.get('pageToken')
        if not isinstance(token, str) or not token or not isinstance(obj.get('pagerId'), str):
            continue
        if any(type(payload.get(k)) is not int or payload[k] <= 0 for k in ('pageSize', 'subsequentPageSize')):
            continue
        try:
            update = payload['updateKey']
            if set(update) - {'feedType', 'items', 'aggregationType', 'isVideoCarousel'}:
                continue
            items = update['items']
            if any(set(item) - {'feedUpdateUrn', 'trackingId'} or set(item['feedUpdateUrn']) != {'updateUrnActivityUrn'} for item in items):
                continue
            if not items or any(linkedin_post_id(item['feedUpdateUrn']['updateUrnActivityUrn']['activityUrn']['activityId']) != linkedin_post_id(post_id) for item in items):
                continue
        except (KeyError, TypeError, AdapterError):
            continue
        found[(obj['pagerId'], token)] = obj
    return list(found.values())


def pagination_body(context, pager, post_id):
    """Keep opaque tokens intact; never invent a cursor or change the read scope."""
    if not isinstance(context, dict) or context.get('post_id') != linkedin_post_id(post_id):
        raise AdapterError('linkedin_read_scope_mismatch')
    flight = FlightData([])
    flight.rows = {'0': pager}
    if len(comment_pagers(flight, post_id)) != 1:
        raise AdapterError('linkedin_comment_pager_unavailable')
    args = deepcopy(pager['requestedArguments'])
    args.update(deepcopy(context['client']))
    return {'pagerId': pager['pagerId'], 'clientArguments': args, 'paginationRequest': deepcopy(pager)}


async def fetch_comments_page(page, context, pager, post_id):
    if linkedin_post_id(page.url) != linkedin_post_id(post_id):
        raise AdapterError('linkedin_read_scope_mismatch')
    body = pagination_body(context, pager, post_id)
    path = context.get('path', PAGINATION_PATH)
    parts = urlsplit(path)
    if (parts.scheme or parts.netloc or parts.path != PAGINATION_PATH or parts.fragment
            or set(k for k, _ in parse_qsl(parts.query, keep_blank_values=True)) - {'sduiid', 'parentSpanId'}):
        raise AdapterError('linkedin_read_scope_mismatch')
    try:
        result = await page.evaluate('''async ({path, headers, body}) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 30000);
          try {
            const response = await fetch(path, {method: 'POST', credentials: 'include',
              redirect: 'manual', headers, body: JSON.stringify(body), signal: controller.signal});
            if (response.status !== 200) return {status: response.status};
            const reader = response.body.getReader(), decoder = new TextDecoder();
            let text = '', size = 0;
            while (true) {
              const {done, value} = await reader.read();
              if (done) break;
              size += value.length;
              if (size > 16000000) { await reader.cancel(); return {status: 200, tooLarge: true}; }
              text += decoder.decode(value, {stream: true});
            }
            return {status: response.status, text: text + decoder.decode()};
          } finally { clearTimeout(timer); }
        }''', {'path': path, 'headers': context['headers'], 'body': body})
    except Exception:
        raise AdapterError('linkedin_comment_read_failed') from None
    if result.get('status') in (0, 401, 403, 429):
        raise AdapterError('linkedin_access_denied_or_rate_limited')
    if result.get('status') != 200:
        raise AdapterError('linkedin_comment_read_failed')
    if result.get('tooLarge'):
        raise AdapterError('linkedin_stream_too_large')
    flight = FlightData(result.get('text', ''))
    if not flight.rows:
        raise AdapterError('linkedin_stream_unavailable')
    return flight
