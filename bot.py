"""
RISEx 급락 반등 봇 (BTC 롱 전용)

전략:
  1) POLL_SEC 초마다 마크가격을 조회해 최근 DROP_WINDOW_SEC 초 동안의 가격을 기억한다.
  2) 그 구간 최고가 대비 DROP_PCT% 이상 떨어지면 → 롱 진입 (ORDER_SIZE 개).
  3) 진입가 대비 -STOP_LOSS_PCT% 면 손절, +TAKE_PROFIT_PCT% 면 익절.
  4) 진입 "시도" 후 COOLDOWN_SEC 초 동안은 새로 진입하지 않는다. 하루 MAX_TRADES_PER_DAY 회까지만.

안전장치:
  - 실전 모드에서는 매 루프 거래소 포지션을 다시 조회한다. 조회 실패 시 그 루프는 진입하지 않는다.
  - 진입 시도 횟수·쿨다운은 주문을 보내기 "전에" 기록한다 (주문 후 오류가 나도 반복 매수 안 함).
  - 숏 포지션, 진입가 0, ORDER_SIZE*2 초과 포지션을 발견하면 봇을 멈춘다.
  - 연속 오류 MAX_CONSECUTIVE_ERRORS 회면 봇을 멈춘다.
  - 손절/익절은 봇이 처리한다. 봇이 꺼지면 손절이 안 걸린다!

LIVE_TRADING=yes 가 아니면 모의 모드: 실제 주문 없이 로그만 남긴다.
"""

import asyncio
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from risex_client import RisexClient

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def env_float(name, default):
    return float(os.getenv(name, default))


SYMBOL = os.getenv("SYMBOL", "BTC")
ORDER_SIZE = env_float("ORDER_SIZE", "0.00015")
DROP_WINDOW_SEC = env_float("DROP_WINDOW_SEC", "60")
DROP_PCT = env_float("DROP_PCT", "1.0")
STOP_LOSS_PCT = env_float("STOP_LOSS_PCT", "1.0")
TAKE_PROFIT_PCT = env_float("TAKE_PROFIT_PCT", "2.0")
COOLDOWN_SEC = env_float("COOLDOWN_SEC", "300")
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MAX_ORDER_USD = env_float("MAX_ORDER_USD", "30")
MAX_CONSECUTIVE_ERRORS = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "10"))
MAX_CLOSE_FAILURES = int(os.getenv("MAX_CLOSE_FAILURES", "3"))
POLL_SEC = env_float("POLL_SEC", "2")
LIVE = os.getenv("LIVE_TRADING", "no").strip().lower() == "yes"
AUTO_REGISTER_SIGNER = os.getenv("AUTO_REGISTER_SIGNER", "no").strip().lower() == "yes"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(HERE / "bot.log", encoding="utf-8")],
)
log = logging.getLogger("bot")


class HaltBot(Exception):
    """봇을 멈춰야 하는 상황 (사람이 확인해야 함)."""


class Bot:
    def __init__(self, client: RisexClient):
        self.c = client
        self.prices = deque()          # (timestamp, price)
        self.position = None           # {"side","size","entry_price"}
        self.cooldown_until = 0.0
        self.trades_today = 0
        self.day = datetime.now().date()
        self.sim_pnl = 0.0
        self.close_failures = 0

    def tag(self):
        return "[실전]" if LIVE else "[모의]"

    def check_position(self, pos, price):
        """거래소 포지션이 봇이 관리할 수 있는 상태인지 확인. 아니면 HaltBot."""
        if not pos:
            return None
        if pos["side"] != "long":
            raise HaltBot(f"숏 포지션 발견 — 이 봇은 롱만 관리합니다. 거래소에서 직접 확인하세요: {pos}")
        if pos["size"] > ORDER_SIZE * 2 + 1e-12:
            raise HaltBot(f"포지션 크기 {pos['size']} 가 예상(최대 {ORDER_SIZE * 2})보다 큼 — 중복 체결 의심. 직접 확인하세요")
        if pos["entry_price"] <= 0:
            if price is None:
                raise HaltBot(f"진입가 0 이고 현재가도 없음 — 손절 기준을 정할 수 없음: {pos}")
            log.warning("거래소가 진입가를 0으로 줌 → 현재가 %.1f 를 진입가로 사용", price)
            pos["entry_price"] = price
        return pos

    async def sync_position(self, price):
        """실전 모드: 거래소 포지션을 다시 읽는다. 예외는 위로 던져 그 루프 진입을 막는다."""
        if not LIVE:
            return
        pos = await self.c.get_position(SYMBOL)
        if pos and self.position and self.position.get("entry_price", 0) > 0 and pos["entry_price"] <= 0:
            pos["entry_price"] = self.position["entry_price"]
        self.position = self.check_position(pos, price)

    async def enter(self, price: float) -> bool:
        notional = ORDER_SIZE * price
        if notional > MAX_ORDER_USD:
            log.error("주문금액 %.2f USD 가 상한 MAX_ORDER_USD=%.2f 를 넘어 진입 취소", notional, MAX_ORDER_USD)
            return False
        # 주문 전에 먼저 기록: 이후 어떤 오류가 나도 반복 매수하지 않게
        self.trades_today += 1
        self.cooldown_until = time.time() + COOLDOWN_SEC
        log.info("%s 롱 진입 시도 (%d/%d): %s %s (약 %.2f USD) @ ~%.1f", self.tag(),
                 self.trades_today, MAX_TRADES_PER_DAY, ORDER_SIZE, SYMBOL, notional, price)
        if LIVE:
            try:
                res = await self.c.create_order(SYMBOL, "buy", ORDER_SIZE, order_type="market")
                log.info("주문 응답: %s", res)
            except Exception as e:
                log.error("주문 오류 (서버에 도달했을 수도 있음 — 다음 루프에서 포지션 재확인): %s", e)
            await asyncio.sleep(2)
            await self.sync_position(price)
            if not self.position:
                log.warning("포지션이 안 잡힘 (IOC 미체결 가능). 쿨다운 %.0f초 후 다시 노림", COOLDOWN_SEC)
        else:
            self.position = {"side": "long", "size": ORDER_SIZE, "entry_price": price}
        log.info("%s 포지션: %s", self.tag(), self.position)
        return True

    async def exit(self, price: float, reason: str):
        pos = self.position
        pnl = (price - pos["entry_price"]) * pos["size"]
        log.info("%s %s 청산 시도 @ ~%.1f (진입 %.1f, 예상손익 %.4f USD, 수수료 제외)",
                 self.tag(), reason, price, pos["entry_price"], pnl)
        if LIVE:
            try:
                res = await self.c.close_position(SYMBOL, pos)
                log.info("청산 응답: %s", res)
            except Exception as e:
                log.error("청산 주문 오류: %s", e)
            await asyncio.sleep(2)
            try:
                await self.sync_position(price)
            except HaltBot:
                raise
            except Exception as e:
                log.error("청산 후 포지션 확인 실패: %s", e)
            if self.position:
                self.close_failures += 1
                log.warning("청산 실패 %d/%d — 포지션 남음: %s", self.close_failures, MAX_CLOSE_FAILURES, self.position)
                if self.close_failures >= MAX_CLOSE_FAILURES:
                    raise HaltBot(f"청산이 {self.close_failures}번 연속 실패 — 봇을 멈춥니다")
                return
            self.close_failures = 0
        else:
            self.sim_pnl += pnl
            log.info("[모의] 누적 손익 %.4f USD (수수료 제외)", self.sim_pnl)
            self.position = None
        self.cooldown_until = max(self.cooldown_until, time.time() + COOLDOWN_SEC)

    async def step(self):
        now = time.time()
        if datetime.now().date() != self.day:
            self.day, self.trades_today = datetime.now().date(), 0

        price = await self.c.get_mark_price(SYMBOL)
        if price is None:
            raise RuntimeError("마크가격 조회 실패")
        self.prices.append((now, price))
        while self.prices and now - self.prices[0][0] > DROP_WINDOW_SEC:
            self.prices.popleft()

        # 실전: 매 루프 거래소 상태를 기준으로 삼는다 (실패하면 예외 → 이번 루프 진입 안 함)
        await self.sync_position(price)

        if self.position:
            entry = self.position["entry_price"]
            change = (price - entry) / entry * 100
            if change <= -STOP_LOSS_PCT:
                await self.exit(price, "손절")
            elif change >= TAKE_PROFIT_PCT:
                await self.exit(price, "익절")
            return

        high = max(p for _, p in self.prices)
        drop = (high - price) / high * 100
        if drop >= DROP_PCT:
            if now < self.cooldown_until:
                log.info("급락 %.2f%% 감지했지만 쿨다운 중 (%.0f초 남음)", drop, self.cooldown_until - now)
            elif self.trades_today >= MAX_TRADES_PER_DAY:
                log.info("급락 %.2f%% 감지했지만 오늘 거래 한도 %d회 도달", drop, MAX_TRADES_PER_DAY)
            else:
                log.info("급락 감지: %.0f초 최고가 %.1f → 현재 %.1f (-%.2f%%)", DROP_WINDOW_SEC, high, price, drop)
                if await self.enter(price):
                    self.prices.clear()

    async def run(self, max_loops=None):
        if LIVE:
            price = await self.c.get_mark_price(SYMBOL)
            await self.sync_position(price)
            if self.position:
                log.info("기존 롱 포지션 발견 — 손절/익절 관리합니다: %s", self.position)
        loops, last_status, errors = 0, 0.0, 0
        while max_loops is None or loops < max_loops:
            loops += 1
            try:
                await self.step()
                errors = 0
            except HaltBot:
                raise
            except Exception as e:
                errors += 1
                log.error("루프 오류 (%d/%d 연속): %s", errors, MAX_CONSECUTIVE_ERRORS, e)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    raise HaltBot(f"연속 오류 {errors}회 — 봇을 멈춥니다")
            if time.time() - last_status > 60 or max_loops is not None:
                last_status = time.time()
                p = self.prices[-1][1] if self.prices else None
                log.info("%s 상태: 가격 %s, 포지션 %s, 오늘 거래 %d/%d",
                         self.tag(), p, self.position, self.trades_today, MAX_TRADES_PER_DAY)
            await asyncio.sleep(POLL_SEC)


OPEN_POSITION_WARNING = ("!!! 봇이 멈췄습니다. 열린 포지션이 있다면 손절이 걸려있지 않습니다. "
                         "rise.trade 에서 직접 확인·정리하세요 !!!")


async def main():
    max_loops = int(sys.argv[1]) if len(sys.argv) > 1 else None
    pk = os.getenv("RISEX_PRIVATE_KEY", "").strip() or None
    if LIVE and not pk:
        log.error("LIVE_TRADING=yes 인데 .env 에 RISEX_PRIVATE_KEY 가 없습니다.")
        return
    client = await RisexClient(private_key=pk if LIVE else None).init()
    try:
        min_size = client.min_order_size(SYMBOL)
        if ORDER_SIZE < min_size:
            log.error("ORDER_SIZE %s 가 거래소 최소 주문량 %s 보다 작습니다.", ORDER_SIZE, min_size)
            return
        log.info("=== RISEx 급락반등봇 시작 %s === 종목 %s, 수량 %s, %.0f초 내 -%.2f%% 진입, 손절 %.1f%% / 익절 %.1f%%",
                 "실전 모드(실제 돈!)" if LIVE else "모의 모드", SYMBOL, ORDER_SIZE,
                 DROP_WINDOW_SEC, DROP_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT)
        if LIVE:
            log.info("계정 %s", client.account_address)
            if not await client.is_signer_registered():
                if not AUTO_REGISTER_SIGNER:
                    log.error("이 지갑의 서명키가 RISEx 에 등록돼 있지 않습니다. "
                              "등록하려면 .env 에 AUTO_REGISTER_SIGNER=yes 를 넣고 다시 실행하세요.")
                    return
                log.info("서명키 등록 시도 (30일 유효)")
                log.info("등록 응답: %s", await client.register_signer())
            log.info("담보 잔고(조회값, 필드명 미검증): %s", await client.get_collateral())
        await Bot(client).run(max_loops)
    except HaltBot as e:
        log.error("봇 정지: %s", e)
        if LIVE:
            log.error(OPEN_POSITION_WARNING)
    except Exception as e:
        log.error("봇 정지 (예상 못 한 오류): %s", e)
        if LIVE:
            log.error(OPEN_POSITION_WARNING)
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("사용자가 중지함 (Ctrl+C).")
        if LIVE:
            log.error(OPEN_POSITION_WARNING)
