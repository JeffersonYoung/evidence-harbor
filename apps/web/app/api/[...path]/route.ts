import { NextRequest, NextResponse } from 'next/server';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
const ALLOWED_ROOTS = new Set([
  'auth',
  'hybrid-search',
  'index-generations',
  'me',
  'pipelines',
  'pipeline-registry',
  'providers',
  'web-search',
  'source-watches',
  'captures',
  'representations',
  'projects',
  'documents',
  'operations',
  'questions',
  'research-runs',
  'proposals',
  'search',
  'sources',
  'health',
  'diagnostics',
  'exports',
  'ingestions',
  'evidence',
]);
async function boundedBody(request: NextRequest): Promise<ArrayBuffer | undefined> {
  if (!request.body || ['GET', 'HEAD'].includes(request.method)) return undefined;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 26 * 1024 * 1024) {
      await reader.cancel();
      throw new RangeError('Request body too large');
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body.buffer;
}
async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (
    !path.length ||
    !ALLOWED_ROOTS.has(path[0]) ||
    path.some((p) => p === '..' || p === '.' || p.includes('/') || p.includes('\\'))
  ) {
    return NextResponse.json({ detail: 'Unknown API route' }, { status: 404 });
  }
  // Only same-origin browser writes. API credentials are never sent to the client.
  const origin = request.headers.get('origin');
  if (
    !['GET', 'HEAD'].includes(request.method) &&
    origin &&
    origin !== request.nextUrl.origin &&
    origin !==
      `${request.headers.get('x-forwarded-proto') || request.nextUrl.protocol.slice(0, -1)}://${request.headers.get('host')}`
  ) {
    return NextResponse.json({ detail: 'Cross-origin write rejected' }, { status: 403 });
  }
  const base = process.env.API_BASE_URL || 'http://127.0.0.1:8000';
  const url = new URL(`${base.replace(/\/$/, '')}/v1/${path.map(encodeURIComponent).join('/')}`);
  url.search = request.nextUrl.search;
  const headers = new Headers();
  for (const name of ['content-type', 'accept', 'if-match', 'idempotency-key']) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const token =
    request.cookies.get('evidenceharbor_session')?.value ||
    (process.env.SINGLE_USER_MODE === 'true' ? process.env.API_TOKEN : undefined);
  const publicRoute = ['auth/login', 'health'].includes(path.join('/'));
  if (!token && !publicRoute)
    return NextResponse.json({ detail: 'Authentication required' }, { status: 401 });
  if (
    request.headers.get('sec-fetch-site') === 'cross-site' &&
    !['GET', 'HEAD'].includes(request.method)
  )
    return NextResponse.json({ detail: 'Cross-site write rejected' }, { status: 403 });
  if (Number(request.headers.get('content-length') || '0') > 26 * 1024 * 1024)
    return NextResponse.json({ detail: '上传内容超过代理大小限制' }, { status: 413 });
  if (token) headers.set('Authorization', `Bearer ${token}`);
  try {
    const response = await fetch(url, {
      method: request.method,
      headers,
      body: await boundedBody(request),
      cache: 'no-store',
      signal: AbortSignal.timeout(120_000),
      redirect: 'error',
    });
    if (path.join('/') === 'auth/login' && response.ok) {
      const data = await response.json();
      if (typeof data.access_token !== 'string')
        return NextResponse.json({ detail: '登录响应无效' }, { status: 502 });
      const safe = NextResponse.json(
        { role: data.role, expires_at: data.expires_at },
        { headers: { 'Cache-Control': 'no-store' } },
      );
      safe.cookies.set('evidenceharbor_session', data.access_token, {
        httpOnly: true,
        sameSite: 'strict',
        secure:
          request.nextUrl.protocol === 'https:' ||
          request.headers.get('x-forwarded-proto') === 'https',
        path: '/',
        maxAge: Math.floor(
          Math.max(
            60,
            Math.min(
              86400,
              data.expires_at ? (new Date(data.expires_at).getTime() - Date.now()) / 1000 : 28800,
            ),
          ),
        ),
      });
      return safe;
    }
    const outgoing = new Headers({
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    });
    for (const name of ['content-type', 'content-disposition', 'etag', 'retry-after']) {
      const value = response.headers.get(name);
      if (value) outgoing.set(name, value);
    }
    const result = new NextResponse(response.body, { status: response.status, headers: outgoing });
    if (path.join('/') === 'auth/logout' && (response.ok || response.status === 401))
      result.cookies.delete('evidenceharbor_session');
    return result;
  } catch (error) {
    if (error instanceof RangeError)
      return NextResponse.json({ detail: '上传内容超过代理大小限制' }, { status: 413 });
    return NextResponse.json(
      { detail: '暂时无法连接研究服务。请确认 API 服务已经启动。' },
      { status: 502 },
    );
  }
}
export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
