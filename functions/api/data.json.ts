// Cloudflare Pages Function: serves the current KV value at /api/data.json on the
// same origin as the static frontend (avoids CORS entirely, unlike calling the
// separate refresh Worker's own workers.dev URL directly). Needs the same PAR_DATA
// KV namespace bound to this Pages project at deploy time (see worker/wrangler.toml
// for the namespace id created via `wrangler kv:namespace create PAR_DATA`).

interface Env {
  PAR_DATA: KVNamespace;
}

export const onRequestGet: PagesFunction<Env> = async (context) => {
  const stored = await context.env.PAR_DATA.get('data.json');
  if (!stored) {
    return new Response(JSON.stringify({ error: 'no data available yet' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
  return new Response(stored, {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
};
