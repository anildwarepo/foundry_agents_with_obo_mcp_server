# Assigns Cosmos DB Built-in Data Contributor role to the signed-in user
# This resolves: "principal does not have required RBAC permissions to perform action [Microsoft.DocumentDB/databaseAccounts/readMetadata]"

$resourceGroup = "rg-hostedagent136"
$accountName = "trz5cosmosdb"
$subscriptionId = (az account show --query "id" -o tsv)
$principalId = (az ad signed-in-user show --query "id" -o tsv)

# Cosmos DB Built-in Data Contributor role
$roleDefinitionId = "00000000-0000-0000-0000-000000000002"

$scope = "/subscriptions/$subscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.DocumentDB/databaseAccounts/$accountName"

Write-Host "Assigning Cosmos DB Built-in Data Contributor role..."
Write-Host "  Account:   $accountName"
Write-Host "  Principal: $principalId"
Write-Host "  Scope:     $scope"

az cosmosdb sql role assignment create `
    --resource-group $resourceGroup `
    --account-name $accountName `
    --role-definition-id $roleDefinitionId `
    --principal-id $principalId `
    --scope $scope

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nRole assignment created successfully." -ForegroundColor Green
} else {
    Write-Host "`nFailed to create role assignment." -ForegroundColor Red
}
