"""
Deploy helper for the delete Logic App workflow.

This deploys a separate Logic App that polls SharePoint every 5 minutes,
compares the current file list to a stored snapshot, and sends delete
signals to the ingestion agent for any files that have disappeared.

Pre-requisites (one-time, in Azure Portal or CLI):
  1. Create Logic App resource:
       az logic workflow create --resource-group rg-aisharedservices-eastus-prod \
           --location eastus --name lgcapp-delete-aishrdsvcs-eus-prod
  2. Enable system-assigned managed identity on the Logic App:
       az logic workflow identity assign --resource-group rg-aisharedservices-eastus-prod \
           --name lgcapp-delete-aishrdsvcs-eus-prod --identity-type SystemAssigned
  3. Grant the managed identity Storage Blob Data Contributor on your storage account:
       OBJECT_ID=$(az logic workflow show \
           --resource-group rg-aisharedservices-eastus-prod \
           --name lgcapp-delete-aishrdsvcs-eus-prod \
           --query identity.principalId -o tsv)
       az role assignment create \
           --assignee $OBJECT_ID \
           --role "Storage Blob Data Contributor" \
           --scope /subscriptions/41d22965-fc9f-4e6b-8e10-c70bdba716c9/resourceGroups/rg-aisharedservices-eastus-prod/providers/Microsoft.Storage/storageAccounts/<YOUR_STORAGE_ACCOUNT>

Usage:
    python deploy_delete_logic_app.py <logicAppSecret> <storageAccountName>
"""
import json
import subprocess
import sys

import requests

SUBSCRIPTION_ID  = "41d22965-fc9f-4e6b-8e10-c70bdba716c9"
RESOURCE_GROUP   = "rg-aisharedservices-eastus-prod"
WORKFLOW_NAME    = "lgcapp-delete-aishrdsvcs-eus-prod"
LOCATION         = "eastus"
DEFINITION_PATH  = "logic_apps/delete-workflow.json"

if len(sys.argv) != 3:
    print("Usage: python deploy_delete_logic_app.py <logicAppSecret> <storageAccountName>")
    sys.exit(1)

logic_app_secret     = sys.argv[1]
storage_account_name = sys.argv[2]

with open(DEFINITION_PATH, "r", encoding="utf-8") as f:
    definition = json.load(f)

token = subprocess.check_output(
    ["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"],
    text=True,
    shell=True,
).strip()

body = {
    "location": LOCATION,
    "properties": {
        "state": "Enabled",
        "definition": definition,
        "parameters": {
            "$connections": {
                "value": {
                    "sharepointonline": {
                        "id": f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Web/locations/{LOCATION}/managedApis/sharepointonline",
                        "connectionId": f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Web/connections/sharepointonline",
                        "connectionName": "sharepointonline",
                    }
                }
            },
            "sharepointSiteUrl":  {"value": "https://irondrive.sharepoint.com/sites/OPSPlaybook"},
            "sharepointLibrary":  {"value": "Global Ops Playbook"},
            "domain":             {"value": "ops"},
            "ingestionAgentUrl":  {
                "value": "https://cntapp-ingbot-aishrdvcs-eus-prod.mangoisland-637b477f.eastus.azurecontainerapps.io"
            },
            "logicAppSecret":     {"value": logic_app_secret},
            "storageAccountName": {"value": storage_account_name},
        },
    },
}

url = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
    f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{WORKFLOW_NAME}"
    "?api-version=2019-05-01"
)

resp = requests.put(
    url,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json=body,
)

print(resp.status_code)
print(resp.text[:2000])
