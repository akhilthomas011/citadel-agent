import { ResourceRef } from '../types.bicep'

@description('Hosting region.')
param location string

@description('Validated name prefix.')
param namePrefix string

@description('Existing pre-authorized BFF identity.')
param bffIdentityRef ResourceRef

@description('Hosting resource tags.')
param tags object

resource bffIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: bffIdentityRef.name
  scope: resourceGroup(bffIdentityRef.resourceGroupName)
}

resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${namePrefix}-agent'
  location: location
  tags: tags
}

resource analyticsIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${namePrefix}-analytics'
  location: location
  tags: tags
}

@description('New analytics worker principal ID, without session, model, vault or gateway write privileges.')
output analyticsPrincipalId string = analyticsIdentity.properties.principalId

@description('New agent principal ID for scoped role assignment before the agent starts.')
output agentPrincipalId string = agentIdentity.properties.principalId

@description('Existing BFF principal ID for its per-secret access.')
output bffPrincipalId string = bffIdentity.properties.principalId
