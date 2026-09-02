/** Exchange metadata is authoritative; legacy symbols are only display fallbacks. */
export type ContractMeta = {
  symbol?: string;
  base_asset?: string;
  quote_asset?: string;
  margin_asset?: string;
  contract_type?: string;
  contract_type_label?: string;
  underlying_type?: string;
  underlying_sub_type?: string[];
  category?: string;
  category_label?: string;
  is_tradfi?: boolean;
  is_perpetual?: boolean;
  delivery_date?: number | null;
};
export const CATEGORIES: [string, string][] = [
  ['all', '全部资产'],
  ['tradfi', '全部传统金融'],
  ['crypto', '加密资产'],
  ['us_equity', '股票 / ETF'],
  ['hk_equity', '港股'],
  ['cn_equity', 'A 股'],
  ['kr_equity', '韩股'],
  ['commodity', '大宗商品'],
  ['index', '指数'],
  ['premarket', '传统金融盘前'],
  ['other', '其他 / 未分类'],
];
const RAW_CATEGORY: Record<string, string> = {
  COIN: 'crypto',
  INDEX: 'index',
  EQUITY: 'us_equity',
  STOCK: 'us_equity',
  HK_EQUITY: 'hk_equity',
  CN_EQUITY: 'cn_equity',
  KR_EQUITY: 'kr_equity',
  COMMODITY: 'commodity',
  PREMARKET: 'premarket',
};
export const categoryOf = (m: ContractMeta) =>
  m.category || RAW_CATEGORY[m.underlying_type || ''] || 'other';
export const isTradfi = (m: ContractMeta) =>
  m.is_tradfi === true || m.contract_type === 'TRADIFI_PERPETUAL';
export const matchesCategory = (m: ContractMeta, category: string) =>
  category === 'all' ||
  (category === 'tradfi' ? isTradfi(m) : categoryOf(m) === category);
export const categoryLabel = (m: ContractMeta) =>
  m.category_label ||
  CATEGORIES.find(([key]) => key === categoryOf(m))?.[1] ||
  m.underlying_type ||
  '未分类';
export const contractLabel = (m: ContractMeta) =>
  m.contract_type_label ||
  (
    {
      PERPETUAL: '永续',
      TRADIFI_PERPETUAL: '传统金融永续',
      CURRENT_QUARTER: '当季交割',
      NEXT_QUARTER: '次季交割',
    } as Record<string, string>
  )[m.contract_type || ''] ||
  m.contract_type ||
  '类型待核验';
export const isPerpetual = (m: ContractMeta) =>
  m.is_perpetual ?? !!m.contract_type?.endsWith('PERPETUAL');
export const quoteOf = (m: ContractMeta | string): string => {
  if (typeof m !== 'string' && m.quote_asset) return m.quote_asset;
  const symbol = typeof m === 'string' ? m : m.symbol || '';
  return (
    symbol.match(/(USDT|USDC|USD1|BTC|U)(?:_\d{6,8})?$/)?.[1] || '报价待核验'
  );
};
export const assetOf = (m: ContractMeta | string): string => {
  if (typeof m !== 'string' && m.base_asset) return m.base_asset;
  const symbol = typeof m === 'string' ? m : m.symbol || '';
  // Keep the dated identifier in legacy records so two delivery contracts never look identical.
  return symbol.includes('_')
    ? symbol
    : symbol.replace(/(USDT|USDC|USD1)$/, '');
};
