from codepilot.cli_router import SlashCommandRouter


def test_router_parses_normalizes_and_dispatches():
    calls = []

    def handler(app, name, value):
        calls.append((app, name, value))
        return False

    router = SlashCommandRouter(["/status"], handler)
    assert router.dispatch("app", "  /STATUS   details  ") is False
    assert calls == [("app", "/status", "details")]
    assert router.dispatch("app", "/missing") is None
