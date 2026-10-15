import pytest

from codepilot.agent import CodePilot


class DummyRegistry:
    pass


def test_rewind_refuses_to_overwrite_external_change(tmp_path):
    app = CodePilot(tmp_path, tmp_path / "state.db", DummyRegistry())
    session_id = app.new_session()
    path = tmp_path / "sample.txt"
    before = app.file_checkpoints.capture([path])
    path.write_text("assistant", encoding="utf-8")
    after = app.file_checkpoints.capture([path])
    checkpoint_id = app.file_checkpoints.record(session_id, "write_file", before, after)
    path.write_text("user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since"):
        app.file_checkpoints.rewind(session_id, checkpoint_id)
    assert path.read_text(encoding="utf-8") == "user edit"
    app.close()


def test_rewind_multiple_checkpoints_to_original_content(tmp_path):
    app = CodePilot(tmp_path, tmp_path / "state.db", DummyRegistry())
    session_id = app.new_session()
    path = tmp_path / "sample.txt"
    path.write_text("original", encoding="utf-8")
    first_before = app.file_checkpoints.capture([path])
    path.write_text("first", encoding="utf-8")
    first_after = app.file_checkpoints.capture([path])
    first_id = app.file_checkpoints.record(session_id, "write_file", first_before, first_after)
    second_before = app.file_checkpoints.capture([path])
    path.write_text("second", encoding="utf-8")
    second_after = app.file_checkpoints.capture([path])
    app.file_checkpoints.record(session_id, "write_file", second_before, second_after)
    app.file_checkpoints.rewind(session_id, first_id)
    assert path.read_text(encoding="utf-8") == "original"
    assert app.file_checkpoints.list(session_id) == []
    app.close()
