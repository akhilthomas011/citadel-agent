@description('Existing gateway Log Analytics workspace name.')
param workspaceName string

@description('Agent principal ID.')
param agentPrincipalId string

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' existing = { name: workspaceName }

var roleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workspace.id, agentPrincipalId, roleId)
  scope: workspace
  properties: { roleDefinitionId: roleId, principalId: agentPrincipalId, principalType: 'ServicePrincipal' }
}
