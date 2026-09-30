/**
 * Error classes for the Plaidify SDK.
 */

export class PlaidifyError extends Error {
  public readonly statusCode?: number;
  public readonly detail: string;
  /** Machine-readable code from the server's `{error, error_code}` body, when sent. */
  public readonly errorCode?: string;

  constructor(message: string, statusCode?: number, errorCode?: string) {
    super(message);
    this.name = "PlaidifyError";
    this.statusCode = statusCode;
    this.detail = message;
    this.errorCode = errorCode;
  }
}

export class AuthenticationError extends PlaidifyError {
  constructor(message = "Authentication failed", errorCode?: string) {
    super(message, 401, errorCode);
    this.name = "AuthenticationError";
  }
}

export class NotFoundError extends PlaidifyError {
  constructor(message = "Resource not found", errorCode?: string) {
    super(message, 404, errorCode);
    this.name = "NotFoundError";
  }
}

export class RateLimitError extends PlaidifyError {
  constructor(message = "Rate limit exceeded", errorCode?: string) {
    super(message, 429, errorCode);
    this.name = "RateLimitError";
  }
}

export class ServerError extends PlaidifyError {
  constructor(message = "Internal server error", errorCode?: string) {
    super(message, 500, errorCode);
    this.name = "ServerError";
  }
}
