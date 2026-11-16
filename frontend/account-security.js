// Authenticator state comes from Supabase Auth, never editable profile metadata.
export async function mfaState(auth) {
  const { data, error } = await auth.mfa.getAuthenticatorAssuranceLevel();
  if (error) throw new Error('Could not check sign-in security. Please try again.');
  return { required: data.nextLevel === 'aal2' && data.currentLevel !== 'aal2',
    currentLevel: data.currentLevel };
}

export function safeReturnPage(value) {
  const pages = new Set(['dashboard.html', 'transactions.html', 'patterns.html',
    'spend-check.html', 'profile.html']);
  return pages.has(value) ? value : 'dashboard.html';
}

export async function verifiedFactors(auth) {
  const { data, error } = await auth.mfa.listFactors();
  if (error) throw new Error('Could not load your authenticator settings. Please try again.');
  return (data.totp || []).filter(factor => factor.status === 'verified');
}

export async function verifyAuthenticator(auth, factorId, code) {
  if (String(code).length !== 6 || !/^\d{6}$/.test(String(code))) throw new Error('Enter the six-digit code from your authenticator app.');
  const challenge = await auth.mfa.challenge({ factorId });
  if (challenge.error) throw new Error('Could not start verification. Please try again.');
  const result = await auth.mfa.verify({ factorId, challengeId: challenge.data.id, code });
  if (result.error) throw new Error('That code could not be verified. Check the current code and try again.');
  return result.data;
}

export async function reauthenticatePassword(auth, session, password) {
  if (!session?.user?.email || !password) throw new Error('Enter your current password.');
  const { data, error } = await auth.signInWithPassword({ email: session.user.email, password });
  if (error) throw new Error('Could not verify your password. Please try again.');
  if (data.user?.id !== session.user.id) {
    await auth.signOut();
    throw new Error('Your account changed. Please sign in again.');
  }
}
