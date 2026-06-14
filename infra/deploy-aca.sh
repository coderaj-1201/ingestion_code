#!/usr/bin/env bash
# =============================================================================
# Deploy all three ingestion agents to Azure Container Apps.
#
# Prerequisites:
#   az login
#   az extension add --name containerapp
#   az acr login --name <your-acr>
#
# Usage:
#   chmod +x infra/deploy-aca.sh
#   ./infra/deploy-aca.sh \
#       --resource-group  rg-rag-prod \
#       --acr             myacr \
#       --env             rag-aca-env \
#       --tag             v1.0.0
#
# What this script does:
#   1. Builds and pushes the Docker image to ACR (one image, three agents)
#   2. Creates or updates the Container App Environment (shared VNet + Log Analytics)
#   3. Deploys three Container Apps (ingestion / processing / embedding)
#      each with the correct AGENT and PORT env vars
#   4. Sets all secrets from your local .env file
#
# Secrets strategy:
#   Secrets are set from .env via `az containerapp secret set`.
#   In a mature setup, replace this with Azure Key Vault references.
# =============================================================================

set -euo pipefail

# ── Parse arguments ───────────────────────────────────────────────────────────
RESOURCE_GROUP=""
ACR_NAME=""
ACA_ENV=""
IMAGE_TAG="latest"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --acr)            ACR_NAME="$2";        shift 2 ;;
    --env)            ACA_ENV="$2";         shift 2 ;;
    --tag)            IMAGE_TAG="$2";       shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ -z "$RESOURCE_GROUP" || -z "$ACR_NAME" || -z "$ACA_ENV" ]]; then
  echo "Usage: $0 --resource-group <rg> --acr <acr-name> --env <aca-env> [--tag <tag>]"
  exit 1
fi

IMAGE="${ACR_NAME}.azurecr.io/ingestion-pipeline:${IMAGE_TAG}"
LOCATION=$(az group show --name "$RESOURCE_GROUP" --query location -o tsv)

echo "=== Building and pushing Docker image ==="
docker build -t "$IMAGE" .
docker push "$IMAGE"

echo "=== Ensuring Container App Environment exists ==="
az containerapp env show \
    --name "$ACA_ENV" \
    --resource-group "$RESOURCE_GROUP" \
    --query name -o tsv 2>/dev/null || \
az containerapp env create \
    --name "$ACA_ENV" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION"

# ── Read .env into az containerapp secret format ──────────────────────────────
# Secrets are passed as  key=value pairs from .env (ignoring blank lines / comments).
ENV_SECRETS=""
if [[ -f .env ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        value="${value%%#*}"   # strip inline comments
        value="${value//\"/}"  # strip quotes
        ENV_SECRETS+="${key,,}=${value} "  # ACA secret names must be lowercase
    done < .env
fi

# ── Helper: deploy or update one Container App ────────────────────────────────
deploy_agent() {
    local name="$1"
    local agent="$2"
    local port="$3"

    echo ""
    echo "=== Deploying ${name} (AGENT=${agent}, PORT=${port}) ==="

    # Check if it already exists
    if az containerapp show \
        --name "$name" \
        --resource-group "$RESOURCE_GROUP" \
        --query name -o tsv 2>/dev/null; then
        # Update existing
        az containerapp update \
            --name "$name" \
            --resource-group "$RESOURCE_GROUP" \
            --image "$IMAGE" \
            --set-env-vars \
                AGENT="$agent" \
                PORT="$port" \
                RUNNING_IN_AZURE="true"
    else
        # Create new
        az containerapp create \
            --name "$name" \
            --resource-group "$RESOURCE_GROUP" \
            --environment "$ACA_ENV" \
            --image "$IMAGE" \
            --registry-server "${ACR_NAME}.azurecr.io" \
            --min-replicas 1 \
            --max-replicas 3 \
            --cpu 0.5 \
            --memory 1.0Gi \
            --ingress external \
            --target-port "$port" \
            --env-vars \
                AGENT="$agent" \
                PORT="$port" \
                RUNNING_IN_AZURE="true" \
            ${ENV_SECRETS:+--secrets $ENV_SECRETS}
    fi
}

deploy_agent "rag-ingestion-agent"  "ingestion"  "8010"
deploy_agent "rag-processing-agent" "processing" "8011"
deploy_agent "rag-embedding-agent"  "embedding"  "8012"

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Ingestion Agent URL:"
az containerapp show \
    --name rag-ingestion-agent \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn -o tsv | xargs -I{} echo "  https://{}"

echo ""
echo "Next steps:"
echo "  1. Run: python infra/create_search_index.py   (create AI Search index)"
echo "  2. Deploy Logic Apps:  az deployment group create --template-file logic_apps/arm-deploy.json ..."
echo "  3. Set the Ingestion Agent URL in Logic App parameters"
echo "  4. Authenticate the SharePoint connection in Azure Portal → Logic App → Connections"
