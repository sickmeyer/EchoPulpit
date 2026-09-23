from types import SimpleNamespace

from sermon_pipeline import ClaudeBackend


class FakeStream:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.resp


class FakeClient:
    def __init__(self, resp):
        self.calls = []
        self.resp = resp
        client = self

        class Messages:
            def stream(self, **kw):
                client.calls.append(kw)
                return FakeStream(client.resp)

        self.messages = Messages()


def _resp(text="Article", stop="end_turn"):
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop, stop_details=None, usage=None)


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]


def test_streams_with_adaptive_thinking_and_claude_ceiling():
    client = FakeClient(_resp("  Hello  "))
    out = ClaudeBackend(client, "claude-opus-5-5").chat(
        MESSAGES, {"claude_max_tokens": 48000, "max_tokens": 16000, "thinking_effort": "medium"})
    assert out == "Hello"
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["max_tokens"] == 48000
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "go"}]
    assert "temperature" not in call and "top_p" not in call


def test_small_pass_overrides_do_not_shrink_claude_ceiling():
    # The repair/regenerate passes call chat() with {**cfg, "max_tokens": 8000}.
    client = FakeClient(_resp())
    ClaudeBackend(client, "m").chat(MESSAGES, {"claude_max_tokens": 48000, "max_tokens": 8000})
    assert client.calls[0]["max_tokens"] == 48000


def test_falls_back_to_max_tokens_without_claude_setting():
    client = FakeClient(_resp())
    ClaudeBackend(client, "m").chat(MESSAGES, {"max_tokens": 16000})
    assert client.calls[0]["max_tokens"] == 16000


def test_refusal_returns_empty_text():
    client = FakeClient(_resp("partial", stop="refusal"))
    assert ClaudeBackend(client, "m").chat(MESSAGES, {"claude_max_tokens": 48000}) == ""
