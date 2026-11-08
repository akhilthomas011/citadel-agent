@description('Hosting region.')
param location string

@description('Globally unique state account name.')
param accountName string

@description('Enable supported availability zones.')
param isZoneRedundant bool

@description('Maximum autoscale throughput for state.')
param cosmosMaxThroughput int

@description('BFF and agent principal IDs.')
param principalIds array

@description('Diagnostics workspace resource ID.')
param workspaceId string

@description('Hosting resource tags.')
param tags object

var databaseName = 'citadel-portal'
var containerName = 'portal-state'

resource account 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' = {
  name: accountName
  location: location
  kind: 'GlobalDocumentDB'
  tags: tags
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    enableMultipleWriteLocations: false
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: isZoneRedundant
      }
    ]
    publicNetworkAccess: 'Disabled'
    isVirtualNetworkFilterEnabled: true
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    minimalTlsVersion: 'Tls12'
    networkAclBypass: 'None'
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: { tier: 'Continuous7Days' }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: { id: databaseName }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: { paths: ['/id'], kind: 'Hash', version: 2 }
      defaultTtl: -1
      indexingPolicy: {
        automatic: true
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
    options: { autoscaleSettings: { maxThroughput: cosmosMaxThroughput } }
  }
}

resource access 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = [
  for principalId in principalIds: {
    parent: account
    name: guid(account.id, principalId, databaseName, containerName, 'contributor')
    properties: {
      principalId: principalId
      roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
      scope: '${account.id}/dbs/${databaseName}/colls/${containerName}'
    }
    dependsOn: [container]
  }
]

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'portal-state'
  scope: account
  properties: {
    workspaceId: workspaceId
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'Requests', enabled: true }]
  }
}

@description('State Cosmos resource ID.')
output accountId string = account.id

@description('State Cosmos HTTPS endpoint.')
output endpoint string = account.properties.documentEndpoint

@description('Portal database name.')
output stateDatabase string = databaseName

@description('Portal state container name.')
output stateContainer string = containerName
