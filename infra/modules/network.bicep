import { NetworkConfig } from '../types.bicep'

@description('Hosting region.')
param location string

@description('Validated resource prefix.')
param namePrefix string

@description('Non-overlapping network prefixes.')
param network NetworkConfig

@description('Hosting resource tags.')
param tags object

resource outboundIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: 'pip-${namePrefix}-outbound'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static', publicIPAddressVersion: 'IPv4' }
}

resource outboundNat 'Microsoft.Network/natGateways@2024-07-01' = {
  name: 'nat-${namePrefix}-outbound'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [{ id: outboundIp.id }]
  }
}

resource agentNsg 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-${namePrefix}-agent'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource bffNsg 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-${namePrefix}-bff'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource endpointNsg 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-${namePrefix}-endpoints'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: 'vnet-${namePrefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [network.addressPrefix]
    }
    subnets: [
      {
        name: 'agent'
        properties: {
          addressPrefix: network.agentPrefix
          defaultOutboundAccess: false
          natGateway: { id: outboundNat.id }
          networkSecurityGroup: { id: agentNsg.id }
          delegations: [
            {
              name: 'container-apps'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'bff'
        properties: {
          addressPrefix: network.bffPrefix
          defaultOutboundAccess: false
          natGateway: { id: outboundNat.id }
          networkSecurityGroup: { id: bffNsg.id }
          delegations: [
            {
              name: 'app-service'
              properties: { serviceName: 'Microsoft.Web/serverFarms' }
            }
          ]
        }
      }
      {
        name: 'endpoints'
        properties: {
          addressPrefix: network.endpointPrefix
          defaultOutboundAccess: false
          networkSecurityGroup: { id: endpointNsg.id }
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
    ]
  }
}

@description('Portal virtual network ID.')
output vnetId string = vnet.id

@description('Dedicated agent environment subnet ID.')
output agentSubnetId string = '${vnet.id}/subnets/agent'

@description('Existing App Service integration subnet ID.')
output bffSubnetId string = '${vnet.id}/subnets/bff'

@description('Private endpoints subnet ID.')
output endpointSubnetId string = '${vnet.id}/subnets/endpoints'
