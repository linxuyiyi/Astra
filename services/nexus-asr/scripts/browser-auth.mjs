/**
 * Local QA login for Playwright acceptance scripts.
 *
 * The v1.4.8 collaborative-group work requires a real server-side session.
 * This helper registers the first local QA account if needed, otherwise logs
 * in. Credentials are test-only and can be overridden with NEXUS_QA_USERNAME /
 * NEXUS_QA_PASSWORD.
 */
export async function ensureBrowserLogin(context, { base = 'http://127.0.0.1:18120' } = {}) {
  if (!/^https?:\/\/(127\.0\.0\.1|localhost)([:/]|$)/.test(base)) {
    throw new Error('QA login is restricted to localhost; do not run it against a shared Nexus');
  }
  const username = process.env.NEXUS_QA_USERNAME || 'local-qa';
  const password = process.env.NEXUS_QA_PASSWORD || 'local-qa-password';
  const displayName = process.env.NEXUS_QA_DISPLAY_NAME || 'Local QA';
  const body = { username, password, display_name: displayName };
  let response = await context.request.post(`${base}/api/v1/auth/register`, { data: body });
  if (response.status() === 409) {
    response = await context.request.post(`${base}/api/v1/auth/login`, { data: body });
  }
  if (!response.ok()) {
    throw new Error(`QA login failed: ${response.status()} ${(await response.text()).slice(0, 200)}`);
  }
  const user = await response.json();
  const cookies = await context.cookies();
  const session = cookies.find((cookie) => cookie.name === 'nexus_session');
  if (!session) throw new Error('QA login did not set nexus_session');
  return { cookie: `${session.name}=${session.value}`, user };
}
