"""
Azure client factories — ingestion pipeline.

All Azure SDK calls use ``DefaultAzureCredential``, which automatically selects
``ManagedIdentityCredential`` inside Azure Container Apps and ``AzureCliCredential``
on a developer workstation. No API keys or connection strings are used.

No Document Intelligence — PDF parsing is done natively with pdfplumber + pymupdf.
"""
from __future__ import annotations

from functools import lru_cache

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

from shared.config import settings


def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


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
    return get_foundry_client().get_openai_client()


@lru_cache(maxsize=1)
def get_blob_service_client() -> BlobServiceClient:
    """Return a cached synchronous BlobServiceClient."""
    return BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def get_search_client() -> SearchClient:
    """Return a cached AI Search client pointed at ``AZURE_SEARCH_INDEX``."""
    return SearchClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        index_name=settings.AZURE_SEARCH_INDEX,
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def get_search_index_client() -> SearchIndexClient:
    """Return a cached AI Search index management client (used by infra scripts)."""
    return SearchIndexClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        credential=_credential(),
    )


def get_service_bus_client():
    """New instance per use — always use as async context manager."""
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultCredential

    return AsyncSBClient(
        fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
        credential=AsyncDefaultCredential(),
    )
