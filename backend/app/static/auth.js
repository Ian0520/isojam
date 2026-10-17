export async function authenticate(
  request,
  { email, password, invitationCode },
  { register = false, onRegistered = () => {} } = {},
) {
  const credentials = { email, password };
  if (register) {
    await request("/register", {
      method: "POST",
      body: {
        ...credentials,
        ...(invitationCode ? { invitation_code: invitationCode } : {}),
      },
      auth: false,
    });
    // Registration has committed even if the following login request fails.
    onRegistered();
  }
  try {
    const result = await request("/login", {
      method: "POST",
      body: credentials,
      auth: false,
    });
    if (typeof result?.access_token !== "string" || !result.access_token)
      throw new Error("The sign-in response was incomplete. Please sign in again.");
    return result;
  } catch (error) {
    if (register)
      throw new Error(
        "Your account was created, but automatic sign-in did not finish. Sign in with your email and password.",
        { cause: error },
      );
    throw error;
  }
}
