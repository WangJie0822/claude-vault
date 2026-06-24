"""tests for scripts/_sensitive_patterns.py"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from _sensitive_patterns import is_sensitive_path, is_sensitive_content


# 路径模式应命中
@pytest.mark.parametrize("path", [
    "/Users/test/project/.env",
    "D:/project/.env.production",
    "/home/u/secrets/credentials.json",
    "/home/u/credential.toml",
    "/Users/testuser/.claude/CLAUDE.local.md",
    "C:/Users/testuser/.claude/settings.json",
    "C:/Users/testuser/.claude/settings.local.json",
    "C:/Users/testuser/.claude/projects/abc/memory/state.json",
    "C:/Users/testuser/.claude/jobs/xxx/tmp.json",
    "/path/some-secret-config.yaml",
    "/path/api-token-store.json",
])
def test_sensitive_path_hit(path):
    assert is_sensitive_path(path) is True


# 白名单豁免：文件名含 design/plan/spec/doc 关键词的 markdown 文档
@pytest.mark.parametrize("path", [
    "D:/Work/cc/docs/superpowers/specs/2026-05-26-credentials-path-unify-design.md",
    "D:/Work/cc/docs/superpowers/plans/2026-05-26-lark-cli-token-refresh.md",
    "~/.claude/knowledge-vault/Claude Code/specs/2026-04-22-secrets-handling-design.md",
])
def test_sensitive_path_whitelist_for_docs(path):
    assert is_sensitive_path(path) is False


# 普通项目 spec/plan 不命中
@pytest.mark.parametrize("path", [
    "D:/Work/cc/docs/superpowers/specs/2026-05-28-design.md",
    "C:/project/README.md",
    "~/.claude/knowledge-vault/项目笔记/x/specs/feature.md",
])
def test_sensitive_path_normal(path):
    assert is_sensitive_path(path) is False


# 内容启发式：私钥 / API key
def test_sensitive_content_private_key():
    content = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END..."
    assert is_sensitive_content(content) is True


def test_sensitive_content_api_key_assignment():
    content = 'api_key = "abcdef0123456789abcdef0123456789"'
    assert is_sensitive_content(content) is True


def test_sensitive_content_normal_markdown():
    content = "# Spec\n\nThis spec talks about credentials handling.\n"
    assert is_sensitive_content(content) is False


# Issue 1 回归测试：白名单优先级倒置 —— 硬 deny 名单必须凌驾于白名单段/关键词之上
@pytest.mark.parametrize("path", [
    "/repo/docs/CLAUDE.local.md",       # 路径段白名单不应放行
    "/repo/notes/CLAUDE.local.md",
    "/repo/plans/CLAUDE.local.md",
    "/repo/docs/.env",                   # .env 在白名单段下也不能放行
    "/repo/specs/credentials.json",
])
def test_hard_deny_overrides_whitelist(path):
    assert is_sensitive_path(path) is True


# Issue 2 回归测试：_API_KEY_RE 缺词边界 —— prose 子串 'mysecret' 不应命中
def test_sensitive_content_prose_secret_not_match():
    """'mysecret = ...' 是 prose 子串误判，不应命中"""
    content = 'mysecret = "abcdefghij0123456789abc"'
    assert is_sensitive_content(content) is False


def test_sensitive_content_api_key_still_matches_at_word_boundary():
    """正例必须仍命中"""
    assert is_sensitive_content('api_key = "abcdef0123456789abcdef0123456789"') is True
    assert is_sensitive_content('access_token: "abcdef0123456789abcdef"') is True


def test_sensitive_content_normal_md_with_word_secret_unaffected():
    """正文 '...mysecret...' 不命中（同 prose 测试，再次保险）"""
    assert is_sensitive_content('# Notes about mysecret usage tips') is False
