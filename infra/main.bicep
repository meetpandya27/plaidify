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

@description('Deploy the API and access-executor Container Apps (the workflow sets this after migrations succeed).')
param deployApplication bool = true

@description('Deploy the migration Container Apps Job with containerImage (the workflow runs it before deploying the apps).')
param deployMigrationJob bool = true

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

@description('Startup and liveness probes on the executor\'s own /health (ACCESS_WORKER_METRICS_PORT), so a hung worker is restarted. Needs an image whose worker serves that endpoint (src/access_job_worker.py starting src.metrics.start_worker_metrics_server).')
param accessExecutorProbes bool = true
param migrationJobCpu string = '0.5'
param migrationJobMemory string = '1Gi'
param migrationReplicaTimeout int = 1800

// ── Database connection budget ───────────────────────────────────────────────
// Peak connections = maxReplicas × gunicornWorkers × (dbPoolSize + dbMaxOverflow)
//                  + accessExecutorMaxReplicas × (dbPoolSize + dbMaxOverflow)
//                  + 2 (migration job).
// The defaults give 3×2×4 + 1×4 + 2 = 30, inside what a Burstable B1ms server
// (max_connections 50, part of it reserved for Azure) leaves for applications.
// Raise them together with the PostgreSQL SKU.

@description('Gunicorn workers per API replica (GUNICORN_WORKERS).')
param gunicornWorkers int = 2

@description('SQLAlchemy pool size per process (DB_POOL_SIZE).')
param dbPoolSize int = 2

@description('SQLAlchemy overflow connections per process (DB_MAX_OVERFLOW).')
param dbMaxOverflow int = 2

@secure()
param postgresAdminLogin string

@secure()
param postgresAdminPassword string

@description('Login role the API and the executor use: DML only on the application tables, created and granted by the migration job. The server admin is used by migrations alone.')
param appDatabaseRole string = 'plaidify_app'

param postgresSkuName string = 'B_Standard_B1ms'

@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresSkuTier string = 'Burstable'

param postgresVersion string = '16'
param postgresStorageSizeGB int = 32

@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
@description('PostgreSQL high-availability mode. ZoneRedundant/SameZone require the GeneralPurpose or MemoryOptimized tier (not Burstable).')
param postgresHighAvailabilityMode string = 'Disabled'

@description('Standby availability zone used when postgresHighAvailabilityMode is ZoneRedundant (must differ from the primary zone 1).')
param postgresStandbyAvailabilityZone string = '2'

@description('Enable geo-redundant PostgreSQL backups for cross-region disaster recovery.')
param postgresGeoRedundantBackup bool = false

param postgresDatabaseName string = 'plaidify'
param logAnalyticsRetentionInDays int = 30

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
@description('Standard or Premium: queued access jobs, link sessions and MFA state live in Redis, and Basic is a single node without replication or an SLA.')
param redisSkuName string = 'Standard'

param redisSkuFamily string = 'C'
param redisSkuCapacity int = 1
param enableLlmApiKeySecret bool = false
param enableHealthCheckTokenSecret bool = false

@description('Pass the audit-hmac-key-previous Key Vault secret as AUDIT_HMAC_KEY_PREVIOUS while the audit key is being rotated (docs/RUNBOOK.md). The workflow sets it while the GitHub variable AZURE_AUDIT_HMAC_KEY_ROTATING is true.')
param enableAuditHmacKeyPreviousSecret bool = false

// ── Password-reset mail and the first administrator (API only) ───────────────

@description('SMTP server for password-reset mail. Empty: no reset mail is sent.')
param smtpHost string = ''

@description('SMTP port (587 for STARTTLS).')
param smtpPort int = 587

@description('SMTP login user. Empty: no login.')
param smtpUsername string = ''

@description('From address of password-reset mail. Required with smtpHost.')
param smtpFrom string = ''

@description('The app page reset emails link to, e.g. https://app.example.com/reset?token={token}. Empty: the email carries the one-time code.')
#disable-next-line secure-secrets-in-params // a page URL, not a password
param passwordResetUrl string = ''

@description('Pass the smtp-password Key Vault secret as SMTP_PASSWORD.')
param enableSmtpPasswordSecret bool = false

@description('Administrator the API creates at startup when no account holds this username or email (with bootstrapUserEmail and the bootstrap-user-password secret). Production has self-registration off, so this is how the first account is made.')
param bootstrapUserUsername string = ''

@description('Email of the bootstrap administrator.')
param bootstrapUserEmail string = ''

@description('Pass the bootstrap-user-password Key Vault secret as BOOTSTRAP_USER_PASSWORD. Needs bootstrapUserUsername and bootstrapUserEmail.')
param enableBootstrapUserPasswordSecret bool = false

// ── Networking ───────────────────────────────────────────────────────────────
// PostgreSQL (VNet integration) and Redis (private endpoint) have no public
// network access; only the Container Apps environment's subnet reaches them.

@description('Address space of the virtual network.')
param vnetAddressPrefix string = '10.40.0.0/16'

@description('Subnet of the Container Apps environment (workload profiles; /27 minimum, /23 leaves room to scale).')
param containerAppsSubnetPrefix string = '10.40.0.0/23'

@description('Subnet delegated to PostgreSQL Flexible Server.')
param postgresSubnetPrefix string = '10.40.2.0/28'

@description('Subnet for private endpoints (Redis).')
param privateEndpointSubnetPrefix string = '10.40.3.0/27'

@description('Spread the Container Apps environment across availability zones (run minReplicas >= 2 to benefit).')
param containerAppsZoneRedundant bool = false

@description('Peers the API trusts to set X-Forwarded-For / X-Forwarded-Proto (gunicorn FORWARDED_ALLOW_IPS). The API is reachable only through the environment ingress, whose proxies use private addresses; without this the HTTPS redirect loops and every client shares the ingress address for rate limiting.')
param forwardedAllowIps string = '10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10'

@description('Run Chromium with its sandbox (BROWSER_CHROMIUM_SANDBOX). If the executor logs "No usable sandbox" on this platform, set false; see docs/AZURE_DEPLOYMENT.md for the trade-off.')
param browserChromiumSandbox bool = true

// ── Key Vault ────────────────────────────────────────────────────────────────

@description('Object ID of the service principal the deployment workflow signs in as. It gets Key Vault Secrets Officer on this vault to write the runtime secrets. Empty skips the assignment.')
param deployPrincipalObjectId string = ''

@description('Suffix of the Key Vault name. A deleted vault keeps its name for the soft-delete retention period: recover it (az keyvault recover) or change this to create a new one.')
@maxLength(2)
param keyVaultGeneration string = ''

@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteRetentionInDays int = 90

var normalizedSeed = toLower(replace(replace('${namePrefix}${environmentName}', '-', ''), '_', ''))
var shortSeed = take(normalizedSeed, 12)
var uniqueSuffix = uniqueString(resourceGroup().id, shortSeed)
var resourceStem = take('${shortSeed}${uniqueSuffix}', 18)

var logAnalyticsName = '${resourceStem}log'
var vnetName = '${resourceStem}vnet'
var containerEnvironmentName = '${resourceStem}env'
var containerAppName = '${resourceStem}app'
var accessExecutorAppName = '${resourceStem}exec'
var migrationJobName = '${resourceStem}migrate'
var identityName = '${resourceStem}id'
var keyVaultName = take('${resourceStem}kv${keyVaultGeneration}', 24)
var registryName = take('${resourceStem}acr', 50)
var postgresServerName = take('${resourceStem}pg', 63)
var redisName = take('${resourceStem}redis', 63)

var containerAppsSubnetName = 'container-apps'
var postgresSubnetName = 'postgres'
var privateEndpointSubnetName = 'private-endpoints'
var workerMetricsPort = 9101

// Built-in role definition IDs.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

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

// ── Network ──────────────────────────────────────────────────────────────────

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: containerAppsSubnetName
        properties: {
          addressPrefix: containerAppsSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: postgresSubnetName
        properties: {
          addressPrefix: postgresSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.DBforPostgreSQL.flexibleServers'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource postgresDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${postgresServerName}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource postgresDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresDnsZone
  name: '${vnetName}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource redisDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.redis.cache.windows.net'
  location: 'global'
  tags: tags
}

resource redisDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: redisDnsZone
  name: '${vnetName}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// ── Secrets and images ───────────────────────────────────────────────────────

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
    // Reachable from the deployment workflow, which writes the runtime
    // secrets; every read and write is authorized by Entra ID RBAC.
    publicNetworkAccess: 'Enabled'
    softDeleteRetentionInDays: keyVaultSoftDeleteRetentionInDays
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

// ── Data tier ────────────────────────────────────────────────────────────────

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
      geoRedundantBackup: postgresGeoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    createMode: 'Create'
    highAvailability: postgresHighAvailabilityMode == 'ZoneRedundant'
      ? {
          mode: 'ZoneRedundant'
          standbyAvailabilityZone: postgresStandbyAvailabilityZone
        }
      : {
          mode: postgresHighAvailabilityMode
        }
    network: {
      // VNet integration: a private address in the delegated subnet and no
      // public endpoint (and so no firewall rules).
      delegatedSubnetResourceId: '${vnet.id}/subnets/${postgresSubnetName}'
      privateDnsZoneArmResourceId: postgresDnsZone.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: postgresStorageSizeGB
    }
    version: postgresVersion
  }
  dependsOn: [
    postgresDnsZoneLink
  ]
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: postgresDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
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
    publicNetworkAccess: 'Disabled'
  }
}

resource redisPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${redisName}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/${privateEndpointSubnetName}'
    }
    privateLinkServiceConnections: [
      {
        name: 'redis'
        properties: {
          privateLinkServiceId: redis.id
          groupIds: [
            'redisCache'
          ]
        }
      }
    ]
  }
}

resource redisPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: redisPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'redis'
        properties: {
          privateDnsZoneId: redisDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    redisDnsZoneLink
  ]
}

// ── Role assignments ─────────────────────────────────────────────────────────

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, appIdentity.name, acrPullRoleId)
  scope: acr
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource keyVaultSecretsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.name, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

// The deployment workflow writes the runtime secrets into this vault; RBAC
// vaults grant that only through a data-plane role, scoped here to this vault.
resource deployPrincipalKeyVaultRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployPrincipalObjectId)) {
  name: guid(keyVault.id, deployPrincipalObjectId, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    principalId: deployPrincipalObjectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
  }
}

// ── Container Apps ───────────────────────────────────────────────────────────

resource containerEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: '${vnet.id}/subnets/${containerAppsSubnetName}'
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: containerAppsZoneRedundant
  }
}

var runtimeSecrets = [
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
  // Generated once by the workflow, then kept. Both processes append to the
  // audit chain, so both must sign with the same key.
  {
    name: 'audit-hmac-key'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/audit-hmac-key'
    identity: appIdentity.id
  }
]

// Only the API signs launch tokens, serves /metrics through the ingress,
// sends reset mail and creates the bootstrap administrator.
var webSecrets = concat([
  {
    name: 'link-launch-secret'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/link-launch-secret'
    identity: appIdentity.id
  }
  {
    name: 'metrics-token'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/metrics-token'
    identity: appIdentity.id
  }
], enableSmtpPasswordSecret ? [
  {
    name: 'smtp-password'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/smtp-password'
    identity: appIdentity.id
  }
] : [], enableBootstrapUserPasswordSecret ? [
  {
    name: 'bootstrap-user-password'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/bootstrap-user-password'
    identity: appIdentity.id
  }
] : [])

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
  ] : [],
  enableAuditHmacKeyPreviousSecret ? [
    {
      name: 'audit-hmac-key-previous'
      keyVaultUrl: '${keyVault.properties.vaultUri}secrets/audit-hmac-key-previous'
      identity: appIdentity.id
    }
  ] : []
)

// The migration job alone holds the server admin credentials (schema
// changes) and the password it sets on the application role.
var migrationSecrets = [
  {
    name: 'database-admin-url'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/database-admin-url'
    identity: appIdentity.id
  }
  {
    name: 'database-app-password'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/database-app-password'
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

var commonEnvironmentVariables = [
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
    name: 'ENCRYPTION_KEY'
    secretRef: 'encryption-key'
  }
  {
    name: 'JWT_SECRET_KEY'
    secretRef: 'jwt-secret-key'
  }
]

var runtimeEnvironmentVariables = concat(commonEnvironmentVariables, [
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
    name: 'DB_POOL_SIZE'
    value: string(dbPoolSize)
  }
  {
    name: 'DB_MAX_OVERFLOW'
    value: string(dbMaxOverflow)
  }
  {
    name: 'ACCESS_JOB_EXECUTION_MODE'
    value: 'redis-worker'
  }
  {
    name: 'BROWSER_CHROMIUM_SANDBOX'
    value: string(browserChromiumSandbox)
  }
  {
    name: 'AUDIT_HMAC_KEY'
    secretRef: 'audit-hmac-key'
  }
])

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
  ] : [],
  enableAuditHmacKeyPreviousSecret ? [
    {
      name: 'AUDIT_HMAC_KEY_PREVIOUS'
      secretRef: 'audit-hmac-key-previous'
    }
  ] : []
)

var webOnlyEnvironmentVariables = concat([
  {
    name: 'LINK_LAUNCH_SECRET'
    secretRef: 'link-launch-secret'
  }
  {
    name: 'METRICS_TOKEN'
    secretRef: 'metrics-token'
  }
], !empty(smtpHost) ? [
  {
    name: 'SMTP_HOST'
    value: smtpHost
  }
  {
    name: 'SMTP_PORT'
    value: string(smtpPort)
  }
  {
    name: 'SMTP_FROM'
    value: smtpFrom
  }
] : [], !empty(smtpUsername) ? [
  {
    name: 'SMTP_USERNAME'
    value: smtpUsername
  }
] : [], enableSmtpPasswordSecret ? [
  {
    name: 'SMTP_PASSWORD'
    secretRef: 'smtp-password'
  }
] : [], !empty(passwordResetUrl) ? [
  {
    name: 'PASSWORD_RESET_URL'
    value: passwordResetUrl
  }
] : [], enableBootstrapUserPasswordSecret ? [
  {
    name: 'BOOTSTRAP_USER_USERNAME'
    value: bootstrapUserUsername
  }
  {
    name: 'BOOTSTRAP_USER_EMAIL'
    value: bootstrapUserEmail
  }
  {
    name: 'BOOTSTRAP_USER_PASSWORD'
    secretRef: 'bootstrap-user-password'
  }
] : [])

var webEnvironmentVariables = concat(runtimeEnvironmentVariables, optionalEnvironmentVariables, webOnlyEnvironmentVariables, [
  {
    name: 'GUNICORN_WORKERS'
    value: string(gunicornWorkers)
  }
  {
    name: 'FORWARDED_ALLOW_IPS'
    value: forwardedAllowIps
  }
])

var executorEnvironmentVariables = concat(runtimeEnvironmentVariables, optionalEnvironmentVariables, [
  {
    name: 'ACCESS_JOB_WORKER_CONCURRENCY'
    value: string(accessJobWorkerConcurrency)
  }
  {
    name: 'ACCESS_WORKER_METRICS_PORT'
    value: string(workerMetricsPort)
  }
])

var migrationEnvironmentVariables = concat(commonEnvironmentVariables, [
  {
    name: 'DATABASE_URL'
    secretRef: 'database-admin-url'
  }
  {
    name: 'APP_DB_ROLE'
    value: appDatabaseRole
  }
  {
    name: 'APP_DB_PASSWORD'
    secretRef: 'database-app-password'
  }
])

// `args` only, never `command`: the image's entrypoint (tini, then the
// secret-to-URL entrypoint script) must stay in front of every process.

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApplication) {
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
    workloadProfileName: 'Consumption'
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
      secrets: concat(runtimeSecrets, optionalSecrets, webSecrets)
    }
    template: {
      containers: [
        {
          name: 'plaidify'
          image: containerImage
          env: webEnvironmentVariables
          // /health answers 200 over plain HTTP (the probes don't come
          // through the ingress); a redirect counts as a failure.
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8000
              }
              periodSeconds: 5
              failureThreshold: 24
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              periodSeconds: 15
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
        }
      ]
      terminationGracePeriodSeconds: 45
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
    redisPrivateDnsZoneGroup
  ]
}

resource accessExecutorApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApplication) {
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
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: concat(runtimeSecrets, optionalSecrets)
    }
    template: {
      containers: [
        {
          name: 'access-executor'
          image: containerImage
          args: [
            'python'
            '-m'
            'src.access_job_worker'
          ]
          env: executorEnvironmentVariables
          // The executor serves no API. Its own endpoint on
          // ACCESS_WORKER_METRICS_PORT answers 503 once the worker's event loop
          // stops running, so a hung worker is restarted.
          probes: accessExecutorProbes
            ? [
                {
                  type: 'Startup'
                  httpGet: {
                    path: '/health'
                    port: workerMetricsPort
                  }
                  periodSeconds: 5
                  failureThreshold: 24
                }
                {
                  type: 'Liveness'
                  httpGet: {
                    path: '/health'
                    port: workerMetricsPort
                  }
                  periodSeconds: 30
                  failureThreshold: 3
                }
              ]
            : []
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
    redisPrivateDnsZoneGroup
  ]
}

resource migrationJob 'Microsoft.App/jobs@2024-03-01' = if (deployMigrationJob) {
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
    workloadProfileName: 'Consumption'
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
      secrets: migrationSecrets
    }
    template: {
      containers: [
        {
          name: 'db-migrate'
          image: containerImage
          // Schema first (as the server admin), then (re)grant the
          // application role DML on every table, current and future.
          args: [
            'sh'
            '-c'
            'alembic upgrade head && python scripts/provision_app_db_role.py'
          ]
          env: migrationEnvironmentVariables
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
output appDatabaseRole string = appDatabaseRole
output redisName string = redis.name
output redisHostName string = redis.properties.hostName
output redisSslPort int = redis.properties.sslPort
output vnetName string = vnet.name
