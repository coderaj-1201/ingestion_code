"""Service Bus send helpers for the ingestion pipeline.

Provides a single :func:`send_to_queue` coroutine used by all three agents to
push JSON messages onto Azure Service Bus queues. Each call opens a fresh
``ServiceBusClient`` and sender so there is no shared connection state to
manage across concurrent tasks.

Uses ``DefaultAzureCredential`` — ``ManagedIdentityCredential`` in ACA,
``AzureCliCredential`` on a developer workstation.
"""
from __future__ import annotations

import json
import logging

from azure.servicebus import ServiceBusMessage
from shared.config import settings


logger = logging.getLogger(__name__)


async def send_to_queue(queue_name: str, payload: dict, correlation_id: str = "") -> None:
    """Send a single JSON message to a Service Bus queue."""
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient

    async with DefaultAzureCredential() as credential, AsyncSBClient(
        fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
        credential=credential,
    ) as sb:
        async with sb.get_queue_sender(queue_name) as sender:
            msg = ServiceBusMessage(
                body=json.dumps(payload),
                correlation_id=correlation_id,
                content_type="application/json",
            )
            await sender.send_messages(msg)
            logger.debug("Sent to queue=%s correlation_id=%s", queue_name, correlation_id)


