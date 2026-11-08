using './main.bicep'

// Compilation example only. Replace customer values and use the installer for deployment.
var example = loadJsonContent('./install.example.json')

param resourceGroupName = example.resourceGroupName
param namePrefix = example.namePrefix
param location = 'swedencentral'
param agentImage = 'ghcr.io/example/citadel-agent@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
param isZoneRedundant = example.isZoneRedundant
param cosmosMaxThroughput = example.cosmosMaxThroughput
param shouldCreateGatewayPrivateEndpoint = example.shouldCreateGatewayPrivateEndpoint
param network = example.network
param connections = {
  webApp: { resourceGroupName: example.webApp.resourceGroupName, name: example.webApp.name }
  bffIdentity: { resourceGroupName: example.bffIdentity.resourceGroupName, name: example.bffIdentity.name }
  keyVault: { resourceGroupName: example.keyVault.resourceGroupName, name: example.keyVault.name }
  usageAccount: { resourceGroupName: example.usageAccount.resourceGroupName, name: example.usageAccount.name }
  modelAccount: { resourceGroupName: example.modelAccount.resourceGroupName, name: example.modelAccount.name }
  apim: { resourceGroupName: example.hub.apimResourceGroup, name: example.hub.apimName }
  logs: { resourceGroupName: example.logs.resourceGroupName, name: example.logs.name }
  usageDatabase: example.hub.usageDatabase
  clientSecretName: example.entra.clientSecretName
  githubSecretName: example.hub.githubTokenSecretName
}
param agentSettings = {
  CITADEL_PORTAL_TENANT_ID: example.hub.scope.tenantId
  CITADEL_PORTAL_CLIENT_ID: example.entra.clientId
  CITADEL_PORTAL_API_AUDIENCE: example.entra.apiClientId
  CITADEL_PORTAL_DELEGATED_SCOPE: 'api://${example.entra.apiClientId}/access_as_user'
  CITADEL_PORTAL_DELEGATED_SCOPE_NAME: example.entra.delegatedScopeName
  CITADEL_PORTAL_CLIENT_SECRET_NAME: example.entra.clientSecretName
  CITADEL_PORTAL_KEY_VAULT_URL: 'https://${example.keyVault.name}.vault.azure.net'
  CITADEL_PORTAL_PORTAL_ORIGIN: 'https://${example.webApp.name}.azurewebsites.net'
  CITADEL_PORTAL_CALLBACK_URI: 'https://${example.webApp.name}.azurewebsites.net/auth/callback'
  CITADEL_PORTAL_AGENT_API_AUDIENCE: example.entra.apiClientId
  CITADEL_PORTAL_AGENT_API_TOKEN_SCOPE: 'api://${example.entra.apiClientId}/.default'
  CITADEL_PORTAL_HUB_CONFIG: string(example.hub)
}
