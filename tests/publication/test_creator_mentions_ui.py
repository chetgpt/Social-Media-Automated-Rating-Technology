"""Native mention composer tests with a synthetic visible editor only."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import engage_creator_mentions_ui as ui


@pytest.fixture(autouse=True)
def synthetic_page_has_no_challenge(monkeypatch):
    # These editor-only fakes have no DOM. Challenge observations and real
    # integration rejection are exercised separately below and in guard tests.
    async def clear(*_args, **_kwargs):
        return None
    monkeypatch.setattr(ui, "ensure_no_challenge", clear)


class Editor:
    def __init__(self, *, fill_fails=False):
        self.text = "Unapproved existing text"
        self.links = []
        self.fill_fails = fill_fails

    async def focus(self): pass

    async def scroll_into_view_if_needed(self): pass

    async def fill(self, text):
        if self.fill_fails: raise RuntimeError("unsupported fill")
        self.text, self.links = text, []

    async def evaluate(self, script):
        if "document.activeElement" in script:
            return True
        return {"text": self.text, "links": deepcopy(self.links)}


class Keyboard:
    def __init__(self, editor): self.editor, self.selected_all = editor, False
    async def insert_text(self, text): self.editor.text += text
    async def type(self, text, **kwargs): self.editor.text += text
    async def press(self, key):
        if key == "ControlOrMeta+A": self.selected_all = True
        elif key == "Shift+Enter": self.editor.text += "\n"
        elif key == "Backspace":
            if self.selected_all:
                self.editor.text, self.editor.links, self.selected_all = "", [], False
            else: self.editor.text = self.editor.text[:-1]


async def bring_to_front(): pass


@pytest.mark.parametrize("fill_fails", [False, True])
def test_composer_clears_existing_text_resolves_two_handles_and_checks_exact_text(monkeypatch, fill_fails):
    editor = Editor(fill_fails=fill_fails)
    page = SimpleNamespace(keyboard=Keyboard(editor), bring_to_front=bring_to_front)
    selected = []

    async def choose(page, handle, *, editor=None, trace=None):
        selected.append(handle)
        editor.links.append({"href": f"/@{handle}", "text": "@" + handle})
        editor.text += " "

    monkeypatch.setattr(ui, "_choose_exact_suggestion", choose)
    final = "AI-assisted perspective: 8.4/10. Clear tutorial. @jazz_a: Shared jazz technique. @jazz_l: Shared learning goal."
    asyncio.run(ui.compose_mentions(page, editor, final, ["jazz_a", "jazz_l"]))
    assert editor.text == final
    assert selected == ["jazz_a", "jazz_l"]
    assert "Unapproved existing" not in editor.text


@pytest.mark.parametrize("mutation", ["plain_text", "wrong_handle", "extra_text", "duplicate_tag"])
def test_plain_handles_or_wrong_editor_contents_cannot_pass_native_verification(mutation):
    editor = Editor()
    editor.text = "AI-assisted perspective: 8.4/10. @jazz_a: Related voicings."
    final = editor.text
    editor.links = [{"href": "/@jazz_a", "text": "@jazz_a"}]
    if mutation == "plain_text": editor.links = []
    elif mutation == "wrong_handle": editor.links[0]["href"] = "/@rock_b"
    elif mutation == "extra_text": editor.text = "Old text " + editor.text
    elif mutation == "duplicate_tag": editor.links.append(editor.links[0].copy())
    with pytest.raises(RuntimeError):
        asyncio.run(ui.verify_editor(editor, final, ["jazz_a"]))


class Option:
    def __init__(self, handle, *, href=None, visible=True):
        self.handle, self.href, self.visible, self.clicked = handle, href, visible, False
        self.connected = True
    async def is_visible(self): return self.visible
    async def evaluate(self, script):
        return {"text": "@" + self.handle, "hrefs": [self.href or "/@" + self.handle], "connected": self.connected, "visible": self.visible}
    def as_element(self): return self
    async def click(self, **kwargs): self.clicked = True
    async def wait_for_element_state(self, *args, **kwargs): pass
    async def dispose(self): pass


class Options:
    def __init__(self, options): self.options = options
    async def count(self): return len(self.options)
    def nth(self, index): return self.options[index]
    async def evaluate_all(self, script):
        return [{"index": index, "visible": await option.is_visible(), **(await option.evaluate(script))}
                for index, option in enumerate(self.options[:50])]


class Snapshot:
    def __init__(self, options, on_read=None):
        self.options = list(options)
        self.on_read = on_read
    async def evaluate(self, script):
        result = [{"index": i, **(await option.evaluate(script))} for i, option in enumerate(self.options)]
        if self.on_read: self.on_read()
        return result
    async def evaluate_handle(self, script, index): return self.options[index]
    async def dispose(self): pass


def suggestion_page(options, on_read=None):
    async def evaluate_handle(script, selector):
        return Snapshot(options, on_read)
    return SimpleNamespace(evaluate_handle=evaluate_handle)


def test_suggestion_selection_uses_exact_creator_identity():
    wrong, wanted = Option("jazz_alike"), Option("jazz_a")
    page = suggestion_page([wrong, wanted])
    asyncio.run(ui._choose_exact_suggestion(page, "jazz_a"))
    assert wanted.clicked and not wrong.clicked


def test_ambiguous_suggestions_never_click_any_creator():
    options = [Option("jazz_a"), Option("jazz_a")]
    page = suggestion_page(options)
    with pytest.raises(RuntimeError, match="ambiguous"):
        asyncio.run(ui._choose_exact_suggestion(page, "jazz_a"))
    assert not any(option.clicked for option in options)


def test_unavailable_mention_fails_without_submitting(monkeypatch):
    page = suggestion_page([])
    async def no_wait(_): pass
    monkeypatch.setattr(ui.asyncio, "sleep", no_wait)
    with pytest.raises(RuntimeError, match="nothing was submitted"):
        asyncio.run(ui._choose_exact_suggestion(page, "jazz_a"))


@pytest.mark.parametrize("url", ["https://evil.test/@jazz_a", "/@jazz_a/video/123", "javascript:/@jazz_a"])
def test_only_tiktok_profile_links_prove_a_resolved_mention(url):
    assert ui.profile_handle(url) == ""


def test_display_name_match_cannot_select_different_username():
    class TextOption(Option):
        async def evaluate(self, script):
            return {"text": self.handle, "hrefs": [], "connected": self.connected, "visible": self.visible}
    wrong = TextOption("pianoarchivee\n\n_pianoarchive_")
    right = TextOption("Pianoarchivee\n\npianoarchivee")
    page = suggestion_page([wrong, right])
    asyncio.run(ui._choose_exact_suggestion(page, "pianoarchivee"))
    assert right.clicked and not wrong.clicked


def test_suggestion_reordering_keeps_the_observed_account_node():
    wanted, wrong = Option('jazz_a'), Option('lookalike')
    options = [wanted, wrong]
    page = suggestion_page(options, lambda: options.reverse())
    asyncio.run(ui._choose_exact_suggestion(page, 'jazz_a'))
    assert wanted.clicked and not wrong.clicked


@pytest.mark.parametrize('mutation', ['replaced_identity', 'detached'])
def test_changed_suggestion_identity_is_rejected_before_click(mutation):
    wanted = Option('jazz_a')
    def change():
        if mutation == 'replaced_identity': wanted.handle = 'lookalike'
        else: wanted.connected = False
    page = suggestion_page([wanted], change)
    with pytest.raises(RuntimeError, match='changed before selection'):
        asyncio.run(ui._choose_exact_suggestion(page, 'jazz_a'))
    assert not wanted.clicked


class NativeEditor(Editor):
    def __init__(self):
        super().__init__()
        self.nodes = []

    async def evaluate(self, script, args=None):
        if "document.activeElement" in script:
            return True
        if args is not None:
            return self.nodes[args['index']] is args['node']
        return {'text': self.text, 'links': [], 'native_labels': [n['label'] for n in self.nodes]}

    def locator(self, selector):
        editor = self
        class Nodes:
            def nth(self, index):
                class Node:
                    async def element_handle(self):
                        return editor.nodes[index]
                return Node()
        return Nodes()


def native_composer(monkeypatch):
    editor = NativeEditor()
    keyboard = Keyboard(editor)
    page = SimpleNamespace(keyboard=keyboard, bring_to_front=bring_to_front)
    mapping = {'pianoarchivee': '@Pianoarchivee', 'midi.piano': '@MIDI Piano 🎹'}
    async def choose(page, handle, *, editor=None, trace=None):
        token = '@' + handle
        assert editor.text.endswith(token)
        label = mapping[handle]
        editor.text = editor.text[:-len(token)] + label + ' '
        editor.nodes.append({'label': label})
    monkeypatch.setattr(ui, '_choose_exact_suggestion', choose)
    return page, editor, list(mapping), list(mapping.values())


def test_two_native_display_names_preserve_reviewed_text(monkeypatch):
    page, editor, handles, labels = native_composer(monkeypatch)
    final = 'AI-assisted perspective: 8.2/10 - Specific observation. ' + labels[0] + ': First connection. ' + labels[1] + ': Second connection.'
    bindings = asyncio.run(ui.compose_mentions(page, editor, final, handles, labels=labels))
    assert editor.text == final
    asyncio.run(ui.verify_editor(editor, final, handles, labels=labels, bindings=bindings))


@pytest.mark.parametrize('mutation', ['no_binding', 'wrong_account', 'replaced_node', 'changed_text', 'changed_label'])
def test_native_display_name_alone_cannot_prove_creator_identity(monkeypatch, mutation):
    page, editor, handles, labels = native_composer(monkeypatch)
    final = labels[0]+': First connection. '+labels[1]+': Second connection.'
    bindings = asyncio.run(ui.compose_mentions(page, editor, final, handles, labels=labels))
    if mutation == 'no_binding': bindings = None
    elif mutation == 'wrong_account': bindings[0] = ('someone_else', labels[0], editor.nodes[0])
    elif mutation == 'replaced_node': editor.nodes[0] = editor.nodes[0].copy()
    elif mutation == 'changed_text': editor.text += ' extra'
    elif mutation == 'changed_label': editor.nodes[0]['label'] = '@Other'
    with pytest.raises(RuntimeError):
        asyncio.run(ui.verify_editor(editor, final, handles, labels=labels, bindings=bindings))


def test_changed_native_label_requires_new_review_not_silent_substitution(monkeypatch):
    page, editor, handles, labels = native_composer(monkeypatch)
    with pytest.raises(RuntimeError, match='display label differs'):
        asyncio.run(ui.compose_mentions(page, editor, '@pianoarchivee: Connection.', handles[:1]))


def test_multiline_comment_is_rejected_before_any_editor_action():
    async def run():
        with pytest.raises(RuntimeError, match="one paragraph"):
            await ui.compose_mentions(None, None, "Base\n@someone: Connection.", ["someone"])
    asyncio.run(run())


@pytest.mark.parametrize("with_trace", [False, True])
def test_traced_operation_preserves_result_and_does_not_record_result_data(with_trace):
    events = []
    secret_result = {"url": "https://private.test/?token=secret"}
    async def operation():
        return secret_result
    result = asyncio.run(ui.traced_operation(
        "suggestion_snapshot", operation(),
        trace=(lambda event, record: events.append({"event": event, **record})) if with_trace else None,
        handle="guitar.les", attempt=2,
    ))
    assert result is secret_result
    assert [item["event"] for item in events] == (["started", "passed"] if with_trace else [])
    assert "private" not in json.dumps(events) and "secret" not in json.dumps(events)


def test_suggestion_timeout_diagnostics_preserve_original_error_when_disposal_also_fails():
    import engage_mentions_probe as probe

    timeout = TimeoutError("https://private.test/?token=secret Authorization: private")
    disposed = []
    class FailingOption(Option):
        async def click(self, **kwargs):
            raise timeout
        async def dispose(self):
            disposed.append("option")
            raise RuntimeError("private cleanup token=secret")
    class FailingSnapshot(Snapshot):
        async def dispose(self):
            disposed.append("snapshot")
            raise RuntimeError("private snapshot token=secret")
    async def snapshot(*_args):
        return FailingSnapshot([FailingOption("guitar.les")])

    report = {"phase": "exact_creator_label_observation"}
    trace = probe.ProbeDiagnostics(report)
    with pytest.raises(TimeoutError) as result:
        asyncio.run(ui._choose_exact_suggestion(SimpleNamespace(evaluate_handle=snapshot), "guitar.les", trace=trace))
    assert result.value is timeout
    assert disposed == ["option", "snapshot"]
    failure = report["diagnostics"]["failure"]
    assert failure["code"] == "suggestion_click_timeout"
    assert failure["creator_handle"] == "guitar.les"
    assert failure["timeout_ms"] == 5000
    observed = next(item for item in report["diagnostics"]["events"] if item["event"] == "observed")
    assert (observed["candidate_count"], observed["visible_candidate_count"], observed["exact_candidate_count"]) == (1, 1, 1)
    assert "private" not in json.dumps(report) and "secret" not in json.dumps(report)


def test_traced_operation_without_callback_preserves_the_original_exception():
    failure = TimeoutError("transport detail")
    async def failed():
        raise failure
    with pytest.raises(TimeoutError) as result:
        asyncio.run(ui.traced_operation("editor_focus", failed()))
    assert result.value is failure


class InsertionSnapshot:
    """Synthetic read-only editor observations for the insertion gate."""
    def __init__(self, *, before_native=0, native_added=0, before_links=None, added_links=None, preserved=True):
        self.state = {
            "connected": True, "existing_native_preserved": preserved, "existing_links_preserved": preserved,
            "native_added_count": native_added, "link_added_count": len(added_links or []),
            "before_native_count": before_native, "after_native_count": before_native + native_added,
            "before_link_hrefs": before_links or [],
            "after_link_hrefs": (before_links or []) + (added_links or []), "added_link_hrefs": added_links or [],
        }
        self.disposed = False
    async def evaluate(self, _script):
        return deepcopy(self.state)
    async def dispose(self):
        self.disposed = True


@pytest.mark.parametrize("insertion", ["native", "exact_link"])
def test_editor_insertion_can_succeed_while_the_selected_suggestion_remains_visible(insertion):
    observations = InsertionSnapshot(before_native=1)
    actions = []
    class InsertOption(Option):
        async def click(self, **_kwargs):
            actions.append("click")
            assert actions == ["snapshot", "click"]
            self.clicked = True
            if insertion == "native":
                observations.state.update(native_added_count=1, after_native_count=2)
            else:
                observations.state.update(link_added_count=1, added_link_hrefs=["/@guitar.les"], after_link_hrefs=["/@guitar.les"])
        async def wait_for_element_state(self, *_args, **_kwargs):
            raise AssertionError("a visible suggestion must not prevent proven editor insertion")
    async def editor_snapshot(_script):
        actions.append("snapshot")
        return observations
    wanted = InsertOption("guitar.les")
    asyncio.run(ui._choose_exact_suggestion(suggestion_page([wanted]), "guitar.les",
                                            editor=SimpleNamespace(evaluate_handle=editor_snapshot)))
    assert wanted.clicked and wanted.visible
    assert observations.disposed


@pytest.mark.parametrize("before_native,before_links", [(0, []), (1, []), (0, ["/@guitar.les"])])
def test_no_new_entity_times_out_even_with_plain_handle_or_preexisting_mentions(before_native, before_links):
    import engage_mentions_probe as probe

    observations = InsertionSnapshot(before_native=before_native, before_links=before_links)
    observations.state["text"] = "@guitar.les"
    report = {"phase": "exact_creator_label_observation"}
    trace = probe.ProbeDiagnostics(report)
    with pytest.raises(TimeoutError):
        asyncio.run(ui.traced_operation("mention_insertion_settlement",
                    ui._wait_for_mention_insertion(observations, "guitar.les", trace=trace, timeout_ms=10),
                    trace=trace, handle="guitar.les", timeout_ms=10))
    assert report["diagnostics"]["failure"]["code"] == "mention_insertion_settlement_timeout"
    state = next(item for item in report["diagnostics"]["events"] if item["event"] == "observed")
    assert state["before_native_count"] == state["after_native_count"] == before_native
    assert state["before_exact_link_count"] == state["after_exact_link_count"] == len(before_links)
    assert "text" not in state


@pytest.mark.parametrize("kwargs,reason", [
    ({"native_added": 2}, "unexpected number"),
    ({"added_links": ["/@different_creator"]}, "different creator"),
    ({"native_added": 1, "added_links": ["/@different_creator"]}, "different creator"),
    ({"added_links": ["/@guitar.les", "/@guitar.les"]}, "unexpected number"),
    ({"native_added": 1, "preserved": False}, "identities changed"),
])
def test_new_entity_gate_rejects_ambiguous_or_changed_creator_entities(kwargs, reason):
    with pytest.raises(RuntimeError, match=reason):
        asyncio.run(ui._wait_for_mention_insertion(InsertionSnapshot(**kwargs), "guitar.les", timeout_ms=10))


def test_insertion_trace_keeps_counts_without_profile_link_values():
    import engage_mentions_probe as probe

    observations = InsertionSnapshot(added_links=["https://www.tiktok.com/@guitar.les?token=secret"])
    report = {"phase": "exact_creator_label_observation"}
    asyncio.run(ui._wait_for_mention_insertion(observations, "guitar.les", trace=probe.ProbeDiagnostics(report)))
    state = report["diagnostics"]["events"][0]
    assert state["before_exact_link_count"] == 0 and state["after_exact_link_count"] == 1
    assert "secret" not in json.dumps(report) and "https" not in json.dumps(report)


def test_composer_does_not_type_when_editor_focus_is_not_obtained():
    class UnfocusedEditor(Editor):
        async def evaluate(self, script):
            if "document.activeElement" in script:
                return False
            return await super().evaluate(script)
    editor = UnfocusedEditor()
    page = SimpleNamespace(keyboard=Keyboard(editor), bring_to_front=bring_to_front)
    with pytest.raises(RuntimeError, match="did not receive focus"):
        asyncio.run(ui.compose_mentions(page, editor, "AI-assisted: Specific text.", []))
    assert editor.text == "Unapproved existing text"
    assert page.keyboard.selected_all is False


def test_visible_challenge_blocks_editor_focus_and_keyboard(monkeypatch):
    from engage_browser_guard import HumanVerificationRequired
    async def challenge(_page, phase):
        raise HumanVerificationRequired(phase, "captcha_container")
    monkeypatch.setattr(ui, "ensure_no_challenge", challenge)
    editor = Editor()
    page = SimpleNamespace(keyboard=Keyboard(editor), bring_to_front=bring_to_front)
    with pytest.raises(HumanVerificationRequired):
        asyncio.run(ui.compose_mentions(page, editor, "AI-assisted: Specific text.", []))
    assert editor.text == "Unapproved existing text"
    assert page.keyboard.selected_all is False


def test_challenge_appearing_during_suggestion_failure_is_reported(monkeypatch):
    from engage_browser_guard import HumanVerificationRequired
    checks = []
    async def challenge(_page, phase):
        checks.append(phase)
        if phase == "mention_suggestion_selection":
            raise HumanVerificationRequired(phase, "captcha_container")
    monkeypatch.setattr(ui, "ensure_no_challenge", challenge)
    async def failure(*_args, **_kwargs):
        raise TimeoutError("transport private")
    monkeypatch.setattr(ui, "_choose_exact_suggestion_impl", failure)
    with pytest.raises(HumanVerificationRequired):
        asyncio.run(ui._choose_exact_suggestion(object(), "jazz_a"))
    assert checks == ["mention_suggestion_selection"]
