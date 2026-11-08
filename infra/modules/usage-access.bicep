@description('Existing Cosmos usage account name.')
param accountName string

@description('Existing usage database name.')
param databaseName string

@description('Portal service principal receiving read-only source-data access.')
param agentPrincipalId string

resource account 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = { name: accountName }

resource reader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, agentPrincipalId, databaseName, 'reader')
  properties: {
    principalId: agentPrincipalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
    scope: '${account.id}/dbs/${databaseName}'
  }
}
