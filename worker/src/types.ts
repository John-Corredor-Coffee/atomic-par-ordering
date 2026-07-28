export interface Env {
  PAR_DATA: KVNamespace;
  SHOPIFY_STORE_DOMAIN: string;
  SHOPIFY_CLIENT_ID: string;
  SHOPIFY_CLIENT_SECRET: string;
}

export interface Item {
  sku: string;
  name: string;
  usageUnit: string;
  orderUnit: string;
  unitsPerOrder: number;
  avgDaily: number;
  yoy: number;
  w3: number;
  ly_w3: number;
  w7: number;
  ly_w7: number;
  qw3l: number;
  qly_w3l: number;
  lastOrderDate: string;
  lastOnHand: number;
  weekZero?: boolean;
}

export interface Location {
  id: number;
  name: string;
  deliveryDay: string;
  daysOpen: number;
  safetyDays: number;
  reviewStatus: 'active' | 'needs-review' | 'new';
  items: Item[];
}

export interface Company {
  id: number;
  name: string;
  isInternal?: boolean;
  locations: Location[];
}

export interface DataPayload {
  generated: string;
  windowDays: number;
  companies: Company[];
}

export interface SkuBucket {
  name: string;
  qty60: number;
  qw3l: number;
  qw3p: number;
  qly_w3l: number;
  qly_w3p: number;
  qw7l: number;
  qw7p: number;
  qly_w7l: number;
  qly_w7p: number;
}

export interface LocBucket {
  companyName: string;
  locationName: string;
  deliveryDay: string;
  skus: Map<string, SkuBucket>;
}

export interface NewCompany {
  name: string;
  createdAt: string;
  ordersCount: number;
  locations: string[];
  catalogTag: string;
}
