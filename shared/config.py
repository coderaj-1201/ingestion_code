"""
Ingestion pipeline settings.
No Document Intelligence — PDF parsing uses pdfplumber + pymupdf + LLM.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Azure AI Foundry ──────────────────────────────────────────────────────
    AZURE_FOUNDRY_PROJECT_ENDPOINT: AnyHttpUrl
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str  = "text-embedding-3-large"
    AZURE_OPENAI_API_VERSION: str           = "2024-08-01-preview"
    # Light LLM for page cleaning + table serialisation — gpt-4o-mini or phi-3-mini
    AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT: str  = "gpt-4.1-mini"

    # ── Azure Blob Storage ────────────────────────────────────────────────────
    AZURE_STORAGE_ACCOUNT_NAME: str
    AZURE_STORAGE_CONTAINER_RAW: str        = "raw-documents"
    AZURE_STORAGE_CONTAINER_PROCESSED: str  = "processed-chunks"

    # ── Azure AI Search ───────────────────────────────────────────────────────
    AZURE_SEARCH_ENDPOINT: AnyHttpUrl
    AZURE_SEARCH_API_KEY: SecretStr
    AZURE_SEARCH_INDEX: str                 = "idx-rag"
    AZURE_SEARCH_SEMANTIC_CONFIG: str       = "rag-semantic-config"

    # ── Azure Service Bus ─────────────────────────────────────────────────────
    AZURE_SERVICE_BUS_CONNECTION_STR: SecretStr | None = None  # local dev
    AZURE_SERVICE_BUS_NAMESPACE: str        = ""               # prod (keyless)
    SB_QUEUE_INGESTION: str                 = "ingestion-queue"
    SB_QUEUE_PROCESSING: str                = "processing-queue"
    SB_QUEUE_EMBEDDING: str                 = "embedding-queue"
    
    # ── Microsoft Graph / SharePoint ──────────────────────────────────────────
    SHAREPOINT_TENANT_ID: str
    SHAREPOINT_CLIENT_ID: str
    SHAREPOINT_CLIENT_SECRET: SecretStr
    SHAREPOINT_WEBHOOK_SECRET: str          = "changeme"
 
    # Comma-separated SharePoint site URLs → resolved to site_id + drive_id at startup.
    # Format: https://<tenant>.sharepoint.com/sites/<site1>,https://<tenant>.sharepoint.com/sites/<site2>
    # Each URL maps to a domain via SITE_DOMAIN_MAP (site-url:domain,...)
    # Never store site_id or drive_id directly — resolve them from URLs at runtime.
    SHAREPOINT_SITE_URLS: str               = ""
 
    # Comma-separated site-url:domain pairs.
    # Example: https://ironman.sharepoint.com/sites/HR:hr,https://ironman.sharepoint.com/sites/Legal:legal
    # The site URL here must match exactly what's in SHAREPOINT_SITE_URLS.
    SITE_DOMAIN_MAP: str                    = ""
 
    # ── Processing tuning ─────────────────────────────────────────────────────
    CHILD_CHUNK_MAX_TOKENS: int             = Field(default=200, ge=50,  le=500)
    PARENT_CHUNK_MAX_TOKENS: int            = Field(default=1000, ge=200, le=3000)
    HEADER_FOOTER_MARGIN_PCT: float         = Field(default=0.07, ge=0.02, le=0.15)

    # ── Observability ─────────────────────────────────────────────────────────
    APPLICATIONINSIGHTS_CONNECTION_STRING: str | None = None
    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
