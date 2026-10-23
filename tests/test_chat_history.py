"""Chat history: the list and search behind the History dropdown and the Chats page, and Delete
with its Undo window (then the messages and attachment files are gone from disk)."""

from __future__ import annotations

import time
from pathlib import Path

from night_scout.core import attachments as att
from night_scout.core import chat
from night_scout.core.config import Config, load_config
from night_scout.core.db import Store
from night_scout.core.llm import ToolCall, ToolReply
from night_scout.web.app import Jobs

from .test_assistant import PNG, data_url
from .test_chat_routes import env, wait  # noqa: F401  (the route fixture)


def ask(client, question: str, context: dict | None = None, thread: int | None = None, **kw):
    body = {"context": context or {"kind": "app"}, "question": question, **kw}
    if thread is not None:
        body["thread"] = thread
    r = client.post("/api/chat", json=body)
    assert r.status_code == 200, r.text
    tid = r.json()["thread"]
    wait(client, tid)
    return tid


def data_dir(config_path: Path) -> Path:
    return load_config(config_path).data_path()


def test_list_is_newest_first_with_labels_counts_and_plain_snippets(env) -> None:  # noqa: F811
    client, llm, ids, *_ = env
    llm.script = [
        ToolReply("", [ToolCall("search_tonight", {"query": "llama"})]),
        ToolReply("**Ollama** updated [llama.cpp](https://e.example/a).\n\n- faster"),
        ToolReply("The Digest shows your topics."),
        ToolReply("Follow-up answer."),
    ]
    story = ask(client, "What changed?", {"kind": "item", "id": ids["a"]})
    general = ask(client, "What does the Digest show?")
    ask(client, "And when does it run?", thread=general)
    data = client.get("/api/chats").json()
    assert data["total"] == 2
    first, second = data["threads"]
    assert first["id"] == general  # the follow-up made it the newest
    assert first["label"] == "Night Scout" and first["icon"] == "app"
    assert first["questions"] == 2 and first["title"] == "What does the Digest show?"
    assert first["snippet"] == "Follow-up answer."
    assert second["id"] == story and second["label"] == "Story · Ollama v0.40.9 released"
    assert second["icon"] == "story" and second["tools"] == 1
    assert second["url"] == "https://e.example/a" and first["url"] is None
    assert second["snippet"] == "Ollama updated llama.cpp. faster"  # no markdown, no link URL


def test_digest_label_and_kind_filters(env) -> None:  # noqa: F811
    client, llm, ids, *_ = env
    llm.script = [ToolReply("a"), ToolReply("b"), ToolReply("c")]
    d = ask(client, "Main thread tonight?", {"kind": "digest", "day": "2026-10-22"})
    s = ask(client, "About this?", {"kind": "item", "id": ids["b"]})
    a = ask(client, "How do I add a source?")
    got = lambda kind: [t["id"] for t in client.get(f"/api/chats?kind={kind}").json()["threads"]]  # noqa: E731
    assert got("all") == [a, s, d]
    assert got("app") == [a] and got("item") == [s] and got("digest") == [d]
    assert got("files") == []
    digest = next(t for t in client.get("/api/chats").json()["threads"] if t["id"] == d)
    assert digest["label"] == "Digest · Thu Oct 22" and digest["icon"] == "digest"
    assert client.get("/api/chats?kind=nope").status_code == 400


def test_search_covers_questions_and_answers_and_windows_the_snippet(env) -> None:  # noqa: F811
    client, llm, *_ = env
    filler = "word " * 60
    llm.script = [ToolReply(f"{filler}the needle is here {filler}"), ToolReply("Nothing.")]
    hit = ask(client, "Tell me something long")
    ask(client, "Unrelated question")
    data = client.get("/api/chats?q=NEEDLE").json()
    assert [t["id"] for t in data["threads"]] == [hit] and data["total"] == 2
    snip = data["threads"][0]["snippet"]
    assert "the needle is here" in snip and snip.startswith("…") and snip.endswith("…")
    by_question = client.get("/api/chats?q=unrelated").json()["threads"]
    assert len(by_question) == 1 and by_question[0]["title"] == "Unrelated question"
    assert client.get("/api/chats?q=zebra").json()["threads"] == []


def test_search_treats_like_wildcards_literally(env) -> None:  # noqa: F811
    client, llm, *_ = env
    llm.script = [ToolReply("50% off"), ToolReply("snake_case names")]
    ask(client, "first")
    ask(client, "second")
    assert len(client.get("/api/chats?q=%25").json()["threads"]) == 1  # "%"
    assert len(client.get("/api/chats?q=_").json()["threads"]) == 1
    assert len(client.get("/api/chats?q=e_c").json()["threads"]) == 1  # not "any char"


def test_files_filter_and_file_names(env) -> None:  # noqa: F811
    client, llm, *_ = env
    llm.script = [ToolReply("I see a screenshot."), ToolReply("plain")]
    shot = ask(
        client,
        "What's in this?",
        attachments=[{"kind": "image", "name": "error.png", "data": data_url("image/png", PNG)}],
    )
    ask(client, "No files here")
    files = client.get("/api/chats?kind=files").json()["threads"]
    assert [t["id"] for t in files] == [shot] and files[0]["files"] == ["error.png"]


def test_delete_hides_at_once_and_undo_restores(env) -> None:  # noqa: F811
    client, llm, *_ = env
    llm.script = [ToolReply("kept")]
    tid = ask(client, "Keep me?")
    r = client.delete(f"/api/chat/{tid}")
    assert r.status_code == 200 and r.json()["undo_seconds"] == chat.UNDO_SECONDS
    assert client.get("/api/chats").json()["total"] == 0
    assert client.get(f"/api/chat/{tid}").status_code == 404  # the drawer forgets it
    assert client.delete(f"/api/chat/{tid}").status_code == 404  # already deleted
    assert client.post(f"/api/chat/{tid}/restore").status_code == 200
    assert [t["id"] for t in client.get("/api/chats").json()["threads"]] == [tid]
    assert client.get(f"/api/chat/{tid}").json()["messages"][-1]["content"] == "kept"
    assert client.post(f"/api/chat/{tid}/restore").status_code == 404  # nothing to undo


def test_purge_removes_messages_and_attachment_files(env) -> None:  # noqa: F811
    client, llm, _ids, config_path, _t = env
    llm.script = [ToolReply("seen"), ToolReply("other")]
    tid = ask(
        client,
        "Look",
        attachments=[{"kind": "image", "name": "a.png", "data": data_url("image/png", PNG)}],
    )
    other = ask(
        client,
        "Mine stays",
        attachments=[{"kind": "image", "name": "b.png", "data": data_url("image/png", PNG)}],
    )
    folder = att.folder(data_dir(config_path), tid)
    assert any(folder.iterdir())
    msg = client.get(f"/api/chat/{tid}").json()["messages"][0]
    att_url = msg["attachments"][0]["url"]
    assert client.get(att_url).status_code == 200
    assert client.delete(f"/api/chat/{tid}").status_code == 200
    assert client.get(att_url).status_code == 404  # hidden during the Undo window too
    assert folder.exists()  # …but still restorable
    cfg = load_config(config_path)
    store = Store(cfg.db_file())
    try:
        assert chat.purge_deleted(store, cfg.data_path()) == 0  # window still open
        assert chat.purge_deleted(store, cfg.data_path(), older_than=0) == 1
        left = store.conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE thread_id=?", (tid,)
        ).fetchone()[0]
        assert left == 0
        assert (
            store.conn.execute("SELECT 1 FROM chat_threads WHERE id=?", (tid,)).fetchone() is None
        )
    finally:
        store.close()
    assert not folder.exists()
    assert any(att.folder(cfg.data_path(), other).iterdir())  # other conversations untouched
    assert client.post(f"/api/chat/{tid}/restore").status_code == 404
    assert [t["id"] for t in client.get("/api/chats").json()["threads"]] == [other]


def test_purge_runs_by_itself_after_the_undo_window(env, monkeypatch) -> None:  # noqa: F811
    client, llm, _ids, config_path, _t = env
    monkeypatch.setattr(chat, "UNDO_SECONDS", 0.05)
    llm.script = [ToolReply("bye")]
    tid = ask(client, "Delete me")
    client.delete(f"/api/chat/{tid}")
    store = Store(load_config(config_path).db_file())
    try:
        for _ in range(100):  # the timer fires UNDO_SECONDS + 1 s later
            gone = store.conn.execute("SELECT 1 FROM chat_threads WHERE id=?", (tid,)).fetchone()
            if gone is None:
                break
            time.sleep(0.05)
        assert gone is None
    finally:
        store.close()


def test_cannot_delete_while_gemma_is_answering(env, monkeypatch) -> None:  # noqa: F811
    client, llm, *_ = env
    llm.script = [ToolReply("done")]
    tid = ask(client, "Busy?")
    real_get = Jobs.get
    monkeypatch.setattr(
        Jobs,
        "get",
        lambda self, key: {"state": "running"} if key == f"chat:{tid}" else real_get(self, key),
    )
    r = client.delete(f"/api/chat/{tid}")
    assert r.status_code == 409 and "still answering" in r.json()["error"]
    monkeypatch.setattr(Jobs, "get", real_get)
    assert client.delete(f"/api/chat/{tid}").status_code == 200


def test_deleted_conversation_refuses_follow_ups_and_answers(env) -> None:  # noqa: F811
    client, llm, *_ = env
    llm.script = [ToolReply("one")]
    tid = ask(client, "First")
    client.delete(f"/api/chat/{tid}")
    r = client.post(
        "/api/chat", json={"context": {"kind": "app"}, "question": "More", "thread": tid}
    )
    assert r.status_code == 404


def test_restart_ends_undo_windows(tmp_path: Path) -> None:
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True)
    store = Store(cfg.db_file())
    try:
        chat.ensure_tables(store)
        tid = chat.create_thread(store, {"kind": "app"}, "q")
        chat.add_message(store, tid, 1, "user", "q")
        assert chat.delete_thread(store, tid)
        assert chat.purge_deleted(store, cfg.data_path(), older_than=0) == 1
        assert chat.list_threads(store)["total"] == 0
    finally:
        store.close()


def test_history_writes_need_csrf(env) -> None:  # noqa: F811
    client, llm, *_ = env
    llm.script = [ToolReply("x")]
    tid = ask(client, "Guarded")
    from fastapi.testclient import TestClient

    from .test_chat_routes import BASE

    bare = TestClient(client.app, base_url=BASE, cookies={"night_scout_session": "test-key"})
    assert bare.delete(f"/api/chat/{tid}").status_code == 403
    assert bare.post(f"/api/chat/{tid}/restore").status_code == 403
    assert bare.get("/api/chats").status_code == 200  # reads need no token
    assert client.get("/api/chats").json()["total"] == 1


def test_plain_strips_markdown() -> None:
    assert chat.plain("## Title\n\n* **bold** and `code`\n1. [link](https://x.example) end") == (
        "Title bold and code link end"
    )
