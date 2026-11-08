import { Connections, NetworkConfig } from '../types.bicep'

@description('Hosting region.')
param location string

@description('Validated resource prefix.')
param namePrefix string

@description('Existing customer connections.')
param connections Connections

@description('Reviewed network prefixes.')
param network NetworkConfig

@description('Non-secret agent runtime settings.')
param agentSettings object

@description('Immutable public release image.')
param agentImage string

@description('Enable supported availability zones.')
param isZoneRedundant bool

@description('Create a supported Gateway private endpoint without changing APIM configuration.')
param shouldCreateGatewayPrivateEndpoint bool

@description('State container autoscale maximum.')
param cosmosMaxThroughput int

@description('Hosting resource tags.')
param tags object

var agentName = 'ca-${namePrefix}-agent'
var cosmosName = 'cosmos-${namePrefix}-${uniqueString(subscription().subscriptionId, resourceGroup().name)}'

resource bffIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: connections.bffIdentity.name
  scope: resourceGroup(connections.bffIdentity.resourceGroupName)
}

resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: 'id-${namePrefix}-agent'
}

resource analyticsIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: 'id-${namePrefix}-analytics'
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: 'log-${namePrefix}-portal'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    features: { disableLocalAuth: true }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

module networking './network.bicep' = {
  name: 'portal-network'
  params: { location: location, namePrefix: namePrefix, network: network, tags: tags }
}

module state './state.bicep' = {
  name: 'portal-state'
  params: {
    location: location
    accountName: cosmosName
    isZoneRedundant: isZoneRedundant
    cosmosMaxThroughput: cosmosMaxThroughput
    principalIds: [bffIdentity.properties.principalId, agentIdentity.properties.principalId]
    workspaceId: workspace.id
    tags: tags
  }
}

var analyticsContainerName = 'portal-analytics'

module analyticsStorage './analytics.bicep' = {
  params: {
    accountName: cosmosName
    databaseName: state.outputs.stateDatabase
    containerName: analyticsContainerName
    writerPrincipalId: analyticsIdentity.properties.principalId
    readerPrincipalId: agentIdentity.properties.principalId
    maxThroughput: cosmosMaxThroughput
  }
}

module privateLinks './private-links.bicep' = {
  name: 'portal-private-links'
  params: {
    location: location
    namePrefix: namePrefix
    vnetId: networking.outputs.vnetId
    subnetId: networking.outputs.endpointSubnetId
    targets: concat(
      [
        {
          name: 'state'
          resourceId: state.outputs.accountId
          groupId: 'Sql'
          zones: ['privatelink.documents.azure.com']
        }
        {
          name: 'usage'
          resourceId: resourceId(
            connections.usageAccount.resourceGroupName,
            'Microsoft.DocumentDB/databaseAccounts',
            connections.usageAccount.name
          )
          groupId: 'Sql'
          zones: ['privatelink.documents.azure.com']
        }
        {
          name: 'vault'
          resourceId: resourceId(
            connections.keyVault.resourceGroupName,
            'Microsoft.KeyVault/vaults',
            connections.keyVault.name
          )
          groupId: 'vault'
          zones: ['privatelink.vaultcore.azure.net']
        }
        {
          name: 'model'
          resourceId: resourceId(
            connections.modelAccount.resourceGroupName,
            'Microsoft.CognitiveServices/accounts',
            connections.modelAccount.name
          )
          groupId: 'account'
          zones: [
            'privatelink.openai.azure.com'
            'privatelink.cognitiveservices.azure.com'
            'privatelink.services.ai.azure.com'
          ]
        }
      ],
      shouldCreateGatewayPrivateEndpoint
        ? [
            {
              name: 'gateway'
              resourceId: resourceId(
                connections.apim.resourceGroupName,
                'Microsoft.ApiManagement/service',
                connections.apim.name
              )
              groupId: 'Gateway'
              zones: ['privatelink.azure-api.net']
            }
          ]
        : []
    )
    tags: tags
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-${namePrefix}-portal'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: networking.outputs.agentSubnetId
      internal: true
    }
    zoneRedundant: isZoneRedundant
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: { destination: 'azure-monitor' }
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'portal-environment'
  scope: environment
  properties: {
    workspaceId: workspace.id
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

module environmentDns './environment-dns.bicep' = {
  name: 'portal-environment-dns'
  params: {
    domain: environment.properties.defaultDomain
    staticIp: environment.properties.staticIp
    vnetId: networking.outputs.vnetId
    namePrefix: namePrefix
    tags: tags
  }
}

var agentUrl = 'https://${agentName}.${environment.properties.defaultDomain}'
var generatedSettings = [
  { name: 'CITADEL_PORTAL_COSMOS_ENDPOINT', value: state.outputs.endpoint }
  { name: 'CITADEL_PORTAL_COSMOS_DATABASE', value: state.outputs.stateDatabase }
  { name: 'CITADEL_PORTAL_COSMOS_CONTAINER', value: state.outputs.stateContainer }
  { name: 'CITADEL_PORTAL_AGENT_API_URL', value: agentUrl }
  { name: 'CITADEL_PORTAL_BFF_SERVICE_CLIENT_ID', value: bffIdentity.properties.clientId }
  { name: 'CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID', value: agentIdentity.properties.clientId }
  { name: 'CITADEL_PORTAL_ANALYTICS_CONTAINER', value: analyticsContainerName }
  { name: 'CITADEL_PORTAL_DURABLE_TURNS_ENABLED', value: 'true' }
]
var configuredSettings = [for pair in items(agentSettings): { name: pair.key, value: string(pair.value) }]

resource agent 'Microsoft.App/containerApps@2025-01-01' = {
  name: agentName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${agentIdentity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
      }
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: agentImage
          resources: { cpu: 1, memory: '2Gi' }
          env: concat(configuredSettings, generatedSettings)
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8000, scheme: 'HTTP' }
              periodSeconds: 10
              failureThreshold: 30
              timeoutSeconds: 5
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8000, scheme: 'HTTP' }
              periodSeconds: 30
              failureThreshold: 3
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8000, scheme: 'HTTP' }
              periodSeconds: 15
              failureThreshold: 3
              timeoutSeconds: 5
            }
          ]
        }
      ]
      scale: {
        minReplicas: 2
        maxReplicas: 5
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '10' } } }]
      }
    }
  }
  dependsOn: [privateLinks, environmentDns, analyticsStorage]
}

var workerHubConfig = union(json(string(agentSettings.CITADEL_PORTAL_HUB_CONFIG)), {
  initialGrants: []
  githubTokenSecretName: null
})

// Keep job properties type-checked by the release-pinned Bicep compiler.
resource analyticsWorker 'Microsoft.App/jobs@2025-01-01' = {
  name: 'caj-${namePrefix}-analytics'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${analyticsIdentity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: '*/5 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
    }
    template: {
      containers: [
        {
          name: 'analytics'
          image: agentImage
          args: ['analytics']
          resources: { cpu: 1, memory: '2Gi' }
          env: [
            { name: 'CITADEL_PORTAL_COSMOS_ENDPOINT', value: state.outputs.endpoint }
            { name: 'CITADEL_PORTAL_COSMOS_DATABASE', value: state.outputs.stateDatabase }
            { name: 'CITADEL_PORTAL_ANALYTICS_CONTAINER', value: analyticsContainerName }
            { name: 'CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID', value: analyticsIdentity.properties.clientId }
            { name: 'CITADEL_PORTAL_HUB_CONFIG', value: string(workerHubConfig) }
          ]
        }
      ]
    }
  }
  dependsOn: [privateLinks, environmentDns, analyticsStorage]
}

resource assistantWorker 'Microsoft.App/jobs@2025-01-01' = {
  name: 'caj-${namePrefix}-assistant'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${agentIdentity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 900
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: '* * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
    }
    template: {
      containers: [
        {
          name: 'assistant-worker'
          image: agentImage
          args: ['assistant-worker']
          resources: { cpu: 1, memory: '2Gi' }
          env: concat(configuredSettings, generatedSettings)
        }
      ]
    }
  }
  dependsOn: [privateLinks, environmentDns, analyticsStorage]
}

@description('Non-secret values for updating the selected existing web app without replacing it.')
output hosts object = {
  agentApp: agent.name
  agentResourceGroup: resourceGroup().name
  agentApiUrl: agentUrl
  bffIdentityId: bffIdentity.id
  bffIdentityClientId: bffIdentity.properties.clientId
  bffSubnetId: networking.outputs.bffSubnetId
  stateEndpoint: state.outputs.endpoint
  stateDatabase: state.outputs.stateDatabase
  stateContainer: state.outputs.stateContainer
  analyticsContainer: analyticsContainerName
  analyticsJob: analyticsWorker.name
  assistantJob: assistantWorker.name
}
