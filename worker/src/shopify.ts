import type { Env } from './types';

export interface ShopifyClient {
  gqlUrl: string;
  token: string;
}

export async function getAccessToken(env: Env): Promise<string> {
  const res = await fetch(`https://${env.SHOPIFY_STORE_DOMAIN}/admin/oauth/access_token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'client_credentials',
      client_id: env.SHOPIFY_CLIENT_ID,
      client_secret: env.SHOPIFY_CLIENT_SECRET,
    }),
  });
  if (!res.ok) throw new Error(`Shopify OAuth token exchange failed: HTTP ${res.status}`);
  const data = (await res.json()) as { access_token: string };
  return data.access_token;
}

export function makeClient(env: Env, token: string): ShopifyClient {
  return {
    gqlUrl: `https://${env.SHOPIFY_STORE_DOMAIN}/admin/api/2024-10/graphql.json`,
    token,
  };
}

interface GraphQLResponse<T> {
  data?: T;
  errors?: Array<{ message: string; extensions?: { code?: string } }>;
  extensions?: {
    cost?: {
      requestedQueryCost: number;
      throttleStatus?: { currentlyAvailable: number; restoreRate: number };
    };
  };
}

// Cost-aware pacing: read Shopify's own throttle-status signal on every response
// and sleep proportionally before the next call, rather than a fixed delay.
export async function graphqlRequest<T>(
  client: ShopifyClient,
  query: string,
  variables: Record<string, unknown> = {},
  attempt = 0
): Promise<T> {
  const res = await fetch(client.gqlUrl, {
    method: 'POST',
    headers: {
      'X-Shopify-Access-Token': client.token,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ query, variables }),
  });
  const payload = (await res.json()) as GraphQLResponse<T>;

  const throttled = payload.errors?.some((e) => e.extensions?.code === 'THROTTLED');
  if (throttled && attempt < 3) {
    await sleep(2000);
    return graphqlRequest<T>(client, query, variables, attempt + 1);
  }
  if (payload.errors?.length) {
    throw new Error(`Shopify GraphQL error: ${JSON.stringify(payload.errors)}`);
  }

  const cost = payload.extensions?.cost;
  if (cost?.throttleStatus) {
    const { currentlyAvailable, restoreRate } = cost.throttleStatus;
    const requested = cost.requestedQueryCost;
    if (currentlyAvailable < requested) {
      const waitMs = ((requested - currentlyAvailable) / Math.max(restoreRate, 1)) * 1000 + 500;
      await sleep(waitMs);
    }
  }

  return payload.data as T;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function fetchShopTimezone(client: ShopifyClient): Promise<string> {
  const query = `{ shop { ianaTimezone } }`;
  const data = await graphqlRequest<{ shop: { ianaTimezone: string } }>(client, query);
  return data.shop.ianaTimezone;
}

// ── Orders ──────────────────────────────────────────────────────────────────

export interface RawLineItem {
  sku: string | null;
  title: string;
  currentQuantity: number;
}

export interface RawOrder {
  processedAt: string;
  displayFinancialStatus: string;
  companyName: string | null;
  locationName: string | null;
  lineItems: RawLineItem[];
}

const ORDERS_QUERY = `
  query FetchOrders($cursor: String, $searchQuery: String!) {
    orders(first: 25, after: $cursor, query: $searchQuery, sortKey: PROCESSED_AT) {
      edges {
        node {
          id
          processedAt
          displayFinancialStatus
          purchasingEntity {
            __typename
            ... on PurchasingCompany {
              company { name }
              location { name }
            }
          }
          lineItems(first: 50) {
            pageInfo { hasNextPage }
            edges { node { sku title currentQuantity } }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
`;

const ACCEPTED_FINANCIAL_STATUSES = new Set(['PAID', 'PARTIALLY_REFUNDED', 'PARTIALLY_PAID']);

export interface OrdersFetchResult {
  orders: RawOrder[];
  truncatedOrderCount: number;
}

// Fetches every B2B order since `sinceDateIso` (a plain 'created_at:>=' filter, no
// financial_status filter in the search string — that's applied in code below, since
// a bare `financial_status:paid` search filter would silently drop orders that were
// later partially refunded, which is a real undercount, not just noise).
export async function fetchAllOrders(client: ShopifyClient, sinceDateIso: string): Promise<OrdersFetchResult> {
  const orders: RawOrder[] = [];
  let truncatedOrderCount = 0;
  let cursor: string | null = null;
  const searchQuery = `created_at:>=${sinceDateIso}`;

  while (true) {
    const data: any = await graphqlRequest(client, ORDERS_QUERY, { cursor, searchQuery });
    const edges = data.orders.edges as any[];
    for (const { node } of edges) {
      if (node.purchasingEntity?.__typename !== 'PurchasingCompany') continue;
      if (!ACCEPTED_FINANCIAL_STATUSES.has(node.displayFinancialStatus)) continue;
      if (node.lineItems.pageInfo.hasNextPage) truncatedOrderCount += 1;
      orders.push({
        processedAt: node.processedAt,
        displayFinancialStatus: node.displayFinancialStatus,
        companyName: node.purchasingEntity.company?.name ?? null,
        locationName: node.purchasingEntity.location?.name ?? null,
        lineItems: (node.lineItems.edges as any[]).map((e) => ({
          sku: e.node.sku,
          title: e.node.title,
          currentQuantity: e.node.currentQuantity,
        })),
      });
    }
    if (!data.orders.pageInfo.hasNextPage) break;
    cursor = data.orders.pageInfo.endCursor;
  }

  return { orders, truncatedOrderCount };
}

// ── Companies (new zero-order signups) ───────────────────────────────────────

export interface RawCompany {
  name: string;
  createdAt: string;
  ordersCount: number;
  locations: string[];
  catalogTag: string;
}

const COMPANIES_QUERY = `
  query FetchCompanies($cursor: String, $searchQuery: String!) {
    companies(first: 50, after: $cursor, query: $searchQuery) {
      edges {
        node {
          name
          createdAt
          ordersCount { count }
          locations(first: 20) { edges { node { name } } }
          mainContact { customer { tags } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
`;

export async function fetchNewSignups(
  client: ShopifyClient,
  cutoffDateIso: string,
  excludeNames: Set<string>,
  catalogTags: Set<string>
): Promise<RawCompany[]> {
  const results: RawCompany[] = [];
  let cursor: string | null = null;
  const searchQuery = `created_at:>${cutoffDateIso}`;

  while (true) {
    const data: any = await graphqlRequest(client, COMPANIES_QUERY, { cursor, searchQuery });
    const edges = data.companies.edges as any[];
    for (const { node } of edges) {
      if (excludeNames.has(node.name)) continue;
      if (node.ordersCount.count !== 0) continue;
      const tags: string[] = node.mainContact?.customer?.tags ?? [];
      const catalogTag = tags.map((t: string) => t.toLowerCase()).find((t: string) => catalogTags.has(t)) ?? '';
      const locNames = (node.locations.edges as any[]).map((e) => e.node.name);
      results.push({
        name: node.name,
        createdAt: node.createdAt,
        ordersCount: node.ordersCount.count,
        locations: locNames.length ? locNames : [node.name],
        catalogTag,
      });
    }
    if (!data.companies.pageInfo.hasNextPage) break;
    cursor = data.companies.pageInfo.endCursor;
  }

  return results;
}
