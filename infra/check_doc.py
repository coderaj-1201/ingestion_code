import os
from dotenv import load_dotenv
load_dotenv()

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

doc_name = "Parle-G - Wikipedia.pdf"

client = SearchClient(
    endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
    index_name=os.environ.get("AZURE_SEARCH_INDEX", "idx-rag"),
    credential=DefaultAzureCredential(),
)

escaped = doc_name.replace("'", "''")
results = list(client.search(
    search_text="*",
    filter=f"doc_name eq '{escaped}'",
    select=["id", "doc_name", "domain", "ingested_at", "page_number"],
    top=10,
))

print(f"Chunks found: {len(results)}")
for r in results:
    print(r)
