metadata description = 'Shared types for the Citadel hosted-product infrastructure.'

@export()
@sealed()
@description('An existing resource in the deployment subscription.')
type ResourceRef = {
  @description('Existing resource group name.')
  resourceGroupName: string
  @description('Existing resource name.')
  name: string
}

@export()
@sealed()
@description('Non-secret connections to existing customer resources.')
type Connections = {
  @description('Dedicated existing Linux App Service.')
  webApp: ResourceRef
  @description('Pre-authorized BFF user-assigned managed identity.')
  bffIdentity: ResourceRef
  @description('Existing Key Vault using Azure RBAC.')
  keyVault: ResourceRef
  @description('Existing Cosmos usage account.')
  usageAccount: ResourceRef
  @description('Existing Azure OpenAI-compatible model account.')
  modelAccount: ResourceRef
  @description('Existing APIM gateway.')
  apim: ResourceRef
  @description('Existing gateway Log Analytics workspace.')
  logs: ResourceRef
  @description('Usage database name.')
  usageDatabase: string
  @description('Confidential BFF client credential secret name, never a secret value.')
  clientSecretName: string
  @description('Canonical repository credential secret name, never a secret value.')
  githubSecretName: string
}

@export()
@sealed()
@description('Reviewed non-overlapping IPv4 prefixes.')
type NetworkConfig = {
  @description('Virtual network address space.')
  addressPrefix: string
  @description('Dedicated Container Apps subnet, at least /23 for this zone-redundant topology.')
  agentPrefix: string
  @description('Dedicated App Service integration subnet, at least /26.')
  bffPrefix: string
  @description('Separate private-endpoint subnet, at least /27.')
  endpointPrefix: string
}
