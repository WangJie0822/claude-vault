import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import _hook_common


def test_fail_open_swallows_exception():
    import pytest
    def boom():
        raise RuntimeError("x")
    with pytest.raises(SystemExit) as e:
        _hook_common.fail_open(boom)
    assert e.value.code == 0


def test_read_stdin_json_empty(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _hook_common.read_stdin_json() == {}
