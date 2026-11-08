# RISEx 급락 반등 봇

[RISEx](https://rise.trade)(RISE Chain 메인넷, chain_id 4153)용 BTC 무기한선물 자동매매 봇입니다.

> ⚠️ **실제 돈이 걸린 메인넷입니다.** 기본값은 모의 모드이며, 실주문은 아직 검증되지 않았습니다.

## 전략

| 항목 | 기본값 | `.env` 키 |
|---|---|---|
| 진입 조건 | 최근 60초 최고가 대비 1% 이상 하락 → 롱 | `DROP_WINDOW_SEC`, `DROP_PCT` |
| 주문 크기 | 0.00015 BTC (거래소 최소 수량) | `ORDER_SIZE` |
| 손절 / 익절 | -1% / +2% | `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT` |
| 진입 후 쉬는 시간 | 300초 | `COOLDOWN_SEC` |
| 하루 최대 진입 | 5회 | `MAX_TRADES_PER_DAY` |
| 주문 1건 금액 상한 | 30 USD | `MAX_ORDER_USD` |

## 안전장치

- `LIVE_TRADING=yes`가 아니면 주문을 내지 않습니다(모의 모드). 이때는 개인키도 읽지 않습니다.
- 진입 횟수와 쿨다운은 주문을 보내기 **전에** 기록합니다. 그래서 주문 뒤 오류가 나도 반복 매수하지 않습니다.
- 실전 모드에서는 매 루프 거래소 포지션을 다시 조회합니다. 조회에 실패하거나 응답 모양이 다르면 그 루프에서는 진입하지 않습니다.
- 다음 경우에는 봇이 스스로 멈춥니다.
  - 숏 포지션이 있을 때
  - 포지션이 `ORDER_SIZE`의 2배를 넘을 때
  - 청산이 3번 연속 실패할 때
  - 오류가 10번 연속 날 때
  - 서버의 chainId가 4153이 아닐 때
- 서명키 자동 등록은 기본으로 꺼져 있습니다(`AUTO_REGISTER_SIGNER=no`).

## 알려진 한계

- **손절과 익절은 봇이 처리합니다.** 봇이 꺼지면 손절이 걸리지 않습니다.
- 봇을 재시작하면 그날의 거래 횟수가 0으로 초기화됩니다.
- 수수료는 손익 계산에 포함되지 않습니다.
- 실제 주문 경로(`/v1/orders/place`)는 아직 한 번도 실행해 보지 않았습니다.
- 잔고 조회 필드명(`balance`)은 검증되지 않았습니다.

## 설치와 실행 (Windows)

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python bot.py
```

`bot.py 30`처럼 숫자를 붙이면 그 횟수만큼만 루프를 돌고 끝납니다.

## 실전 전환 순서

1. 봇 전용 새 지갑을 만들고 소액만 입금합니다.
2. `.env`에 `RISEX_PRIVATE_KEY`를 넣고 `LIVE_TRADING=yes`로 바꿉니다.
3. `bot.py 30`으로 짧게 실행한 뒤 rise.trade에서 주문과 포지션을 확인합니다.

**`.env`는 절대 커밋하거나 공유하지 마세요.**

## 출처

`risex_client.py`는 아래 두 가지를 바탕으로 만들었습니다.

- 비공식 npm SDK [`risex-client`](https://www.npmjs.com/package/risex-client) 0.1.4의 서명·인코딩 로직을 파이썬으로 옮긴 RISEx 래퍼
- 그 래퍼를 단독으로 동작하게 하고 버그를 고친 버전
