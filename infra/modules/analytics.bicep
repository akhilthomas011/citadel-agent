metadata name = 'Private analytics storage'
metadata description = 'Application-owned analytics generations with worker-only writes and agent-only reads.'

@description('Existing application-owned Cosmos account name.')
param accountName string

@description('Existing application-owned database name.')
param databaseName string

@description('Dedicated analytics container name.')
param containerName string

@description('Analytics worker managed-identity principal ID.')
param writerPrincipalId string

@description('Private agent managed-identity principal ID.')
param readerPrincipalId string

@description('Maximum autoscale throughput for the analytics container.')
param maxThroughput int

resource account 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: accountName
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' existing = {
  parent: account
  name: databaseName
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: { paths: ['/partitionKey'], kind: 'Hash', version: 2 }
      defaultTtl: -1
      indexingPolicy: {
        automatic: true
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [
          { path: '/"_etag"/?' }
          { path: '/payload/continuation/?' }
          { path: '/payload/feedRanges/*' }
        ]
      }
    }
    options: { autoscaleSettings: { maxThroughput: maxThroughput } }
  }
}

resource writer 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, writerPrincipalId, databaseName, containerName, 'writer')
  properties: {
    principalId: writerPrincipalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${account.id}/dbs/${databaseName}/colls/${containerName}'
  }
  dependsOn: [container]
}

resource reader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, readerPrincipalId, databaseName, containerName, 'reader')
  properties: {
    principalId: readerPrincipalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
    scope: '${account.id}/dbs/${databaseName}/colls/${containerName}'
  }
  dependsOn: [container]
}
