@description('Existing API Management gateway name.')
param apimName string

@description('Agent principal ID.')
param agentPrincipalId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = { name: apimName }

var roleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, agentPrincipalId, roleId)
  scope: apim
  properties: { roleDefinitionId: roleId, principalId: agentPrincipalId, principalType: 'ServicePrincipal' }
}
