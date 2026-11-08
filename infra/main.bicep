targetScope = 'subscription'

import { Connections, NetworkConfig } from './types.bicep'

@description('Dedicated resource group for the new portal hosting resources.')
param resourceGroupName string

@description('Azure region, verified to match the existing APIM and Linux App Service.')
param location string

@minLength(3)
@maxLength(16)
@description('Lowercase alphanumeric/hyphen prefix, validated by the installer.')
param namePrefix string

@description('Existing customer resources. They are referenced, never redeployed.')
param connections Connections

@description('Reviewed network prefixes which do not overlap customer virtual networks.')
param network NetworkConfig

@description('Complete non-secret runtime settings for the private agent, excluding generated host values.')
param agentSettings object

@description('Immutable, anonymously pullable Linux agent image pinned by SHA-256 digest.')
param agentImage string

@description('Enable zone redundancy in a supported region.')
param isZoneRedundant bool = true

@description('Connect the existing gateway through a supported Gateway private endpoint.')
param shouldCreateGatewayPrivateEndpoint bool = true

@minValue(1000)
@maxValue(100000)
@description('Maximum autoscale RU/s for each application state and analytics container.')
param cosmosMaxThroughput int = 1000

@description('Resource tags for the dedicated hosting resources.')
param tags object = {
  application: 'citadel-portal'
  managedBy: 'citadel-installer'
}

resource hostingGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module identities './modules/identities.bicep' = {
  name: '${namePrefix}-identities'
  scope: hostingGroup
  params: {
    location: location
    namePrefix: namePrefix
    bffIdentityRef: connections.bffIdentity
    tags: tags
  }
}

module hosting './modules/hosting.bicep' = {
  name: '${namePrefix}-hosting'
  scope: hostingGroup
  params: {
    location: location
    namePrefix: namePrefix
    connections: connections
    network: network
    agentSettings: agentSettings
    agentImage: agentImage
    isZoneRedundant: isZoneRedundant
    shouldCreateGatewayPrivateEndpoint: shouldCreateGatewayPrivateEndpoint
    cosmosMaxThroughput: cosmosMaxThroughput
    tags: tags
  }
  dependsOn: [usage, analyticsUsage, vault, model, gateway, logs]
}

module analyticsUsage './modules/usage-access.bicep' = {
  name: '${namePrefix}-analytics-source-access'
  scope: resourceGroup(connections.usageAccount.resourceGroupName)
  params: {
    accountName: connections.usageAccount.name
    databaseName: connections.usageDatabase
    agentPrincipalId: identities.outputs.analyticsPrincipalId
  }
}

module usage './modules/usage-access.bicep' = {
  name: '${namePrefix}-usage-access'
  scope: resourceGroup(connections.usageAccount.resourceGroupName)
  params: {
    accountName: connections.usageAccount.name
    databaseName: connections.usageDatabase
    agentPrincipalId: identities.outputs.agentPrincipalId
  }
}

module vault './modules/vault-access.bicep' = {
  name: '${namePrefix}-vault-access'
  scope: resourceGroup(connections.keyVault.resourceGroupName)
  params: {
    vaultName: connections.keyVault.name
    clientSecretName: connections.clientSecretName
    githubSecretName: connections.githubSecretName
    bffPrincipalId: identities.outputs.bffPrincipalId
    agentPrincipalId: identities.outputs.agentPrincipalId
  }
}

module model './modules/model-access.bicep' = {
  name: '${namePrefix}-model-access'
  scope: resourceGroup(connections.modelAccount.resourceGroupName)
  params: {
    accountName: connections.modelAccount.name
    agentPrincipalId: identities.outputs.agentPrincipalId
  }
}

module gateway './modules/gateway-access.bicep' = {
  name: '${namePrefix}-gateway-access'
  scope: resourceGroup(connections.apim.resourceGroupName)
  params: {
    apimName: connections.apim.name
    agentPrincipalId: identities.outputs.agentPrincipalId
  }
}

module logs './modules/log-access.bicep' = {
  name: '${namePrefix}-log-access'
  scope: resourceGroup(connections.logs.resourceGroupName)
  params: {
    workspaceName: connections.logs.name
    agentPrincipalId: identities.outputs.agentPrincipalId
  }
}

@description('Non-secret host values used to configure the existing dedicated BFF.')
output hosts object = hosting.outputs.hosts
