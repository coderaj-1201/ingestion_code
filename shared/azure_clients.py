"""
Azure client factories — ingestion pipeline.

Auth:
  - Foundry / OpenAI : AzureCliCredential locally, ManagedIdentity in ACA
  - AI Search        : API key (Contributor access is enough locally)
  - Blob Storage     : AzureCliCredential / ManagedIdentity
  - Service Bus      : connection string locally, ManagedIdentity in ACA

No Document Intelligence — PDF parsing is done natively with pdfplumber + pymupdf.
"""
from __future__ import annotations

import os
from functools import lru_cache

from azure.ai.projects import AIProjectClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

from shared.config import settings


def _credential():
    """Return ``ManagedIdentityCredential`` in Azure, ``AzureCliCredential`` locally."""
    if os.getenv("RUNNING_IN_AZURE"):
        return ManagedIdentityCredential()
    return AzureCliCredential()


@lru_cache(maxsize=1)
def get_foundry_client() -> AIProjectClient:
    """Return a cached Azure AI Foundry project client."""
    return AIProjectClient(
        endpoint=str(settings.AZURE_FOUNDRY_PROJECT_ENDPOINT),
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def get_openai_client() -> AzureOpenAI:
    """Return a cached Azure OpenAI client sourced from the Foundry project."""
    return get_foundry_client().get_openai_client(
        api_version=settings.AZURE_OPENAI_API_VERSION

    )


@lru_cache(maxsize=1)
def get_blob_service_client() -> BlobServiceClient:
    """Return a cached synchronous BlobServiceClient."""
    return BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=_credential(),
    )


def _search_credential():
    # In Azure: Managed Identity — keyless, auditable, no secret to rotate.
    # Locally:  API key — set AZURE_SEARCH_API_KEY in .env.
    #           AzureCliCredential is also accepted locally if AZURE_SEARCH_API_KEY
    #           is left blank (useful when the developer has Search RBAC role assigned).
    if os.getenv("RUNNING_IN_AZURE"):
        return _credential()
    if settings.AZURE_SEARCH_API_KEY:
        return AzureKeyCredential(settings.AZURE_SEARCH_API_KEY.get_secret_value())
    # No API key set locally → fall back to AzureCliCredential.
    # Requires: az role assignment create --role "Search Index Data Contributor" ...
    return _credential()


@lru_cache(maxsize=1)
def get_search_client() -> SearchClient:
    """Return a cached AI Search client pointed at ``AZURE_SEARCH_INDEX``."""
    return SearchClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        index_name=settings.AZURE_SEARCH_INDEX,
        credential=_search_credential(),
    )


@lru_cache(maxsize=1)
def get_search_index_client() -> SearchIndexClient:
    """Return a cached AI Search index management client (used by infra scripts)."""
    return SearchIndexClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        credential=_search_credential(),
    )


def get_service_bus_client():
    """New instance per use — always use as async context manager."""
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient
    conn_str = (
        settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
        if settings.AZURE_SERVICE_BUS_CONNECTION_STR
        else None
    )
    if conn_str:
        return AsyncSBClient.from_connection_string(conn_str)
    return AsyncSBClient(
        fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
        credential=_credential(),
    )
