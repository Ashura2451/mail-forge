import pytest

from mailforge.application.transform import count_legacy_list, load_legacy_list


def test_legacy_list_parser_rejects_executable_content(tmp_path):
    path = tmp_path / "values.txt"
    path.write_text("__import__('os').system('touch escaped')", encoding="utf-8")
    with pytest.raises((ValueError, SyntaxError)):
        load_legacy_list(path)
    assert not (tmp_path / "escaped").exists()


def test_legacy_list_parser_enforces_size_limit(tmp_path):
    path = tmp_path / "values.txt"
    path.write_text("['a@example.com']", encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        count_legacy_list(path, max_bytes=1)
