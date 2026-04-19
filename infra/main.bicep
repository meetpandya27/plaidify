targetScope = 'resourceGroup'

@description('Short prefix used for Azure resource names. Use lowercase letters and numbers only.')
param namePrefix string = 'plaidify'

@description('Environment label appended to resource names.')
param environmentName string = 'production'

@description('Primary deployment location.')
param location string = resourceGroup().location

@allowed([
  'development'
  'staging'
  'production'
])
param appEnv string = 'production'

param appName string = 'Plaidify'
param appVersion string = '0.3.0b1'
param corsOrigins string = 'https://example.com'

@allowed([
  'DEBUG'
  'INFO'
  'WARNING'
  'ERROR'
  'CRITICAL'
])
param logLevel string = 'INFO'

@allowed([
  'json'
  'text'
])
param logFormat string = 'json'

param enforceHttps bool = true

@allowed([
  'openai'
  'anthropic'
])
param llmProvider string = 'openai'

@description('Optional model override. Leave empty to use the provider default.')
param llmModel string = ''

param deployApplication bool = true
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param containerCpu string = '0.5'
param containerMemory string = '1Gi'
param minReplicas int = 1
param maxReplicas int = 3
param accessExecutorContainerCpu string = '0.5'
param accessExecutorContainerMemory string = '1Gi'
param accessExecutorMinReplicas int = 1
param accessExecutorMaxReplicas int = 1
param accessJobWorkerConcurrency int = 2
param migrationJobCpu string = '0.5'
param migrationJobMemory string = '1Gi'
param migrationReplicaTimeout int = 1800

@secure()
param postgresAdminLogin string

@secure()
param postgresAdminPassword string

param postgresSkuName string = 'B_Standard_B1ms'

@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresSkuTier string = 'Burstable'

param postgresVersion string = '16'
param postgresStorageSizeGB int = 32
param postgresDatabaseName string = 'plaidify'
param logAnalyticsRetentionInDays int = 30

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param redisSkuName string = 'Basic'

param redisSkuFamily string = 'C'
param redisSkuCapacity int = 0
param enableLlmApiKeySecret bool = false
param enableHealthCheckTokenSecret bool = false

var normalizedSeed = toLower(replace(replace('${namePrefix}${environmentName}', '-', ''), '_', ''))
var shortSeed = take(normalizedSeed, 12)
var uniqueSuffix = uniqueString(resourceGroup().id, shortSeed)
var resourceStem = take('${shortSeed}${uniqueSuffix}', 18)

var logAnalyticsName = '${resourceStem}log'
var containerEnvironmentName = '${resourceStem}env'
var containerAppName = '${resourceStem}app'
var accessExecutorAppName = '${resourceStem}exec'
var migrationJobName = '${resourceStem}migrate'
var identityName = '${resourceStem}id'
var keyVaultName = take('${resourceStem}kv', 24)
var registryName = take('${resourceStem}acr', 50)
var postgresServerName = take('${resourceStem}pg', 63)
var redisName = take('${resourceStem}redis', 63)

var tags = {
  app: 'Plaidify'
  environment: environmentName
  managedBy: 'bicep'
  repoVisibility: 'public'
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logAnalyticsRetentionInDays
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    softDeleteRetentionInDays: 90
    sku: {
      family: 'A'
      name: 'standard'
    }
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-06-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: location
  tags: tags
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: {
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    availabilityZone: '1'
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    createMode: 'Create'
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: postgresStorageSizeGB
    }
    version: postgresVersion
  }
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: postgresDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource postgresFirewallRule 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource redis 'Microsoft.Cache/redis@2024-11-01' = {
  name: redisName
  location: location
  tags: tags
  properties: {
    sku: {
      name: redisSkuName
      family: redisSkuFamily
      capacity: redisSkuCapacity
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, appIdentity.name, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: acr
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource keyVaultSecretsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.name, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: keyVault
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource containerEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: listKeys(logAnalytics.id, '2020-08-01').primarySharedKey
      }
    }
  }
}

var baseSecrets = [
  {
    name: 'database-url'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/database-url'
    identity: appIdentity.id
  }
  {
    name: 'redis-url'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/redis-url'
    identity: appIdentity.id
  }
  {
    name: 'encryption-key'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/encryption-key'
    identity: appIdentity.id
  }
  {
    name: 'jwt-secret-key'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/jwt-secret-key'
    identity: appIdentity.id
  }
]

var optionalSecrets = concat(
  enableLlmApiKeySecret ? [
    {
      name: 'llm-api-key'
      keyVaultUrl: '${keyVault.properties.vaultUri}secrets/llm-api-key'
      identity: appIdentity.id
    }
  ] : [],
  enableHealthCheckTokenSecret ? [
    {
      name: 'health-check-token'
      keyVaultUrl: '${keyVault.properties.vaultUri}secrets/health-check-token'
      identity: appIdentity.id
    }
  ] : []
)

var baseEnvironmentVariables = [
  {
    name: 'APP_NAME'
    value: appName
  }
  {
    name: 'APP_VERSION'
    value: appVersion
  }
  {
    name: 'ENV'
    value: appEnv
  }
  {
    name: 'LOG_LEVEL'
    value: logLevel
  }
  {
    name: 'LOG_FORMAT'
    value: logFormat
  }
  {
    name: 'CORS_ORIGINS'
    value: corsOrigins
  }
  {
    name: 'ENFORCE_HTTPS'
    value: string(enforceHttps)
  }
  {
    name: 'LLM_PROVIDER'
    value: llmProvider
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'REDIS_URL'
    secretRef: 'redis-url'
  }
  {
    name: 'ENCRYPTION_KEY'
    secretRef: 'encryption-key'
  }
  {
    name: 'JWT_SECRET_KEY'
    secretRef: 'jwt-secret-key'
  }
]

var optionalEnvironmentVariables = concat(
  !empty(llmModel) ? [
    {
      name: 'LLM_MODEL'
      value: llmModel
    }
  ] : [],
  enableLlmApiKeySecret ? [
    {
      name: 'LLM_API_KEY'
      secretRef: 'llm-api-key'
    }
  ] : [],
  enableHealthCheckTokenSecret ? [
    {
      name: 'HEALTH_CHECK_TOKEN'
      secretRef: 'health-check-token'
    }
  ] : []
)

var webEnvironmentVariables = concat(
  baseEnvironmentVariables,
  optionalEnvironmentVariables,
  [
    {
      name: 'ACCESS_JOB_EXECUTION_MODE'
      value: 'redis-worker'
    }
  ]
)

var executorEnvironmentVariables = concat(
  baseEnvironmentVariables,
  optionalEnvironmentVariables,
  [
    {
      name: 'ACCESS_JOB_EXECUTION_MODE'
      value: 'redis-worker'
    }
    {
      name: 'ACCESS_JOB_WORKER_CONCURRENCY'
      value: string(accessJobWorkerConcurrency)
    }
  ]
)

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = if (deployApplication) {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: concat(baseSecrets, optionalSecrets)
    }
    template: {
      containers: [
        {
          name: 'plaidify'
          image: containerImage
          env: webEnvironmentVariables
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 30
              periodSeconds: 15
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    keyVaultSecretsRoleAssignment
    postgresDatabase
    postgresFirewallRule
    redis
  ]
}

resource accessExecutorApp 'Microsoft.App/containerApps@2023-05-01' = if (deployApplication) {
  name: accessExecutorAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: concat(baseSecrets, optionalSecrets)
    }
    template: {
      containers: [
        {
          name: 'access-executor'
          image: containerImage
          command: [
            'python'
          ]
          args: [
            '-m'
            'src.access_job_worker'
          ]
          env: executorEnvironmentVariables
          resources: {
            cpu: json(accessExecutorContainerCpu)
            memory: accessExecutorContainerMemory
          }
        }
      ]
      terminationGracePeriodSeconds: 45
      scale: {
        minReplicas: accessExecutorMinReplicas
        maxReplicas: accessExecutorMaxReplicas
      }
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    keyVaultSecretsRoleAssignment
    postgresDatabase
    postgresFirewallRule
    redis
  ]
}

resource migrationJob 'Microsoft.App/jobs@2023-05-01' = if (deployApplication) {
  name: migrationJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerEnvironment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: migrationReplicaTimeout
      replicaRetryLimit: 1
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: concat(baseSecrets, optionalSecrets)
    }
    template: {
      containers: [
        {
          name: 'db-migrate'
          image: containerImage
          command: [
            'alembic'
          ]
          args: [
            'upgrade'
            'head'
          ]
          env: concat(baseEnvironmentVariables, optionalEnvironmentVariables)
          resources: {
            cpu: json(migrationJobCpu)
            memory: migrationJobMemory
          }
        }
      ]
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    keyVaultSecretsRoleAssignment
    postgresDatabase
    postgresFirewallRule
    redis
  ]
}

output containerAppName string = containerAppName
output accessExecutorAppName string = accessExecutorAppName
output migrationJobName string = migrationJobName
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output postgresServerName string = postgres.name
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output postgresDatabaseName string = postgresDatabase.name
output redisName string = redis.name
output redisHostName string = redis.properties.hostName
output redisSslPort int = redis.properties.sslPort