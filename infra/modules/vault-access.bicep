@description('Existing RBAC-enabled Key Vault name.')
param vaultName string

@description('Existing confidential-client credential secret name.')
param clientSecretName string

@description('Existing canonical-repository credential secret name.')
param githubSecretName string

@description('BFF principal ID.')
param bffPrincipalId string

@description('Agent principal ID.')
param agentPrincipalId string

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = { name: vaultName }
resource clientSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = {
  parent: vault
  name: clientSecretName
}
resource githubSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = {
  parent: vault
  name: githubSecretName
}

var roleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource bffAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(clientSecret.id, bffPrincipalId, roleId)
  scope: clientSecret
  properties: { roleDefinitionId: roleId, principalId: bffPrincipalId, principalType: 'ServicePrincipal' }
}

resource agentAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(githubSecret.id, agentPrincipalId, roleId)
  scope: githubSecret
  properties: { roleDefinitionId: roleId, principalId: agentPrincipalId, principalType: 'ServicePrincipal' }
}
