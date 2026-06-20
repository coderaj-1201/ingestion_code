"""
Ingestion Agent — entrypoint.

Imports the FastAPI ``app`` from ``agents.ingestion.routes`` and starts
Uvicorn. All business logic lives in the ``agents/ingestion/`` sub-package:

    agents/ingestion/blob_ops.py      — Blob Storage helpers (upload, dedup, delete)
    agents/ingestion/search_ops.py    — AI Search delete helper
    agents/ingestion/sharepoint_ops.py — SharePoint site resolution + MAF workflow
    agents/ingestion/routes.py        — FastAPI app, lifespan, and all route handlers
"""
import uvicorn

from agents.ingestion.routes import app  # noqa: F401 — re-exported for uvicorn discovery

if __name__ == "__main__":
    uvicorn.run("agents.ingestion_agent:app", host="0.0.0.0", port=8010, reload=False)
