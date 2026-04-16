/**
 * @plaidify/client — Official JavaScript/TypeScript SDK for Plaidify.
 */

export { Plaidify } from "./client";
export {
  PlaidifyError,
  AuthenticationError,
  NotFoundError,
  RateLimitError,
  ServerError,
} from "./errors";
export type {
  PlaidifyConfig,
  HealthStatus,
  BlueprintInfo,
  BlueprintListResult,
  ConnectResult,
  MFAChallenge,
  AuthToken,
  UserProfile,
  LinkSession,
  LinkEvent,
  WebhookRegistration,
  AgentInfo,
  AgentListResult,
  ConsentRequest,
  ConsentGrant,
  ApiKeyInfo,
  AuditEntry,
  AuditLogResult,
  AuditVerifyResult,
  WebhookDelivery,
  WebhookDeliveryResult,
  PublicTokenExchangeResult,
  RefreshScheduleResult,
  PlaidifyLinkConfig,
  LinkTheme,
  PlaidifyErrorResponse,
} from "./types";
