"""Knowledge page: the documents the assistant answers from, by folder, with upload and remove."""
from __future__ import annotations

import streamlit as st

from ui.components.api import api


def render() -> None:
    st.title(":material/menu_book: Knowledge")
    st.caption("The company documents the assistant answers from. Older versions (e.g. files in an archive folder) "
               "are kept but only used when a question asks for them.")
    rows, err = api("GET", "/documents")
    if err:
        st.error(err)
        return
    rows = rows or []
    folders = sorted({r["folder"].split("/")[0] or "(top level)" for r in rows})
    old = [r for r in rows if r.get("superseded_by")]
    m = st.columns(4)
    m[0].metric("Documents", len(rows) - len(old))
    m[1].metric("Passages", f"{sum(r['chunks'] for r in rows):,}")
    m[2].metric("Folders", len(folders))
    m[3].metric("Older versions", len(old))
    if not any(r["indexed"] for r in rows):
        st.warning("Nothing is indexed yet. Run `uv run -m veltrix_copilot.knowledge.ingest`.", icon=":material/warning:")

    with st.expander("Add a document", icon=":material/upload_file:"):
        upload = st.file_uploader("PDF, Word, Markdown or text", type=["pdf", "docx", "md", "txt"],
                                  key=f"upload-{st.session_state.get('upload_n', 0)}")
        if upload is not None and st.button("Add to the knowledge base", icon=":material/add:", type="primary"):
            with st.spinner(f"Reading and indexing {upload.name}…"):
                res, e = api("POST", "/documents", params={"name": upload.name}, content=upload.getvalue())
            st.session_state.doc_msg = ("error", e) if e else ("success", f"Added {res['name']} ({res['chunks']} passages)."
                + (f" It replaces {', '.join(res['replaces'])} in answers." if res.get("replaces") else ""))
            st.session_state.upload_n = st.session_state.get("upload_n", 0) + 1
            st.rerun()
    if msg := st.session_state.pop("doc_msg", None):
        (st.success if msg[0] == "success" else st.error)(msg[1])

    f1, f2 = st.columns([1, 2])
    folder = f1.selectbox("Folder", ["All folders"] + folders)
    text = f2.text_input("Find a document", placeholder="e.g. travel policy, kestrel, pulsemonitor")
    shown = [r for r in rows if (folder == "All folders" or (r["folder"].split("/")[0] or "(top level)") == folder)
             and all(w in r["name"].lower() for w in text.lower().split())]
    st.dataframe(
        [{"Folder": r["folder"] or "—", "Document": r["name"].rsplit("/", 1)[-1], "Passages": r["chunks"],
          "Indexed": r["indexed"], "Replaced by": r.get("superseded_by") or "", "Added": (r.get("added") or "")[:10]}
         for r in shown],
        use_container_width=True, hide_index=True, height=min(600, 38 + 35 * max(1, len(shown))),
        column_config={"Indexed": st.column_config.CheckboxColumn(), "Passages": st.column_config.NumberColumn(width="small")})
    st.caption(f"{len(shown)} of {len(rows)} documents")

    with st.expander("Remove a document", icon=":material/delete:"):
        name = st.selectbox("Document", [r["name"] for r in shown], index=None, placeholder="Choose a document")
        if name and st.button(f"Remove {name}", type="primary"):
            _, e = api("DELETE", f"/documents/{name}")
            st.session_state.doc_msg = ("error", e) if e else ("success", f"Removed {name}.")
            st.rerun()
