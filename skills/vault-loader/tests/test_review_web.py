"""网页版标注界面：勾选后一次提交，直接写回 annotations.jsonl。

用户要求：「应该生成交互式网页，用户在网页勾选提交，而不是在命令行依次执行」。

命令行版逐条 `input()` 的问题很实在：一条一条问、看不到全局、改不了已答的、
中途退出就只存了一半。网页版把整批摆开，可以来回改、一次提交。

落地方式是本地 HTTP 服务（只绑 127.0.0.1）+ 自动开浏览器：勾选后 POST 回来直接写
`annotations.jsonl`，一条命令闭环。不用「导出 JSON 再 CLI 导入」是因为那多一步手工
搬运，而这套工具的痛点恰恰是标注成本太高（实测 92.3% 的精度标注是 unsure）。
"""

import json

from scripts.analyze_metrics import (apply_web_annotations, build_review_html,
                                     load_annotations)


def _item(path="n/a.md", kind="admitted_list", count=3, contexts=None):
    return {"path": path, "kind": kind, "count": count,
            "topical_max": 9.0,
            "contexts": contexts if contexts is not None
            else [("这次问的是内存泄露", ["内存", "泄露"])],
            "unreadable": {"corrupt": 0, "unresolved": 0}}


# ── 页面内容 ────────────────────────────────────────────────────────────

def test_html_contains_item_data():
    html = build_review_html([_item()])
    for frag in ("n/a.md", "admitted_list", "这次问的是内存泄露", "内存"):
        assert frag in html, f"页面缺少 {frag!r}"


def test_html_reports_unreadable_counts():
    html = build_review_html([_item(contexts=[("可读的", [])])
                              | {"unreadable": {"corrupt": 2, "unresolved": 1}}])
    assert "2" in html and "1" in html


# ── 转义：页面嵌入的是笔记路径与 prompt 原文，都是外部内容 ────────────────

def test_html_escapes_note_path():
    """路径来自 Vault，可能含 HTML 元字符。

    变异验证：去掉 escape，本用例转红。
    """
    html = build_review_html([_item(path="n/<script>alert(1)</script>.md")])
    assert "<script>alert(1)</script>" not in html, "笔记路径未转义，页面可被注入"
    assert "&lt;script&gt;" in html


def test_html_escapes_prompt_text():
    """提问原文来自 transcript，同样是不可信输入。"""
    html = build_review_html([_item(contexts=[("<img src=x onerror=alert(1)>", [])])])
    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img" in html


def test_html_escapes_hits():
    html = build_review_html([_item(contexts=[("正常提问", ["<b>x</b>"])])])
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;" in html


def test_embedded_json_is_not_html_breakable():
    """内嵌 JSON 里的 `</script>` 必须被打断，否则提前闭合 script 标签。

    变异验证：去掉对 `</` 的处理，本用例转红。
    """
    html = build_review_html([_item(path="a</script><script>evil()</script>.md")])
    assert "</script><script>evil()" not in html


# ── 提交处理 ────────────────────────────────────────────────────────────

def test_apply_writes_annotations(tmp_path):
    payload = [{"path": "n/a.md", "kind": "admitted_list", "verdict": "relevant"},
               {"path": "n/b.md", "kind": "near_miss", "verdict": "irrelevant"}]
    n, errs = apply_web_annotations(tmp_path, payload)
    assert (n, errs) == (2, [])
    done = load_annotations(tmp_path)
    assert done[("admitted_list", "n/a.md")] == "relevant"
    assert done[("near_miss", "n/b.md")] == "irrelevant"


def test_apply_rejects_bad_verdict(tmp_path):
    """非法 verdict 必须被拒且不写入 —— 请求体来自浏览器，不可信。

    变异验证：去掉校验，本用例转红。
    """
    n, errs = apply_web_annotations(
        tmp_path, [{"path": "n/a.md", "kind": "admitted_list", "verdict": "drop"}])
    assert n == 0 and errs, "非法 verdict 被接受了"
    assert not load_annotations(tmp_path)


def test_apply_rejects_bad_kind(tmp_path):
    n, errs = apply_web_annotations(
        tmp_path, [{"path": "n/a.md", "kind": "bogus", "verdict": "relevant"}])
    assert n == 0 and errs
    assert not load_annotations(tmp_path)


def test_apply_skips_unanswered(tmp_path):
    """没勾选的条目跳过，不写入也不算错误。"""
    n, errs = apply_web_annotations(
        tmp_path, [{"path": "n/a.md", "kind": "admitted_list", "verdict": ""},
                   {"path": "n/b.md", "kind": "admitted_list", "verdict": "relevant"}])
    assert n == 1 and errs == []
    assert len(load_annotations(tmp_path)) == 1


def test_apply_tolerates_malformed_payload(tmp_path):
    """请求体结构不对时报错而不是抛异常 —— 它决定 HTTP 响应码。"""
    n, errs = apply_web_annotations(tmp_path, ["not a dict", {"path": 1}])
    assert n == 0 and len(errs) == 2


# ── 端到端：真起服务、真发请求 ──────────────────────────────────────────

def test_serve_review_end_to_end(tmp_path):
    """GET 拿到页面、POST 写入标注、提交后服务自动退出、返回码 0。

    纯函数测不到的三件事：路由是否接对、表单字段名是否与页面渲染的一致、
    提交后是否真的会退出（不退出就会把调用者永远挂住）。

    变异验证：把 do_POST 的路由从 /submit 改掉，本用例转红。
    """
    import socket
    import threading
    import time
    import urllib.parse
    import urllib.request
    from scripts.analyze_metrics import serve_review

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    items = [_item(path="n/a.md"), _item(path="n/b.md", kind="near_miss")]
    box = {}
    th = threading.Thread(
        target=lambda: box.update(rc=serve_review(tmp_path, items, port=port,
                                                  open_browser=False)),
        daemon=True)
    th.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):                      # 等服务起来
        try:
            urllib.request.urlopen(base + "/", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)

    html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
    assert "n/a.md" in html and "n/b.md" in html
    # 钉住表单控件本身：只断言路径出现是不够的，内嵌 JSON 里也有路径，
    # 把整批卡片删掉那条断言照样绿（变异实测）。
    assert 'name="v0"' in html and 'name="v1"' in html, "没有渲染出可勾选的表单"

    body = urllib.parse.urlencode({
        "p0": "n/a.md", "k0": "admitted_list", "v0": "relevant",
        "p1": "n/b.md", "k1": "near_miss", "v1": "irrelevant",
    }).encode("utf-8")
    resp = urllib.request.urlopen(base + "/submit", data=body,
                                  timeout=5).read().decode("utf-8")
    assert "已保存 2 条" in resp

    th.join(timeout=10)
    assert not th.is_alive(), "提交后服务没有退出，会把调用者挂住"
    done = load_annotations(tmp_path)
    assert done[("admitted_list", "n/a.md")] == "relevant"
    assert done[("near_miss", "n/b.md")] == "irrelevant"
    assert box.get("rc") == 0


def test_serve_binds_localhost_only(tmp_path):
    """绑定地址不可配：无鉴权的本机数据服务，暴露到 0.0.0.0 等于交给同网段任何人。

    变异验证：把绑定地址改成 0.0.0.0，本用例转红。
    """
    import inspect
    import re
    from scripts.analyze_metrics import serve_review
    src = inspect.getsource(serve_review)
    # 钉**绑定调用的形态**，不是「文件里没有 0.0.0.0 这个串」——本函数的 docstring
    # 恰恰要解释「为什么不绑 0.0.0.0」，裸子串断言会被自己的注释打红（实测）。
    binds = re.findall(r"ThreadingHTTPServer\(\(([^)]*)\)", src)
    assert binds == ['"127.0.0.1", port'], f"绑定地址不是仅本机：{binds}"
