# Copyright (c) 2023 Carter Lasalle
"""Attachment text in the search index.

``search_attachment_contents`` used to open and extract text from every
candidate file on every call. The index now keeps extracted text per
attachment ROWID, updated incrementally, retries files that were not yet
downloaded, and serves snippets from the stored text.
"""

from __future__ import annotations

import pytest

from mac_messages_mcp import extract, search_index
from mac_messages_mcp.content import search_attachment_contents
from mac_messages_mcp.messages import check_messages_db_access
from mac_messages_mcp.untrusted import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

ALICE = "+15555550100"


def _inner(fenced: str) -> str:
    assert fenced.startswith(UNTRUSTED_OPEN) and fenced.endswith(UNTRUSTED_CLOSE)
    return (
        fenced[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip().replace("\\n", "\n")
    )


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    path = tmp_path / "idx" / "search_index.sqlite"
    monkeypatch.setenv("MAC_MESSAGES_SEARCH_INDEX", str(path))
    search_index.reset()
    yield path
    search_index.reset()


@pytest.fixture
def files(chat_db, tmp_path):
    alice = chat_db.add_handle(ALICE)
    paths = []
    for i in range(6):
        f = tmp_path / f"note{i}.txt"
        f.write_text(
            f"note {i}: " + ("the lease renewal is due" if i == 2 else "nothing to see")
        )
        msg = chat_db.add_message(f"file {i}", handle_id=alice)
        chat_db.add_attachment(msg, str(f), "text/plain")
        paths.append(f)
    return {"alice": alice, "paths": paths}


def test_content_search_uses_the_index_and_does_not_reopen_files(
    chat_db, files, index_path, monkeypatch
):
    first = _inner(search_attachment_contents("lease renewal"))
    assert "note2.txt" in first and "lease renewal is due" in first
    assert search_index.status()["attachments_indexed"] == 6

    opened: list[str] = []
    real = extract.extract_attachment_text

    def spy(path, mime, max_bytes):
        opened.append(path)
        return real(path, mime, max_bytes)

    monkeypatch.setattr(extract, "extract_attachment_text", spy)
    second = _inner(search_attachment_contents("lease renewal"))
    assert "note2.txt" in second
    assert opened == [], "a second search must be served from the index"


def test_index_is_incremental_for_attachments(chat_db, files, index_path):
    search_attachment_contents("lease")
    f = files["paths"][0].parent / "new.txt"
    f.write_text("a brand new contract draft")
    msg = chat_db.add_message("new file", handle_id=files["alice"])
    att = chat_db.add_attachment(msg, str(f), "text/plain")
    out = _inner(search_attachment_contents("contract"))
    assert f"#{att} new.txt" in out
    assert search_index.status()["attachments_indexed"] == 7


def test_not_yet_downloaded_files_are_retried_later(
    chat_db, files, index_path, tmp_path
):
    pending = tmp_path / "later.txt"  # does not exist yet
    msg = chat_db.add_message("cloud file", handle_id=files["alice"])
    att = chat_db.add_attachment(msg, str(pending), "text/plain", transfer_state=0)
    search_attachment_contents("penguin")
    assert search_index.status()["attachments_pending"] == 1

    pending.write_text("the penguin exhibit opens friday")
    out = _inner(search_attachment_contents("penguin"))
    assert f"#{att} later.txt" in out
    assert search_index.status()["attachments_pending"] == 0


def test_filters_still_apply_on_top_of_index_hits(chat_db, files, index_path, tmp_path):
    bob = chat_db.add_handle("+15555550199")
    f = tmp_path / "bob.txt"
    f.write_text("lease renewal from bob")
    msg = chat_db.add_message("bob file", handle_id=bob)
    chat_db.add_attachment(msg, str(f), "text/plain")
    out = _inner(search_attachment_contents("lease renewal", contact="+15555550199"))
    assert "bob.txt" in out and "note2.txt" not in out
    only_pdf = _inner(
        search_attachment_contents("lease renewal", mime_type="application/pdf")
    )
    assert "note2.txt" not in only_pdf


def test_disabled_index_falls_back_to_the_scan(chat_db, files, monkeypatch):
    monkeypatch.setenv("MAC_MESSAGES_SEARCH_INDEX", "none")
    search_index.reset()
    out = _inner(search_attachment_contents("lease renewal"))
    assert "note2.txt" in out


def test_status_reports_attachment_index(chat_db, files, index_path):
    search_attachment_contents("lease")
    report = check_messages_db_access()
    assert "6 attachments indexed" in report
