"""
Embedding Agent — entrypoint.

Imports the FastAPI ``app`` from ``agents.embedding.listener`` and starts
Uvicorn. All business logic lives in the ``agents/embedding/`` sub-package:

    agents/embedding/search_ops.py — AI Search upload + delete helpers
    agents/embedding/pipeline.py   — chunk download, embedding, MAF workflow
    agents/embedding/listener.py   — Service Bus listener + FastAPI app
"""
import uvicorn

from agents.embedding.listener import app  # noqa: F401 — re-exported for uvicorn discovery

if __name__ == "__main__":
    uvicorn.run("agents.embedding_agent:app", host="0.0.0.0", port=8012, reload=False)
