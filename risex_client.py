"""
RISEx (rise.trade) API 클라이언트.

- 사용자가 준 mpdex 래퍼를 기반으로, mpdex 의존성을 걷어내고 단독으로 동작하게 고친 버전.
- 주의: 이 서버는 **메인넷(Rise Mainnet, chain_id 4153)** 이다. 실제 돈이 오간다.
  (2026-11-08 /v1/system/config 조회로 확인. 원본 주석의 "Testnet only"는 틀림)

원본 대비 수정:
- mpdex.base 상속 제거
- get_position: 진입가 필드를 avg_entry_price 로 읽음 (원본은 entry_price → 항상 0)
- signer 등록(_register_signer)은 init() 에서 자동 실행하지 않고 명시적으로 호출할 때만
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiohttp
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

logger = logging.getLogger("risex")

DEFAULT_BASE_URL = "https://api.rise.trade"
DEFAULT_TIMEOUT = 10.0
CHAIN_ID = 4153
DEFAULT_SIGNER_EXPIRY_SECONDS = 30 * 24 * 60 * 60
DEFAULT_PERMIT_DEADLINE_SECONDS = 300

ACTION_PLACE_ORDER = "RISE_PERPS_PLACE_ORDER_V1"
ACTION_CANCEL_ORDER = "RISE_PERPS_CANCEL_ORDER_V1"
ACTION_CANCEL_ALL_ORDERS = "RISE_PERPS_CANCEL_ALL_ORDERS_V1"
REGISTER_SIGNER_MESSAGE = "Registering signer for RISEx"

V3_FLAG_PERMIT = 1
V3_FLAG_BUILDER = 2
V3_FLAG_CLIENT_ID = 4
V3_FLAG_PERMIT_ERC1271 = 9
V3_FLAG_TTL = 16

SIDE_LONG = 0
SIDE_SHORT = 1
ORDER_TYPE_MARKET = 0
ORDER_TYPE_LIMIT = 1
TIF_GTC = 0
TIF_IOC = 3
STP_EXPIRE_MAKER = 0

REGISTER_SIGNER_TYPES = {
    "RegisterSigner": [
        {"name": "account", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "message", "type": "string"},
        {"name": "expiration", "type": "uint32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]
}
VERIFY_SIGNER_TYPES = {
    "VerifySigner": [
        {"name": "account", "type": "address"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]
}
VERIFY_WITNESS_TYPES = {
    "VerifyWitness": [
        {"name": "account", "type": "address"},
        {"name": "target", "type": "address"},
        {"name": "hash", "type": "bytes32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
        {"name": "deadline", "type": "uint32"},
    ]
}

ACTION_PLACE_ORDER_HASH = keccak(text=ACTION_PLACE_ORDER)
ACTION_CANCEL_ORDER_HASH = keccak(text=ACTION_CANCEL_ORDER)
ACTION_CANCEL_ALL_ORDERS_HASH = keccak(text=ACTION_CANCEL_ALL_ORDERS)


def _fix_v(signature: bytes) -> bytes:
    if len(signature) == 65 and signature[64] < 27:
        signature = signature[:64] + bytes([signature[64] + 27])
    return signature


def _encode_order_data(market_id, side, order_type, price_ticks, size_steps,
                       time_in_force, post_only, reduce_only, stp_mode) -> int:
    order_flags = 0
    if side & 1:
        order_flags |= 1
    if post_only:
        order_flags |= 2
    if reduce_only:
        order_flags |= 4
    order_flags |= (stp_mode & 3) << 3
    order_flags |= (order_type & 1) << 5
    order_flags |= (time_in_force & 3) << 6
    header_version = 1
    data = 0
    data |= (market_id & 0xFFFF) << 70
    data |= (size_steps & 0xFFFFFFFF) << 38
    data |= (price_ticks & 0xFFFFFF) << 14
    data |= (order_flags & 0xFF) << 6
    data |= (header_version & 0x1F) << 1
    return data


def _compute_header_flags(builder_id, client_order_id, ttl_units, is_erc1271) -> int:
    flags = V3_FLAG_PERMIT_ERC1271 if is_erc1271 else V3_FLAG_PERMIT
    if builder_id != 0:
        flags |= V3_FLAG_BUILDER
    if client_order_id != 0:
        flags |= V3_FLAG_CLIENT_ID
    if ttl_units != 0:
        flags |= V3_FLAG_TTL
    return flags


def _encode_order_hash(*, market_id, side, order_type, price_ticks, size_steps,
                       time_in_force, post_only, reduce_only, stp_mode, ttl_units,
                       client_order_id=0, builder_id=0, is_erc1271=False) -> bytes:
    order_data = _encode_order_data(market_id, side, order_type, price_ticks, size_steps,
                                    time_in_force, post_only, reduce_only, stp_mode)
    header_flags = _compute_header_flags(builder_id, client_order_id, ttl_units, is_erc1271)
    encoded = abi_encode(
        ["bytes32", "uint8", "uint256", "uint16", "uint64", "uint16"],
        [ACTION_PLACE_ORDER_HASH, header_flags, order_data, builder_id, client_order_id, ttl_units],
    )
    return keccak(encoded)


def _encode_cancel_all_hash(market_id: int) -> bytes:
    return keccak(abi_encode(["bytes32", "uint256"], [ACTION_CANCEL_ALL_ORDERS_HASH, market_id]))


def _hex_to_base64(hex_str: str) -> str:
    clean = hex_str[2:] if hex_str.startswith("0x") else hex_str
    return base64.b64encode(bytes.fromhex(clean)).decode("ascii")


def _build_typed_data(domain, types, primary_type, message) -> Dict[str, Any]:
    return {
        "domain": domain,
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            **types,
        },
        "primaryType": primary_type,
        "message": message,
    }


def _sign_typed_data(private_key: str, typed: Dict[str, Any]) -> str:
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, private_key=private_key)
    return "0x" + _fix_v(bytes(signed.signature)).hex()


class RisexClient:
    """private_key 가 없으면 조회 전용(모의 모드용)으로 동작한다."""

    def __init__(self, private_key: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self._pk: Optional[str] = None
        self.account_address: Optional[str] = None
        if private_key:
            pk = private_key if private_key.startswith("0x") else "0x" + private_key
            acct = Account.from_key(pk)
            self._pk = pk
            self.account_address = acct.address
        self.signer_address = self.account_address
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._domain: Dict[str, Any] = {}
        self._target: Optional[str] = None
        self._markets: Dict[int, Dict[str, Any]] = {}
        self._symbol_to_mid: Dict[str, int] = {}

    @property
    def can_trade(self) -> bool:
        return self._pk is not None

    # ----- lifecycle -----
    async def init(self) -> "RisexClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            await self._load_domain()
            await self.load_markets()
        except Exception:
            await self.close()
            raise
        return self

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ----- HTTP -----
    async def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        if self._session is None:
            raise RuntimeError("[risex] not initialized")
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(method, url, json=body) as resp:
                txt = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"{method} {path} -> {resp.status}: {txt[:400]}")
                if not txt:
                    return None
                data = json.loads(txt)
                if isinstance(data, dict) and "error" in data:
                    raise RuntimeError(f"{method} {path} api error: {data['error']}")
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data
        except asyncio.TimeoutError:
            raise RuntimeError(f"{method} {path} timeout after {self.timeout}s")

    async def _get(self, path):
        return await self._request("GET", path)

    async def _post(self, path, body):
        return await self._request("POST", path, body)

    # ----- init helpers -----
    async def _load_domain(self):
        raw = await self._get("/v1/auth/eip712-domain")
        self._domain = {
            "name": raw["name"],
            "version": raw["version"],
            "chainId": int(raw["chain_id"]),
            "verifyingContract": raw["verifying_contract"],
        }
        if self._domain["chainId"] != CHAIN_ID:
            raise RuntimeError(f"unexpected chainId {self._domain['chainId']} (expected {CHAIN_ID}) — 중단")
        cfg = await self._get("/v1/system/config")
        addrs = (cfg or {}).get("addresses", {}) or {}
        self._target = addrs.get("router") or addrs.get("orders_manager")
        if not self._target:
            raise RuntimeError("router/orders_manager not in system config")

    async def load_markets(self):
        raw = await self._get("/v1/markets")
        markets = (raw or {}).get("markets", []) if isinstance(raw, dict) else []
        new_markets, new_map = {}, {}
        for m in markets:
            try:
                mid = int(m.get("market_id"))
            except Exception:
                continue
            new_markets[mid] = m
            base = (m.get("base_asset_symbol") or m.get("display_name") or "").upper()
            coin = base.split("/")[0].split("-")[0].strip()
            if coin:
                new_map[coin] = mid
        if not new_map:
            raise RuntimeError("no markets returned")
        self._markets, self._symbol_to_mid = new_markets, new_map

    # ----- symbol helpers -----
    def _market_id(self, symbol: str) -> int:
        coin = symbol.upper().replace("-PERP", "").split("/")[0].split("-")[0].strip()
        if coin not in self._symbol_to_mid:
            raise ValueError(f"unknown symbol: {symbol}")
        return self._symbol_to_mid[coin]

    def market(self, symbol: str) -> Dict[str, Any]:
        return self._markets[self._market_id(symbol)]

    def min_order_size(self, symbol: str) -> float:
        return float(self.market(symbol).get("config", {}).get("min_order_size") or 0)

    def _step_size(self, symbol) -> Decimal:
        return Decimal(str(self.market(symbol).get("config", {}).get("step_size")))

    def _step_price(self, symbol) -> Decimal:
        return Decimal(str(self.market(symbol).get("config", {}).get("step_price")))

    def _size_to_steps(self, symbol, amount: float) -> int:
        return int((Decimal(str(amount)) / self._step_size(symbol)).to_integral_value(rounding="ROUND_DOWN"))

    def _price_to_ticks(self, symbol, price: float) -> int:
        return int((Decimal(str(price)) / self._step_price(symbol)).to_integral_value(rounding="ROUND_HALF_EVEN"))

    # ----- signing -----
    async def _get_nonce_state(self) -> Dict[str, Any]:
        raw = await self._get(f"/v1/nonce-state/{self.account_address}")
        if not isinstance(raw, dict):
            raise RuntimeError(f"nonce-state bad shape: {raw!r}")
        return raw

    async def is_signer_registered(self) -> bool:
        raw = await self._get(
            f"/v1/auth/session-key-status?account={self.account_address}&signer={self.signer_address}")
        return isinstance(raw, dict) and int(raw.get("status") or 0) == 1

    async def register_signer(self, label: str = "risex-bot") -> Any:
        nonce = await self._get_nonce_state()
        expiration = int(time.time()) + DEFAULT_SIGNER_EXPIRY_SECONDS
        anchor = int(nonce.get("nonce_anchor") or 0) + 1
        bitmap = 0
        account_sig = _sign_typed_data(self._pk, _build_typed_data(
            self._domain, REGISTER_SIGNER_TYPES, "RegisterSigner",
            {"account": self.account_address, "signer": self.signer_address,
             "message": REGISTER_SIGNER_MESSAGE, "expiration": expiration,
             "nonceAnchor": anchor, "nonceBitmap": bitmap}))
        signer_sig = _sign_typed_data(self._pk, _build_typed_data(
            self._domain, VERIFY_SIGNER_TYPES, "VerifySigner",
            {"account": self.account_address, "nonceAnchor": anchor, "nonceBitmap": bitmap}))
        body = {
            "account": self.account_address, "signer": self.signer_address,
            "message": REGISTER_SIGNER_MESSAGE, "nonce_anchor": str(anchor),
            "nonce_bitmap_index": bitmap, "expiration": str(expiration),
            "account_signature": account_sig, "signer_signature": signer_sig, "label": label,
        }
        return await self._post("/v1/auth/register-signer", body)

    async def _create_permit(self, action_hash: bytes) -> Dict[str, Any]:
        nonce = await self._get_nonce_state()
        deadline = int(time.time()) + DEFAULT_PERMIT_DEADLINE_SECONDS
        anchor = int(nonce.get("nonce_anchor") or 0)
        bitmap_idx = int(nonce.get("current_bitmap_index") or 0)
        typed = _build_typed_data(
            self._domain, VERIFY_WITNESS_TYPES, "VerifyWitness",
            {"account": self.account_address, "target": self._target,
             "hash": "0x" + action_hash.hex(), "nonceAnchor": anchor,
             "nonceBitmap": bitmap_idx, "deadline": deadline})
        return {
            "account": self.account_address, "signer": self.signer_address,
            "nonce_anchor": anchor, "nonce_bitmap_index": bitmap_idx,
            "deadline": deadline, "signature": _hex_to_base64(_sign_typed_data(self._pk, typed)),
        }

    # ----- 조회 -----
    async def get_mark_price(self, symbol: str) -> Optional[float]:
        await self.load_markets()
        m = self.market(symbol)
        mp = m.get("mark_price") or m.get("index_price") or m.get("last_price")
        if mp is None:
            return None
        v = float(mp)
        return v if math.isfinite(v) and v > 0 else None

    async def get_best_bid_ask(self, symbol: str):
        ob = await self._get(f"/v1/orderbook?market_id={self._market_id(symbol)}")
        bids = (ob or {}).get("bids") or []
        asks = (ob or {}).get("asks") or []
        bid = float(bids[0]["price"]) if bids else None
        ask = float(asks[0]["price"]) if asks else None
        return bid, ask

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        mid = self._market_id(symbol)
        raw = await self._get(f"/v1/account/position?market_id={mid}&account={self.account_address}")
        # 응답 모양이 예상과 다르면 "포지션 없음"으로 착각하지 말고 오류로 처리한다.
        # (2026-11-08 조회한 실제 응답: {"position": {"size": "0", "side": 0, "avg_entry_price": "", ...}})
        if not isinstance(raw, dict) or not isinstance(raw.get("position"), dict):
            raise RuntimeError(f"포지션 응답 모양이 예상과 다름: {str(raw)[:200]}")
        pos = raw["position"]
        if "size" not in pos or "side" not in pos:
            raise RuntimeError(f"포지션 응답에 size/side 없음: {str(pos)[:200]}")
        try:
            size = float(pos["size"])
        except (TypeError, ValueError):
            raise RuntimeError(f"포지션 size 해석 불가: {pos['size']!r}")
        if size == 0:
            return None
        side_val = pos["side"]
        if side_val not in (0, "0", 1, "1"):
            raise RuntimeError(f"포지션 side 값이 예상과 다름: {side_val!r}")
        return {
            "side": "long" if side_val in (0, "0") else "short",
            "size": abs(size),
            "entry_price": float(pos.get("avg_entry_price") or 0),
            "unrealized_pnl": float(pos.get("unrealized_pnl") or 0),
            "liquidation_price": float(pos["liquidation_price"]) if pos.get("liquidation_price") else None,
            "leverage": pos.get("leverage"),
        }

    async def get_collateral(self) -> Optional[float]:
        """필드명 'balance'는 원본 코드 기준이며 실제 계정으로 검증되지 않았다."""
        try:
            raw = await self._get(f"/v1/account/cross-margin-balance?account={self.account_address}")
            return float(raw.get("balance") or 0) if isinstance(raw, dict) else None
        except Exception as e:
            logger.warning("잔고 조회 실패: %s", e)
            return None

    # ----- 주문 -----
    async def create_order(self, symbol: str, side: str, amount: float,
                           price: Optional[float] = None, order_type: str = "market",
                           *, reduce_only: bool = False) -> Any:
        if not self.can_trade:
            raise RuntimeError("private key 없음 — 주문 불가")
        mid = self._market_id(symbol)
        side_int = SIDE_LONG if side.lower() in ("buy", "long") else SIDE_SHORT
        size_steps = self._size_to_steps(symbol, amount)
        if size_steps <= 0:
            raise ValueError(f"size too small: {amount}")
        if order_type == "market":
            # 메인넷은 순수 시장가를 거부 → 호가를 0.3% 넘는 지정가 IOC 로 대체 (원본 코드 방식)
            bid, ask = await self.get_best_bid_ask(symbol)
            if side_int == SIDE_LONG:
                if ask is None:
                    raise RuntimeError("no ask in orderbook")
                cross = ask * 1.003
            else:
                if bid is None:
                    raise RuntimeError("no bid in orderbook")
                cross = bid * 0.997
            price_ticks = self._price_to_ticks(symbol, cross)
            tif = TIF_IOC
        else:
            if price is None or price <= 0:
                raise ValueError("limit order requires positive price")
            price_ticks = self._price_to_ticks(symbol, price)
            tif = TIF_GTC
        action_hash = _encode_order_hash(
            market_id=mid, side=side_int, order_type=ORDER_TYPE_LIMIT,
            price_ticks=price_ticks, size_steps=size_steps, time_in_force=tif,
            post_only=False, reduce_only=reduce_only, stp_mode=STP_EXPIRE_MAKER, ttl_units=0)
        permit = await self._create_permit(action_hash)
        body = {
            "market_id": mid, "side": side_int, "order_type": ORDER_TYPE_LIMIT,
            "price_ticks": price_ticks, "size_steps": size_steps, "time_in_force": tif,
            "post_only": False, "reduce_only": reduce_only, "stp_mode": STP_EXPIRE_MAKER,
            "ttl_units": 0, "client_order_id": "0", "builder_id": 0, "permit": permit,
        }
        return await self._post("/v1/orders/place", body)

    async def close_position(self, symbol: str, position: Dict[str, Any]) -> Any:
        side = "sell" if position["side"] == "long" else "buy"
        return await self.create_order(symbol, side, position["size"], order_type="market",
                                       reduce_only=True)
