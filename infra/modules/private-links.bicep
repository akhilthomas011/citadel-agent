@description('Private endpoint region.')
param location string

@description('Validated resource prefix.')
param namePrefix string

@description('Virtual network resource ID.')
param vnetId string

@description('Dedicated private-endpoint subnet resource ID.')
param subnetId string

@description('Target resources with group and DNS zone names.')
param targets array

@description('Hosting resource tags.')
param tags object

var zoneNames = union(flatten(map(targets, target => target.zones)), [])

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [
  for zoneName in zoneNames: {
    name: zoneName
    location: 'global'
    tags: tags
  }
]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [
  for (zoneName, i) in zoneNames: {
    parent: zones[i]
    name: '${namePrefix}-portal'
    location: 'global'
    properties: {
      registrationEnabled: false
      virtualNetwork: { id: vnetId }
    }
  }
]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-07-01' = [
  for target in targets: {
    name: 'pe-${namePrefix}-${target.name}'
    location: location
    tags: tags
    properties: {
      subnet: { id: subnetId }
      privateLinkServiceConnections: [
        {
          name: target.name
          properties: {
            privateLinkServiceId: target.resourceId
            groupIds: [target.groupId]
          }
        }
      ]
    }
  }
]

resource zoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = [
  for (target, i) in targets: {
    parent: endpoints[i]
    name: 'default'
    properties: {
      privateDnsZoneConfigs: [
        for zoneName in target.zones: {
          name: replace(zoneName, '.', '-')
          properties: {
            privateDnsZoneId: resourceId('Microsoft.Network/privateDnsZones', zoneName)
          }
        }
      ]
    }
    dependsOn: [zones]
  }
]
