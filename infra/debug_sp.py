import subprocess
import requests

token = subprocess.check_output(
    "az account get-access-token --resource https://irondrive.sharepoint.com --query accessToken -o tsv",
    shell=True, text=True
).strip()

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json;odata=nometadata",
}

base = "https://irondrive.sharepoint.com/sites/OPSPlaybook/_api/web/lists/GetByTitle('Global Ops Playbook')/items"

# Total file count
r = requests.get(base, headers=headers, params={
    "$select": "FileLeafRef",
    "$filter": "FileSystemObjectType eq 0",
    "$top": "1",
    "$inlinecount": "allpages",
})
print("Total files in library:", r.json().get("odata.count", r.text[:200]))

# Files in zz-Test folder
r2 = requests.get(base, headers=headers, params={
    "$select": "FileLeafRef,FileDirRef",
    "$filter": "FileSystemObjectType eq 0 and substringof('zz', FileDirRef)",
    "$top": "50",
})
items = r2.json().get("value", [])
print(f"\nFiles in zz-Test folder ({len(items)}):")
for i in items:
    print(f"  {i.get('FileDirRef')} / {i.get('FileLeafRef')}")

# What the Logic App connector sees (no filter, top 500)
r3 = requests.get(base, headers=headers, params={
    "$select": "FileLeafRef,FileSystemObjectType",
    "$filter": "FileSystemObjectType eq 0",
    "$top": "500",
    "$inlinecount": "allpages",
})
data3 = r3.json()
all_files = [i["FileLeafRef"] for i in data3.get("value", []) if i.get("FileLeafRef")]
print(f"\nLogic App would see {len(all_files)} files (top 500)")
print("Next page link:", data3.get("odata.nextLink", "none — all fit in 500"))
