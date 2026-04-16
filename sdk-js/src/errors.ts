/**
 * Error classes for the Plaidify SDK.
 */

export class PlaidifyError extends Error {
  public readonly statusCode?: number;
  public readonly detail: string;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "PlaidifyError";
    this.statusCode = statusCode;
    this.detail = message;
  }
}

export class AuthenticationError extends PlaidifyError {
  constructor(message = "Authentication failed") {
    super(message, 401);
    this.name = "AuthenticationError";
  }
}

export class NotFoundError extends PlaidifyError {
  constructor(message = "Resource not found") {
    super(message, 404);
    this.name = "NotFoundError";
  }
}

export class RateLimitError extends PlaidifyError {
  constructor(message = "Rate limit exceeded") {
    super(message, 429);
    this.name = "RateLimitError";
  }
}

export class ServerError extends PlaidifyError {
  constructor(message = "Internal server error") {
    super(message, 500);
    this.name = "ServerError";
  }
}
