"""
Check if documents are present in AI Search index.

Usage:
    python infra/check_indexed.py
    python infra/check_indexed.py "Test_Corrupted Data.docx" "Test_Random Text.docx"

Reads credentials from .env or existing environment variables.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DOC_NAMES: list[str] = [
    "Test_Corrupted Data.docx",
    "Test_Random Text.docx",
    "Test_Empty Doc.docx",
    "Test_Cotrodic1.docx",
    "Test_Cotrodic2.docx",
    "Test_Cotrodic3.docx",
    "Test_Versioning1.docx",
    "Test_Versioning2.docx",
]


def _load_env() -> None:
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)


def _odata_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_docs(doc_names: list[str]) -> None:
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient

    endpoint  = os.environ["AZURE_SEARCH_ENDPOINT"]
    api_key   = os.environ["AZURE_SEARCH_API_KEY"]
    index     = os.environ["AZURE_SEARCH_INDEX"]

    client = SearchClient(
        endpoint=endpoint,
        index_name=index,
        credential=AzureKeyCredential(api_key),
    )

    print(f"\nIndex: {index}\n{'─' * 60}")
    found_count = 0

    for doc_name in doc_names:
        results = list(client.search(
            search_text="*",
            filter=f"doc_name eq {_odata_str(doc_name)}",
            select=["id", "doc_name", "chunk_type", "page_number"],
            top=1000,
        ))
        chunk_count = len(results)
        if chunk_count > 0:
            found_count += 1
            print(f"  FOUND   {doc_name!r:45s}  ({chunk_count} chunks)")
        else:
            print(f"  MISSING {doc_name!r}")

    print(f"{'─' * 60}")
    print(f"Result: {found_count}/{len(doc_names)} documents indexed\n")


if __name__ == "__main__":
    _load_env()
    names = sys.argv[1:] if len(sys.argv) > 1 else DOC_NAMES
    check_docs(names)
