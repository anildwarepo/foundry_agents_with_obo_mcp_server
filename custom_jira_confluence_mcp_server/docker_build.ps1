param(
    [Parameter(Mandatory=$true)]
    [string]$AcrName,
    
    [Parameter(Mandatory=$true)]
    [string]$ImageName,
    
    [Parameter(Mandatory=$true)]
    [string]$ImageVersion,
    
    [Parameter(Mandatory=$true)]
    [string]$ResourceGroup,

    [Parameter(Mandatory=$true)]
    [string]$ContainerAppEnv,

    [Parameter(Mandatory=$false)]
    [switch]$UseAcrBuild

)

az acr login --name $AcrName

if ($UseAcrBuild) {
    Write-Host "Using ACR Build to build and push the image..."
    # Build directly in ACR (no local Docker required)
    az acr build --registry $AcrName --image "${ImageName}:$ImageVersion" .
}
else {
    Write-Host "Using local Docker build to build and push the image..."
    # Local Docker build, tag, and push
    docker build -t "${ImageName}:$ImageVersion" .
    docker tag "${ImageName}:$ImageVersion" "$AcrName.azurecr.io/${ImageName}:$ImageVersion"
    docker push "$AcrName.azurecr.io/${ImageName}:$ImageVersion"
}

if (-not $ResourceGroup -or -not $ContainerAppEnv) {
    Write-Host "Error: -ResourceGroup and -ContainerAppEnv are required when using -Deploy"
    Exit 1
}


Write-Host "Deploying to Azure Container Apps..."

# Check if the Container App Environment exists, create if not
$ErrorActionPreference = "SilentlyContinue"
$existingEnv = az containerapp env show --name $ContainerAppEnv --resource-group $ResourceGroup --query "name" -o tsv 2>$null
$ErrorActionPreference = "Continue"
if (-not $existingEnv) {
    Write-Host "Container App Environment '$ContainerAppEnv' not found. Creating..."
    $location = az group show --name $ResourceGroup --query location -o tsv
    az containerapp env create `
        --name $ContainerAppEnv `
        --resource-group $ResourceGroup `
        --location $location
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: Failed to create Container App Environment" -ForegroundColor Red
        Exit 1
    }
    Write-Host "Container App Environment '$ContainerAppEnv' created successfully." -ForegroundColor Green
} else {
    Write-Host "Container App Environment '$ContainerAppEnv' already exists."
}

# Check if the Container App already exists
$ErrorActionPreference = "SilentlyContinue"
$existingApp = az containerapp show --name $ImageName --resource-group $ResourceGroup --query "name" -o tsv 2>$null
$ErrorActionPreference = "Continue"
if ($existingApp) {
    Write-Host "Container App '$ImageName' already exists. Updating the image..."
    Write-Host "fqdn: $(az containerapp show --name $ImageName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv)"
    az containerapp update `
        --name $ImageName `
        --resource-group $ResourceGroup `
        --image "$AcrName.azurecr.io/${ImageName}:$ImageVersion"
    Exit 0
}


# az containerapp delete --name $ImageName --resource-group $ResourceGroup --yes

Write-Host "Creating new Container App '$ImageName'..."

# Deploy to Azure Container Apps - new app
az containerapp create `
    --name $ImageName `
    --resource-group $ResourceGroup `
    --environment $ContainerAppEnv `
    --image "$AcrName.azurecr.io/${ImageName}:$ImageVersion" `
    --registry-server "$AcrName.azurecr.io" `
    --cpu 0.5 `
    --memory 1.0Gi `
    --min-replicas 1 `
    --max-replicas 1 `
    --ingress 'external' `
    --target-port 8000

if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Failed to create Container App. Please verify:" -ForegroundColor Red
    Write-Host "  - The environment '$ContainerAppEnv' exists in resource group '$ResourceGroup'" -ForegroundColor Yellow
    Write-Host "  - You have the required permissions" -ForegroundColor Yellow
    Write-Host "  - Run: az containerapp env list -g $ResourceGroup -o table" -ForegroundColor Yellow
    Exit 1
}

# Check deployment status
$fqdn = az containerapp show `
    --name $ImageName `
    --resource-group $ResourceGroup `
    --query properties.configuration.ingress.fqdn -o tsv

if ($fqdn) {
    Write-Host "Deployment successful!" -ForegroundColor Green
    Write-Host "Application URL: https://$fqdn" -ForegroundColor Cyan
} else {
    Write-Host "Deployment completed but could not retrieve FQDN." -ForegroundColor Yellow
}

Write-Host "Deployment completed."