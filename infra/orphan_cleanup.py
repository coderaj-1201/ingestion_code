#!/usr/bin/env python3
"""
Orphan cleanup: cross-references AI Search against SharePoint and removes
documents that exist in the index but have been deleted from SharePoint.

Covers the gap that snapshot-based delete detection cannot handle: files
removed from SharePoint *before* the Logic App's first snapshot was saved
will never be detected by the recurrence workflow and stay in the index forever.

Default is dry-run (safe). Pass --execute to actually delete.

Usage:
    # See what would be deleted — no changes made:
    python infra/orphan_cleanup.py \\
        --site-url https://irondrive.sharepoint.com/sites/OPSPlaybook \\
        --library "Global Ops Playbook" \\
        --domain ops

    # Delete the orphans:
    python infra/orphan_cleanup.py \\
        --site-url https://irondrive.sharepoint.com/sites/OPSPlaybook \\
        --library "Global Ops Playbook" \\
        --domain ops \\
        --execute

Auth:
    Run `az login` first. Uses DefaultAzureCredential for blob and AI Search
    (AzureCliCredential locally). SharePoint REST uses `az account get-access-token`.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import requests


# ── env ───────────────────────────────────────────────────────────────────────

def _load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
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
        sys.exit(f"[ERROR] {name!r} is not set — add it to .env or export it.")
    return value


# ── SharePoint ────────────────────────────────────────────────────────────────

def _sp_token(site_url: str) -> str:
    tenant_root = "/".join(site_url.rstrip("/").split("/")[:3])
    try:
        token = subprocess.check_output(
            ["az", "account", "get-access-token",
             "--resource", tenant_root, "--query", "accessToken", "-o", "tsv"],
            text=True,
            stderr=subprocess.PIPE,
            shell=(os.name == "nt"),
        ).strip()
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[ERROR] Could not get SharePoint token: {exc.stderr.strip()}\n"
                 "  Make sure you've run `az login` first.")
    if not token:
        sys.exit("[ERROR] `az account get-access-token` returned empty. Run `az login`.")
    return token


def fetch_sharepoint_filenames(site_url: str, library: str) -> set[str]:
    """
    Return every filename (with extension) currently in the SharePoint library,
    across all subfolders, by walking the SharePoint REST list-items endpoint.
    """
    token   = _sp_token(site_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json;odata=nometadata",
    }

    # GetByTitle expects the raw library name; requests will percent-encode the URL.
    encoded_library = quote(library, safe="")
    url: str | None = (
        f"{site_url.rstrip('/')}/_api/web/lists"
        f"/GetByTitle('{encoded_library}')/items"
        f"?$select=FileLeafRef,FileSystemObjectType"
        f"&$filter=FileSystemObjectType eq 0"
        f"&$top=1000"
    )

    filenames: set[str] = set()
    page = 0

    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 401:
            sys.exit("[ERROR] SharePoint returned 401. Check your az login session "
                     "and that you have read access to the library.")
        resp.raise_for_status()

        data  = resp.json()
        items = data.get("value", [])
        filenames.update(
            item["FileLeafRef"]
            for item in items
            if item.get("FileLeafRef")
        )
        page += 1
        print(f"  SharePoint page {page}: {len(items)} items  "
              f"({len(filenames)} files total)", flush=True)

        url = data.get("@odata.nextLink") or data.get("odata.nextLink")

    return filenames


# ── AI Search ─────────────────────────────────────────────────────────────────

def fetch_indexed_doc_names(domain: str) -> set[str]:
    """
    Return every unique doc_name in the AI Search index for the given domain.
    The SDK iterator handles pagination automatically.
    """
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient

    endpoint   = _require("AZURE_SEARCH_ENDPOINT").rstrip("/")
    index      = os.getenv("AZURE_SEARCH_INDEX", "idx-rag")
    credential = DefaultAzureCredential()

    client    = SearchClient(endpoint=endpoint, index_name=index, credential=credential)
    escaped   = domain.replace("'", "''")
    doc_names : set[str] = set()
    chunks    = 0

    for result in client.search(
        search_text="*",
        filter=f"domain eq '{escaped}'",
        select=["doc_name"],
        top=1000,
    ):
        doc_names.add(result["doc_name"])
        chunks += 1
        if chunks % 5000 == 0:
            print(f"  AI Search: {chunks:,} chunks scanned, "
                  f"{len(doc_names)} unique documents so far", flush=True)

    print(f"  AI Search: {chunks:,} chunks total → {len(doc_names)} unique documents")
    return doc_names


# ── Deletion ──────────────────────────────────────────────────────────────────

def _delete_search_chunks(doc_name: str, client) -> int:
    escaped = doc_name.replace("'", "''")
    deleted = 0

    while True:
        results = client.search(
            search_text="*",
            filter=f"doc_name eq '{escaped}'",
            select=["id"],
            top=1000,
        )
        ids = [r["id"] for r in results]
        if not ids:
            break
        client.delete_documents(documents=[{"id": i} for i in ids])
        deleted += len(ids)

    return deleted


def _delete_blobs(domain: str, doc_name: str, blob_svc) -> None:
    container_raw  = os.getenv("AZURE_STORAGE_CONTAINER_RAW",       "raw-documents")
    container_proc = os.getenv("AZURE_STORAGE_CONTAINER_PROCESSED",  "processed-chunks")

    for container, path in [
        (container_raw,  f"{domain}/{doc_name}"),
        (container_proc, f"{domain}/{doc_name}.json"),
    ]:
        try:
            blob_svc.get_blob_client(container=container, blob=path).delete_blob()
            print(f"    [blob]   ✓ {container}/{path}")
        except Exception as exc:
            msg = str(exc)
            if "BlobNotFound" in msg or "ResourceNotFound" in msg or "404" in msg:
                print(f"    [blob]   – not found (already gone): {container}/{path}")
            else:
                print(f"    [blob]   ✗ {container}/{path}: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove AI Search documents that no longer exist in SharePoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--site-url", required=True,
                        help="SharePoint site URL")
    parser.add_argument("--library", required=True,
                        help="Document library name (e.g. 'Global Ops Playbook')")
    parser.add_argument("--domain", required=True,
                        help="Domain to clean (e.g. ops)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete. Omit to dry-run (default).")
    args = parser.parse_args()

    _load_env()

    dry_run = not args.execute
    banner  = "DRY RUN — no changes will be made" if dry_run else "EXECUTE MODE — will delete orphans"
    print(f"\n{'='*60}")
    print(f"  Orphan cleanup  |  domain: {args.domain}  |  {banner}")
    print(f"{'='*60}\n")

    # ── 1. SharePoint ground truth ────────────────────────────────────────────
    print("Step 1: Fetching current files from SharePoint...")
    sp_files = fetch_sharepoint_filenames(args.site_url, args.library)
    print(f"→ {len(sp_files):,} files in SharePoint\n")

    # ── 2. AI Search current state ────────────────────────────────────────────
    print(f"Step 2: Fetching indexed documents for domain '{args.domain}'...")
    indexed = fetch_indexed_doc_names(args.domain)
    print(f"→ {len(indexed):,} documents in AI Search\n")

    # ── 3. Diff ───────────────────────────────────────────────────────────────
    orphans = sorted(indexed - sp_files)

    print(f"Step 3: Computing diff...")
    print(f"  In SharePoint only (not yet ingested): {len(sp_files - indexed):,}")
    print(f"  In both (healthy):                     {len(sp_files & indexed):,}")
    print(f"  Orphans (indexed but deleted from SP): {len(orphans):,}\n")

    if not orphans:
        print("✓ Index is clean — nothing to do.")
        return

    print(f"Orphans ({len(orphans)}):")
    for name in orphans:
        print(f"  • {name}")
    print()

    if dry_run:
        print(f"[DRY RUN] {len(orphans)} document(s) would be deleted from AI Search and blob storage.")
        print("          Re-run with --execute to proceed.")
        return

    # ── 4. Confirm ────────────────────────────────────────────────────────────
    answer = input(
        f"Delete {len(orphans)} orphan(s) from AI Search and blob storage? [yes/N] "
    ).strip().lower()
    if answer != "yes":
        print("Aborted.")
        return

    # ── 5. Delete ─────────────────────────────────────────────────────────────
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.storage.blob import BlobServiceClient

    endpoint = _require("AZURE_SEARCH_ENDPOINT").rstrip("/")
    index    = os.getenv("AZURE_SEARCH_INDEX", "idx-rag")

    search   = SearchClient(endpoint=endpoint, index_name=index, credential=DefaultAzureCredential())
    blob_svc = BlobServiceClient(
        account_url=f"https://{_require('AZURE_STORAGE_ACCOUNT_NAME')}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )

    total_chunks = 0
    for i, doc_name in enumerate(orphans, 1):
        print(f"\n[{i}/{len(orphans)}] {doc_name}")
        chunks = _delete_search_chunks(doc_name, search)
        print(f"    [search] ✓ {chunks} chunk(s) deleted")
        _delete_blobs(args.domain, doc_name, blob_svc)
        total_chunks += chunks

    print(f"\n{'='*60}")
    print(f"  Done. Removed {len(orphans)} document(s), {total_chunks:,} search chunk(s).")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
