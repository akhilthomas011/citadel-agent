@description('Existing Azure OpenAI-compatible account name.')
param accountName string

@description('Agent principal ID.')
param agentPrincipalId string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = { name: accountName }

var roleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, agentPrincipalId, roleId)
  scope: account
  properties: { roleDefinitionId: roleId, principalId: agentPrincipalId, principalType: 'ServicePrincipal' }
}
