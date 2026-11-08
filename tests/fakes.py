"""A scripted stand-in for anthropic.Anthropic: tests run the agent with no API calls, no cost."""

from types import SimpleNamespace


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, tool_input, id_):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=id_)


def response(*blocks, stop_reason=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        # Snapshot the history as it was at call time (the agent keeps appending to the list).
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)
