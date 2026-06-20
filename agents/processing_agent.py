"""
Processing Agent — entrypoint.

Imports the FastAPI ``app`` from ``agents.processing.listener`` and starts
Uvicorn. All business logic lives in the ``agents/processing/`` sub-package:

    agents/processing/blob_ops.py  — Blob Storage helpers + SHA-256 dedup check
    agents/processing/pipeline.py  — MAF steps and core processing logic
    agents/processing/listener.py  — Service Bus listener + FastAPI app
"""
import uvicorn

from agents.processing.listener import app  # noqa: F401 — re-exported for uvicorn discovery

if __name__ == "__main__":
    uvicorn.run("agents.processing_agent:app", host="0.0.0.0", port=8011, reload=False)
