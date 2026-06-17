"""
One-off deploy helper: pushes logic_apps/upsert_workflow.json live via the
ARM REST API directly, bypassing `az logic workflow update`'s shorthand-syntax
parser (which chokes on this nested JSON on Windows).

Usage:
    python deploy_logic_app.py PASTE_NEW_SECRET_HERE
"""
import json
import subprocess
import sys

import requests

SUBSCRIPTION_ID = "41d22965-fc9f-4e6b-8e10-c70bdba716c9"
RESOURCE_GROUP = "rg-aisharedservices-eastus-prod"
WORKFLOW_NAME = "lgcapp-aishrdsvcs-eus-prod"
LOCATION = "eastus"
DEFINITION_PATH = "logic_apps/upsert-workflow.json"

if len(sys.argv) != 2:
    print("Usage: python deploy_logic_app.py <logicAppSecret>")
    sys.exit(1)

logic_app_secret = sys.argv[1]

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
            "sharepointSiteUrl": {"value": "https://irondrive.sharepoint.com/sites/OPSPlaybook"},
            "sharepointLibrary": {"value": "Global Ops Playbook"},
            "domain": {"value": "ops"},
            "ingestionAgentUrl": {
                "value": "https://bpf2vqkh-8010.inc1.devtunnels.ms"
            },
            "logicAppSecret": {"value": logic_app_secret},
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
