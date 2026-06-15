"""
Delete documents from Azure Blob Storage and AI Search.

Usage:
    python infra/delete_documents.py

Reads credentials from .env file in the current directory.
Edit DOC_NAMES below to specify which documents to delete.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ── Documents to delete ───────────────────────────────────────────────────────
DOC_NAMES: list[str] = [
    "ops/SOP 10 01 001 About the Playbook.pdf",
    "ops/SOP 10 01 002 SOP Playbook Rollout.pdf",
    "ops/SOP 10 01 003 SOP Editing Guidelines.pdf",
]


# ── Load .env ─────────────────────────────────────────────────────────────────
def _load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        print(f"[WARN] {path} not found — relying on existing environment variables.")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"[ERROR] Environment variable {name!r} is not set.")
    return value


# ── Blob deletion ─────────────────────────────────────────────────────────────
def delete_blobs(doc_names: list[str]) -> None:
    from azure.identity import AzureCliCredential
    from azure.storage.blob import BlobServiceClient

    account_name  = _require("AZURE_STORAGE_ACCOUNT_NAME")
    container_raw = os.getenv("AZURE_STORAGE_CONTAINER_RAW", "raw-documents")
    container_proc = os.getenv("AZURE_STORAGE_CONTAINER_PROCESSED", "processed-chunks")

    credential = AzureCliCredential()
    client = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=credential,
    )

    for doc_name in doc_names:
        # Raw blob — stored at the doc_name path
        _delete_blob(client, container_raw, doc_name)

        # Processed chunks — prefix is the doc_name without extension
        stem = doc_name.rsplit(".", 1)[0]  # e.g. "ops/SOP 10 01 001 About the Playbook"
        _delete_prefix(client, container_proc, stem)


def _delete_blob(client, container: str, blob_name: str) -> None:
    try:
        blob = client.get_blob_client(container=container, blob=blob_name)
        blob.delete_blob()
        print(f"[OK]   Deleted blob  {container}/{blob_name}")
    except Exception as exc:
        _handle_not_found(exc, f"blob {container}/{blob_name}")


def _delete_prefix(client, container: str, prefix: str) -> None:
    try:
        cc = client.get_container_client(container)
        blobs = list(cc.list_blobs(name_starts_with=prefix))
        if not blobs:
            print(f"[SKIP] No blobs found under {container}/{prefix}*")
            return
        for blob in blobs:
            cc.delete_blob(blob.name)
            print(f"[OK]   Deleted blob  {container}/{blob.name}")
    except Exception as exc:
        print(f"[ERR]  {exc}")


# ── AI Search deletion ────────────────────────────────────────────────────────
def delete_from_search(doc_names: list[str]) -> None:
    from azure.core.credentials import AzureKeyCredential
    from azure.identity import AzureCliCredential
    from azure.search.documents import SearchClient

    endpoint = _require("AZURE_SEARCH_ENDPOINT")
    index    = os.getenv("AZURE_SEARCH_INDEX", "idx-rag")
    api_key  = os.getenv("AZURE_SEARCH_API_KEY", "").strip()

    credential = AzureKeyCredential(api_key) if api_key else AzureCliCredential()
    search = SearchClient(endpoint=endpoint, index_name=index, credential=credential)

    for doc_name in doc_names:
        _delete_doc_chunks(search, doc_name)


def _delete_doc_chunks(search: "SearchClient", doc_name: str) -> None:
    escaped = doc_name.replace("'", "''")
    filt    = f"doc_name eq '{escaped}'"

    ids: list[str] = []
    try:
        results = search.search(search_text="*", filter=filt, select=["id"], top=1000)
        for r in results:
            ids.append(r["id"])
    except Exception as exc:
        print(f"[ERR]  Search query failed for {doc_name!r}: {exc}")
        return

    if not ids:
        print(f"[SKIP] No search chunks found for {doc_name!r}")
        return

    docs_to_delete = [{"id": chunk_id} for chunk_id in ids]
    try:
        search.delete_documents(documents=docs_to_delete)
        print(f"[OK]   Deleted {len(ids)} search chunk(s) for {doc_name!r}")
    except Exception as exc:
        print(f"[ERR]  Search delete failed for {doc_name!r}: {exc}")


def _handle_not_found(exc: Exception, label: str) -> None:
    msg = str(exc)
    if "BlobNotFound" in msg or "ResourceNotFound" in msg or "404" in msg:
        print(f"[SKIP] Not found: {label}")
    else:
        print(f"[ERR]  {label}: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _load_env()

    print(f"\nDocuments to delete ({len(DOC_NAMES)}):")
    for d in DOC_NAMES:
        print(f"  • {d}")
    print()

    print("=== Blob Storage ===")
    delete_blobs(DOC_NAMES)

    print("\n=== AI Search ===")
    delete_from_search(DOC_NAMES)

    print("\nDone.")
