from codepilot.models import ModelRegistry


def test_all_provider_adapters_can_be_constructed(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "test-key")
    registry = ModelRegistry()
    for alias in ("deepseek", "qwen", "openai"):
        model = registry.get(alias)
        assert callable(model.bind_tools)
        assert registry.get(alias) is model
