"""
Microsoft Graph API client for SharePoint.

Handles:
  - App-only auth (client credentials) for daemon access to SharePoint
  - File listing in a folder (recursive)
  - File download as bytes
  - Webhook subscription create / renew / delete
  - Change notification parsing
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL  = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


class GraphClient:
    """
    Async Microsoft Graph client using app-only (client credentials) auth.
    One instance per agent process — token is refreshed automatically.
    """

    def __init__(self):
        self._token: str = ""
        self._token_expiry: float = 0.0

    async def _get_token(self) -> str:
        import time
        if self._token and time.monotonic() < self._token_expiry - 60:
            return self._token

        url = _TOKEN_URL.format(tenant_id=settings.SHAREPOINT_TENANT_ID)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, data={
                "grant_type":    "client_credentials",
                "client_id":     settings.SHAREPOINT_CLIENT_ID,
                "client_secret": settings.SHAREPOINT_CLIENT_SECRET.get_secret_value(),
                "scope":         "https://graph.microsoft.com/.default",
            })
            resp.raise_for_status()
            data = resp.json()
            self._token        = data["access_token"]
            self._token_expiry = time.monotonic() + data["expires_in"]
            logger.debug("Graph token refreshed, expires in %ds", data["expires_in"])
        return self._token

    async def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {await self._get_token()}",
            "Content-Type":  "application/json",
        }

    # ── File operations ───────────────────────────────────────────────────────

    async def list_folder_items(
        self,
        site_id: str,
        folder_path: str = "/",
        recursive: bool  = True,
    ) -> list[dict]:
        """
        Returns list of file items (not folders) under folder_path.
        Each item: {id, name, size, webUrl, file.mimeType, parentReference.driveId,
                    lastModifiedDateTime, @microsoft.graph.downloadUrl}
        """
        if folder_path in ("/", ""):
            url = f"{_GRAPH_BASE}/sites/{site_id}/drive/root/children"
        else:
            encoded = folder_path.strip("/").replace("/", ":/") + ":"
            url = f"{_GRAPH_BASE}/sites/{site_id}/drive/root:/{encoded}/children"

        items = []
        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                resp = await client.get(url, headers=await self._headers(),
                                        params={"$top": "200",
                                                "$select": "id,name,size,webUrl,file,folder,parentReference,lastModifiedDateTime"})
                resp.raise_for_status()
                data  = resp.json()
                for item in data.get("value", []):
                    if "file" in item:
                        items.append(item)
                    elif "folder" in item and recursive:
                        sub_path = (folder_path.rstrip("/") + "/" + item["name"])
                        sub_items = await self.list_folder_items(site_id, sub_path, recursive)
                        items.extend(sub_items)
                url = data.get("@odata.nextLink")
        logger.info("list_folder_items site=%s folder=%s found=%d", site_id, folder_path, len(items))
        return items

    async def download_file(self, site_id: str, drive_id: str, item_id: str) -> bytes:
        """Download file content as raw bytes."""
        url = f"{_GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{item_id}/content"
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url, headers=await self._headers())
            resp.raise_for_status()
            return resp.content

    async def get_item_metadata(self, site_id: str, drive_id: str, item_id: str) -> dict:
        """Get full metadata for a single drive item."""
        url = f"{_GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{item_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=await self._headers())
            resp.raise_for_status()
            return resp.json()

    # ── Webhook / subscription management ────────────────────────────────────

    async def create_subscription(
        self,
        site_id: str,
        notification_url: str,
        expiration_hours: int = 4230,   # Graph max for SharePoint is ~4230 min
    ) -> dict:
        """
        Subscribe to changes on the root drive of a SharePoint site.
        notification_url must be publicly reachable HTTPS.
        Returns the subscription object (id, expirationDateTime).
        """
        import datetime
        expiry = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=expiration_hours)
        ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

        payload = {
            "changeType":         "created,updated,deleted",
            "notificationUrl":    notification_url,
            "resource":           f"/sites/{site_id}/drive/root",
            "expirationDateTime": expiry,
            "clientState":        settings.SHAREPOINT_WEBHOOK_SECRET,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/subscriptions",
                headers=await self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            sub = resp.json()
            logger.info("Created Graph subscription id=%s expiry=%s", sub["id"], sub["expirationDateTime"])
            return sub

    async def renew_subscription(self, subscription_id: str, expiration_hours: int = 4230) -> dict:
        import datetime
        expiry = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=expiration_hours)
        ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
                headers=await self._headers(),
                json={"expirationDateTime": expiry},
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_subscription(self, subscription_id: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
                headers=await self._headers(),
            )
            resp.raise_for_status()
            logger.info("Deleted Graph subscription id=%s", subscription_id)

    async def get_changed_items(self, site_id: str, drive_id: str, delta_token: str | None = None) -> tuple[list[dict], str]:
        """
        Use drive delta to get changed items since last poll.
        Returns (changed_items, new_delta_token).
        Store delta_token in Blob or Table Storage between runs.
        """
        if delta_token:
            url = f"{_GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/root/delta?$deltatoken={delta_token}"
        else:
            url = f"{_GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/root/delta"

        items = []
        new_token = ""
        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                resp = await client.get(url, headers=await self._headers())
                resp.raise_for_status()
                data = resp.json()
                items.extend(data.get("value", []))
                if "@odata.nextLink" in data:
                    url = data["@odata.nextLink"]
                elif "@odata.deltaLink" in data:
                    new_token = data["@odata.deltaLink"].split("deltatoken=")[-1]
                    url = None
        return items, new_token


    async def resolve_site(self, site_url: str) -> dict:
        """
        Resolve a human-readable SharePoint site URL to its Graph site object.

        Accepts two formats:
          - Full URL:  https://ironman.sharepoint.com/sites/HR
          - Host/path: ironman.sharepoint.com:/sites/HR

        Returns dict with at minimum: id, displayName, webUrl
        This is the stable way to get site_id — the URL never changes
        even if the site is migrated or the drive is recreated.
        """
        # Normalise: strip scheme, build host:/path form expected by Graph
        url = site_url.strip().rstrip("/")
        if url.startswith("https://"):
            url = url[len("https://"):]
        if url.startswith("http://"):
            url = url[len("http://"):]

        # url is now: ironman.sharepoint.com/sites/HR
        # Graph expects:  /sites/ironman.sharepoint.com:/sites/HR
        if ":/" not in url:
            host, path = url.split("/", 1) if "/" in url else (url, "")
            graph_path = f"{host}:/{path}" if path else host
        else:
            graph_path = url  # already in host:/path form

        endpoint = f"{_GRAPH_BASE}/sites/{graph_path}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(endpoint, headers=await self._headers())
            resp.raise_for_status()
            site = resp.json()
            logger.debug("Resolved site_url=%s → site_id=%s", site_url, site["id"])
            return site

    async def get_default_drive_id(self, site_id: str) -> str:
        """
        Return the ID of the default document library drive for a site.
        This is the drive that contains the root /documents folder.
        More stable than storing drive_id in config — the default drive
        is always present and its ID can be fetched fresh on startup.
        """
        url = f"{_GRAPH_BASE}/sites/{site_id}/drive"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=await self._headers())
            resp.raise_for_status()
            drive = resp.json()
            logger.debug("Resolved site_id=%s → drive_id=%s", site_id, drive["id"])
            return drive["id"]

    async def resolve_site_and_drive(self, site_url: str) -> tuple[str, str]:
        """
        Convenience wrapper: resolve site URL → (site_id, drive_id).
        Call once at startup and cache the result.
        """
        site     = await self.resolve_site(site_url)
        site_id  = site["id"]
        drive_id = await self.get_default_drive_id(site_id)
        return site_id, drive_id


# Module-level singleton
graph_client = GraphClient()
