/**
 * Minimal Amazon Cognito user-pool client: the handful of public (unauthenticated)
 * API calls a sign-up / sign-in / reset-password UI needs, over plain fetch.
 * Passwords go to Cognito over TLS (USER_PASSWORD_AUTH) and never touch our backend;
 * the backend only ever sees — and verifies — the resulting ID token.
 */

const CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID ?? "";
const POOL_ID = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID ?? "";
const REGION = POOL_ID.split("_")[0] || "us-east-1";

/** Real accounts when a user pool is configured; otherwise the backend's dev login. */
export const cognitoEnabled = CLIENT_ID !== "";

export class CognitoError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}

const FRIENDLY: Record<string, string> = {
  NotAuthorizedException: "Incorrect email or password.",
  UserNotFoundException: "Incorrect email or password.",
  UserNotConfirmedException: "Please confirm your email address first.",
  UsernameExistsException: "An account with this email already exists. Try logging in.",
  CodeMismatchException: "That code isn't right. Check the email and try again.",
  ExpiredCodeException: "That code has expired. Request a new one.",
  LimitExceededException: "Too many attempts. Please wait a few minutes and try again.",
  TooManyRequestsException: "Too many attempts. Please wait a few minutes and try again.",
  TooManyFailedAttemptsException: "Too many attempts. Please wait a few minutes and try again.",
};

async function call<T>(target: string, body: object): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`https://cognito-idp.${REGION}.amazonaws.com/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
      },
      body: JSON.stringify({ ClientId: CLIENT_ID, ...body }),
    });
  } catch {
    throw new CognitoError("NetworkError", "Couldn't reach the sign-in service. Check your connection.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const code = String(data.__type ?? "Error").split("#").pop() ?? "Error";
    // Password-policy and validation messages from Cognito are already readable.
    throw new CognitoError(code, FRIENDLY[code] ?? String(data.message ?? data.Message ?? "Something went wrong."));
  }
  return data as T;
}

export interface Tokens {
  idToken: string;
  refreshToken?: string;
  expiresAt: number;
}

interface AuthResult {
  AuthenticationResult?: { IdToken: string; RefreshToken?: string; ExpiresIn: number };
  ChallengeName?: string;
}

function tokens(r: AuthResult): Tokens {
  const a = r.AuthenticationResult;
  if (!a) throw new CognitoError(r.ChallengeName ?? "Challenge", "This account needs an extra sign-in step that isn't supported here.");
  return { idToken: a.IdToken, refreshToken: a.RefreshToken, expiresAt: Date.now() + a.ExpiresIn * 1000 };
}

export const signIn = async (email: string, password: string) =>
  tokens(await call<AuthResult>("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: email, PASSWORD: password },
  }));

/** A fresh ID token from the 30-day refresh token (the refresh token itself is kept). */
export const refreshTokens = async (refreshToken: string) => ({
  ...tokens(await call<AuthResult>("InitiateAuth", {
    AuthFlow: "REFRESH_TOKEN_AUTH", AuthParameters: { REFRESH_TOKEN: refreshToken },
  })),
  refreshToken,
});

export const signUp = (email: string, password: string) =>
  call<{ UserConfirmed: boolean }>("SignUp", {
    Username: email, Password: password, UserAttributes: [{ Name: "email", Value: email }],
  });

export const confirmSignUp = (email: string, code: string) =>
  call("ConfirmSignUp", { Username: email, ConfirmationCode: code.trim() });

export const resendCode = (email: string) => call("ResendConfirmationCode", { Username: email });

export const forgotPassword = (email: string) => call("ForgotPassword", { Username: email });

export const confirmForgotPassword = (email: string, code: string, password: string) =>
  call("ConfirmForgotPassword", { Username: email, ConfirmationCode: code.trim(), Password: password });

/** Best effort: makes the refresh token useless if it was copied somewhere. */
export const revoke = (refreshToken: string) => call("RevokeToken", { Token: refreshToken }).catch(() => undefined);
