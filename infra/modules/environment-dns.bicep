@description('Generated default domain of the internal Container Apps environment.')
param domain string

@description('Private load balancer IPv4 address.')
param staticIp string

@description('Portal virtual network resource ID.')
param vnetId string

@description('Validated name prefix.')
param namePrefix string

@description('Hosting resource tags.')
param tags object

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: domain
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: '${namePrefix}-portal'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: vnetId } }
}

resource records 'Microsoft.Network/privateDnsZones/A@2024-06-01' = [
  for recordName in ['*', '@']: {
    parent: zone
    name: recordName
    properties: {
      ttl: 300
      aRecords: [{ ipv4Address: staticIp }]
    }
  }
]
