from __future__ import annotations

from decimal import Decimal
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import event_bus
from app.core.precision import ZERO, to_decimal, utc_now
from app.core.security import mask_secret, redact_sensitive_data, secret_vault
from app.exchanges.base import ExchangeAdapter
from app.exchanges.binance.adapter import BinanceAdapter
from app.exchanges.okx.adapter import OKXAdapter
# NOTE: `app.market_data.service` is imported lazily inside the functions that need it.
# A module-level import here would create an import cycle:
#   market_data.service -> exchanges.base -> exchanges/__init__ -> registry -> market_data.service
from app.models.entities import (
    AuditLog,
    BalanceSnapshot,
    EquitySnapshot,
    ExchangeConnection,
    Instrument,
    MarketCandle,
    MarketSnapshot,
    RiskEvent,
    RiskProfile,
    SystemEvent,
    TradingAccount,
)
from app.schemas.domain import ExchangeConnectionCreate, RiskLimitsSchema


def _default_risk_limits_dict() -> dict[str, Any]:
    schema = RiskLimitsSchema(
        max_risk_per_trade_pct=settings.DEFAULT_MAX_RISK_PER_TRADE_PCT,
        max_daily_loss_pct=settings.DEFAULT_MAX_DAILY_LOSS_PCT,
        max_total_exposure_pct=settings.DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
        max_symbol_exposure_pct=settings.DEFAULT_MAX_SYMBOL_EXPOSURE_PCT,
        max_open_positions=settings.DEFAULT_MAX_OPEN_POSITIONS,
        max_leverage=settings.DEFAULT_MAX_LEVERAGE,
        max_slippage_bps=settings.DEFAULT_MAX_SLIPPAGE_BPS,
        max_spread_bps=settings.DEFAULT_MAX_SPREAD_BPS,
        max_data_stale_seconds=settings.DEFAULT_MAX_DATA_STALE_SECONDS,
        max_consecutive_api_failures=settings.DEFAULT_MAX_CONSECUTIVE_API_FAILURES,
        max_consecutive_losses=settings.DEFAULT_MAX_CONSECUTIVE_LOSSES,
        max_daily_trades=settings.DEFAULT_MAX_DAILY_TRADES,
        account_paused=False,
    )
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in schema.model_dump().items()}


class ExchangeRegistry:
    """
    Manages ExchangeConnections, TradingAccounts, encrypted credential storage,
    withdrawal-permission rejection (Section 3.2), and default Demo seeding.
    """

    def __init__(self) -> None:
        self._adapter_cache: dict[str, ExchangeAdapter] = {}

    def list_supported_exchanges(self) -> list[dict[str, Any]]:
        return [
            {
                "exchange": "binance",
                "display_name": "Binance",
                "supported_environments": ["demo", "testnet", "live"],
                "supported_market_types": ["spot", "usdt_perp"],
                "requires_passphrase": False,
                "contract_size_note": "1 contract = 1 unit of base asset (BTC, ETH, SOL)",
                "spot_rest_url": settings.BINANCE_SPOT_REST_URL,
                "futures_rest_url": settings.BINANCE_FUTURES_REST_URL,
            },
            {
                "exchange": "okx",
                "display_name": "OKX V5",
                "supported_environments": ["demo", "testnet", "live"],
                "supported_market_types": ["spot", "usdt_perp"],
                "requires_passphrase": True,
                "contract_size_note": "BTC-USDT-SWAP = 0.01 BTC/contract, ETH-USDT-SWAP = 0.1 ETH/contract",
                "rest_url": settings.OKX_REST_URL,
            },
        ]

    def get_adapter(
        self,
        exchange: str = "binance",
        environment: str | None = None,
        connection: ExchangeConnection | None = None,
        **kwargs: Any,
    ) -> ExchangeAdapter:
        if connection is not None:
            return self.get_adapter_for_connection(connection)

        ex = (exchange or "binance").strip().lower()
        env = (environment or settings.DEFAULT_TRADING_ENVIRONMENT or "demo").strip().lower()
        adapter_cls = OKXAdapter if ex == "okx" else BinanceAdapter
        return adapter_cls(environment=env, **kwargs)

    def get_adapter_for_connection(self, connection: ExchangeConnection) -> ExchangeAdapter:
        api_key = secret_vault.decrypt(connection.encrypted_key_ref) or "demo_key"
        api_secret = secret_vault.decrypt(connection.encrypted_secret_ref) or "demo_secret"
        passphrase = (
            secret_vault.decrypt(connection.encrypted_passphrase_ref)
            if connection.encrypted_passphrase_ref
            else None
        )
        region = str((connection.permissions_json or {}).get("region", "GLOBAL"))
        adapter_cls = OKXAdapter if connection.exchange.lower() == "okx" else BinanceAdapter
        return adapter_cls(
            environment=connection.environment,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            region=region,
            enable_withdraw_permission_simulation=bool(connection.has_withdraw_permission),
            ip_whitelist_configured=bool(connection.ip_whitelist_configured),
        )

    def get_adapter_for_account(
        self,
        db: Session,
        account_id: str | None = None,
        exchange: str | None = None,
    ) -> ExchangeAdapter:
        account = self.get_account(db, account_id=account_id, exchange=exchange)
        if account:
            conn = db.get(ExchangeConnection, account.connection_id)
            if conn:
                return self.get_adapter_for_connection(conn)
            return self.get_adapter(exchange=account.exchange, environment=account.environment)
        return self.get_adapter(exchange=exchange or "binance", environment="demo")

    def _sync_account_balances_and_equity(
        self,
        db: Session,
        account: TradingAccount,
        adapter: ExchangeAdapter,
    ) -> list[BalanceSnapshot]:
        raw_balances = adapter.get_balance()
        now = utc_now()
        existing_rows = list(
            db.execute(
                select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)
            ).scalars().all()
        )
        by_asset = {row.asset: row for row in existing_rows}

        snapshots: list[BalanceSnapshot] = []
        total_equity = ZERO
        available_usdt = ZERO

        for item in raw_balances:
            asset = str(item["asset"])
            total = to_decimal(item["total"])
            avail = to_decimal(item["available"])
            locked = to_decimal(item.get("locked", ZERO))
            usd_val = to_decimal(item.get("usd_value", total if asset == "USDT" else ZERO))

            total_equity += usd_val
            if asset == "USDT":
                available_usdt = avail

            snap = by_asset.get(asset)
            if snap is None:
                snap = BalanceSnapshot(
                    account_id=account.id,
                    exchange=account.exchange,
                    asset=asset,
                    total=total,
                    available=avail,
                    locked=locked,
                    usd_value=usd_val,
                    observed_at=now,
                )
                db.add(snap)
            else:
                snap.total = total
                snap.available = avail
                snap.locked = locked
                snap.usd_value = usd_val
                snap.observed_at = now
            snapshots.append(snap)

        eq_exists = db.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == account.id)
            .order_by(EquitySnapshot.observed_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if eq_exists is None:
            eq_snap = EquitySnapshot(
                account_id=account.id,
                exchange=account.exchange,
                equity=total_equity.quantize(Decimal("0.0001")),
                available_balance=available_usdt.quantize(Decimal("0.0001")),
                margin_balance=total_equity.quantize(Decimal("0.0001")),
                notional_exposure=ZERO,
                realized_pnl=ZERO,
                unrealized_pnl=ZERO,
                fees_paid=ZERO,
                funding_pnl=ZERO,
                net_transfers=ZERO,
                observed_at=now,
            )
            db.add(eq_snap)

        return snapshots

    def create_connection(
        self,
        db: Session,
        payload: ExchangeConnectionCreate | dict[str, Any],
    ) -> ExchangeConnection:
        data = (
            payload
            if isinstance(payload, ExchangeConnectionCreate)
            else ExchangeConnectionCreate.model_validate(payload)
        )

        api_key_clean = data.api_key.strip()
        api_secret_clean = data.api_secret.strip()
        passphrase_clean = data.passphrase.strip() if data.passphrase else None

        encrypted_key = secret_vault.encrypt(api_key_clean) or ""
        encrypted_secret = secret_vault.encrypt(api_secret_clean) or ""
        encrypted_passphrase = secret_vault.encrypt(passphrase_clean) if passphrase_clean else None
        masked_key = mask_secret(api_key_clean)

        has_withdraw = bool(
            data.enable_withdraw_permission_simulation or ("WITHDRAW" in api_key_clean.upper())
        )

        adapter = self.get_adapter(
            exchange=data.exchange,
            environment=data.environment,
            api_key=api_key_clean,
            api_secret=api_secret_clean,
            passphrase=passphrase_clean,
            region=data.region,
            enable_withdraw_permission_simulation=has_withdraw,
            ip_whitelist_configured=data.ip_whitelist_configured,
        )
        conn_result = adapter.connect()
        caps = adapter.get_capabilities()
        has_withdraw = bool(has_withdraw or caps.get("can_withdraw", False))

        now = utc_now()
        if has_withdraw:
            status = "REJECTED_WITHDRAW_PERMISSION"
            last_error = (
                "Rejected by security policy (Section 3.2): API key has withdrawal permissions enabled. "
                "Only Read and Trade permissions are allowed."
            )
        else:
            status = str(conn_result.get("status", "CONNECTED"))
            last_error = None

        connection = ExchangeConnection(
            name=data.name,
            exchange=data.exchange.lower(),
            environment=data.environment.lower(),
            api_key_masked=masked_key,
            encrypted_key_ref=encrypted_key,
            encrypted_secret_ref=encrypted_secret,
            encrypted_passphrase_ref=encrypted_passphrase,
            status=status,
            has_withdraw_permission=has_withdraw,
            ip_whitelist_configured=data.ip_whitelist_configured,
            permissions_json=redact_sensitive_data(caps),
            last_sync_at=now if not has_withdraw else None,
            last_error=last_error,
            created_at=now,
            updated_at=now,
        )
        db.add(connection)
        db.flush()

        account = TradingAccount(
            connection_id=connection.id,
            exchange=connection.exchange,
            environment=connection.environment,
            account_type="unified",
            region=data.region.upper(),
            capabilities=redact_sensitive_data(caps),
            is_active=not has_withdraw,
            trading_locked=has_withdraw,
            lock_reason=(
                "API key has withdrawal permission enabled (REJECTED_WITHDRAW_PERMISSION)"
                if has_withdraw
                else None
            ),
            sync_status="REJECTED_WITHDRAW_PERMISSION" if has_withdraw else "SYNCED",
            last_reconciled_at=now if not has_withdraw else None,
            created_at=now,
        )
        db.add(account)
        db.flush()

        risk_profile = RiskProfile(
            account_id=account.id,
            limits_json=_default_risk_limits_dict(),
            enabled=True,
            updated_at=now,
        )
        db.add(risk_profile)

        if has_withdraw:
            risk_event = RiskEvent(
                account_id=account.id,
                event_type="WITHDRAW_PERMISSION_REJECTED",
                rule_code="SEC_WITHDRAW_PERMISSION_FORBIDDEN",
                severity="CRITICAL",
                details={
                    "connection_id": connection.id,
                    "account_id": account.id,
                    "exchange": connection.exchange,
                    "environment": connection.environment,
                    "api_key_masked": masked_key,
                    "reason": last_error,
                },
                created_at=now,
            )
            db.add(risk_event)
            event_bus.publish_sync(
                channel="risk",
                event_type="risk.withdraw_permission_rejected",
                data={
                    "connection_id": connection.id,
                    "account_id": account.id,
                    "exchange": connection.exchange,
                    "severity": "CRITICAL",
                    "status": status,
                },
                source="security",
            )
        else:
            self._sync_account_balances_and_equity(db, account, adapter)

        db.add(
            AuditLog(
                actor="local_admin",
                action="CREATE_EXCHANGE_CONNECTION",
                resource=f"exchange_connection:{connection.id}",
                before_json={},
                after_json={
                    "id": connection.id,
                    "name": connection.name,
                    "exchange": connection.exchange,
                    "environment": connection.environment,
                    "api_key_masked": connection.api_key_masked,
                    "status": connection.status,
                    "has_withdraw_permission": connection.has_withdraw_permission,
                    "account_id": account.id,
                },
                created_at=now,
            )
        )
        db.commit()
        db.refresh(connection)
        return connection

    register_connection = create_connection

    def list_connections(self, db: Session) -> list[ExchangeConnection]:
        stmt = select(ExchangeConnection).order_by(ExchangeConnection.created_at.asc())
        return list(db.execute(stmt).scalars().all())

    def get_connection(self, db: Session, connection_id: str) -> ExchangeConnection | None:
        return db.get(ExchangeConnection, connection_id)

    def test_connection(self, db: Session, connection_id: str) -> dict[str, Any]:
        conn = db.get(ExchangeConnection, connection_id)
        if conn is None:
            raise ValueError(f"ExchangeConnection {connection_id} not found")

        adapter = self.get_adapter_for_connection(conn)
        res = adapter.connect()
        caps = adapter.get_capabilities()
        has_withdraw = bool(conn.has_withdraw_permission or caps.get("can_withdraw", False))
        now = utc_now()

        account = db.execute(
            select(TradingAccount).where(TradingAccount.connection_id == conn.id)
        ).scalar_one_or_none()

        if has_withdraw:
            conn.status = "REJECTED_WITHDRAW_PERMISSION"
            conn.has_withdraw_permission = True
            conn.last_error = "Rejected: API key has withdrawal permissions enabled."
            conn.permissions_json = redact_sensitive_data(caps)
            conn.updated_at = now
            if account:
                account.trading_locked = True
                account.is_active = False
                account.lock_reason = "REJECTED_WITHDRAW_PERMISSION"
                account.sync_status = "REJECTED_WITHDRAW_PERMISSION"
                db.add(
                    RiskEvent(
                        account_id=account.id,
                        event_type="WITHDRAW_PERMISSION_REJECTED",
                        rule_code="SEC_WITHDRAW_PERMISSION_FORBIDDEN",
                        severity="CRITICAL",
                        details={
                            "connection_id": conn.id,
                            "exchange": conn.exchange,
                            "api_key_masked": conn.api_key_masked,
                        },
                        created_at=now,
                    )
                )
        else:
            conn.status = "CONNECTED"
            conn.last_sync_at = now
            conn.last_error = None
            conn.permissions_json = redact_sensitive_data(caps)
            conn.updated_at = now
            if account:
                account.capabilities = redact_sensitive_data(caps)
                account.last_reconciled_at = now
                if account.lock_reason == "REJECTED_WITHDRAW_PERMISSION":
                    account.trading_locked = False
                    account.is_active = True
                    account.lock_reason = None
                    account.sync_status = "SYNCED"

        db.commit()
        db.refresh(conn)
        return {
            "connection_id": conn.id,
            "exchange": conn.exchange,
            "environment": conn.environment,
            "api_key_masked": conn.api_key_masked,
            "status": conn.status,
            "ok": not has_withdraw,
            "has_withdraw_permission": conn.has_withdraw_permission,
            "ip_whitelist_configured": conn.ip_whitelist_configured,
            "capabilities": redact_sensitive_data(caps),
            "last_error": conn.last_error,
            "tested_at": now,
        }

    def get_connection_status(self, db: Session, connection_id: str) -> dict[str, Any]:
        conn = db.get(ExchangeConnection, connection_id)
        if conn is None:
            raise ValueError(f"ExchangeConnection {connection_id} not found")
        account = db.execute(
            select(TradingAccount).where(TradingAccount.connection_id == conn.id)
        ).scalar_one_or_none()
        return {
            "connection_id": conn.id,
            "name": conn.name,
            "exchange": conn.exchange,
            "environment": conn.environment,
            "api_key_masked": conn.api_key_masked,
            "status": conn.status,
            "has_withdraw_permission": conn.has_withdraw_permission,
            "ip_whitelist_configured": conn.ip_whitelist_configured,
            "last_sync_at": conn.last_sync_at,
            "last_error": conn.last_error,
            "account_id": account.id if account else None,
            "trading_locked": account.trading_locked if account else True,
            "sync_status": account.sync_status if account else "UNKNOWN",
        }

    def sync_connection(self, db: Session, connection_id: str) -> dict[str, Any]:
        conn = db.get(ExchangeConnection, connection_id)
        if conn is None:
            raise ValueError(f"ExchangeConnection {connection_id} not found")

        if conn.has_withdraw_permission or conn.status == "REJECTED_WITHDRAW_PERMISSION":
            return {
                "connection_id": conn.id,
                "exchange": conn.exchange,
                "status": "REJECTED_WITHDRAW_PERMISSION",
                "synced": False,
                "error": conn.last_error or "Connection rejected due to withdrawal permission",
            }

        adapter = self.get_adapter_for_connection(conn)
        account = db.execute(
            select(TradingAccount).where(TradingAccount.connection_id == conn.id)
        ).scalar_one_or_none()

        now = utc_now()
        balances: list[BalanceSnapshot] = []
        if account:
            balances = self._sync_account_balances_and_equity(db, account, adapter)
            account.sync_status = "SYNCED"
            account.last_reconciled_at = now

        from app.market_data.service import market_data_service

        instruments = market_data_service.sync_instruments(db, exchange=conn.exchange)
        conn.status = "CONNECTED"
        conn.last_sync_at = now
        conn.updated_at = now
        db.commit()

        return {
            "connection_id": conn.id,
            "account_id": account.id if account else None,
            "exchange": conn.exchange,
            "environment": conn.environment,
            "status": conn.status,
            "synced": True,
            "balances_count": len(balances),
            "instruments_count": len(instruments),
            "synced_at": now,
        }

    def delete_connection(self, db: Session, connection_id: str) -> dict[str, Any]:
        conn = db.get(ExchangeConnection, connection_id)
        if conn is None:
            raise ValueError(f"ExchangeConnection {connection_id} not found")

        accounts = list(
            db.execute(
                select(TradingAccount).where(TradingAccount.connection_id == conn.id)
            ).scalars().all()
        )
        for acc in accounts:
            acc.is_active = False
            acc.trading_locked = True
            acc.lock_reason = "Connection deleted by operator"
            db.delete(acc)

        deleted_summary = {
            "id": conn.id,
            "name": conn.name,
            "exchange": conn.exchange,
            "environment": conn.environment,
            "api_key_masked": conn.api_key_masked,
            "deleted": True,
        }
        db.delete(conn)
        db.add(
            AuditLog(
                actor="local_admin",
                action="DELETE_EXCHANGE_CONNECTION",
                resource=f"exchange_connection:{connection_id}",
                before_json=deleted_summary,
                after_json={"deleted": True},
                created_at=utc_now(),
            )
        )
        db.commit()
        return deleted_summary

    def list_accounts(self, db: Session, exchange: str | None = None) -> list[TradingAccount]:
        stmt = select(TradingAccount).order_by(TradingAccount.created_at.asc())
        if exchange:
            stmt = stmt.where(TradingAccount.exchange == exchange.lower())
        return list(db.execute(stmt).scalars().all())

    def get_account(
        self,
        db: Session,
        account_id: str | None = None,
        exchange: str | None = None,
    ) -> TradingAccount | None:
        if account_id:
            acc = db.get(TradingAccount, account_id)
            if acc:
                return acc
        stmt = select(TradingAccount)
        if exchange:
            stmt = stmt.where(TradingAccount.exchange == exchange.lower())
        stmt = stmt.order_by(TradingAccount.is_active.desc(), TradingAccount.created_at.asc()).limit(1)
        return db.execute(stmt).scalar_one_or_none()

    def get_account_balances(self, db: Session, account_id: str) -> list[BalanceSnapshot]:
        account = db.get(TradingAccount, account_id)
        if account is None:
            raise ValueError(f"TradingAccount {account_id} not found")

        rows = list(
            db.execute(
                select(BalanceSnapshot)
                .where(BalanceSnapshot.account_id == account_id)
                .order_by(BalanceSnapshot.asset.asc())
            ).scalars().all()
        )
        if not rows and not account.trading_locked:
            adapter = self.get_adapter_for_account(db, account_id=account_id)
            rows = self._sync_account_balances_and_equity(db, account, adapter)
            db.commit()
        return rows

    def get_account_capabilities(self, db: Session, account_id: str) -> dict[str, Any]:
        account = db.get(TradingAccount, account_id)
        if account is None:
            raise ValueError(f"TradingAccount {account_id} not found")
        return {
            "account_id": account.id,
            "connection_id": account.connection_id,
            "exchange": account.exchange,
            "environment": account.environment,
            "account_type": account.account_type,
            "region": account.region,
            "is_active": account.is_active,
            "trading_locked": account.trading_locked,
            "lock_reason": account.lock_reason,
            "sync_status": account.sync_status,
            "capabilities": redact_sensitive_data(account.capabilities or {}),
        }

    def ensure_default_demo_connections(self, db: Session) -> list[ExchangeConnection]:
        """
        Seeds default Demo connections & accounts for both binance and okx with
        initial balances (USDT, BTC, ETH), initial instruments, and initial closed
        candles + market snapshots if the database is empty.
        """
        existing_conns = self.list_connections(db)
        by_ex = {c.exchange.lower(): c for c in existing_conns if c.environment == "demo"}

        if "binance" not in by_ex:
            bn_conn = self.create_connection(
                db,
                ExchangeConnectionCreate(
                    name="Binance Demo Trading",
                    exchange="binance",
                    environment="demo",
                    api_key="SIMULATED-DEMO-KEY-BINANCE",
                    api_secret="SIMULATED-DEMO-SECRET-BINANCE",
                    region="GLOBAL",
                    enable_withdraw_permission_simulation=False,
                    ip_whitelist_configured=True,
                ),
            )
            by_ex["binance"] = bn_conn

        if "okx" not in by_ex:
            okx_conn = self.create_connection(
                db,
                ExchangeConnectionCreate(
                    name="OKX V5 Simulated Trading",
                    exchange="okx",
                    environment="demo",
                    api_key="SIMULATED-DEMO-KEY-OKX",
                    api_secret="SIMULATED-DEMO-SECRET-OKX",
                    passphrase="SIMULATED-DEMO-PASSPHRASE-OKX",
                    region="GLOBAL",
                    enable_withdraw_permission_simulation=False,
                    ip_whitelist_configured=True,
                ),
            )
            by_ex["okx"] = okx_conn

        # Ensure instruments, closed candles, and market snapshots are seeded
        has_instruments = db.execute(select(Instrument.id).limit(1)).scalar_one_or_none()
        has_candles = db.execute(select(MarketCandle.id).limit(1)).scalar_one_or_none()
        has_snapshots = db.execute(select(MarketSnapshot.id).limit(1)).scalar_one_or_none()

        if not has_instruments or not has_candles or not has_snapshots:
            from app.market_data.service import market_data_service

            market_data_service.sync_all_default_markets(db, timeframe="15m", limit=60)

        db.add(
            SystemEvent(
                component="exchange_registry",
                event_type="DEMO_CONNECTIONS_READY",
                severity="INFO",
                payload={
                    "exchanges": list(by_ex.keys()),
                    "environment": "demo",
                },
                created_at=utc_now(),
            )
        )
        db.commit()
        return self.list_connections(db)


exchange_registry = ExchangeRegistry()


# Module-level convenience functions delegating to exchange_registry
def list_supported_exchanges() -> list[dict[str, Any]]:
    return exchange_registry.list_supported_exchanges()


def get_adapter(
    exchange: str = "binance",
    environment: str | None = None,
    connection: ExchangeConnection | None = None,
    **kwargs: Any,
) -> ExchangeAdapter:
    return exchange_registry.get_adapter(
        exchange=exchange,
        environment=environment,
        connection=connection,
        **kwargs,
    )


def get_adapter_for_connection(connection: ExchangeConnection) -> ExchangeAdapter:
    return exchange_registry.get_adapter_for_connection(connection)


def get_adapter_for_account(
    db: Session,
    account_id: str | None = None,
    exchange: str | None = None,
) -> ExchangeAdapter:
    return exchange_registry.get_adapter_for_account(db, account_id=account_id, exchange=exchange)


def create_connection(
    db: Session,
    payload: ExchangeConnectionCreate | dict[str, Any],
) -> ExchangeConnection:
    return exchange_registry.create_connection(db, payload)


def list_connections(db: Session) -> list[ExchangeConnection]:
    return exchange_registry.list_connections(db)


def get_connection(db: Session, connection_id: str) -> ExchangeConnection | None:
    return exchange_registry.get_connection(db, connection_id)


def test_connection(db: Session, connection_id: str) -> dict[str, Any]:
    return exchange_registry.test_connection(db, connection_id)


def get_connection_status(db: Session, connection_id: str) -> dict[str, Any]:
    return exchange_registry.get_connection_status(db, connection_id)


def sync_connection(db: Session, connection_id: str) -> dict[str, Any]:
    return exchange_registry.sync_connection(db, connection_id)


def delete_connection(db: Session, connection_id: str) -> dict[str, Any]:
    return exchange_registry.delete_connection(db, connection_id)


def list_accounts(db: Session, exchange: str | None = None) -> list[TradingAccount]:
    return exchange_registry.list_accounts(db, exchange=exchange)


def get_account(
    db: Session,
    account_id: str | None = None,
    exchange: str | None = None,
) -> TradingAccount | None:
    return exchange_registry.get_account(db, account_id=account_id, exchange=exchange)


def get_account_balances(db: Session, account_id: str) -> list[BalanceSnapshot]:
    return exchange_registry.get_account_balances(db, account_id)


def get_account_capabilities(db: Session, account_id: str) -> dict[str, Any]:
    return exchange_registry.get_account_capabilities(db, account_id)


def ensure_default_demo_connections(db: Session) -> list[ExchangeConnection]:
    return exchange_registry.ensure_default_demo_connections(db)
