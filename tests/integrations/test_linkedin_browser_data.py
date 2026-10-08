import json

import pytest

from social_engage.adapters import AdapterError
from social_engage.linkedin_data import (
    FlightData, collect_comments, collect_posts, find_profile, linkedin_profile, linkedin_post_id,
    post_cards, project_card,
)


@pytest.mark.parametrize("value", ["Some-Member", "@Some-Member", "https://www.linkedin.com/in/Some-Member/"])
def test_profile_identity_normalizes(value):
    assert linkedin_profile(value) == "some-member"


@pytest.mark.parametrize("value", ["12345", "urn:li:activity:12345", "https://www.linkedin.com/feed/update/urn:li:activity:12345/",
                                  "https://www.linkedin.com/posts/some-member_post-activity-12345-abCD?tracking=discard"])
def test_post_aliases_share_activity_identity(value):
    assert linkedin_post_id(value) == "urn:li:activity:12345"


@pytest.mark.parametrize("value", ["urn:li:share:12345", "https://evil.invalid/feed/update/urn:li:activity:12345/", "https://www.linkedin.com.evil.invalid/posts/x-activity-12345-a", "https://user:pass@www.linkedin.com/feed/update/urn:li:activity:12345/", "0", "123x"])
def test_post_authority_rejects_unobserved_or_invalid_aliases(value):
    with pytest.raises(AdapterError):
        linkedin_post_id(value)


@pytest.mark.parametrize("value", ["urn:li:organization:123", "https://www.linkedin.com/company/example/", "https://evil.invalid/in/member/", "", "member/path"])
def test_member_scope_rejects_other_identities(value):
    with pytest.raises(AdapterError):
        linkedin_profile(value)


def flight_rows(rows):
    return FlightData(''.join(f'{key}:{json.dumps(value)}\n' for key, value in rows.items()))


def post_card(*, activity='12345', owner='some-member', caption='Exact public text', count='6'):
    return {
        'role': 'listitem', 'componentkey': 'update-card-focus-fixture',
        'children': [
            {'activityUrn': {'activityId': activity}},
            {'vanityName': owner, 'updateKeyContainer': {}},
            {'isExpandableTextV2Enabled': True, 'textProps': {'children': caption}},
            {'aria-label': 'Comment', 'children': count},
        ],
    }


def test_flight_rows_reassemble_chunks_and_utf8_length_framed_text():
    caption = 'First line\nMusik 🎵\nLast line'
    raw = f'a:T{len(caption.encode("utf-8")):x},{caption}\nb:{{"children":"$a"}}\n'
    flight = FlightData([raw[:7], raw[7:19], raw[19:]])
    assert flight.visible_text('$b') == caption
    assert flight.ref('$La') == caption
    assert flight.ref('$@a') == caption


def test_flight_ignores_non_json_imports_without_losing_following_rows():
    flight = FlightData('a:I["module"]\nb:{"children":"observed text"}\n')
    assert 'a' not in flight.rows
    assert flight.visible_text('$b') == 'observed text'


def test_flight_rejects_truncated_length_framed_text():
    with pytest.raises(AdapterError, match='linkedin_stream_truncated'):
        FlightData('a:T10,short')


def test_walk_terminates_on_cyclic_and_duplicate_references():
    flight = flight_rows({'a': {'children': ['$b', '$b']}, 'b': {'children': '$a'}})
    found = list(flight.walk('$a'))
    assert len(found) == 3
    assert len({id(item) for item in found}) == 3


def test_visible_text_uses_only_rendered_children_and_preserves_literal_dollar():
    flight = flight_rows({'a': {'children': ['First ', ['$', 'span', None, {
        'children': '$b', 'action': {'text': 'not evidence'}, 'title': 'not caption',
    }], ' end'], 'tracking': {'children': 'not rendered'}}, 'b': '$$100'})
    assert flight.visible_text('$a') == 'First $100 end'


def test_visible_text_rejects_missing_references_instead_of_truncating_caption():
    flight = flight_rows({'a': {'children': ['visible prefix ', '$f']}})
    with pytest.raises(AdapterError, match='linkedin_text_reference_unavailable'):
        flight.visible_text('$a')


def profile_flight(*, own=True, viewee='ACoABCDEFGHIJKLMN', extra=None):
    rows = {
        'a': {'vanityName': 'some-member', 'nonIterableProfileId': 'ACoABCDEFGHIJKLMN'},
        'b': {'isSelfView': own, 'profileRefreshKey': 'some-member', 'vieweeProfileId': viewee},
    }
    rows.update(extra or {})
    return flight_rows(rows)


def test_profile_requires_matching_self_view_and_exact_member_identifier():
    assert find_profile(profile_flight(), '@Some-Member') == {
        'id': 'urn:li:fsd_profile:ACoABCDEFGHIJKLMN', 'username': 'some-member',
    }


@pytest.mark.parametrize('flight', [
    profile_flight(own=False),
    profile_flight(viewee='ACoDIFFERENT12345'),
    profile_flight(extra={
        'c': {'vanityName': 'some-member', 'profileId': 'ACoDIFFERENT12345'},
        'd': {'isSelfView': True, 'profileRefreshKey': 'some-member', 'vieweeProfileId': 'ACoDIFFERENT12345'},
    }),
])
def test_profile_rejects_nonself_mismatched_and_ambiguous_identity(flight):
    with pytest.raises(AdapterError, match='linkedin_self_profile_unverified'):
        find_profile(flight, 'some-member')


def test_profile_does_not_reuse_another_members_self_view():
    with pytest.raises(AdapterError, match='linkedin_self_profile_unverified'):
        find_profile(profile_flight(), 'other-member')


def test_post_projection_follows_references_and_keeps_unknown_publication_time():
    card = post_card(caption='$b', count='1,234')
    flight = flight_rows({'a': card, 'b': {'children': ['Line 1\n', ['$', 'em', None, {'children': 'Line 2'}]]}})
    actual = project_card(flight, next(post_cards(flight)))
    assert actual['id'] == 'urn:li:activity:12345'
    assert actual['author'] == 'some-member'
    assert actual['url'] == 'https://www.linkedin.com/feed/update/urn:li:activity:12345/'
    assert actual['text'] == 'Line 1\nLine 2'
    assert actual['metrics'] == {'comments': 1234}
    assert actual['comment_count'] == 1234
    assert actual['published_at'] == ''
    assert actual['publication_time_status'] == 'not_exposed_as_exact_timestamp'
    assert 'tracking' not in actual


@pytest.mark.parametrize('extra', [
    {'activityUrn': {'activityId': '98765'}},
    {'vanityName': 'other-member', 'updateKeyContainer': {}},
    {'isExpandableTextV2Enabled': True, 'textProps': {'children': 'Different caption'}},
])
def test_projection_rejects_ambiguous_post_identity_or_caption(extra):
    card = post_card()
    card['children'].append(extra)
    flight = flight_rows({'a': card})
    with pytest.raises(AdapterError, match='linkedin_post_identity_or_text_unavailable'):
        project_card(flight, flight.rows['a'])


@pytest.mark.parametrize('count', ['1.2K', 'Comment', '', '-1'])
def test_projection_does_not_invent_a_comment_count(count):
    flight = flight_rows({'a': post_card(count=count)})
    post = project_card(flight, flight.rows['a'])
    assert post['comment_count'] is None
    assert post['metrics'] == {}
    assert post['metrics_status'] == 'not_provided'


def test_projection_treats_conflicting_observed_counts_as_unknown():
    card = post_card(count='6')
    card['children'].append({'aria-label': 'Comment', 'children': '7'})
    flight = flight_rows({'a': card})
    assert project_card(flight, flight.rows['a'])['comment_count'] is None


def test_projection_rejects_oversized_caption():
    flight = flight_rows({'a': post_card(caption='x' * 20001)})
    with pytest.raises(AdapterError, match='linkedin_post_text_too_large'):
        project_card(flight, flight.rows['a'])


def test_collection_filters_unrelated_and_invalid_cards_and_deduplicates_post_ids():
    invalid = post_card(activity='98765', caption='')
    unrelated = post_card(activity='87654')
    unrelated['componentkey'] = 'some-other-list'
    flight = flight_rows({'a': post_card(), 'b': post_card(), 'c': invalid, 'd': unrelated})
    posts = collect_posts(flight)
    assert [post['id'] for post in posts] == ['urn:li:activity:12345']


@pytest.mark.parametrize('normalizer,value', [
    (linkedin_profile, 'https://www.linkedin.com:444/in/member/'),
    (linkedin_profile, 'https://user@www.linkedin.com/in/member/'),
    (linkedin_profile, 'http://www.linkedin.com/in/member/'),
    (linkedin_post_id, 'https://www.linkedin.com:444/feed/update/urn:li:activity:12345/'),
    (linkedin_post_id, 'https://www.linkedin.com/posts/member-text-share-12345-xyz/'),
    (linkedin_post_id, 'https://www.linkedin.com/feed/update/urn:li:activity:012345/'),
])
def test_identity_url_boundaries(normalizer, value):
    with pytest.raises(AdapterError):
        normalizer(value)


@pytest.mark.parametrize('normalizer,path', [
    (linkedin_profile, '/in/member/'),
    (linkedin_post_id, '/feed/update/urn:li:activity:12345/'),
])
def test_malformed_url_port_raises_a_closed_adapter_error(normalizer, path):
    with pytest.raises(AdapterError):
        normalizer('https://www.linkedin.com:invalid' + path)


def test_flight_rejects_text_frame_that_splits_a_utf8_character():
    with pytest.raises(AdapterError, match='linkedin_stream_invalid_text'):
        FlightData('a:T1,é')


def test_visible_text_terminates_on_cyclic_dictionary_reference():
    flight = flight_rows({'a': {'children': '$b'}, 'b': {'children': '$a'}})
    assert flight.visible_text('$a') == ''


def test_visible_text_preserves_repeated_rendered_references():
    flight = flight_rows({'a': {'children': ['$b', ' / ', '$b']}, 'b': {'children': 'Again'}})
    assert flight.visible_text('$a') == 'Again / Again'


def test_visible_text_preserves_rendered_line_breaks():
    flight = flight_rows({'a': {'children': ['first', ['$', 'br', None, {}], 'second']}})
    assert flight.visible_text('$a') == 'first\nsecond'


def comment_card(*, root='12345', comment='456', owner='some-member', text='Exact comment'):
    return {'componentkey': f'CommentComponentReference_urn:li:comment:(activity:{root},{comment})',
            'children': [
                {'vanityName': owner, 'contentRef': 'observed-marker'},
                {'commentUrn': {'thread': f'urn:li:activity:{root}', 'commentId': comment}},
                {'isExpandableTextV2Enabled': True, 'textProps': {'children': text}},
            ]}


def test_comment_projection_binds_root_without_inventing_parent_or_metrics():
    flight = flight_rows({'a': comment_card(text='$b'), 'b': {'children': 'Exact comment'}})
    assert collect_comments(flight, '12345') == [{
        'id': 'urn:li:comment:(activity:12345,456)', 'author': 'some-member', 'text': 'Exact comment',
        'root_id': 'urn:li:activity:12345', 'parent_id': '', 'parent_binding': 'not_exposed',
        'published_at': '', 'metrics': {}, 'metrics_status': 'not_provided',
    }]


def test_comment_collection_excludes_other_posts_and_deduplicates_stable_ids():
    flight = flight_rows({'a': comment_card(), 'b': comment_card(), 'c': comment_card(root='99999')})
    assert [row['id'] for row in collect_comments(flight, '12345')] == ['urn:li:comment:(activity:12345,456)']


def test_nested_comment_cards_keep_their_own_author_and_text():
    parent = comment_card(owner='parent-author', text='Parent text')
    parent['children'].append('$b')
    reply = comment_card(comment='789', owner='reply-author', text='Reply text')
    comments = collect_comments(flight_rows({'a': parent, 'b': reply}), '12345')
    assert {row['id']: (row['author'], row['text']) for row in comments} == {
        'urn:li:comment:(activity:12345,456)': ('parent-author', 'Parent text'),
        'urn:li:comment:(activity:12345,789)': ('reply-author', 'Reply text'),
    }
    assert all(row['parent_id'] == '' and row['parent_binding'] == 'not_exposed' for row in comments)


def test_nested_comment_for_another_root_cannot_contaminate_selected_comment():
    parent = comment_card(owner='parent-author', text='Parent text')
    parent['children'].append(comment_card(root='99999', owner='other-author', text='Other post text'))
    comments = collect_comments(flight_rows({'a': parent}), '12345')
    assert [(row['id'], row['author'], row['text']) for row in comments] == [
        ('urn:li:comment:(activity:12345,456)', 'parent-author', 'Parent text'),
    ]


def test_comment_current_expandable_text_excludes_author_and_header_text():
    card = comment_card()
    card['children'][2] = {
        'textProps': {'children': 'Current comment text'},
        'bindingKey': 'observed-binding', 'expansionKey': 'observed-expansion',
        'onShowMoreAction': {}, 'onShowLessAction': {},
    }
    card['children'].extend([
        {'textProps': {'children': 'Author name'}, 'maxLineCountExpression': 1, 'textColorExpression': {}},
        {'textProps': {'children': 'Author headline'}, 'maxLineCountExpression': 2, 'textColorExpression': {}},
    ])
    comments = collect_comments(flight_rows({'a': card}), '12345')
    assert len(comments) == 1
    assert comments[0]['text'] == 'Current comment text'
    assert comments[0]['author'] == 'some-member'


@pytest.mark.parametrize('binding', [
    {'thread': 'urn:li:activity:99999', 'commentId': '456'},
    {'thread': 'urn:li:activity:12345', 'commentId': '789'},
    {},
])
def test_comment_requires_matching_payload_and_component_identity(binding):
    card = comment_card()
    card['children'][1]['commentUrn'] = binding
    assert collect_comments(flight_rows({'a': card}), '12345') == []


@pytest.mark.parametrize('extra', [
    {'contentRef': 'other-marker', 'vanityName': 'other-member'},
    {'isExpandableTextV2Enabled': True, 'textProps': {'children': 'Other comment'}},
])
def test_comment_rejects_ambiguous_owner_or_rendered_text(extra):
    card = comment_card()
    card['children'].append(extra)
    assert collect_comments(flight_rows({'a': card}), '12345') == []
