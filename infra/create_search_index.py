"""
Create (or update) the AI Search index for the RAG ingestion pipeline.

Run once before the first embedding job:
    python infra/create_search_index.py

The script is idempotent — if the index already exists it is left unchanged.
To force a full rebuild (DESTRUCTIVE — deletes all indexed documents):
    python infra/create_search_index.py --recreate

Requirements:
    pip install azure-search-documents azure-identity python-dotenv

Auth:
    Uses DefaultAzureCredential — run `az login` first for local dev.
    In Azure, the caller's Managed Identity must have
    "Search Index Data Contributor" on the Search resource.
"""
from __future__ import annotations

import argparse
import os
import sys

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — env vars must be set externally

from azure.identity import DefaultAzureCredential


def _credential():
    return DefaultAzureCredential()


def build_index_definition(index_name: str, semantic_config: str) -> dict:
    """
    Returns the full index schema as a dict matching the AI Search REST API.
    Field names here are the single source of truth — they must match
    RawChunk.to_search_doc() in shared/models.py exactly.

    Vector field: content_vector (1536 dims for text-embedding-3-large)
    Semantic config: enables hybrid search with semantic re-ranking.
    """
    return {
        "name": index_name,
        "fields": [
            # ── Identity ──────────────────────────────────────────────────────
            {"name": "id",                 "type": "Edm.String",  "key": True,  "filterable": True},
            {"name": "parent_id",          "type": "Edm.String",  "filterable": True, "retrievable": True},
            {"name": "chunk_type",         "type": "Edm.String",  "filterable": True, "retrievable": True},

            # ── Document provenance ───────────────────────────────────────────
            {"name": "domain",             "type": "Edm.String",  "filterable": True,  "facetable": True,  "retrievable": True},
            {"name": "doc_name",           "type": "Edm.String",  "filterable": True,  "retrievable": True},
            {"name": "doc_path",           "type": "Edm.String",  "filterable": True,  "retrievable": True},
            {"name": "source",             "type": "Edm.String",  "filterable": True,  "retrievable": True},
            {"name": "doc_url",            "type": "Edm.String",  "filterable": False, "retrievable": True},
            {"name": "file_type",          "type": "Edm.String",  "filterable": True,  "facetable": True,  "retrievable": True},
            {"name": "blob_path",          "type": "Edm.String",  "filterable": False, "retrievable": True},
            {"name": "ingested_at",        "type": "Edm.String",  "filterable": False, "retrievable": True, "sortable": True},

            # ── Position in document ──────────────────────────────────────────
            {"name": "page_number",        "type": "Edm.Int32",   "filterable": True,  "retrievable": True, "sortable": True},
            {"name": "title",              "type": "Edm.String",  "searchable": True,  "retrievable": True},
            {"name": "section_heading",    "type": "Edm.String",  "searchable": True,  "retrievable": True},
            {"name": "section_subheading", "type": "Edm.String",  "searchable": True,  "retrievable": True},

            # ── Content ───────────────────────────────────────────────────────
            {"name": "content",            "type": "Edm.String",  "searchable": True,  "retrievable": True},
            {"name": "table_raw",          "type": "Edm.String",  "searchable": False, "retrievable": True},

            # ── Lifecycle ─────────────────────────────────────────────────────
            {"name": "file_sha256",        "type": "Edm.String",  "filterable": True,  "retrievable": False},
            {"name": "is_deleted",         "type": "Edm.Boolean", "filterable": True,  "retrievable": True},

            # ── Vector field ──────────────────────────────────────────────────
            # Dimensions must match AZURE_OPENAI_EMBEDDING_DEPLOYMENT:
            #   text-embedding-3-large  → 3072  (or 1536 with compression)
            #   text-embedding-ada-002  → 1536
            # This schema uses 1536 (ada-002 / 3-large compressed).
            {
                "name": "content_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": 1536,
                "vectorSearchProfile": "vector-profile",
            },
        ],

        "vectorSearch": {
            "profiles": [
                {
                    "name":       "vector-profile",
                    "algorithm":  "hnsw-config",
                }
            ],
            "algorithms": [
                {
                    "name": "hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "metric":         "cosine",
                        "m":              4,
                        "efConstruction": 400,
                        "efSearch":       500,
                    },
                }
            ],
        },

        "semantic": {
            "configurations": [
                {
                    "name": semantic_config,
                    "prioritizedFields": {
                        "titleField":          {"fieldName": "title"},
                        "prioritizedKeywordsFields": [
                            {"fieldName": "section_heading"},
                            {"fieldName": "section_subheading"},
                        ],
                        "prioritizedContentFields": [
                            {"fieldName": "content"},
                        ],
                    },
                }
            ]
        },

        "corsOptions": {
            "allowedOrigins": ["*"],
            "maxAgeInSeconds": 300,
        },
    }


def _auth_header(credential) -> dict:
    """Return a Bearer token Authorization header from the credential."""
    token = credential.get_token("https://search.azure.com/.default")
    return {"Authorization": f"Bearer {token.token}"}


def create_or_skip(endpoint: str, index_name: str, semantic_config: str, credential) -> None:
    from azure.search.documents.indexes import SearchIndexClient

    client = SearchIndexClient(endpoint=endpoint, credential=credential)

    try:
        client.get_index(index_name)
        print(f"Index '{index_name}' already exists — skipping creation.")
        print("  Run with --recreate to drop and rebuild (WARNING: deletes all data).")
        print("  Run with --update to add new fields without touching existing documents.")
        return
    except Exception:
        pass  # index does not exist

    definition = build_index_definition(index_name, semantic_config)

    import json
    import urllib.request

    url  = f"{endpoint.rstrip('/')}/indexes?api-version=2024-05-01-preview"
    body = json.dumps(definition).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={**_auth_header(credential), "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print(f"Created index '{result['name']}' successfully.")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"HTTP {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)


def update_index(endpoint: str, index_name: str, semantic_config: str, credential) -> None:
    """PUT the full schema to add new fields without deleting existing documents.

    Azure AI Search allows adding new non-key fields to a live index — existing
    documents are untouched and their vectors remain valid. Old documents will
    have null for newly added fields until they are re-ingested.
    """
    import json
    import urllib.request

    definition = build_index_definition(index_name, semantic_config)
    url  = f"{endpoint.rstrip('/')}/indexes/{index_name}?api-version=2024-05-01-preview&allowIndexDowntime=false"
    body = json.dumps(definition).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={**_auth_header(credential), "Content-Type": "application/json"},
        method="PUT",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print(f"Updated index '{result['name']}' — existing documents and embeddings preserved.")
            print("  New fields will be null on existing documents until they are re-ingested.")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"HTTP {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)


def recreate(endpoint: str, index_name: str, semantic_config: str, credential) -> None:
    import json
    import urllib.request

    auth = _auth_header(credential)

    # Delete
    url = f"{endpoint.rstrip('/')}/indexes/{index_name}?api-version=2024-05-01-preview"
    req = urllib.request.Request(url, headers=auth, method="DELETE")
    try:
        with urllib.request.urlopen(req):
            print(f"Deleted existing index '{index_name}'.")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"Delete failed HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
            sys.exit(1)

    # Recreate
    create_or_skip(endpoint, index_name, semantic_config, credential)


def main():
    parser = argparse.ArgumentParser(description="Create the RAG AI Search index")
    parser.add_argument("--recreate", action="store_true",
                        help="Drop and rebuild the index (DELETES ALL DATA)")
    parser.add_argument("--update", action="store_true",
                        help="Add new fields to existing index (preserves all documents and embeddings)")
    args = parser.parse_args()

    endpoint       = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
    index_name     = os.environ.get("AZURE_SEARCH_INDEX",    "idx-rag")
    semantic_cfg   = os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG", "rag-semantic-config")

    if not endpoint:
        print("ERROR: AZURE_SEARCH_ENDPOINT is not set.", file=sys.stderr)
        sys.exit(1)

    credential = _credential()
    print(f"Endpoint : {endpoint}")
    print(f"Index    : {index_name}")
    print(f"Semantic : {semantic_cfg}")

    if args.recreate:
        confirm = input("This will DELETE all indexed documents. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)
        recreate(endpoint, index_name, semantic_cfg, credential)
    elif args.update:
        update_index(endpoint, index_name, semantic_cfg, credential)
    else:
        create_or_skip(endpoint, index_name, semantic_cfg, credential)


if __name__ == "__main__":
    main()
