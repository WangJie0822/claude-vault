"""tests for _yaml_scalar list 分支扩展"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from _frontmatter import _yaml_scalar


def test_yaml_scalar_string_no_special_chars():
    assert _yaml_scalar('hello') == 'hello'


def test_yaml_scalar_string_with_colon_quoted():
    out = _yaml_scalar('foo: bar')
    assert out.startswith('"') and out.endswith('"')


def test_yaml_scalar_list_simple():
    # 期望 YAML inline 风格而非 Python repr
    assert _yaml_scalar(['spec', 'archived']) == '[spec, archived]'


def test_yaml_scalar_list_single_item():
    assert _yaml_scalar(['spec']) == '[spec]'


def test_yaml_scalar_list_empty():
    assert _yaml_scalar([]) == '[]'


def test_yaml_scalar_list_with_special_char_quotes_items():
    out = _yaml_scalar(['a:b', 'c'])
    assert out == '["a:b", c]'


def test_yaml_scalar_number_unchanged():
    assert _yaml_scalar(1726894000) == '1726894000'
