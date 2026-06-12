"""Service Bus send helpers for the ingestion pipeline."""
from __future__ import annotations

import json
import logging
import os

from azure.servicebus import ServiceBusMessage
from shared.config import settings


logger = logging.getLogger(__name__)


async def send_to_queue(queue_name: str, payload: dict, correlation_id: str = "") -> None:
    """Send a single JSON message to a Service Bus queue. Always uses Managed Identity in Azure."""
    import asyncio
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient
    from shared.config import settings

    if os.getenv("RUNNING_IN_AZURE"):
        from azure.identity.aio import ManagedIdentityCredential
        credential = ManagedIdentityCredential()
        sb = AsyncSBClient(
            fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
            credential=credential,
        )
    elif settings.AZURE_SERVICE_BUS_CONNECTION_STR:
        sb = AsyncSBClient.from_connection_string(
            settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
        )
    else:
        from azure.identity.aio import AzureCliCredential
        sb = AsyncSBClient(
            fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
            credential=AzureCliCredential(),
        )

    async with sb:
        async with sb.get_queue_sender(queue_name) as sender:
            msg = ServiceBusMessage(
                body=json.dumps(payload),
                correlation_id=correlation_id,
                content_type="application/json",
            )
            await sender.send_messages(msg)
            logger.debug("Sent to queue=%s correlation_id=%s", queue_name, correlation_id)


