export type ExchangeName = "binance" | "okx";
export type MarketType = "spot" | "usdt_perp";
export type TradingEnvironment = "demo" | "testnet" | "live";
export type TradeDirection = "LONG" | "SHORT" | "BUY" | "SELL";

export type OrderStatus =
  | "CREATED"
  | "VALIDATED"
  | "SUBMITTED"
  | "ACKNOWLEDGED"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "REJECTED"
  | "CANCELED"
  | "EXPIRED"
  | "UNKNOWN";

export interface WsEnvelope<T = unknown> {
  event_id: string;
  event_type: string;
  occurred_at: string;
  source: string;
  data: T;
}

export interface TradePlanContract {
  exchange: ExchangeName;
  market_type: MarketType;
  symbol: string;
  direction: TradeDirection;
  entry_type: "MARKET" | "LIMIT";
  entry_price: string;
  entry_zone?: [string, string] | null;
  stop_loss: string;
  take_profit: string;
  confidence: string;
  timeframe: string;
  thesis: string;
  invalidation_conditions: string[];
  expires_at: string;
  strategy_version: string;
}
